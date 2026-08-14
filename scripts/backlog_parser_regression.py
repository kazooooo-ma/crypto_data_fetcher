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
OUT = Path("out/backlog-parser-regression")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 backlog-parser-regression/2.2.1"})


def tdnet_id(url: str) -> str | None:
    match = re.search(r"(\d{18})\.pdf", unquote(url or ""))
    return match.group(1) if match else None


def fetch_pdf(url: str) -> tuple[bytes | None, str | None, list[dict]]:
    queue = [url]
    fid = tdnet_id(url)
    if fid:
        queue.extend([
            f"https://www.release.tdnet.info/inbs/{fid}.pdf",
            f"https://tdnet-pdf.kabutan.jp/{fid[:8]}/{fid}.pdf",
        ])
    audit = []
    seen = set()
    while queue:
        current = queue.pop(0)
        if not current or current in seen:
            continue
        seen.add(current)
        for attempt in range(3):
            try:
                response = SESSION.get(current, timeout=90, allow_redirects=True)
                audit.append({
                    "url": current,
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
                if response.content.lstrip().startswith(b"<") or "html" in (response.headers.get("content-type") or "").lower():
                    soup = BeautifulSoup(response.text, "html.parser")
                    for tag in soup.find_all(["a", "iframe", "embed"]):
                        href = tag.get("href") or tag.get("src")
                        if href and "pdf" in href.lower():
                            queue.append(urljoin(response.url, href))
                break
            except Exception as exc:
                audit.append({"url": current, "error": f"{type(exc).__name__}: {exc}"})
                time.sleep(attempt + 1)
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
        payload, final_url, fetch_audit = fetch_pdf(fixture["source_url"])
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
        result.update({
            "status": "PARSED",
            "parser_audit": parser_audit,
            "selected": selected.to_dict() if selected else None,
            "selected_match": close_enough(selected.current_backlog_m if selected else None, expected),
            "candidate_match": bool(matching),
            "matching_candidates": [candidate.to_dict() for candidate in matching[:10]],
            "top_candidates": [candidate.to_dict() for candidate in candidates[:20]],
        })
        results.append(result)

    direct = [r for r in results if r["fixture"].get("method") != "FLOW_RECONSTRUCTION"]
    fetched = [r for r in direct if r.get("status") == "PARSED"]
    candidate_matches = [r for r in fetched if r.get("candidate_match")]
    selected_matches = [r for r in fetched if r.get("selected_match")]
    summary = {
        "fixtures": len(results),
        "direct_fixtures": len(direct),
        "pdf_fetched_direct": len(fetched),
        "pdf_fetch_rate_direct": len(fetched) / len(direct) if direct else 0,
        "candidate_matches_direct": len(candidate_matches),
        "candidate_match_rate_fetched": len(candidate_matches) / len(fetched) if fetched else 0,
        "selected_matches_direct": len(selected_matches),
        "selected_match_rate_fetched": len(selected_matches) / len(fetched) if fetched else 0,
        "fetch_failures": [r["fixture"]["code"] for r in direct if r.get("status") != "PARSED"],
        "candidate_misses": [r["fixture"]["code"] for r in fetched if not r.get("candidate_match")],
        "selection_misses": [r["fixture"]["code"] for r in fetched if not r.get("selected_match")],
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
