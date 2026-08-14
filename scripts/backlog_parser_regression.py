from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from backlog_structured_parser import extract_candidates, select_candidate

FIXTURES = Path("data/backlog_v22_gold_fixtures.json")
OUT = Path("out/backlog-parser-regression-v3")
BASE = "https://disclosure.catr.jp"
SEARCH = BASE + "/search/typesense"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 backlog-parser-regression/3.0"})
DATE_RE = re.compile(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})")


def tdnet_id(url: str) -> str | None:
    match = re.search(r"(\d{18})\.pdf", unquote(url or ""))
    return match.group(1) if match else None


def get(url: str, **kwargs) -> requests.Response:
    error: Exception | None = None
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


def parse_date(text: str) -> str | None:
    match = DATE_RE.search(text or "")
    if not match:
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def search_catr_company(code: str) -> dict | None:
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
    exact = [
        document
        for document in documents
        if str(document.get("ticker_code") or "").upper() == code.upper()
    ]
    return exact[0] if exact else (documents[0] if len(documents) == 1 else None)


def catr_pdf_candidates(code: str, target_date: str) -> list[str]:
    document = search_catr_company(code)
    if not document:
        return []
    key = document.get("key")
    company_id = document.get("id")
    if not key or not company_id:
        return []
    metrics_url = f"{BASE}/companies/{key}/{company_id}/metrics"
    response = get(metrics_url)
    soup = BeautifulSoup(response.text, "html.parser")
    detail_urls: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(response.url, anchor["href"])
        if href.startswith(BASE) and "/tdnet/" in href:
            detail_urls.append(href)
    detail_urls = list(dict.fromkeys(detail_urls))

    dated: list[tuple[int, str]] = []
    target = int(target_date.replace("-", ""))
    for detail_url in detail_urls:
        try:
            detail = get(detail_url)
            dsoup = BeautifulSoup(detail.text, "html.parser")
            text = dsoup.get_text(" ", strip=True)
            release_date = parse_date(text[:1800])
            pdfs: list[str] = []
            for tag in dsoup.find_all(["a", "iframe", "embed"]):
                href = tag.get("href") or tag.get("src")
                if not href:
                    continue
                href = urljoin(detail.url, href)
                if ".pdf" in href.lower() or "pdf.catr.jp" in href.lower():
                    pdfs.append(href)
            pdfs = list(dict.fromkeys(pdfs))
            if not pdfs:
                continue
            distance = 99_999_999
            if release_date:
                distance = abs(int(release_date.replace("-", "")) - target)
            for pdf in pdfs:
                dated.append((distance, pdf))
        except Exception:
            continue
    return [url for _distance, url in sorted(dated, key=lambda item: item[0])]


def candidate_urls(fixture: dict) -> list[str]:
    url = fixture["source_url"]
    urls = [url]
    fid = tdnet_id(url)
    if fid:
        urls.extend(
            [
                f"https://www.release.tdnet.info/inbs/{fid}.pdf",
                f"https://tdnet-pdf.kabutan.jp/{fid[:8]}/{fid}.pdf",
            ]
        )
    try:
        urls.extend(catr_pdf_candidates(str(fixture["code"]), fixture["ir_date"]))
    except Exception:
        pass
    return list(dict.fromkeys(url for url in urls if url))


def fetch_pdf(fixture: dict) -> tuple[bytes | None, str | None, list[dict]]:
    queue = candidate_urls(fixture)
    audit: list[dict] = []
    seen: set[str] = set()
    while queue:
        current = queue.pop(0)
        if not current or current in seen:
            continue
        seen.add(current)
        for attempt in range(3):
            try:
                response = SESSION.get(current, timeout=90, allow_redirects=True)
                audit.append(
                    {
                        "url": current,
                        "final_url": response.url,
                        "status": response.status_code,
                        "content_type": response.headers.get("content-type"),
                        "bytes": len(response.content),
                    }
                )
                if response.status_code != 200:
                    time.sleep(attempt + 1)
                    continue
                offset = response.content[:8192].find(b"%PDF")
                if offset >= 0:
                    return response.content[offset:], response.url, audit
                if (
                    response.content.lstrip().startswith(b"<")
                    or "html"
                    in (response.headers.get("content-type") or "").lower()
                ):
                    soup = BeautifulSoup(response.text, "html.parser")
                    for tag in soup.find_all(["a", "iframe", "embed"]):
                        href = tag.get("href") or tag.get("src")
                        if href and "pdf" in href.lower():
                            queue.append(urljoin(response.url, href))
                break
            except Exception as exc:
                audit.append(
                    {"url": current, "error": f"{type(exc).__name__}: {exc}"}
                )
                time.sleep(attempt + 1)
    return None, None, audit


