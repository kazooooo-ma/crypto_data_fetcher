from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
from pathlib import Path
from typing import Any

import backlog_parser_regression_v2 as base
from backlog_structured_parser_v6 import extract_candidates, select_candidate

OUT = Path("out/backlog-parser-regression-v6-fast")
MANUAL_REQUIRED = {
    "FLOW_RECONSTRUCTION",
    "SEGMENT_SUM_RECONSTRUCTION",
    "YOY_INFERRED_PRIOR",
}


def process(fixture: dict[str, Any]) -> dict[str, Any]:
    payload, final_url, fetch_audit = base.fetch_pdf(fixture)
    result: dict[str, Any] = {
        "fixture": fixture,
        "fetch_audit": fetch_audit,
        "final_url": final_url,
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
    matching = [
        candidate
        for candidate in candidates
        if base.close_enough(candidate.current_backlog_m, expected)
    ]
    result.update(
        status="PARSED",
        parser_audit=parser_audit,
        selected=selected.to_dict() if selected else None,
        selected_match=base.close_enough(
            selected.current_backlog_m if selected else None, expected
        ),
        candidate_match=bool(matching),
        matching_candidates=[candidate.to_dict() for candidate in matching[:10]],
        top_candidates=[candidate.to_dict() for candidate in candidates[:25]],
    )
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fixtures = json.loads(base.FIXTURES.read_text(encoding="utf-8"))
    with cf.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(process, fixtures))
    machine = [row for row in results if not row["manual_required"]]
    fetched = [row for row in machine if row.get("status") == "PARSED"]
    candidate_matches = [row for row in fetched if row.get("candidate_match")]
    selected_matches = [row for row in fetched if row.get("selected_match")]
    wrong_auto = [
        row
        for row in fetched
        if row.get("selected") is not None and not row.get("selected_match")
    ]
    ambiguous = [
        row
        for row in fetched
        if row.get("selected") is None and row.get("candidate_match")
    ]
    summary = {
        "fixtures": len(results),
        "machine_eligible_fixtures": len(machine),
        "manual_required_fixtures": len(results) - len(machine),
        "pdf_fetched_machine": len(fetched),
        "pdf_fetch_rate_machine": len(fetched) / len(machine) if machine else 0,
        "candidate_matches_machine": len(candidate_matches),
        "candidate_match_rate_fetched": len(candidate_matches) / len(fetched) if fetched else 0,
        "selected_matches_machine": len(selected_matches),
        "selected_match_rate_fetched": len(selected_matches) / len(fetched) if fetched else 0,
        "wrong_auto_selection_machine": len(wrong_auto),
        "wrong_auto_selection_codes": [row["fixture"]["code"] for row in wrong_auto],
        "ambiguous_machine": len(ambiguous),
        "ambiguous_codes": [row["fixture"]["code"] for row in ambiguous],
        "fetch_failures_machine": [row["fixture"]["code"] for row in machine if row.get("status") != "PARSED"],
        "candidate_misses_machine": [row["fixture"]["code"] for row in fetched if not row.get("candidate_match")],
        "manual_required": [row["fixture"]["code"] for row in results if row["manual_required"]],
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
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
