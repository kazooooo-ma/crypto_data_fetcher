from __future__ import annotations

import re
from typing import Any

import fitz

import backlog_structured_parser as legacy
import backlog_structured_parser_v4 as v4

Candidate = legacy.Candidate
norm = legacy.norm
BACKLOG_RE = legacy.BACKLOG_RE
TOTAL_RE = legacy.TOTAL_RE

DIRECT_AMOUNT_RE = re.compile(
    r"受\s*注\s*残(?:高)?[^。]{0,80}?"
    r"[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:兆円|億円|百万円|千円|万円)"
)
PERCENT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?\s*%|前年同期比|増減")
CURRENT_BLOCK_RE = re.compile(r"当第\s*[1-4]\s*四半期")
PRIOR_BLOCK_RE = re.compile(r"前第\s*[1-4]\s*四半期")
SEGMENT_PAGE_RE = re.compile(
    r"セグメント別|事業別|ロボットソリューション事業|"
    r"マシンツール事業|電子機器事業|情報システム事業"
)
SEGMENT_ROW_RE = re.compile(
    r"受\s*注\s*残(?:高)?\s*[|｜:]\s*"
    r"(?:プロジェクト|ポンプ|サービス|国内|海外|[^|｜]{1,24}事業)"
)


def _make(
    current: float,
    prior: float | None,
    method: str,
    page: int,
    evidence: str,
    *,
    unit: str | None = None,
    yoy: float | None = None,
    order: float | None = None,
    sales: float | None = None,
    period_text: str = "",
    scope: str = "TOTAL",
    confidence: str = "A",
) -> Candidate:
    return Candidate(
        current_backlog_m=current,
        prior_backlog_m=prior,
        order_intake_m=order,
        sales_m=sales,
        method=method,
        confidence=confidence,
        score=0,
        page=page,
        evidence=evidence,
        period_text=period_text,
        unit=unit,
        yoy=yoy,
        scope=scope,
    )


def _unit(text: str) -> tuple[float | None, str | None]:
    multiplier, unit = legacy.unit_multiplier(text)
    if multiplier is not None:
        return multiplier, unit
    compact = norm(text)
    if re.search(r"\[\s*億円\s*\]|単位\s*[:：]?\s*億円", compact):
        return 100.0, "億円"
    if re.search(r"\[\s*百万円\s*\]|単位\s*[:：]?\s*百万円", compact):
        return 1.0, "百万円"
    if re.search(r"\[\s*千円\s*\]|単位\s*[:：]?\s*千円", compact):
        return 0.001, "千円"
    return None, None


def _values(row: list[str], multiplier: float | None) -> list[float]:
    outputs: list[float] = []
    for cell in row:
        raw = norm(cell)
        if not raw or "%" in raw or re.search(r"前年同期比|構成比|増減率", raw):
            continue
        value = legacy.parse_money(raw, multiplier)
        if value is None:
            continue
        digits = raw.replace(",", "").replace(".", "").replace("-", "").replace("+", "")
        if digits.isdigit() and len(digits) == 4 and 1900 <= abs(value) <= 2100:
            continue
        outputs.append(value)
    return outputs