def close_enough(actual: float | None, expected: float) -> bool:
    if actual is None or not math.isfinite(actual):
        return False
    tolerance = max(0.5, abs(expected) * 0.005)
    return abs(actual - expected) <= tolerance


def prior_close(actual: float | None, expected: float | None) -> bool | None:
    if expected is None:
        return None
    return close_enough(actual, float(expected))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    results: list[dict] = []
    for fixture in fixtures:
        payload, final_url, fetch_audit = fetch_pdf(fixture)
        result = {
            "fixture": fixture,
            "fetch_audit": fetch_audit,
            "final_url": final_url,
        }
        if payload is None:
            result["status"] = "PDF_FETCH_FAILED"
            results.append(result)
            continue
        result["pdf_sha256"] = hashlib.sha256(payload).hexdigest()
        try:
            candidates, parser_audit = extract_candidates(
                payload, as_of_date=fixture["ir_date"]
            )
        except Exception as exc:
            result.update(
                status="PARSER_EXCEPTION", error=f"{type(exc).__name__}: {exc}"
            )
            results.append(result)
            continue
        selected = select_candidate(candidates)
        expected_current = float(fixture["expected_current_backlog_m"])
        expected_prior_raw = fixture.get("expected_prior_backlog_m")
        expected_prior = (
            float(expected_prior_raw)
            if expected_prior_raw not in (None, "")
            else None
        )
        matching = [
            candidate
            for candidate in candidates
            if close_enough(candidate.current_backlog_m, expected_current)
        ]
        result.update(
            {
                "status": "PARSED",
                "parser_audit": parser_audit,
                "selected": selected.to_dict() if selected else None,
                "selected_match": close_enough(
                    selected.current_backlog_m if selected else None,
                    expected_current,
                ),
                "selected_prior_match": prior_close(
                    selected.prior_backlog_m if selected else None,
                    expected_prior,
                ),
                "candidate_match": bool(matching),
                "matching_candidates": [
                    candidate.to_dict() for candidate in matching[:20]
                ],
                "top_candidates": [
                    candidate.to_dict() for candidate in candidates[:30]
                ],
            }
        )
        results.append(result)

    direct = [
        result
        for result in results
        if "RECONSTRUCTION" not in str(result["fixture"].get("method") or "")
    ]
    fetched = [result for result in direct if result.get("status") == "PARSED"]
    candidate_matches = [
        result for result in fetched if result.get("candidate_match")
    ]
    selected_matches = [
        result for result in fetched if result.get("selected_match")
    ]
    prior_comparable = [
        result
        for result in fetched
        if result["fixture"].get("expected_prior_backlog_m") not in (None, "")
    ]
    prior_matches = [
        result for result in prior_comparable if result.get("selected_prior_match")
    ]
    summary = {
        "fixtures": len(results),
        "direct_fixtures": len(direct),
        "pdf_fetched_direct": len(fetched),
        "pdf_fetch_rate_direct": len(fetched) / len(direct) if direct else 0,
        "candidate_matches_direct": len(candidate_matches),
        "candidate_match_rate_fetched": (
            len(candidate_matches) / len(fetched) if fetched else 0
        ),
        "selected_matches_direct": len(selected_matches),
        "selected_match_rate_fetched": (
            len(selected_matches) / len(fetched) if fetched else 0
        ),
        "prior_matches_direct": len(prior_matches),
        "prior_match_rate_comparable": (
            len(prior_matches) / len(prior_comparable) if prior_comparable else 0
        ),
        "fetch_failures": [
            result["fixture"]["code"]
            for result in direct
            if result.get("status") != "PARSED"
        ],
        "candidate_misses": [
            result["fixture"]["code"]
            for result in fetched
            if not result.get("candidate_match")
        ],
        "selection_misses": [
            result["fixture"]["code"]
            for result in fetched
            if not result.get("selected_match")
        ],
        "prior_selection_misses": [
            result["fixture"]["code"]
            for result in prior_comparable
            if not result.get("selected_prior_match")
        ],
    }
    summary["collection_ready"] = bool(
        summary["pdf_fetch_rate_direct"] >= 0.95
        and summary["candidate_match_rate_fetched"] >= 0.90
        and summary["selected_match_rate_fetched"] >= 0.80
        and summary["prior_match_rate_comparable"] >= 0.65
    )
    with (OUT / "regression_results.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
