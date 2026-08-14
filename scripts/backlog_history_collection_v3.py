from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import fitz
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import backlog_structured_parser as bp  # noqa: E402

COMPANIES = ROOT / "data" / "backlog_v22_positive_strict_companies.json"
FIXTURES = ROOT / "data" / "backlog_v22_gold_fixtures.json"
BASE = "https://disclosure.catr.jp"
SEARCH = BASE + "/search/typesense"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 backlog-history-collection/3.0"})
EARNINGS_RE = re.compile(r"決算短信|四半期決算|中間期決算|決算説明|決算補足|決算概要|決算資料|業績説明|financial results|決算プレゼン|決算報告", re.I)
EXCLUDE_RE = re.compile(r"訂正|再訂正|監査報告|招集通知|有価証券報告書|コーポレート.?ガバナンス")
DATE_RE = re.compile(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})")
QUARTER_RE = re.compile(r"(?:第\s*)?([1-4])\s*四半期|([1-4])Q|通期|中間", re.I)
ONE_OFF_RE = re.compile(r"大型案件|大口案件|一過性|M&A|買収|連結範囲|事業譲受|事業売却|会計方針|受注定義|為替換算")


def get(url: str, **kwargs: Any) -> requests.Response:
    error: Exception | None = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, timeout=75, **kwargs)
            if response.status_code == 200:
                return response
            error = RuntimeError(f"HTTP {response.status_code}: {url}")
        except Exception as exc:  # pragma: no cover - network
            error = exc
        time.sleep(attempt + 1)
    raise error or RuntimeError(url)


def parse_date(text: str) -> str | None:
    match = DATE_RE.search(text or "")
    if not match:
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def quarter_label(text: str) -> str | None:
    normalized = (text or "").replace("第１", "第1").replace("第２", "第2").replace("第３", "第3").replace("第４", "第4")
    match = QUARTER_RE.search(normalized)
    if not match:
        return None
    token = match.group(0)
    if "通期" in token:
        return "4Q"
    if "中間" in token:
        return "2Q"
    number = match.group(1) or match.group(2)
    return f"{number}Q" if number else None


def fiscal_year_from_release(release_date: str | None, quarter: str | None, title: str) -> int | None:
    explicit = re.search(r"(20\d{2})年(?:\d{1,2}月期)?", title or "")
    if explicit:
        return int(explicit.group(1))
    if not release_date or not quarter:
        return None
    date = dt.date.fromisoformat(release_date)
    q = int(quarter[0])
    # Fallback only. Fiscal-year identity is later checked for monotonicity.
    return date.year if q >= 3 else date.year - 1


def search_company(code: str) -> dict[str, Any] | None:
    params = {
        "q": code,
        "query_by": "full_text_search",
        "infix": "always",
        "num_typos": "0",
        "prefix": "false",
        "per_page": 20,
        "page": 1,
        "sort_by": "settlement_count:desc",
    }
    data = get(SEARCH, params=params).json()
    documents = [hit.get("document") or {} for hit in data.get("hits") or []]
    exact = [doc for doc in documents if str(doc.get("ticker_code") or "").upper() == code.upper()]
    return exact[0] if exact else (documents[0] if len(documents) == 1 else None)


def parse_numeric_metric(text: str | None) -> float | None:
    if not text:
        return None
    normalized = bp.norm(text)
    multiplier, _unit = bp.unit_multiplier(normalized)
    return bp.parse_money(normalized, multiplier)


def detail_record(url: str, code: str, company: str) -> dict[str, Any]:
    record: dict[str, Any] = {"code": code, "company": company, "detail_url": url}
    try:
        response = get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        title_node = soup.find("h2") or soup.find("h1")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        record["title"] = title
        record["release_date"] = parse_date(page_text[:1800])
        record["quarter"] = quarter_label(title + " " + page_text[:2500])
        record["fiscal_year"] = fiscal_year_from_release(record["release_date"], record["quarter"], title)
        pdfs: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(response.url, anchor["href"])
            if ".pdf" in href.lower() or "pdf.catr.jp" in href.lower():
                pdfs.append(href)
        record["pdf_candidates"] = list(dict.fromkeys(pdfs))
        record["pdf_url"] = record["pdf_candidates"][0] if record["pdf_candidates"] else None
        metrics: dict[str, str | None] = {"sales_text": None, "operating_profit_text": None}
        for row in soup.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            label = cells[0]
            if label.startswith("売上高") or label.startswith("売上収益"):
                metrics["sales_text"] = cells[1]
            if label.startswith("営業利益"):
                metrics["operating_profit_text"] = cells[1]
        record.update(metrics)
        record["sales_cumulative_m"] = parse_numeric_metric(record["sales_text"])
        record["operating_profit_cumulative_m"] = parse_numeric_metric(record["operating_profit_text"])
        record["status"] = "OK" if record["pdf_url"] else "NO_PDF_LINK"
    except Exception as exc:
        record.update(status="DETAIL_FETCH_FAILED", error=f"{type(exc).__name__}: {exc}")
    return record


