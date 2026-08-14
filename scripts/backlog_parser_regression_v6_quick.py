from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import requests

from backlog_structured_parser_v6 import extract_candidates, select_candidate

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "backlog_v22_gold_fixtures.json"
OUT = Path("out/backlog-parser-regression-v6-quick")
MANUAL_REQUIRED = {
    "FLOW_RECONSTRUCTION",
    "SEGMENT_SUM_RECONSTRUCTION",
    "YOY_INFERRED_PRIOR",
}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 backlog-regression-v6-quick/1.0"})


def close(actual: float | None, expected: float) -> bool:
    return actual is not None and abs(float(actual) - expected) <= max(1.0, abs(expected) * 0.005)


def urls(fixture: dict[str, Any]) -> list[str]:
    values = []
    source = str(fixture.get("source_url") or "")
    if source:
        values.append(source)
    match = re.search(r"(\d{18})\.pdf", source)
    if match:
        file_id = match.group(1)
        date = str(fixture.get("ir_date") or "").replace("-", "")
        values.extend(
            [
                f"https://www.release.tdnet.info/inbs/{file_id}.pdf",
                f"https://tdnet-pdf.kabutan.jp/{date}/{file_id}.pdf",
            ]
        )
    return list(dict.fromkeys(value for value in values if value))


def fetch(fixture: dict[str, Any]) -> tuple[bytes | None, list[dict[str, Any]]]:
    audit = []
    for url in urls(fixture):
        try:
            response = SESSION.get(url, timeout=(10, 25), allow_redirects=True)
            offset = response.content[:32768].find(b"%PDF")
            audit.append({"url": url, "status": response.status_code, "bytes": len(response.content), "final_url": response.url})
            if response.status_code == 200 and offset >= 0:
                return response.content[offset:], audit
        except Exception as exc:
            audit.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
    return None, audit


def process(fixture: dict[str, Any]) -> dict[str, Any]:
    payload, audit = fetch(fixture)
    result: dict[str, Any] = {
        "fixture": fixture,
        "fetch_audit": audit,
        "manual_required": fixture.get("method") in MANUAL_REQUIRED,
    }
    if payload is None:
        result["status"] = "PDF_FETCH_FAILED"
        return result
    result["pdf_sha256"] = hashlib.sha256(payload).hexdigest()
    try:
        candidates, parser_audit = extract_candidates(payload)
        selected = select_candidate(candidates)
    except Exception as exc:
        result.update(status="PARSER_EXCEPTION", error=f"{type(exc).__name__}: {exc}")
        return result
    expected = float(fixture["expected_current_backlog_m"])
    matching = [candidate for candidate in candidates if close(candidate.current_backlog_m, expected)]
    result.update(
        status="PARSED",
        parser_audit=parser_audit,
        selected=selected.to_dict() if selected else None,
        selected_match=close(selected.current_backlog_m if selected else None, expected),
        candidate_match=bool(matching),
        matching_candidates=[candidate.to_dict() for candidate in matching[:10]],
        top_candidates=[candidate.to_dict() for candidate in candidates[:25]],
    )
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    with cf.ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(process, fixtures))
    machine = [row for row in results if not row["manual_required"]]
    fetched = [row for row in machine if row.get("status") == "PARSED"]
    candidate_match = [row for row in fetched if row.get("candidate_match")]
    selected_match = [row for row in fetched if row.get("selected_match")]
    wrong = [row for row in fetched if row.get("selected") is not None and not row.get("selected_match")]
    summary = {
        "fixtures": len(results),
        "machine_eligible_fixtures": len(machine),
        "manual_required_fixtures": len(results) - len(machine),
        "pdf_fetched_machine": len(fetched),
        "pdf_fetch_rate_machine": len(fetched) / len(machine) if machine else 0,
        "candidate_matches_machine": len(candidate_match),
        "candidate_match_rate_fetched": len(candidate_match) / len(fetched) if fetched else 0,
        "selected_matches_machine": len(selected_match),
        "selected_match_rate_fetched": len(selected_match) / len(fetched) if fetched else 0,
        "wrong_auto_selection_machine": len(wrong),
        "wrong_auto_selection_codes": [row["fixture"]["code"] for row in wrong],
        "ambiguous_codes": [row["fixture"]["code"] for row in fetched if row.get("selected") is None and row.get("candidate_match")],
        "fetch_failure_codes": [row["fixture"]["code"] for row in machine if row.get("status") != "PARSED"],
        "candidate_miss_codes": [row["fixture"]["code"] for row in fetched if not row.get("candidate_match")],
    }
    summary["collection_ready"] = bool(
        summary["pdf_fetch_rate_machine"] >= 0.90
        and summary["candidate_match_rate_fetched"] >= 0.90
        and summary["selected_match_rate_fetched"] >= 0.80
        and summary["wrong_auto_selection_machine"] == 0
    )
    with (OUT / "regression_results.jsonl").open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
