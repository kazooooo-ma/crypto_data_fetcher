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
BASE = "https://disclosure.catr.jp"
SEARCH = BASE + "/search/typesense"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 backlog-history-source-recovery/2.2.1"})
DATE_RE = re.compile(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})")
QUARTER_RE = re.compile(r"(?:第\s*)?([1-4])\s*四半期|([1-4])Q|通期|中間", re.I)


def get(url: str, **kwargs) -> requests.Response:
    error = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, timeout=60, **kwargs)
            if response.status_code == 200:
                return response
            error = RuntimeError(f"HTTP {response.status_code}: {url}")
        except Exception as exc:
            error = exc
        time.sleep(attempt + 1)
    raise error or RuntimeError(url)


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


def parse_date(text: str) -> str | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def quarter_label(text: str) -> str | None:
    text = text.replace("第２", "第2").replace("第１", "第1").replace("第３", "第3").replace("第４", "第4")
    match = QUARTER_RE.search(text)
    if not match:
        return None
    if "通期" in match.group(0):
        return "4Q"
    if "中間" in match.group(0):
        return "2Q"
    number = match.group(1) or match.group(2)
    return f"{number}Q" if number else None


def detail_record(url: str, code: str, company: str) -> dict[str, Any]:
    record = {"code": code, "company": company, "detail_url": url}
    try:
        response = get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        record["release_date"] = parse_date(text[:1200])
        record["quarter"] = quarter_label(text[:2000])
        record["title"] = (soup.find("h2") or soup.find("h1")).get_text(" ", strip=True) if (soup.find("h2") or soup.find("h1")) else ""
        pdfs = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(response.url, anchor["href"])
            if ".pdf" in href.lower() or "pdf.catr.jp" in href.lower():
                pdfs.append(href)
        record["pdf_url"] = pdfs[0] if pdfs else None
        record["pdf_candidates"] = pdfs
        # Structured sales on CATR detail page.
        sales = None
        operating_profit = None
        for row in soup.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) >= 2:
                label = cells[0]
                if label == "売上高" or label.startswith("売上高"):
                    sales = cells[1]
                if label == "営業利益" or label.startswith("営業利益"):
                    operating_profit = cells[1]
        record["sales_text"] = sales
        record["operating_profit_text"] = operating_profit
        record["status"] = "OK" if record["pdf_url"] else "NO_PDF_LINK"
    except Exception as exc:
        record.update(status="DETAIL_FETCH_FAILED", error=f"{type(exc).__name__}: {exc}")
    return record


def company_inventory(item: dict[str, str]) -> dict[str, Any]:
    code = item["code"]
    company = item["company"]
    output: dict[str, Any] = {"code": code, "company": company, "details": []}
    try:
        document = search_company(code)
        if not document:
            output["status"] = "COMPANY_NOT_FOUND"
            return output
        output["search_document"] = document
        key = document.get("key")
        company_id = document.get("id")
        metrics_url = f"{BASE}/companies/{key}/{company_id}/metrics"
        output["metrics_url"] = metrics_url
        response = get(metrics_url)
        soup = BeautifulSoup(response.text, "html.parser")
        links = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(response.url, anchor["href"])
            if "/tdnet/" in href and href.startswith(BASE):
                links.append(href)
        links = list(dict.fromkeys(links))
        # Newest first, then limit to history relevant to 2021 onward after detail parsing.
        with cf.ThreadPoolExecutor(max_workers=8) as executor:
            records = list(executor.map(lambda url: detail_record(url, code, company), links))
        records = [record for record in records if not record.get("release_date") or record["release_date"] >= "2021-01-01"]
        records.sort(key=lambda record: record.get("release_date") or "", reverse=True)
        output["details"] = records
        output["status"] = "OK"
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
        "details": len(details),
        "pdf_links": sum(bool(detail.get("pdf_url")) for detail in details),
        "pdf_domain_counts": dict(domains),
        "detail_status_counts": dict(Counter(detail.get("status") for detail in details)),
        "not_found_codes": [item["code"] for item in inventories if item.get("status") != "OK"],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