def _current_change_rows(page_number: int, page: fitz.Page, page_text: str) -> list[Candidate]:
    outputs: list[Candidate] = []
    page_context = norm(page_text)
    for rows, strategy, _bbox in legacy.table_rows(page):
        table_text = "\n".join(" | ".join(row) for row in rows)
        multiplier, unit = _unit(table_text + "\n" + page_text)
        for row in rows:
            labels = [norm(cell) for cell in row]
            row_text = " | ".join(labels)
            first_nonempty = next((cell for cell in labels if cell), "").replace(" ", "")
            if not re.match(r"(?:受注残高?|次期繰越受注残高|期末受注残高)", first_nonempty):
                continue
            values = _values(row, multiplier)
            # Valid layout is prior, current, optional absolute change. Rows
            # containing several segment/forecast values are not this layout.
            if len(values) < 2 or len(values) > 3 or not PERCENT_RE.search(row_text):
                continue
            prior = values[0]
            current = values[1]
            if prior <= 0 or current <= 0:
                continue
            segment = bool(
                SEGMENT_ROW_RE.search(row_text)
                or (
                    SEGMENT_PAGE_RE.search(page_context)
                    and not re.search(r"連結|全社|合計", row_text)
                )
            )
            yoy = current / prior - 1
            outputs.append(
                _make(
                    current,
                    prior,
                    "CURRENT_CHANGE_BACKLOG_ROW",
                    page_number,
                    f"{strategy}: {row_text}",
                    unit=unit,
                    yoy=yoy,
                    period_text="CURRENT_PERIOD_CHANGE_ROW",
                    scope="SINGLE_SEGMENT" if segment else "TOTAL",
                    confidence="B" if segment else "A",
                )
            )
    return outputs


def _order_backlog_yoy_totals(page_number: int, page_text: str) -> list[Candidate]:
    compact = norm(page_text)
    outputs: list[Candidate] = []
    header = re.search(
        r"受\s*注\s*高[^。]{0,120}?前年同期比[^。]{0,120}?"
        r"受\s*注\s*残(?:高)?[^。]{0,120}?前年同期比",
        compact,
    )
    if not header:
        return outputs
    multiplier, unit = _unit(compact)
    if multiplier is None:
        multiplier = 1.0
    section = compact[header.end() : header.end() + 5000]
    pattern = re.compile(
        r"合\s*計\s+"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+"
        r"([-+]?\d+(?:\.\d+)?)\s+"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+"
        r"([-+]?\d+(?:\.\d+)?)"
    )
    for match in pattern.finditer(section):
        order = float(match.group(1).replace(",", "")) * multiplier
        current = float(match.group(3).replace(",", "")) * multiplier
        yoy = float(match.group(4)) / 100.0
        prior = current / (1 + yoy) if yoy > -0.999 else None
        if current <= 0:
            continue
        outputs.append(
            _make(
                current,
                prior,
                "TOTAL_ORDER_BACKLOG_YOY_ROW",
                page_number,
                section[max(0, match.start() - 300) : match.end() + 200],
                unit=unit,
                yoy=yoy,
                order=order,
                period_text="CURRENT_PERIOD_TOTAL_WITH_YOY",
            )
        )
    return outputs


def _period_flow_totals(page_number: int, page_text: str) -> list[Candidate]:
    compact = norm(page_text)
    outputs: list[Candidate] = []
    multiplier, unit = _unit(compact)
    if multiplier is None:
        multiplier = 1.0

    markers = list(re.finditer(r"(?:前|当)第\s*[1-4]\s*四半期[^()]{0,80}?", compact))
    if not markers:
        return outputs
    blocks: list[tuple[str, list[float], str]] = []
    total_pattern = re.compile(
        r"合\s*計\s+"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)"
    )
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else min(len(compact), marker.start() + 3000)
        block = compact[marker.start() : end]
        matches = list(total_pattern.finditer(block))
        if not matches:
            continue
        match = matches[-1]
        values = [float(match.group(i).replace(",", "")) * multiplier for i in range(1, 5)]
        label = marker.group(0)
        blocks.append((label, values, block[max(0, match.start() - 400) : match.end() + 200]))

    prior_backlog: float | None = None
    for label, values, evidence in blocks:
        if PRIOR_BLOCK_RE.search(label):
            prior_backlog = values[3]
            continue
        if not CURRENT_BLOCK_RE.search(label):
            continue
        current = values[3]
        outputs.append(
            _make(
                current,
                prior_backlog,
                "CURRENT_PERIOD_FLOW_TOTAL",
                page_number,
                evidence,
                unit=unit,
                order=values[1],
                sales=values[2],
                yoy=(current / prior_backlog - 1) if prior_backlog not in (None, 0) else None,
                period_text=label,
            )
        )
    return outputs