def pdf_candidates(pdf_bytes: bytes) -> tuple[list[dict[str, Any]], str, str | None]:
    candidates: list[Any] = []
    error: str | None = None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        all_text: list[str] = []
        for page_number, page in enumerate(doc, start=1):
            text = page.get_text("text")
            all_text.append(text)
            for rows, _strategy, _bbox in bp.table_rows(page):
                candidates.extend(bp.table_candidates(page_number, rows, text))
            candidates.extend(bp.narrative_candidates(page_number, text))
            if hasattr(bp, "separated_text_candidates"):
                candidates.extend(bp.separated_text_candidates(page_number, text))
        full_text = "\n".join(all_text)
    except Exception as exc:
        return [], "", f"{type(exc).__name__}: {exc}"
    # Deduplicate near-identical candidates while preserving the strongest score.
    dedup: dict[tuple[int, int | None, str], Any] = {}
    for candidate in candidates:
        current = getattr(candidate, "current_backlog_m", None)
        if current is None or not math.isfinite(float(current)) or float(current) <= 0:
            continue
        prior = getattr(candidate, "prior_backlog_m", None)
        key = (round(float(current) * 1000), round(float(prior) * 1000) if prior is not None else None, str(getattr(candidate, "scope", "")))
        if key not in dedup or float(getattr(candidate, "score", 0)) > float(getattr(dedup[key], "score", 0)):
            dedup[key] = candidate
    return [candidate.to_dict() for candidate in dedup.values()], full_text, error


def selection_key(candidate: dict[str, Any]) -> tuple[float, float, float, float]:
    confidence = {"A": 3.0, "B": 2.0, "C": 1.0}.get(str(candidate.get("confidence")), 0.0)
    scope = {"TOTAL": 3.0, "SINGLE_SEGMENT": 2.0, "METRIC_ROW": 1.5, "NARRATIVE": 1.0}.get(str(candidate.get("scope")), 0.0)
    has_prior = 1.0 if candidate.get("prior_backlog_m") is not None else 0.0
    return (float(candidate.get("score") or 0.0), confidence, scope, has_prior)


def select_candidate(candidates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str, float | None]:
    if not candidates:
        return None, "NO_CANDIDATE", None
    ranked = sorted(candidates, key=selection_key, reverse=True)
    top = ranked[0]
    if len(ranked) == 1:
        return top, "SELECTED_SINGLE", None
    second = ranked[1]
    score_margin = float(top.get("score") or 0) - float(second.get("score") or 0)
    top_value = float(top["current_backlog_m"])
    second_value = float(second["current_backlog_m"])
    relative_gap = abs(top_value - second_value) / max(abs(top_value), 1.0)
    if score_margin < 8 and relative_gap > 0.02:
        return None, "AMBIGUOUS_TOP_CANDIDATES", relative_gap
    return top, "SELECTED_BY_STRUCTURE", relative_gap


def process_detail(detail: dict[str, Any]) -> dict[str, Any]:
    output = dict(detail)
    pdf_url = detail.get("pdf_url")
    if not pdf_url:
        output["parse_status"] = "NO_PDF_LINK"
        return output
    try:
        response = get(str(pdf_url))
        pdf_bytes = response.content
        if not pdf_bytes.startswith(b"%PDF"):
            output.update(parse_status="PDF_INVALID", parse_error="not a PDF")
            return output
        candidates, full_text, error = pdf_candidates(pdf_bytes)
        selected, selection_status, ambiguity_gap = select_candidate(candidates)
        output.update(
            parse_status="PARSED" if candidates else "NO_BACKLOG_CANDIDATE",
            parse_error=error,
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            pdf_size=len(pdf_bytes),
            candidate_count=len(candidates),
            candidates=candidates,
            selection_status=selection_status,
            ambiguity_relative_gap=ambiguity_gap,
            selected=selected,
            one_off_or_scope_text=bool(ONE_OFF_RE.search(full_text)),
        )
        if selected:
            for field in [
                "current_backlog_m", "prior_backlog_m", "order_intake_m", "sales_m",
                "method", "confidence", "score", "page", "evidence", "period_text", "unit", "yoy", "scope",
            ]:
                output[field] = selected.get(field)
    except Exception as exc:
        output.update(parse_status="PDF_FETCH_OR_PARSE_FAILED", parse_error=f"{type(exc).__name__}: {exc}")
    return output


