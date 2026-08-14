from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import fitz
from bs4 import BeautifulSoup

import backlog_history_collection_v3 as base
import backlog_structured_parser_v5 as parser
import catr_backlog_source_inventory as inventory

MANUAL_REQUIRED = {
    "FLOW_RECONSTRUCTION",
    "SEGMENT_SUM_RECONSTRUCTION",
    "YOY_INFERRED_PRIOR",
}


def fixed_company_inventory(item: dict[str, str]) -> dict[str, Any]:
    code = item["code"]
    company = item["company"]
    output: dict[str, Any] = {"code": code, "company": company, "details": []}
    try:
        document = inventory.search_company(code, company)
        if not document:
            output["status"] = "COMPANY_NOT_FOUND"
            return output
        key = document.get("key")
        company_id = document.get("id")
        company_page = f"{inventory.COMPANY_BASE}/companies/{key}/{company_id}"
        detail_url, annual_links = inventory.annual_detail_url(company_page)
        output["company_page"] = company_page
        output["annual_detail_candidates"] = annual_links
        if not detail_url:
            output["status"] = "ANNUAL_DETAIL_NOT_FOUND"
            return output
        records = inventory.quarter_records(detail_url, code, company)
        details: list[dict[str, Any]] = []
        for record in records:
            details.append(
                {
                    **record,
                    "title": f"{record.get('fiscal_year') or ''} {record.get('quarter') or ''} 決算資料",
                    "sales_cumulative_m": record.get("sales_m"),
                    "operating_profit_cumulative_m": record.get("operating_profit_m"),
                }
            )
        details.sort(key=lambda row: (row.get("release_date") or "", row.get("quarter") or ""))
        output["details"] = details
        output["status"] = "OK" if details else "NO_QUARTER_RECORDS"
    except Exception as exc:
        output.update(status="COMPANY_INVENTORY_FAILED", error=f"{type(exc).__name__}: {exc}")
    return output


def _extract_pdf(payload: bytes) -> bytes | None:
    offset = payload[:32768].find(b"%PDF")
    return payload[offset:] if offset >= 0 else None


def fetch_pdf(detail: dict[str, Any]) -> tuple[bytes | None, str | None, list[dict[str, Any]]]:
    queue: list[str] = []
    for key in ["pdf_url", "detail_url"]:
        value = detail.get(key)
        if value:
            queue.append(str(value))
    queue.extend(str(value) for value in detail.get("pdf_candidates") or [] if value)
    seen: set[str] = set()
    audit: list[dict[str, Any]] = []
    while queue:
        url = queue.pop(0)
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            response = base.get(url)
            audit.append(
                {
                    "url": url,
                    "final_url": response.url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "bytes": len(response.content),
                }
            )
            pdf = _extract_pdf(response.content)
            if pdf is not None:
                return pdf, response.url, audit
            if response.content.lstrip().startswith(b"<"):
                soup = BeautifulSoup(response.text, "html.parser")
                for tag in soup.find_all(["a", "iframe", "embed", "object"]):
                    href = tag.get("href") or tag.get("src") or tag.get("data")
                    if not href:
                        continue
                    resolved = urljoin(response.url, href)
                    if "pdf" in resolved.lower() or "tdnet" in resolved.lower():
                        queue.append(resolved)
        except Exception as exc:
            audit.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
    return None, None, audit


def process_detail(detail: dict[str, Any]) -> dict[str, Any]:
    output = dict(detail)
    payload, final_url, fetch_audit = fetch_pdf(detail)
    output["fetch_audit"] = fetch_audit
    output["final_pdf_url"] = final_url
    if payload is None:
        output["parse_status"] = "PDF_FETCH_FAILED"
        output["selection_status"] = "NOT_EVALUATED"
        return output
    output["pdf_sha256"] = hashlib.sha256(payload).hexdigest()
    output["pdf_size"] = len(payload)
    try:
        candidates, parser_audit = parser.extract_candidates(payload)
        selected = parser.select_candidate(candidates)
        output["parser_audit"] = parser_audit
        output["candidate_count"] = len(candidates)
        output["candidates"] = [candidate.to_dict() for candidate in candidates]
        output["selection_status"] = (
            "SELECTED_BY_V5" if selected is not None else "AMBIGUOUS_TOP_CANDIDATES" if candidates else "NO_CANDIDATE"
        )
        output["parse_status"] = "PARSED" if candidates else "NO_BACKLOG_CANDIDATE"
        if selected is not None:
            for field, value in selected.to_dict().items():
                output[field] = value
        document = fitz.open(stream=payload, filetype="pdf")
        full_text = "\n".join(page.get_text("text") for page in document)
        output["one_off_or_scope_text"] = bool(base.ONE_OFF_RE.search(full_text))
    except Exception as exc:
        output.update(
            parse_status="PARSER_EXCEPTION",
            selection_status="NOT_EVALUATED",
            parse_error=f"{type(exc).__name__}: {exc}",
        )
    return output


