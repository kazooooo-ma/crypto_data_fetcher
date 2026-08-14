from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from backlog_structured_parser_v2 import extract_candidates, select_candidate

FIXTURES = Path("data/backlog_v22_gold_fixtures.json")
OUT = Path("out/backlog-parser-regression-v2")
CATR_BASE = "https://disclosure.catr.jp"
CATR_SEARCH = CATR_BASE + "/search/typesense"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 backlog-parser-regression/2.2.2"})
DATE_RE = re.compile(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})")


def get(url: str, **kwargs) -> requests.Response:
    error = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, timeout=90, allow_redirects=True, **kwargs)
            if response.status_code == 200:
                return response
            error = RuntimeError(f"HTTP {response.status_code}: {url}")
        except Exception as exc:
            error = exc
        time.sleep(attempt + 1)
    raise error or RuntimeError(url)


def tdnet_id(url: str) -> str | None:
    match = re.search(r"(\d{18})\.pdf", unquote(url or ""))
    return match.group(1) if match else None


def parse_date(text: str) -> str | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def search_catr_company(query: str, code: str) -> dict | None:
    params = {
        "q": query,
        "query_by": "full_text_search",
        "infix": "always",
        "num_typos": "0",
        "prefix": "false",
        "per_page": 20,
        "page": 1,
        "sort_by": "settlement_count:desc",
    }
    data = get(CATR_SEARCH, params=params).json()
    documents = [hit.get("document") or {} for hit in data.get("hits") or []]
    exact = [doc for doc in documents if str(doc.get("ticker_code") or "").upper() == code.upper()]
    return exact[0] if exact else (documents[0] if len(documents) == 1 else None)


def catr_pdf_urls(code: str, company: str, release_date: str) -> list[str]:
    document = search_catr_company(code, code) or search_catr_company(company, code)
    if not document:
        return []
    key = document.get("key")
    company_id = document.get("id")
    if not key or not company_id:
        return []
    metrics_url = f"{CATR_BASE}/companies/{key}/{company_id}/metrics"
    soup = BeautifulSoup(get(metrics_url).text, "html.parser")
    detail_urls = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(metrics_url, anchor["href"])
        if href.startswith(CATR_BASE) and "/tdnet/" in href:
            detail_urls.append(href)
    outputs = []
    for detail_url in list(dict.fromkeys(detail_urls)):
        try:
            response = get(detail_url)
            detail = BeautifulSoup(response.text, "html.parser")
            text = detail.get_text(" ", strip=True)[:2200]
            if parse_date(text) != release_date:
                continue
            for tag in detail.find_all(["a", "iframe", "embed"]):
                href = tag.get("href") or tag.get("src")
                if not href:
                    continue
                url = urljoin(response.url, href)
                if ".pdf" in url.lower() or "pdf.catr.jp" in url.lower():
                    outputs.append(url)
        except Exception:
            continue
    return list(dict.fromkeys(outputs))


def fetch_pdf(fixture: dict) -> tuple[bytes | None, str | None, list[dict]]:
    original = fixture["source_url"]
    queue = [original]
    fid = tdnet_id(original)
    if fid:
        queue.extend(
            [
                f"https://www.release.tdnet.info/inbs/{fid}.pdf",
                f"https://tdnet-pdf.kabutan.jp/{fid[:8]}/{fid}.pdf",
            ]
        )
    try:
        queue.extend(catr_pdf_urls(fixture["code"], fixture["company"], fixture["ir_date"]))
    except Exception:
        pass

    audit = []
    seen = set()
    while queue:
        current = queue.pop(0)
        if not current or current in seen:
            continue
        seen.add(current)
        try:
            response = get(current)
            audit.append(
                {
                    "url": current,
                    "final_url": response.url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "bytes": len(response.content),
                }
            )
            offset = response.content[:4096].find(b"%PDF")
            if offset >= 0:
                return response.content[offset:], response.url, audit
            if response.content.lstrip().startswith(b"<"):
                soup = BeautifulSoup(response.text, "html.parser")
                for tag in soup.find_all(["a", "iframe", "embed"]):
                    href = tag.get("href") or tag.get("src")
                    if href and "pdf" in href.lower():
                        queue.append(urljoin(response.url, href))
        except Exception as exc:
            audit.append({"url": current, "error": f"{type(exc).__name__}: {exc}"})
    return None, None, audit


def close_enough(actual: float | None, expected: float) -> bool:
    if actual is None:
        return False
    tolerance = max(0.5, abs(expected) * 0.005)
    return abs(actual - expected) <= tolerance


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    results = []
    for fixture in fixtures:
        payload, final_url, fetch_audit = fetch_pdf(fixture)
        result = {"fixture": fixture, "fetch_audit": fetch_audit, "final_url": final_url}
        if payload is None:
            result["status"] = "PDF_FETCH_FAILED"
            results.append(result)
            continue
        result["pdf_sha256"] = hashlib.sha256(payload).hexdigest()
        try:
            candidates, parser_audit = extract_candidates(payload)
        except Exception as exc:
            result.update(status="PARSER_EXCEPTION", error=f"{type(exc).__name__}: {exc}")
            results.append(result)
            continue
        selected = select_candidate(candidates)
        expected = float(fixture["expected_current_backlog_m"])
        matching = [candidate for candidate in candidates if close_enough(candidate.current_backlog_m, expected)]
        result.update(
            {
                "status": "PARSED",
                "parser_audit": parser_audit,
                "selected": selected.to_dict() if selected else None,
                "selected_match": close_enough(selected.current_backlog_m if selected else None, expected),
                "candidate_match": bool(matching),
                "matching_candidates": [candidate.to_dict() for candidate in matching[:10]],
                "top_candidates": [candidate.to_dict() for candidate in candidates[:25]],
            }
        )
        results.append(result)

    direct = [result for result in results if result["fixture"].get("method") != "FLOW_RECONSTRUCTION"]
    fetched = [result for result in direct if result.get("status") == "PARSED"]
    candidate_matches = [result for result in fetched if result.get("candidate_match")]
    selected_matches = [result for result in fetched if result.get("selected_match")]
    summary = {
        "fixtures": len(results),
        "direct_fixtures": len(direct),
        "pdf_fetched_direct": len(fetched),
        "pdf_fetch_rate_direct": len(fetched) / len(direct) if direct else 0,
        "candidate_matches_direct": len(candidate_matches),
        "candidate_match_rate_fetched": len(candidate_matches) / len(fetched) if fetched else 0,
        "selected_matches_direct": len(selected_matches),
        "selected_match_rate_fetched": len(selected_matches) / len(fetched) if fetched else 0,
        "fetch_failures": [result["fixture"]["code"] for result in direct if result.get("status") != "PARSED"],
        "candidate_misses": [result["fixture"]["code"] for result in fetched if not result.get("candidate_match")],
        "selection_misses": [result["fixture"]["code"] for result in fetched if not result.get("selected_match")],
    }
    summary["collection_ready"] = bool(
        summary["pdf_fetch_rate_direct"] >= 0.90
        and summary["candidate_match_rate_fetched"] >= 0.90
        and summary["selected_match_rate_fetched"] >= 0.80
    )
    with (OUT / "regression_results.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