def company_inventory(item: dict[str, str]) -> dict[str, Any]:
    code = item["code"]
    company = item["company"]
    output: dict[str, Any] = {"code": code, "company": company, "details": []}
    try:
        document = search_company(code)
        if not document:
            output["status"] = "COMPANY_NOT_FOUND"
            return output
        key = document.get("key")
        company_id = document.get("id")
        metrics_url = f"{BASE}/companies/{key}/{company_id}/metrics"
        response = get(metrics_url)
        soup = BeautifulSoup(response.text, "html.parser")
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(response.url, anchor["href"])
            if "/tdnet/" in href and href.startswith(BASE):
                links.append(href)
        links = list(dict.fromkeys(links))
        with cf.ThreadPoolExecutor(max_workers=8) as executor:
            details = list(executor.map(lambda url: detail_record(url, code, company), links))
        details = [
            detail for detail in details
            if (not detail.get("release_date") or detail["release_date"] >= "2021-01-01")
            and EARNINGS_RE.search(detail.get("title") or "")
            and not EXCLUDE_RE.search(detail.get("title") or "")
            and detail.get("quarter")
        ]
        details.sort(key=lambda detail: detail.get("release_date") or "")
        output["details"] = details
        output["status"] = "OK"
    except Exception as exc:
        output.update(status="COMPANY_INVENTORY_FAILED", error=f"{type(exc).__name__}: {exc}")
    return output


