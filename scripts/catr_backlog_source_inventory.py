from __future__ import annotations

import concurrent.futures as cf
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

COMPANIES = Path("data/backlog_v22_positive_strict_companies.json")
OUT = Path("out/catr-backlog-source-inventory")
DISCLOSURE_BASE = "https://disclosure.catr.jp"
COMPANY_BASE = "https://catr.jp"
SEARCH = DISCLOSURE_BASE + "/search/typesense"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 backlog-history-source-recovery/2.2.1"})
DATE_RE = re.compile(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})")
FISCAL_YEAR_RE = re.compile(r"(20\d{2})年\s*(\d{1,2})月期")
MONEY_RE = re.compile(r"([0-9][0-9,]*(?:\.\d+)?)\s*(兆円|億円|百万円|千円|万円)")


def get(url: str, **kwargs) -> requests.Response:
    error = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, timeout=90, **kwargs)
            if response.status_code == 200:
                return response
            error = RuntimeError(f"HTTP {response.status_code}: {url}")
        except Exception as exc:
            error = exc
        time.sleep(attempt + 1)
    raise error or RuntimeError(url)


def search_documents(query: str) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "query_by": "full_text_search",
        "infix": "always",
        "num_typos": "0",
        "prefix": "false",
        "per_page": 50,
        "page": 1,
        "sort_by": "settlement_count:desc",
    }
    data = get(SEARCH, params=params).json()
    return [hit.get("document") or {} for hit in data.get("hits") or []]


def search_company(code: str, company: str) -> dict[str, Any] | None:
    for query in [code, company.replace("G-", "").replace("Ｇ－", ""), company.split("ホールディングス")[0]]:
        if not query:
            continue
        documents = search_documents(query)
        exact = [doc for doc in documents if str(doc.get("ticker_code") or "").upper() == code.upper()]
        if exact:
            return exact[0]
        normalized = company.replace("株式会社", "").replace("G-", "").replace("Ｇ－", "").replace(" ", "")
        named = [doc for doc in documents if normalized and normalized in str(doc.get("name") or "").replace("株式会社", "").replace(" ", "")]
        if len(named) == 1:
            return named[0]
    return None


def parse_date(text: str) -> str | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def money_to_million(text: str) -> float | None:
    match = MONEY_RE.search(text.replace(" ", ""))
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    return value * {"兆円": 1_000_000, "億円": 100, "百万円": 1, "千円": 0.001, "万円": 0.01}[match.group(2)]


def annual_detail_url(company_page: str) -> tuple[str | None, list[str]]:
    response = get(company_page)
    soup = BeautifulSoup(response.text, "html.parser")
    links = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(response.url, anchor["href"])
        if href.startswith(DISCLOSURE_BASE) and "/tdnet/" in href:
            links.append(href)
    links = list(dict.fromkeys(links))
    return (links[0] if links else None), links


