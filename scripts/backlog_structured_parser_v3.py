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

BACKLOG_LABEL = re.compile(
    r"(?:連結|全社|当社グループの|手持)?\s*"
    r"(?:受\s*注\s*残\s*高?|繰\s*越\s*受\s*注\s*残\s*高?|"
    r"手\s*持\s*受\s*注\s*残\s*高?)",
    re.I,
)
TOTAL_WORD = re.compile(
    r"合\s*計|総\s*計|全\s*社|連\s*結|当社グループ全体|単一セグメント",
    re.I,
)
FORECAST_WORD = re.compile(
    r"業績予想|通期予想|計画|修正予想|期首受注残|前期末受注残",
    re.I,
)
SEGMENT_WORD = re.compile(r"セグメント|事業別|部門別|品目別|内訳|うち", re.I)
SALES_ONLY = re.compile(r"売上高|売上収益|営業利益|純利益", re.I)
CURRENT_PERIOD = re.compile(r"当第|当期|今回|当四半期|現在|2026年度.*(?:Q|四半期)", re.I)
PRIOR_PERIOD = re.compile(r"前第|前期|前年同四半期|前年同期", re.I)
QUARTER_CONTEXT = re.compile(r"第\s*[1-4]\s*四半期|[1-4]\s*Q|中間期|四半期推移", re.I)
ANNUAL_CONTEXT = re.compile(r"年度推移|通期推移|年度\)|年度\s*$", re.I)
NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
CONFIDENCE_RANK = {"A": 3, "B": 2, "C": 1}


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

    This prevents an amount in the following profit, asset or cash-flow
    paragraph from being selected as backlog.
    """
    outputs: list[Candidate] = []
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_number, page in enumerate(document, start=1):
        text = norm(page.get_text("text"))
        for label in BACKLOG_LABEL.finditer(text):
            start = max(0, label.start() - 180)
            after = text[label.end() : label.end() + 320]
            sentence_end = len(after)
            for separator in ("。", "\n", "(2)", "（2）"):
                pos = after.find(separator)
                if pos >= 0:
                    sentence_end = min(
                        sentence_end, pos + (1 if separator == "。" else 0)
                    )
            sentence_after = after[:sentence_end]
            evidence = text[start : label.end() + sentence_end]
            values = mixed_jpy_values(sentence_after)
            if not values:
                continue

            # "2,916百万円増の27,443百万円" means the final balance is the
            # latter amount, not the increase amount.
            current = None
            final_balance = re.search(
                r"(?:増加額?|減少額?|増|減)[^。]{0,55}?の\s*"
                r"([0-9][0-9,]*(?:\.\d+)?\s*"
                r"(?:兆円|億円|百万円|千円|万円))",
                sentence_after,
            )
            if final_balance:
                found = mixed_jpy_values(final_balance.group(1))
                current = found[0][0] if found else None
            if current is None:
                current = values[0][0]
            if current is None or current <= 0:
                continue

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
            scope = (
                "TOTAL"
                if TOTAL_WORD.search(evidence)
                or re.search(r"連結\s*受\s*注\s*残", evidence)
                else "NARRATIVE"
            )
            outputs.append(
                _make_candidate(current, prior, page_number, evidence, yoy, scope)
            )
    return outputs


def _numeric_count(text: str) -> int:
    return len(NUMBER.findall(norm(text)))


def _support_counts(candidates: list[Candidate]) -> dict[int, int]:
    groups: dict[int, set[str]] = defaultdict(set)
    for candidate in candidates:
        key = round(candidate.current_backlog_m * 10)
        groups[key].add(candidate.method)
    return {key: len(methods) for key, methods in groups.items()}


def _page_context_and_scaled_candidates(
    pdf_bytes: bytes, candidates: list[Candidate]
) -> list[Candidate]:
    """Attach quarter/annual context and recover chart units.

    Some presentation charts emit 1,998 with a page-level [億円] marker. The
    legacy separated-text parser lost that unit and produced 1,998 million
    instead of 199,800 million. Both original and scaled candidates are kept;
    selection decides using period and support evidence.
    """
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_texts = {
        number: norm(page.get_text("text"))
        for number, page in enumerate(document, start=1)
    }
    outputs = list(candidates)
    scaled: list[Candidate] = []
    for candidate in candidates:
        context = page_texts.get(candidate.page, "")
        markers = []
        if QUARTER_CONTEXT.search(context):
            markers.append("__QUARTER_CONTEXT__")
        if ANNUAL_CONTEXT.search(context) and not QUARTER_CONTEXT.search(context):
            markers.append("__ANNUAL_CONTEXT__")
        if CURRENT_PERIOD.search(context):
            markers.append("__CURRENT_PERIOD__")
        candidate.period_text = norm(
            f"{candidate.period_text} {' '.join(markers)}"
        )
        if (
            candidate.unit is None
            and re.search(r"(?:単位\s*[:：]?\s*)?億円|\[億円\]", context)
            and 0 < candidate.current_backlog_m < 10_000
            and candidate.method
            in {
                "SEPARATED_TEXT_PAIR",
                "BACKLOG_TIME_SERIES_LAST",
                "CONSOLIDATED_BACKLOG_SERIES_LAST",
                "ROW_PERIOD_PAIR",
            }
        ):
            scaled.append(
                Candidate(
                    current_backlog_m=candidate.current_backlog_m * 100,
                    prior_backlog_m=(
                        candidate.prior_backlog_m * 100
                        if candidate.prior_backlog_m is not None
                        else None
                    ),
                    order_intake_m=(
                        candidate.order_intake_m * 100
                        if candidate.order_intake_m is not None
                        else None
                    ),
                    sales_m=(
                        candidate.sales_m * 100
                        if candidate.sales_m is not None
                        else None
                    ),
                    method=f"UNIT_SCALED_{candidate.method}",
                    confidence=("B" if candidate.confidence == "C" else candidate.confidence),
                    score=candidate.score,
                    page=candidate.page,
                    evidence=f"[page unit=億円] {candidate.evidence}",
                    period_text=candidate.period_text,
                    unit="億円",
                    yoy=candidate.yoy,
                    scope=candidate.scope,
                )
            )
    outputs.extend(scaled)
    return outputs


def _identity_bonus(candidate: Candidate) -> float:
    """Reward prior/current/change/yoy rows that arithmetically reconcile."""
    if candidate.prior_backlog_m in (None, 0):
        return 0.0
    evidence = norm(candidate.evidence)
    numbers = []
    for token in NUMBER.findall(evidence):
        try:
            numbers.append(float(token.replace(",", "")))
        except ValueError:
            pass
    difference = abs(candidate.current_backlog_m - candidate.prior_backlog_m)
    tolerance = max(1.0, difference * 0.02)
    bonus = 0.0
    if any(abs(abs(value) - difference) <= tolerance for value in numbers):
        bonus += 90
    implied = candidate.current_backlog_m / candidate.prior_backlog_m - 1
    percentages = [float(value) / 100 for value in re.findall(r"([-+]?\d+(?:\.\d+)?)\s*%", evidence)]
    if any(abs(value - implied) <= 0.02 for value in percentages):
        bonus += 55
    return bonus


def _selector_score(
    candidate: Candidate,
    all_candidates: list[Candidate],
    support: dict[int, int],
) -> float:
    base_method = candidate.method.removeprefix("UNIT_SCALED_")
    method_base = {
        "DIRECT_BACKLOG_SENTENCE": 760,
        "TOTAL_THREE_PERIOD_ORDER_BACKLOG": 750,
        "TOTAL_TWO_PERIOD_ORDER_SALES_BACKLOG": 735,
        "TOTAL_TWO_PERIOD_ORDER_BACKLOG": 720,
        "BACKLOG_SECTION_TOTAL_WITH_RATIOS": 710,
        "VERTICAL_TOTAL_ORDER_BACKLOG": 700,
        "TOTAL_BACKLOG_COLUMNS": 650,
        "BACKLOG_TIME_SERIES_LAST": 650,
        "CONSOLIDATED_BACKLOG_SERIES_LAST": 660,
        "ROW_PERIOD_PAIR": 640,
        "ROW_CURRENT": 560,
        "NARRATIVE_FINAL_BALANCE": 540,
        "NARRATIVE_BACKLOG": 510,
        "SEPARATED_TEXT_PAIR": 180,
    }
    score = method_base.get(base_method, 260)
    evidence = norm(candidate.evidence + " " + candidate.period_text)

    if candidate.scope == "TOTAL":
        score += 145
    elif candidate.scope == "NARRATIVE":
        score += 65
    elif candidate.scope == "SINGLE_SEGMENT":
        score -= 190
    elif candidate.scope == "METRIC_ROW":
        score += 15

    if TOTAL_WORD.search(evidence):
        score += 85
    if BACKLOG_LABEL.search(evidence):
        score += 60
    if FORECAST_WORD.search(evidence):
        score -= 520
    if SEGMENT_WORD.search(evidence) and not TOTAL_WORD.search(evidence):
        score -= 120
    if SALES_ONLY.search(evidence) and not BACKLOG_LABEL.search(evidence):
        score -= 450

    # Period direction is a hard semantic distinction. A previous-period total
    # must never beat the otherwise identical current-period total.
    has_current = bool(CURRENT_PERIOD.search(evidence) or "__CURRENT_PERIOD__" in evidence)
    has_prior = bool(PRIOR_PERIOD.search(evidence))
    if has_current:
        score += 190
    if has_prior and not has_current:
        score -= 190
    if "__QUARTER_CONTEXT__" in evidence:
        score += 80
    if "__ANNUAL_CONTEXT__" in evidence:
        score -= 80

    if base_method in {
        "BACKLOG_TIME_SERIES_LAST",
        "CONSOLIDATED_BACKLOG_SERIES_LAST",
    }:
        # A time-series row is a good total fallback. It is especially valuable
        # when the final value is corroborated by component rows.
        score += 50

    if base_method == "ROW_PERIOD_PAIR":
        if re.match(r"^(?:[^:]{0,30}:\s*)?受\s*注\s*残", evidence):
            score += 100
        if _numeric_count(evidence) <= 6:
            score += 45
        score += _identity_bonus(candidate)

    score += 100 * max(
        0, support.get(round(candidate.current_backlog_m * 10), 1) - 1
    )

    credible = [
        c.current_backlog_m
        for c in all_candidates
        if c.current_backlog_m > 0
        and c.method.removeprefix("UNIT_SCALED_") != "SEPARATED_TEXT_PAIR"
        and c.scope != "SINGLE_SEGMENT"
        and not FORECAST_WORD.search(norm(c.evidence + " " + c.period_text))
    ]
    if credible:
        max_value = max(credible)
        if abs(candidate.current_backlog_m - max_value) <= max(1.0, max_value * 0.005):
            score += 115
        elif candidate.current_backlog_m < max_value * 0.03 and candidate.scope != "TOTAL":
            score -= 230

    if candidate.yoy is not None and candidate.prior_backlog_m not in (None, 0):
        implied = candidate.current_backlog_m / candidate.prior_backlog_m - 1
        if abs(implied - candidate.yoy) <= 0.02:
            score += 35
        else:
            score -= 90

    if candidate.confidence == "A":
        score += 35
    elif candidate.confidence == "C":
        score -= 85
    return score


def _support_counts(candidates: list[Candidate]) -> dict[int, int]:
    groups: dict[int, set[str]] = defaultdict(set)
    for candidate in candidates:
        key = round(candidate.current_backlog_m * 10)
        groups[key].add(candidate.method.removeprefix("UNIT_SCALED_"))
    return {key: len(methods) for key, methods in groups.items()}


def extract_candidates(pdf_bytes: bytes) -> tuple[list[Candidate], dict[str, Any]]:
    candidates, audit = v2.extract_candidates(pdf_bytes)
    candidates.extend(_sentence_candidates(pdf_bytes))
    candidates = _page_context_and_scaled_candidates(pdf_bytes, candidates)

    dedup: dict[tuple[int, int, int, str], Candidate] = {}
    for candidate in candidates:
        key = (
            round(candidate.current_backlog_m * 1000),
            round(
                (
                    candidate.prior_backlog_m
                    if candidate.prior_backlog_m is not None
                    else -1
                )
                * 1000
            ),
            candidate.page,
            candidate.scope,
        )
        existing = dedup.get(key)
        if existing is None or CONFIDENCE_RANK.get(
            candidate.confidence, 0
        ) > CONFIDENCE_RANK.get(existing.confidence, 0):
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
        "v3_sentence_candidates": sum(
            c.method == "DIRECT_BACKLOG_SENTENCE" for c in candidates
        ),
        "v3_scaled_candidates": sum(
            c.method.startswith("UNIT_SCALED_") for c in candidates
        ),
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
    if len(ranked) > 1:
        first_score = _selector_score(ranked[0], candidates, support)
        second_score = _selector_score(ranked[1], candidates, support)
        ratio = max(
            ranked[0].current_backlog_m, ranked[1].current_backlog_m
        ) / max(
            min(ranked[0].current_backlog_m, ranked[1].current_backlog_m),
            1e-9,
        )
        # Ambiguity is safer than a wrong auto-selection, but only when there is
        # no semantic evidence separating current/total from prior/segment.
        if first_score - second_score < 8 and ratio > 1.20:
            return None
    return ranked[0]
