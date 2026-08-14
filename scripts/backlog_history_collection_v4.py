from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import fitz

import backlog_history_collection_v3 as base
import backlog_history_collection_v3_fixed as fixed  # noqa: F401; installs fixed inventory
from backlog_structured_parser_v3 import extract_candidates, select_candidate

MANUAL_REQUIRED = {
    "FLOW_RECONSTRUCTION",
    "SEGMENT_SUM_RECONSTRUCTION",
    "YOY_INFERRED_PRIOR",
}


def process_detail(detail: dict[str, Any]) -> dict[str, Any]:
    output = dict(detail)
    pdf_url = detail.get("pdf_url")
    if not pdf_url:
        output["parse_status"] = "NO_PDF_LINK"
        return output
    try:
        response = base.get(str(pdf_url))
        offset = response.content[:4096].find(b"%PDF")
        if offset < 0:
            output.update(parse_status="PDF_INVALID", parse_error="not a PDF")
            return output
        pdf_bytes = response.content[offset:]
        candidates, parser_audit = extract_candidates(pdf_bytes)
        selected = select_candidate(candidates)
        try:
            document = fitz.open(stream=pdf_bytes, filetype="pdf")
            full_text = "\n".join(page.get_text("text") for page in document)
        except Exception:
            full_text = ""
        selection_status = (
            "SELECTED_BY_V4"
            if selected is not None
            else "AMBIGUOUS_TOP_CANDIDATES"
            if candidates
            else "NO_CANDIDATE"
        )
        output.update(
            parse_status="PARSED" if candidates else "NO_BACKLOG_CANDIDATE",
            parse_error=None,
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            pdf_size=len(pdf_bytes),
            candidate_count=len(candidates),
            candidates=[candidate.to_dict() for candidate in candidates],
            selection_status=selection_status,
            selected=selected.to_dict() if selected else None,
            parser_audit=parser_audit,
            one_off_or_scope_text=bool(base.ONE_OFF_RE.search(full_text)),
        )
        if selected:
            for field in [
                "current_backlog_m",
                "prior_backlog_m",
                "order_intake_m",
                "sales_m",
                "method",
                "confidence",
                "score",
                "page",
                "evidence",
                "period_text",
                "unit",
                "yoy",
                "scope",
            ]:
                output[field] = getattr(selected, field)
    except Exception as exc:
        output.update(
            parse_status="PDF_FETCH_OR_PARSE_FAILED",
            parse_error=f"{type(exc).__name__}: {exc}",
        )
    return output


def reconcile(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fixtures = json.loads(base.FIXTURES.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for fixture in fixtures:
        same_code = [
            row
            for row in records
            if str(row.get("code")) == str(fixture.get("code"))
        ]
        same_code.sort(
            key=lambda row: base.date_distance(
                row.get("release_date"), fixture.get("ir_date")
            )
        )
        chosen = (
            same_code[0]
            if same_code
            and base.date_distance(
                same_code[0].get("release_date"), fixture.get("ir_date")
            )
            <= 7
            else None
        )
        expected = fixture.get("expected_current_backlog_m")
        if expected is None:
            expected = fixture.get("current_backlog_m")
        selected_value = chosen.get("current_backlog_m") if chosen else None
        candidate_values = [
            candidate.get("current_backlog_m")
            for candidate in (chosen.get("candidates") if chosen else []) or []
        ]
        candidate_errors = [
            base.relative_error(value, expected) for value in candidate_values
        ]
        best_candidate_error = min(
            (value for value in candidate_errors if value is not None),
            default=None,
        )
        manual_required = fixture.get("method") in MANUAL_REQUIRED
        selected_error = base.relative_error(selected_value, expected)
        results.append(
            {
                "code": fixture.get("code"),
                "company": fixture.get("company"),
                "ir_date": fixture.get("ir_date"),
                "expected_current_backlog_m": expected,
                "manual_required": manual_required,
                "matched_release_date": (
                    chosen.get("release_date") if chosen else None
                ),
                "pdf_status": (
                    chosen.get("parse_status")
                    if chosen
                    else "NO_MATCHED_DETAIL"
                ),
                "selection_status": (
                    chosen.get("selection_status") if chosen else None
                ),
                "selected_current_backlog_m": selected_value,
                "selected_relative_error": selected_error,
                "expected_exists_in_candidates": (
                    best_candidate_error is not None
                    and best_candidate_error <= 0.005
                ),
                "best_candidate_relative_error": best_candidate_error,
                "candidate_count": chosen.get("candidate_count") if chosen else 0,
                "selected_method": chosen.get("method") if chosen else None,
                "selected_scope": chosen.get("scope") if chosen else None,
                "evidence": chosen.get("evidence") if chosen else None,
            }
        )

    machine = [row for row in results if not row["manual_required"]]
    matched = [row for row in machine if row["matched_release_date"]]
    parsed = [row for row in machine if row["pdf_status"] == "PARSED"]
    candidate_matches = [
        row for row in parsed if row["expected_exists_in_candidates"]
    ]
    selected_matches = [
        row
        for row in parsed
        if row["selected_relative_error"] is not None
        and row["selected_relative_error"] <= 0.02
    ]
    wrong_auto = [
        row
        for row in parsed
        if row["selected_current_backlog_m"] is not None
        and (
            row["selected_relative_error"] is None
            or row["selected_relative_error"] > 0.02
        )
    ]
    summary = {
        "fixtures": len(results),
        "machine_eligible_fixtures": len(machine),
        "manual_required_fixtures": len(results) - len(machine),
        "matched_details_machine": len(matched),
        "parsed_details_machine": len(parsed),
        "expected_value_in_candidates_0_5pct_machine": len(candidate_matches),
        "automatic_selection_2pct_machine": len(selected_matches),
        "wrong_auto_selection_machine": len(wrong_auto),
        "wrong_auto_selection_codes": [row["code"] for row in wrong_auto],
        "ambiguous_machine": sum(
            row["selection_status"] == "AMBIGUOUS_TOP_CANDIDATES"
            for row in machine
        ),
    }
    denominator = max(len(machine), 1)
    summary["collection_ready"] = bool(
        len(matched) / denominator >= 0.90
        and len(parsed) / denominator >= 0.85
        and len(candidate_matches) / max(len(parsed), 1) >= 0.90
        and len(selected_matches) / max(len(parsed), 1) >= 0.80
        and not wrong_auto
    )
    return results, summary


base.process_detail = process_detail
base.reconcile = reconcile

if __name__ == "__main__":
    base.main()