def relative_error(actual: float | None, expected: float | None) -> float | None:
    if actual is None or expected in (None, 0):
        return None
    return abs(float(actual) - float(expected)) / abs(float(expected))


def reconcile(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fixtures = json.loads(base.FIXTURES.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for fixture in fixtures:
        same_code = [record for record in records if str(record.get("code")) == str(fixture.get("code"))]
        same_code.sort(key=lambda record: base.date_distance(record.get("release_date"), fixture.get("ir_date")))
        chosen = same_code[0] if same_code and base.date_distance(same_code[0].get("release_date"), fixture.get("ir_date")) <= 14 else None
        expected = fixture.get("expected_current_backlog_m")
        selected_value = chosen.get("current_backlog_m") if chosen else None
        candidates = (chosen.get("candidates") if chosen else []) or []
        candidate_errors = [relative_error(candidate.get("current_backlog_m"), expected) for candidate in candidates]
        best_candidate_error = min((error for error in candidate_errors if error is not None), default=None)
        selected_error = relative_error(selected_value, expected)
        manual_required = fixture.get("method") in MANUAL_REQUIRED
        rows.append(
            {
                "code": fixture.get("code"),
                "company": fixture.get("company"),
                "ir_date": fixture.get("ir_date"),
                "expected_current_backlog_m": expected,
                "fixture_method": fixture.get("method"),
                "manual_required": manual_required,
                "matched_release_date": chosen.get("release_date") if chosen else None,
                "parse_status": chosen.get("parse_status") if chosen else "NO_MATCHED_DETAIL",
                "selection_status": chosen.get("selection_status") if chosen else None,
                "selected_current_backlog_m": selected_value,
                "selected_relative_error": selected_error,
                "expected_exists_in_candidates_0_5pct": best_candidate_error is not None and best_candidate_error <= 0.005,
                "best_candidate_relative_error": best_candidate_error,
                "candidate_count": chosen.get("candidate_count") if chosen else 0,
                "selected_method": chosen.get("method") if chosen else None,
                "selected_scope": chosen.get("scope") if chosen else None,
                "evidence": chosen.get("evidence") if chosen else None,
            }
        )

    machine = [row for row in rows if not row["manual_required"]]
    matched = [row for row in machine if row["matched_release_date"] is not None]
    parsed = [row for row in matched if row["parse_status"] == "PARSED"]
    candidate_match = [row for row in parsed if row["expected_exists_in_candidates_0_5pct"]]
    selected = [row for row in parsed if row["selected_current_backlog_m"] is not None]
    selected_match = [row for row in selected if row["selected_relative_error"] is not None and row["selected_relative_error"] <= 0.005]
    wrong_selected = [row for row in selected if row["selected_relative_error"] is not None and row["selected_relative_error"] > 0.005]
    summary = {
        "fixtures": len(rows),
        "machine_eligible_fixtures": len(machine),
        "manual_required_fixtures": len(rows) - len(machine),
        "matched_details_machine": len(matched),
        "parsed_details_machine": len(parsed),
        "candidate_matches_machine_0_5pct": len(candidate_match),
        "selected_values_machine": len(selected),
        "selected_matches_machine_0_5pct": len(selected_match),
        "wrong_automatic_selections": len(wrong_selected),
        "wrong_automatic_selection_codes": [row["code"] for row in wrong_selected],
        "ambiguous_or_missing_selection_codes": [
            row["code"] for row in parsed if row["selected_current_backlog_m"] is None
        ],
    }
    summary["pdf_match_rate"] = len(matched) / len(machine) if machine else 0
    summary["parse_rate"] = len(parsed) / len(matched) if matched else 0
    summary["candidate_match_rate"] = len(candidate_match) / len(parsed) if parsed else 0
    summary["selected_match_rate"] = len(selected_match) / len(parsed) if parsed else 0
    summary["collection_ready"] = bool(
        summary["pdf_match_rate"] >= 0.90
        and summary["parse_rate"] >= 0.90
        and summary["candidate_match_rate"] >= 0.90
        and summary["selected_match_rate"] >= 0.80
        and summary["wrong_automatic_selections"] == 0
    )
    return rows, summary


base.company_inventory = fixed_company_inventory
base.process_detail = process_detail
base.reconcile = reconcile

if __name__ == "__main__":
    base.main()
