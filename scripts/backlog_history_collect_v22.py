from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from backlog_structured_parser import extract_candidates
from catr_backlog_source_inventory import company_inventory

COMPANIES = Path("data/backlog_v22_positive_strict_companies.json")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 backlog-history-collector/2.2.1"})


def fetch_pdf(urls: list[str]) -> tuple[bytes | None, str | None, list[dict[str, Any]]]:
    queue = [url for url in urls if url]
    seen: set[str] = set()
    audit: list[dict[str, Any]] = []
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        for attempt in range(4):
            try:
                response = SESSION.get(url, timeout=90, allow_redirects=True)
                audit.append({
                    "url": url,
                    "final_url": response.url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "bytes": len(response.content),
                })
                if response.status_code != 200:
                    time.sleep(attempt + 1)
                    continue
                offset = response.content[:4096].find(b"%PDF")
                if offset >= 0:
                    return response.content[offset:], response.url, audit
                content_type = (response.headers.get("content-type") or "").lower()
                if "html" in content_type or response.content.lstrip().startswith(b"<"):
                    soup = BeautifulSoup(response.text, "html.parser")
                    for tag in soup.find_all(["a", "iframe", "embed"]):
                        href = tag.get("href") or tag.get("src")
                        if href and "pdf" in href.lower():
                            queue.append(urljoin(response.url, href))
                break
            except Exception as exc:
                audit.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
                time.sleep(attempt + 1)
    return None, None, audit


def parse_record(company: dict[str, str], detail: dict[str, Any]) -> dict[str, Any]:
    output = {
        "code": company["code"],
        "company": company["company"],
        "fiscal_year": detail.get("fiscal_year"),
        "fiscal_month": detail.get("fiscal_month"),
        "quarter": detail.get("quarter"),
        "release_date": detail.get("release_date"),
        "detail_url": detail.get("detail_url"),
        "pdf_url": detail.get("pdf_url"),
        "catr_sales_m": detail.get("sales_m"),
        "catr_operating_profit_m": detail.get("operating_profit_m"),
        "catr_cell_text": detail.get("cell_text"),
    }
    urls = list(dict.fromkeys([detail.get("pdf_url"), *(detail.get("pdf_candidates") or [])]))
    payload, final_url, fetch_audit = fetch_pdf(urls)
    output["fetch_audit"] = fetch_audit
    output["download_url"] = final_url
    if payload is None:
        output["status"] = "PDF_FETCH_FAILED"
        return output
    output["pdf_sha256"] = hashlib.sha256(payload).hexdigest()
    output["pdf_bytes"] = len(payload)
    try:
        candidates, parser_audit = extract_candidates(payload)
        output["parser_audit"] = parser_audit
        output["candidates"] = [candidate.to_dict() for candidate in candidates[:80]]
        output["status"] = "PARSED" if candidates else "NO_BACKLOG_CANDIDATE"
    except Exception as exc:
        output["status"] = "PARSER_FAILED"
        output["error"] = f"{type(exc).__name__}: {exc}"
    return output


def process_company(company: dict[str, str], workers: int) -> dict[str, Any]:
    inventory = company_inventory(company)
    output = {
        "code": company["code"],
        "company": company["company"],
        "inventory_status": inventory.get("status"),
        "company_page": inventory.get("company_page"),
        "annual_detail_url": inventory.get("annual_detail_url"),
        "inventory_error": inventory.get("error"),
        "records": [],
    }
    details = inventory.get("details") or []
    if not details:
        return output
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        records = list(executor.map(lambda detail: parse_record(company, detail), details))
    records.sort(key=lambda row: (row.get("release_date") or "", row.get("quarter") or ""))
    output["records"] = records
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    companies = json.loads(COMPANIES.read_text(encoding="utf-8"))
    selected = [company for index, company in enumerate(companies) if index % args.shard_count == args.shard_index]
    outputs = []
    for company in selected:
        outputs.append(process_company(company, args.workers))
    with (out / "company_history.jsonl").open("w", encoding="utf-8") as handle:
        for output in outputs:
            handle.write(json.dumps(output, ensure_ascii=False) + "\n")
    records = [record for output in outputs for record in output.get("records", [])]
    summary = {
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
        "companies": len(outputs),
        "inventory_status_counts": dict(Counter(output.get("inventory_status") for output in outputs)),
        "quarter_records": len(records),
        "record_status_counts": dict(Counter(record.get("status") for record in records)),
        "pdf_fetched": sum(record.get("status") in {"PARSED", "NO_BACKLOG_CANDIDATE", "PARSER_FAILED"} for record in records),
        "parsed_with_candidates": sum(record.get("status") == "PARSED" for record in records),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
