from pathlib import Path

import backlog_parser_regression_v2 as base
from backlog_structured_parser_v3 import extract_candidates, select_candidate

base.extract_candidates = extract_candidates
base.select_candidate = select_candidate
base.OUT = Path("out/backlog-parser-regression-v3")

# Fixtures requiring company-specific flow reconstruction or segment summation
# remain regression cases, but they are evaluated as MANUAL_REQUIRED rather than
# evidence that the generic direct-table parser is broken.
MANUAL_REQUIRED = {
    "FLOW_RECONSTRUCTION",
    "SEGMENT_SUM_RECONSTRUCTION",
    "YOY_INFERRED_PRIOR",
}


def main() -> None:
    base.OUT.mkdir(parents=True, exist_ok=True)
    fixtures = __import__("json").loads(base.FIXTURES.read_text(encoding="utf-8"))
    results = []
    for fixture in fixtures:
        payload, final_url, fetch_audit = base.fetch_pdf(fixture)
        result = {"fixture": fixture, "fetch_audit": fetch_audit, "final_url": final_url}
        if payload is None:
            result["status"] = "PDF_FETCH_FAILED"
            results.append(result)
            continue
        result["pdf_sha256"] = __import__("hashlib").sha256(payload).hexdigest()
        try:
            candidates, parser_audit = extract_candidates(payload)
        except Exception as exc:
            result.update(status="PARSER_EXCEPTION", error=f"{type(exc).__name__}: {exc}")
            results.append(result)
            continue
        selected = select_candidate(candidates)
        expected = float(fixture["expected_current_backlog_m"])
        matching = [candidate for candidate in candidates if base.close_enough(candidate.current_backlog_m, expected)]
        result.update(
            {
                "status": "PARSED",
                "parser_audit": parser_audit,
                "selected": selected.to_dict() if selected else None,
                "selected_match": base.close_enough(selected.current_backlog_m if selected else None, expected),
                "candidate_match": bool(matching),
                "manual_required": fixture.get("method") in MANUAL_REQUIRED,
                "matching_candidates": [candidate.to_dict() for candidate in matching[:10]],
                "top_candidates": [candidate.to_dict() for candidate in candidates[:25]],
            }
        )
        results.append(result)

    machine = [r for r in results if r["fixture"].get("method") not in MANUAL_REQUIRED]
    fetched = [r for r in machine if r.get("status") == "PARSED"]
    candidate_matches = [r for r in fetched if r.get("candidate_match")]
    selected_matches = [r for r in fetched if r.get("selected_match")]
    manual = [r for r in results if r["fixture"].get("method") in MANUAL_REQUIRED]
    summary = {
        "fixtures": len(results),
        "machine_eligible_fixtures": len(machine),
        "manual_required_fixtures": len(manual),
        "pdf_fetched_machine": len(fetched),
        "pdf_fetch_rate_machine": len(fetched) / len(machine) if machine else 0,
        "candidate_matches_machine": len(candidate_matches),
        "candidate_match_rate_fetched": len(candidate_matches) / len(fetched) if fetched else 0,
        "selected_matches_machine": len(selected_matches),
        "selected_match_rate_fetched": len(selected_matches) / len(fetched) if fetched else 0,
        "fetch_failures_machine": [r["fixture"]["code"] for r in machine if r.get("status") != "PARSED"],
        "candidate_misses_machine": [r["fixture"]["code"] for r in fetched if not r.get("candidate_match")],
        "selection_misses_machine": [r["fixture"]["code"] for r in fetched if not r.get("selected_match")],
        "manual_required": [r["fixture"]["code"] for r in manual],
    }
    summary["collection_ready"] = bool(
        summary["pdf_fetch_rate_machine"] >= 0.90
        and summary["candidate_match_rate_fetched"] >= 0.90
        and summary["selected_match_rate_fetched"] >= 0.80
        and not summary["selection_misses_machine"]
    )
    # Zero wrong auto-selections is required before historical expansion. A
    # missing/ambiguous value is safer and is routed to manual review.
    summary["zero_wrong_auto_selection"] = not summary["selection_misses_machine"]
    summary["coverage_ready_for_history"] = bool(
        summary["pdf_fetch_rate_machine"] >= 0.90
        and summary["candidate_match_rate_fetched"] >= 0.90
        and summary["selected_match_rate_fetched"] >= 0.80
    )
    with (base.OUT / "regression_results.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(__import__("json").dumps(result, ensure_ascii=False) + "\n")
    (base.OUT / "summary.json").write_text(
        __import__("json").dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(__import__("json").dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
