from __future__ import annotations

import re
from typing import Any

import backlog_structured_parser as legacy
import backlog_structured_parser_v6 as v6

Candidate = legacy.Candidate
norm = legacy.norm
NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
PERCENT = re.compile(r"[-+]?\d+(?:\.\d+)?\s*%")
ORDER_OR_SALES_LABEL = re.compile(r"(?:受\s*注\s*高|売\s*上\s*高)\s*\|", re.I)


def _count_numbers(candidate: Candidate) -> int:
    return len(NUMBER.findall(norm(candidate.evidence)))


def _score(candidate: Candidate, candidates: list[Candidate]) -> float:
    score = v6._score(candidate, candidates)
    method = candidate.method
    text = norm(candidate.evidence + " " + candidate.period_text)

    if method in {
        "TOTAL_TWO_PERIOD_ORDER_BACKLOG",
        "TOTAL_TWO_PERIOD_ORDER_SALES_BACKLOG",
    }:
        # Valid rows contain two periods of order/backlog values. The older
        # parser also emitted this method for forecast order or sales rows; an
        # explicit order/sales label without backlog evidence identifies those
        # false candidates.
        if ORDER_OR_SALES_LABEL.search(text) and not legacy.BACKLOG_RE.search(text):
            score -= 2600
        else:
            score += 2300
            if len(PERCENT.findall(text)) >= 2:
                score += 450
            if _count_numbers(candidate) <= 6:
                score += 250

    if method == "TOTAL_THREE_PERIOD_ORDER_BACKLOG":
        # A six-value row is normally prior/current/reference order+backlog.
        # Longer alternating histories are time-series tables and should not be
        # mistaken for the current total.
        if _count_numbers(candidate) <= 7:
            score += 2300
        else:
            score -= 1800

    if method in {"NARRATIVE_FINAL_BALANCE", "DIRECT_BACKLOG_SENTENCE"}:
        larger_total = [
            other
            for other in candidates
            if other.method
            in {
                "TOTAL_TWO_PERIOD_ORDER_BACKLOG",
                "TOTAL_THREE_PERIOD_ORDER_BACKLOG",
                "TOTAL_BACKLOG_COLUMNS",
                "BACKLOG_SECTION_TOTAL_WITH_RATIOS",
                "TOTAL_ORDER_BACKLOG_YOY_ROW",
            }
            and other.current_backlog_m > candidate.current_backlog_m * 1.03
            and other.scope == "TOTAL"
        ]
        if larger_total:
            score -= 1400

    return score


def extract_candidates(pdf_bytes: bytes) -> tuple[list[Candidate], dict[str, Any]]:
    candidates, audit = v6.extract_candidates(pdf_bytes)
    candidates.sort(
        key=lambda candidate: (
            _score(candidate, candidates),
            candidate.scope == "TOTAL",
            candidate.confidence == "A",
            candidate.current_backlog_m,
        ),
        reverse=True,
    )
    return candidates, {**audit, "v7_candidate_count": len(candidates)}


def select_candidate(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda candidate: _score(candidate, candidates), reverse=True)
    if len(ranked) > 1:
        first_score = _score(ranked[0], candidates)
        second_score = _score(ranked[1], candidates)
        ratio = max(ranked[0].current_backlog_m, ranked[1].current_backlog_m) / max(
            min(ranked[0].current_backlog_m, ranked[1].current_backlog_m), 1e-9
        )
        if first_score - second_score < 20 and ratio > 1.15:
            return None
    return ranked[0]
