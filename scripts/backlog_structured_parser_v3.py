from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

import fitz

import backlog_structured_parser as legacy
import backlog_structured_parser_v2 as v2

Candidate = legacy.Candidate
norm = legacy.norm
mixed_jpy_values = legacy.mixed_jpy_values
parse_percent = legacy.parse_percent

BACKLOG_LABEL = re.compile(r"(?:連結|全社|当社グループの|手持)?\s*(?:受\s*注\s*残\s*高?|繰\s*越\s*受\s*注\s*残\s*高?|手\s*持\s*受\s*注\s*残\s*高?)", re.I)
TOTAL_WORD = re.compile(r"合\s*計|総\s*計|全\s*社|連\s*結|当社グループ全体|単一セグメント", re.I)
FORECAST_WORD = re.compile(r"業績予想|通期予想|計画|修正予想|期首受注残|前期末受注残", re.I)
SEGMENT_WORD = re.compile(r"セグメント|事業別|部門別|品目別|内訳|うち", re.I)
SALES_ONLY = re.compile(r"売上高|売上収益|営業利益|純利益", re.I)
PERCENT = re.compile(r"[-+]?\d+(?:\.\d+)?\s*%")
NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _make_candidate(
    current: float,
    prior: float | None,
    page: int,
    evidence: str,
    yoy: float | None,
    scope: str,
) -> Candidate:
    return Candidate(
        current_backlog_m=current,
        prior_backlog_m=prior,
        order_intake_m=None,
        sales_m=None,
        method="DIRECT_BACKLOG_SENTENCE",
        confidence="A" if scope == "TOTAL" else "B",
        score=0,
        page=page,
        evidence=evidence,
        period_text="",
        unit=None,
        yoy=yoy,
        scope=scope,
    )


def _sentence_candidates(pdf_bytes: bytes) -> list[Candidate]:
    """Parse only the sentence containing the backlog label.

    This avoids the previous bug where a later profit or net-asset amount in the
    next paragraph was selected as the backlog balance.
    """
    outputs: list[Candidate] = []
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_number, page in enumerate(document, start=1):
        text = norm(page.get_text("text"))
        for label in BACKLOG_LABEL.finditer(text):
            start = max(0, label.start() - 160)
            after = text[label.end() : label.end() + 260]
            sentence_end = len(after)
            for separator in ("。", "\n", "(2)", "（2）"):
                pos = after.find(separator)
                if pos >= 0:
                    sentence_end = min(sentence_end, pos + (1 if separator == "。" else 0))
            sentence_after = after[:sentence_end]
            evidence = text[start : label.end() + sentence_end]
            values = mixed_jpy_values(sentence_after)
            if not values:
                continue

            # "2,916百万円増の27,443百万円" means the final balance is the
            # latter amount, not the increase amount.
            current = None
            increase_then_balance = re.search(
                r"(?:増加額?|減少額?|増|減)[^。]{0,40}?の\s*"
                r"([0-9][0-9,]*(?:\.\d+)?\s*(?:兆円|億円|百万円|千円|万円))",
                sentence_after,
            )
            if increase_then_balance:
                found = mixed_jpy_values(increase_then_balance.group(1))
                current = found[0][0] if found else None
            if current is None:
                # Prefer the first explicit amount after the backlog label.
                current = values[0][0]
            if current is None or current <= 0:
                continue

            yoy = None
            comparison = re.search(
                r"(?:前年同期比|前年同四半期比|前期比|前連結会計年度末比)\s*"
                r"([-+]?\d+(?:\.\d+)?)\s*%\s*(増|減)?",
                sentence_after,
            )
            if comparison:
                value = float(comparison.group(1)) / 100.0
                yoy = -abs(value) if comparison.group(2) == "減" else value
            else:
                yoy = parse_percent(sentence_after)
            prior = current / (1 + yoy) if yoy is not None and yoy > -0.999 else None
            scope = "TOTAL" if TOTAL_WORD.search(evidence) or re.search(r"連結\s*受\s*注\s*残", evidence) else "NARRATIVE"
            outputs.append(_make_candidate(current, prior, page_number, evidence, yoy, scope))
    return outputs


def _numeric_count(text: str) -> int:
    return len(NUMBER.findall(norm(text)))


def _support_counts(candidates: list[Candidate]) -> dict[int, int]:
    groups: dict[int, set[str]] = defaultdict(set)
    for candidate in candidates:
        # 0.1% grouping is tight enough to merge table/narrative copies while
        # keeping distinct segment balances separate.
        key = round(candidate.current_backlog_m * 10)
        groups[key].add(candidate.method)
    return {key: len(methods) for key, methods in groups.items()}