def selector_score(candidate: Candidate, candidates: list[Candidate]) -> float:
    score = v4.selector_score(candidate, candidates)
    text = norm(candidate.evidence + " " + candidate.period_text)
    method_bonus = {
        "CURRENT_CHANGE_BACKLOG_ROW": 1800,
        "TOTAL_ORDER_BACKLOG_YOY_ROW": 1750,
        # Flow parsing is retained as a diagnostic candidate, but it cannot
        # outrank a directly identified backlog total unless it reconciles.
        "CURRENT_PERIOD_FLOW_TOTAL": 150,
        "TOTAL_TABLE": 750,
        "METRIC_ROW_TABLE": 0,
        "CHART_TREND": -250,
    }
    score += method_bonus.get(candidate.method, 0)

    if candidate.method == "NARRATIVE_BACKLOG" and not DIRECT_AMOUNT_RE.search(text):
        score -= 1500
    if candidate.method == "METRIC_ROW_TABLE" and PERCENT_RE.search(text):
        score -= 700
    if candidate.method == "CURRENT_CHANGE_BACKLOG_ROW" and candidate.scope == "SINGLE_SEGMENT":
        score -= 2600
    if candidate.method == "CURRENT_PERIOD_FLOW_TOTAL":
        metrics = [
            abs(float(value))
            for value in (candidate.order_intake_m, candidate.sales_m)
            if value not in (None, 0)
        ]
        if candidate.prior_backlog_m is None:
            score -= 1200
        if "構成比" in text:
            score -= 1800
        if metrics and candidate.current_backlog_m < max(metrics) * 0.20:
            score -= 2400
        if candidate.current_backlog_m <= 100.0 and any(value > 1000 for value in metrics):
            score -= 2500
    if candidate.method == "TOTAL_TABLE":
        if CURRENT_BLOCK_RE.search(candidate.period_text):
            score += 900
        if PRIOR_BLOCK_RE.search(candidate.period_text) and not CURRENT_BLOCK_RE.search(candidate.period_text):
            score -= 900
    return score


def _deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    selected: dict[tuple[int, int, int], Candidate] = {}
    for candidate in candidates:
        key = (
            round(candidate.current_backlog_m * 1000),
            round((candidate.prior_backlog_m if candidate.prior_backlog_m is not None else -1) * 1000),
            candidate.page,
        )
        existing = selected.get(key)
        if existing is None or selector_score(candidate, candidates) > selector_score(existing, candidates):
            selected[key] = candidate
    return list(selected.values())


def extract_candidates(pdf_bytes: bytes) -> tuple[list[Candidate], dict[str, Any]]:
    candidates, audit = v4.extract_candidates(pdf_bytes)
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    added: list[Candidate] = []
    for page_number, page in enumerate(document, start=1):
        text = page.get_text("text")
        if not BACKLOG_RE.search(norm(text)):
            continue
        added.extend(_current_change_rows(page_number, page, text))
        added.extend(_order_backlog_yoy_totals(page_number, text))
        added.extend(_period_flow_totals(page_number, text))
    candidates.extend(added)
    candidates = _deduplicate(candidates)
    candidates.sort(
        key=lambda candidate: (
            selector_score(candidate, candidates),
            candidate.scope == "TOTAL",
            candidate.confidence == "A",
            candidate.current_backlog_m,
        ),
        reverse=True,
    )
    return candidates, {
        **audit,
        "v5_added_candidates": len(added),
        "v5_candidate_count": len(candidates),
    }


def select_candidate(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda candidate: selector_score(candidate, candidates), reverse=True)
    best = ranked[0]
    if len(ranked) > 1:
        first = selector_score(ranked[0], candidates)
        second = selector_score(ranked[1], candidates)
        ratio = max(ranked[0].current_backlog_m, ranked[1].current_backlog_m) / max(
            min(ranked[0].current_backlog_m, ranked[1].current_backlog_m),
            1e-9,
        )
        if first - second < 10 and ratio > 1.20:
            return None
    return best
