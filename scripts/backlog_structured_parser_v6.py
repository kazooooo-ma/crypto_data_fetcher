from __future__ import annotations

import re
from typing import Any

import fitz

import backlog_structured_parser as legacy
import backlog_structured_parser_v5 as v5

Candidate = legacy.Candidate
norm = legacy.norm

FORECAST_PAGE = re.compile(r"業績予想|通期予想|修正予想|計画値|会社予想|見通し", re.I)
SEGMENT_PAGE = re.compile(
    r"(?:ロボットソリューション|マシンツール|半導体・液晶関連|研究機関・大学関連|"
    r"セグメント別|事業別|部門別|品目別).{0,40}(?:業績|実績|受注)",
    re.I,
)
CONSOLIDATED_PAGE = re.compile(r"連結|全社|当社グループ|合計", re.I)
EXPLICIT_BACKLOG_AMOUNT = re.compile(
    r"受\s*注\s*残(?:高)?[^。\n]{0,120}?"
    r"\d[\d,]*(?:\.\d+)?\s*(?:兆円|億円|百万円|千円|万円)",
    re.I,
)
TOTAL_ROW = re.compile(r"^(?:[^:]{0,40}:\s*)?合\s*計(?:\s*\||\s)", re.I)
SEGMENT_NARRATIVE = re.compile(
    r"情報システム|電子機器|接合機器|赤外線機器|"
    r"セグメント別|事業別|部門別|品目別|事業につきまして",
    re.I,
)
EXPLICIT_GROUP_BACKLOG = re.compile(
    r"(?:当社グループ|連結|全社)[^。\n]{0,80}?受\s*注\s*残|"
    r"受\s*注\s*残(?:高)?\s*(?:合計|総額)",
    re.I,
)
TOTAL_RESULT_SENTENCE = re.compile(
    r"(?:これにより|この結果)[^。\n]{0,80}?受\s*注\s*残(?:高)?は",
    re.I,
)
NON_BACKLOG_ROW_LABEL = re.compile(r"受\s*注\s*高|売\s*上\s*高|売上収益|営業利益", re.I)
NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _page_markers(pdf_bytes: bytes) -> dict[int, str]:
    markers: dict[int, str] = {}
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_number, page in enumerate(document, start=1):
        text = norm(page.get_text("text"))
        flags: list[str] = []
        if FORECAST_PAGE.search(text):
            flags.append("__FORECAST_PAGE__")
        if SEGMENT_PAGE.search(text):
            flags.append("__SEGMENT_PAGE__")
        if CONSOLIDATED_PAGE.search(text) and legacy.BACKLOG_RE.search(text):
            flags.append("__CONSOLIDATED_PAGE__")
        markers[page_number] = " ".join(flags)
    return markers


def _numeric_count(candidate: Candidate) -> int:
    return len(NUMBER.findall(norm(candidate.evidence)))