def _selector_score(candidate: Candidate, all_candidates: list[Candidate], support: dict[int, int]) -> float:
    method_base = {
        "DIRECT_BACKLOG_SENTENCE": 720,
        "TOTAL_BACKLOG_COLUMNS": 690,
        "BACKLOG_SECTION_TOTAL_WITH_RATIOS": 680,
        "VERTICAL_TOTAL_ORDER_BACKLOG": 670,
        "ROW_PERIOD_PAIR": 610,
        "ROW_CURRENT": 560,
        "BACKLOG_TIME_SERIES_LAST": 530,
        "CONSOLIDATED_BACKLOG_SERIES_LAST": 540,
        "NARRATIVE_FINAL_BALANCE": 500,
        "NARRATIVE_BACKLOG": 480,
        "TOTAL_TWO_PERIOD_ORDER_SALES_BACKLOG": 430,
        "TOTAL_THREE_PERIOD_ORDER_BACKLOG": 420,
        "TOTAL_TWO_PERIOD_ORDER_BACKLOG": 410,
        "SEPARATED_TEXT_PAIR": 180,
    }
    score = method_base.get(candidate.method, 250)
    evidence = norm(candidate.evidence + " " + candidate.period_text)

    if candidate.scope == "TOTAL":
        score += 140
    elif candidate.scope == "NARRATIVE":
        score += 70
    elif candidate.scope == "SINGLE_SEGMENT":
        score -= 180
    elif candidate.scope == "METRIC_ROW":
        score += 10

    if TOTAL_WORD.search(evidence):
        score += 80
    if BACKLOG_LABEL.search(evidence):
        score += 60
    if FORECAST_WORD.search(evidence):
        score -= 500
    if SEGMENT_WORD.search(evidence) and not TOTAL_WORD.search(evidence):
        score -= 120
    if SALES_ONLY.search(evidence) and not BACKLOG_LABEL.search(evidence):
        score -= 450

    # A long time series is useful, but a direct current-quarter summary row is
    # preferred when both exist. It remains a viable fallback for FUJI-like
    # documents where the current value appears only in the historical chart.
    if candidate.method in {"BACKLOG_TIME_SERIES_LAST", "CONSOLIDATED_BACKLOG_SERIES_LAST"}:
        numbers = _numeric_count(evidence)
        if numbers >= 8:
            score -= 45
        else:
            score += 20

    # Direct rows such as "受注残高 | prior | current | change | yoy" are
    # current-quarter evidence, not generic segment rows.
    if candidate.method == "ROW_PERIOD_PAIR":
        if re.match(r"^(?:[^:]{0,30}:\s*)?受\s*注\s*残", evidence):
            score += 100
        if _numeric_count(evidence) <= 5:
            score += 55

    # Duplicate confirmation from independent extraction routes is powerful.
    score += 95 * max(0, support.get(round(candidate.current_backlog_m * 10), 1) - 1)

    # The selected total should normally not be orders of magnitude below
    # another credible total/narrative candidate in the same document.
    credible = [
        c.current_backlog_m
        for c in all_candidates
        if c.current_backlog_m > 0
        and c.method not in {"SEPARATED_TEXT_PAIR"}
        and c.scope != "SINGLE_SEGMENT"
        and not FORECAST_WORD.search(norm(c.evidence + " " + c.period_text))
    ]
    max_value = max(credible) if credible else candidate.current_backlog_m
    if candidate.current_backlog_m < max_value * 0.03 and candidate.scope != "TOTAL":
        score -= 220

    # Validate an explicit YoY relationship when both inputs exist.
    if candidate.yoy is not None and candidate.prior_backlog_m not in (None, 0):
        implied = candidate.current_backlog_m / candidate.prior_backlog_m - 1
        if abs(implied - candidate.yoy) <= 0.02:
            score += 35
        else:
            score -= 90

    if candidate.confidence == "A":
        score += 35
    elif candidate.confidence == "C":
        score -= 80
    return score


def extract_candidates(pdf_bytes: bytes) -> tuple[list[Candidate], dict[str, Any]]:
    candidates, audit = v2.extract_candidates(pdf_bytes)
    candidates.extend(_sentence_candidates(pdf_bytes))

    # Deduplicate by value/prior/page but keep the strongest evidence route.
    dedup: dict[tuple[int, int, int], Candidate] = {}
    for candidate in candidates:
        key = (
            round(candidate.current_backlog_m * 1000),
            round((candidate.prior_backlog_m if candidate.prior_backlog_m is not None else -1) * 1000),
            candidate.page,
        )
        existing = dedup.get(key)
        if existing is None or candidate.confidence < existing.confidence:
            dedup[key] = candidate
    candidates = list(dedup.values())
    support = _support_counts(candidates)
    candidates.sort(
        key=lambda candidate: (
            _selector_score(candidate, candidates, support),
            candidate.scope == "TOTAL",
            candidate.confidence == "A",
            candidate.current_backlog_m,
        ),
        reverse=True,
    )
    audit = {
        **audit,
        "v3_sentence_candidates": sum(c.method == "DIRECT_BACKLOG_SENTENCE" for c in candidates),
        "v3_candidate_count": len(candidates),
    }
    return candidates, audit


def select_candidate(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    support = _support_counts(candidates)
    ranked = sorted(
        candidates,
        key=lambda candidate: _selector_score(candidate, candidates, support),
        reverse=True,
    )
    best = ranked[0]
    # If two incompatible candidates are almost tied, ambiguity is safer than
    # silently choosing the wrong segment/period.
    if len(ranked) > 1:
        first = _selector_score(ranked[0], candidates, support)
        second = _selector_score(ranked[1], candidates, support)
        ratio = max(ranked[0].current_backlog_m, ranked[1].current_backlog_m) / max(
            min(ranked[0].current_backlog_m, ranked[1].current_backlog_m), 1e-9
        )
        if first - second < 12 and ratio > 1.20:
            return None
    return best