def collect(shard: int, shards: int, out_dir: Path, workers: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    companies = json.loads(COMPANIES.read_text(encoding="utf-8"))
    selected_companies = [item for index, item in enumerate(companies) if index % shards == shard]
    inventories: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=min(6, max(1, workers // 2))) as executor:
        futures = [executor.submit(company_inventory, item) for item in selected_companies]
        for future in cf.as_completed(futures):
            inventories.append(future.result())
    details = [detail for inventory in inventories for detail in inventory.get("details", [])]
    parsed: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process_detail, detail) for detail in details]
        for future in cf.as_completed(futures):
            parsed.append(future.result())
    parsed.sort(key=lambda row: (row.get("code", ""), row.get("release_date") or "", row.get("quarter") or ""))
    with (out_dir / "history_records.jsonl").open("w", encoding="utf-8") as handle:
        for row in parsed:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "shard": shard,
        "shards": shards,
        "companies": len(selected_companies),
        "company_status_counts": dict(Counter(item.get("status") for item in inventories)),
        "details": len(details),
        "parse_status_counts": dict(Counter(row.get("parse_status") for row in parsed)),
        "selection_status_counts": dict(Counter(row.get("selection_status") for row in parsed)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def date_distance(left: str | None, right: str | None) -> int:
    if not left or not right:
        return 9999
    return abs((dt.date.fromisoformat(left[:10]) - dt.date.fromisoformat(right[:10])).days)


def relative_error(actual: float | None, expected: float | None) -> float | None:
    if actual is None or expected in (None, 0):
        return None
    return abs(float(actual) - float(expected)) / abs(float(expected))


def reconcile(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for fixture in fixtures:
        same_code = [row for row in records if str(row.get("code")) == str(fixture.get("code"))]
        same_code.sort(key=lambda row: date_distance(row.get("release_date"), fixture.get("ir_date")))
        chosen = same_code[0] if same_code and date_distance(same_code[0].get("release_date"), fixture.get("ir_date")) <= 7 else None
        expected = fixture.get("current_backlog_m")
        selected_value = chosen.get("current_backlog_m") if chosen else None
        candidate_values = [candidate.get("current_backlog_m") for candidate in (chosen.get("candidates") if chosen else []) or []]
        candidate_errors = [relative_error(value, expected) for value in candidate_values]
        best_candidate_error = min((value for value in candidate_errors if value is not None), default=None)
        results.append({
            "code": fixture.get("code"),
            "company": fixture.get("company"),
            "ir_date": fixture.get("ir_date"),
            "expected_current_backlog_m": expected,
            "matched_release_date": chosen.get("release_date") if chosen else None,
            "pdf_status": chosen.get("parse_status") if chosen else "NO_MATCHED_DETAIL",
            "selection_status": chosen.get("selection_status") if chosen else None,
            "selected_current_backlog_m": selected_value,
            "selected_relative_error": relative_error(selected_value, expected),
            "expected_exists_in_candidates": best_candidate_error is not None and best_candidate_error <= 0.005,
            "best_candidate_relative_error": best_candidate_error,
            "candidate_count": chosen.get("candidate_count") if chosen else 0,
            "selected_method": chosen.get("method") if chosen else None,
            "selected_scope": chosen.get("scope") if chosen else None,
            "evidence": chosen.get("evidence") if chosen else None,
        })
    summary = {
        "fixtures": len(results),
        "matched_details": sum(row["matched_release_date"] is not None for row in results),
        "parsed_details": sum(row["pdf_status"] == "PARSED" for row in results),
        "expected_value_in_candidates_0_5pct": sum(row["expected_exists_in_candidates"] for row in results),
        "automatic_selection_0_5pct": sum((row["selected_relative_error"] is not None and row["selected_relative_error"] <= 0.005) for row in results),
        "automatic_selection_2pct": sum((row["selected_relative_error"] is not None and row["selected_relative_error"] <= 0.02) for row in results),
        "ambiguous": sum(row["selection_status"] == "AMBIGUOUS_TOP_CANDIDATES" for row in results),
    }
    summary["collection_ready"] = bool(
        summary["matched_details"] >= math.ceil(len(results) * 0.90)
        and summary["parsed_details"] >= math.ceil(len(results) * 0.85)
        and summary["expected_value_in_candidates_0_5pct"] >= math.ceil(len(results) * 0.80)
        and summary["automatic_selection_2pct"] >= math.ceil(len(results) * 0.75)
    )
    return results, summary


def aggregate(parts_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    all_records: list[dict[str, Any]] = []
    for path in sorted(parts_dir.rglob("history_records.jsonl")):
        all_records.extend(load_jsonl(path))
    dedup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in all_records:
        key = (str(row.get("code")), str(row.get("release_date")), str(row.get("quarter")))
        current = dedup.get(key)
        if current is None or (row.get("selection_status") or "") > (current.get("selection_status") or ""):
            dedup[key] = row
    records = sorted(dedup.values(), key=lambda row: (row.get("code", ""), row.get("release_date") or "", row.get("quarter") or ""))
    with (out_dir / "backlog_history_raw.jsonl").open("w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    reconciliation, reconciliation_summary = reconcile(records)
    with (out_dir / "reconciliation.json").open("w", encoding="utf-8") as handle:
        json.dump(reconciliation, handle, ensure_ascii=False, indent=2)
    with (out_dir / "reconciliation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(reconciliation[0].keys()) if reconciliation else [])
        if reconciliation:
            writer.writeheader()
            writer.writerows(reconciliation)
    company_counts = Counter(row.get("code") for row in records if row.get("selection_status", "").startswith("SELECTED"))
    summary = {
        "records": len(records),
        "companies": len({row.get("code") for row in records}),
        "companies_with_8_selected_quarters": sum(count >= 8 for count in company_counts.values()),
        "companies_with_12_selected_quarters": sum(count >= 12 for count in company_counts.values()),
        "parse_status_counts": dict(Counter(row.get("parse_status") for row in records)),
        "selection_status_counts": dict(Counter(row.get("selection_status") for row in records)),
        "reconciliation": reconciliation_summary,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--shard", type=int, required=True)
    collect_parser.add_argument("--shards", type=int, required=True)
    collect_parser.add_argument("--out", type=Path, required=True)
    collect_parser.add_argument("--workers", type=int, default=10)
    aggregate_parser = subparsers.add_parser("aggregate")
    aggregate_parser.add_argument("--parts", type=Path, required=True)
    aggregate_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "collect":
        collect(args.shard, args.shards, args.out, args.workers)
    else:
        aggregate(args.parts, args.out)


if __name__ == "__main__":
    main()