def _score(candidate: Candidate, candidates: list[Candidate]) -> float:
    score = v5.selector_score(candidate, candidates)
    text = norm(candidate.evidence + " " + candidate.period_text)
    evidence = norm(candidate.evidence)
    method = candidate.method
    number_count = _numeric_count(candidate)

    # This generic route repeatedly picked order, sales or composition ratios
    # instead of the backlog column. Keep it for diagnostics, never for auto
    # selection.
    if method == "CURRENT_PERIOD_FLOW_TOTAL":
        return score - 5000

    # A current-change row is reliable only when it is the simple form
    # backlog | prior | current | change | yoy. Matrix rows and forecast tables
    # are routed to another direct/narrative candidate or manual review.
    if method == "CURRENT_CHANGE_BACKLOG_ROW":
        if number_count <= 5:
            score += 500
        else:
            score -= 1400
        if "__FORECAST_PAGE__" in text:
            score -= 1600
        if "__SEGMENT_PAGE__" in text and "合計" not in text:
            score -= 1100

    if method in {
        "TOTAL_ORDER_BACKLOG_YOY_ROW",
        "BACKLOG_SECTION_TOTAL_WITH_RATIOS",
        "TOTAL_BACKLOG_COLUMNS",
        "CURRENT_PERIOD_FLOW_TOTAL_CORRECTED",
    }:
        score += 950

    # Short current/prior total rows are authoritative. Long total rows are
    # usually multi-quarter time-series that the generic parser has mapped to
    # the wrong pair of columns (IDEC regression).
    if method == "TOTAL_TWO_PERIOD_ORDER_BACKLOG":
        if NON_BACKLOG_ROW_LABEL.search(evidence) and not legacy.BACKLOG_RE.search(evidence):
            score -= 1900
        elif TOTAL_ROW.search(evidence) and 4 <= number_count <= 8:
            score += 1800
        elif number_count > 8:
            score -= 1700
        else:
            score -= 300
    elif method == "TOTAL_THREE_PERIOD_ORDER_BACKLOG":
        if TOTAL_ROW.search(evidence) and 4 <= number_count <= 8:
            score += 1800
        elif number_count > 8:
            score -= 1800
        else:
            score -= 400
    elif method == "TOTAL_TWO_PERIOD_ORDER_SALES_BACKLOG":
        if NON_BACKLOG_ROW_LABEL.search(evidence) and not legacy.BACKLOG_RE.search(evidence):
            score -= 1900
        elif TOTAL_ROW.search(evidence) and 5 <= number_count <= 9:
            score += 1500
        elif number_count > 9:
            score -= 1500
        else:
            score -= 300

    if method in {"DIRECT_BACKLOG_SENTENCE", "NARRATIVE_FINAL_BALANCE"}:
        if EXPLICIT_BACKLOG_AMOUNT.search(text):
            score += 850
        if "__CONSOLIDATED_PAGE__" in text:
            score += 160
        if TOTAL_RESULT_SENTENCE.search(evidence):
            score += 900
        # A segment narrative may be accurate for that segment but is not the
        # company-wide backlog. Prefer a short total table when one exists
        # (Japan Avionics regression).
        elif SEGMENT_NARRATIVE.search(evidence) and not EXPLICIT_GROUP_BACKLOG.search(evidence):
            score -= 1400

    # Company-wide historical series is a valid fallback when segment pages are
    # separately disclosed, as in FUJI. Segment and forecast series are not.
    if method in {"BACKLOG_TIME_SERIES_LAST", "CONSOLIDATED_BACKLOG_SERIES_LAST"}:
        if "__CONSOLIDATED_PAGE__" in text and "__SEGMENT_PAGE__" not in text:
            score += 900
        if "__SEGMENT_PAGE__" in text:
            score -= 900
        if "__FORECAST_PAGE__" in text:
            score -= 500

    if method in {"CHART_TREND", "SEGMENT_SUM_TABLE"}:
        score -= 900

    if candidate.scope == "TOTAL":
        score += 180
    elif candidate.scope in {"SINGLE_SEGMENT", "TOTAL_SUM"}:
        score -= 600

    # Multiple independent extraction routes that reproduce the same value are
    # stronger than a unique candidate.
    same_value_methods = {
        other.method
        for other in candidates
        if abs(other.current_backlog_m - candidate.current_backlog_m)
        <= max(0.5, abs(candidate.current_backlog_m) * 0.002)
    }
    score += 130 * max(0, len(same_value_methods) - 1)
    return score


def extract_candidates(pdf_bytes: bytes) -> tuple[list[Candidate], dict[str, Any]]:
    candidates, audit = v5.extract_candidates(pdf_bytes)
    markers = _page_markers(pdf_bytes)
    for candidate in candidates:
        candidate.period_text = norm(
            f"{candidate.period_text} {markers.get(candidate.page, '')}"
        )
    candidates.sort(
        key=lambda candidate: (
            _score(candidate, candidates),
            candidate.scope == "TOTAL",
            candidate.confidence == "A",
            candidate.current_backlog_m,
        ),
        reverse=True,
    )
    return candidates, {
        **audit,
        "v6_candidate_count": len(candidates),
    }


def select_candidate(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda candidate: _score(candidate, candidates), reverse=True)
    first = ranked[0]
    if len(ranked) > 1:
        first_score = _score(ranked[0], candidates)
        second_score = _score(ranked[1], candidates)
        ratio = max(ranked[0].current_backlog_m, ranked[1].current_backlog_m) / max(
            min(ranked[0].current_backlog_m, ranked[1].current_backlog_m), 1e-9
        )
        # Route unresolved conflicts to manual review rather than silently
        # introducing a false history observation.
        if first_score - second_score < 20 and ratio > 1.15:
            return None
    return first