def quarter_records(detail_url: str, code: str, company: str) -> list[dict[str, Any]]:
    response = get(detail_url)
    soup = BeautifulSoup(response.text, "html.parser")
    records: list[dict[str, Any]] = []
    tables = soup.find_all("table")
    quarter_table = None
    for table in tables:
        headers = " ".join(cell.get_text(" ", strip=True) for cell in table.find_all("th"))
        if all(label in headers for label in ["年度", "1Q", "2Q", "3Q", "4Q"]):
            quarter_table = table
            break
    if quarter_table is None:
        return records
    for row in quarter_table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 5:
            continue
        fiscal_text = cells[0].get_text(" ", strip=True)
        fiscal_match = FISCAL_YEAR_RE.search(fiscal_text)
        fiscal_year = fiscal_match.group(1) if fiscal_match else None
        fiscal_month = fiscal_match.group(2) if fiscal_match else None
        for quarter_no, cell in enumerate(cells[1:5], start=1):
            text = cell.get_text(" ", strip=True)
            if not text or text == "-":
                continue
            detail_links = []
            pdf_links = []
            for anchor in cell.find_all("a", href=True):
                href = urljoin(response.url, anchor["href"])
                if "/tdnet/" in href and href.startswith(DISCLOSURE_BASE):
                    detail_links.append(href)
                if ".pdf" in href.lower() or "pdf.catr.jp" in href.lower() or "release.tdnet.info" in href.lower():
                    pdf_links.append(href)
            release_date = parse_date(text)
            if release_date and release_date < "2021-01-01":
                continue
            sales_match = re.search(r"売上高(?:等)?\s*([^\s]+(?:円|万円|百万円|億円|千円))", text)
            operating_match = re.search(r"営業利益\s*([^\s]+(?:円|万円|百万円|億円|千円))", text)
            records.append({
                "code": code,
                "company": company,
                "fiscal_year": fiscal_year,
                "fiscal_month": fiscal_month,
                "quarter": f"{quarter_no}Q",
                "release_date": release_date,
                "detail_url": detail_links[0] if detail_links else None,
                "pdf_url": pdf_links[0] if pdf_links else None,
                "pdf_candidates": list(dict.fromkeys(pdf_links)),
                "sales_m": money_to_million(sales_match.group(1)) if sales_match else None,
                "operating_profit_m": money_to_million(operating_match.group(1)) if operating_match else None,
                "cell_text": text,
                "status": "OK" if pdf_links else "NO_PDF_LINK",
            })
    return records


def company_inventory(item: dict[str, str]) -> dict[str, Any]:
    code = item["code"]
    company = item["company"]
    output: dict[str, Any] = {"code": code, "company": company, "details": []}
    try:
        document = search_company(code, company)
        if not document:
            output["status"] = "COMPANY_NOT_FOUND"
            return output
        output["search_document"] = document
        key = document.get("key")
        company_id = document.get("id")
        company_page = f"{COMPANY_BASE}/companies/{key}/{company_id}"
        output["company_page"] = company_page
        detail_url, annual_links = annual_detail_url(company_page)
        output["annual_detail_candidates"] = annual_links
        if not detail_url:
            output["status"] = "ANNUAL_DETAIL_NOT_FOUND"
            return output
        output["annual_detail_url"] = detail_url
        records = quarter_records(detail_url, code, company)
        records.sort(key=lambda record: (record.get("release_date") or "", record.get("quarter") or ""), reverse=True)
        output["details"] = records
        output["status"] = "OK" if records else "NO_QUARTER_RECORDS"
        output["detail_count"] = len(records)
        output["pdf_count"] = sum(bool(record.get("pdf_url")) for record in records)
    except Exception as exc:
        output.update(status="COMPANY_INVENTORY_FAILED", error=f"{type(exc).__name__}: {exc}")
    return output


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    companies = json.loads(COMPANIES.read_text(encoding="utf-8"))
    inventories = []
    with cf.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(company_inventory, item) for item in companies]
        for future in cf.as_completed(futures):
            inventories.append(future.result())
    inventories.sort(key=lambda item: item["code"])
    with (OUT / "company_inventories.jsonl").open("w", encoding="utf-8") as handle:
        for item in inventories:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    details = [detail for item in inventories for detail in item.get("details", [])]
    domains = Counter()
    for detail in details:
        url = detail.get("pdf_url") or ""
        if "pdf.catr.jp" in url:
            domains["pdf.catr.jp"] += 1
        elif "release.tdnet.info" in url:
            domains["release.tdnet.info"] += 1
        elif url:
            domains["other"] += 1
    summary = {
        "companies": len(companies),
        "company_status_counts": dict(Counter(item.get("status") for item in inventories)),
        "companies_with_8_or_more_details": sum(item.get("detail_count", 0) >= 8 for item in inventories),
        "companies_with_12_or_more_details": sum(item.get("detail_count", 0) >= 12 for item in inventories),
        "details": len(details),
        "pdf_links": sum(bool(detail.get("pdf_url")) for detail in details),
        "pdf_domain_counts": dict(domains),
        "detail_status_counts": dict(Counter(detail.get("status") for detail in details)),
        "not_ready_codes": [item["code"] for item in inventories if item.get("status") != "OK"],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
