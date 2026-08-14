from __future__ import annotations

import re
from typing import Any

import fitz

import backlog_structured_parser as legacy
import backlog_structured_parser_v3 as v3

Candidate = legacy.Candidate
norm = legacy.norm
BACKLOG_RE = legacy.BACKLOG_RE
TOTAL_RE = legacy.TOTAL_RE

CURRENT_PERIOD_RE = re.compile(r"当第|当期|当四半期|今回|現在|直近")
PRIOR_PERIOD_RE = re.compile(r"前第|前期|前年|前四半期|前連結会計年度")
FORECAST_RE = re.compile(r"業績予想|通期予想|計画|予想値|見通し|次期")
START_BALANCE_RE = re.compile(r"期首受注残|期首繰越|前期末受注残")
SEGMENT_AFTER_LABEL_RE = re.compile(
    r"受\s*注\s*残(?:高)?\s*[|｜:]\s*(?:プロジェクト|[A-Za-zＡ-Ｚａ-ｚ]+事業|[^|｜]{1,20}事業|機器|装置|工事)"
)
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _candidate(
    current: float,
    prior: float | None,
    page: int,
    evidence: str,
    unit: str | None,
) -> Candidate:
    return Candidate(
        current_backlog_m=current,
        prior_backlog_m=prior,
        order_intake_m=None,
        sales_m=None,
        method="CONSOLIDATED_QUARTERLY_CHART_BACKLOG",
        confidence="A",
        score=0,
        page=page,
        evidence=evidence,
        period_text="四半期推移 連結",
        unit=unit,
        yoy=(current / prior - 1) if prior not in (None, 0) else None,
        scope="TOTAL",
    )


def _page_multiplier(text: str) -> tuple[float | None, str | None]:
    multiplier, unit = legacy.unit_multiplier(text)
    if multiplier is not None:
        return multiplier, unit
    compact = norm(text)
    if re.search(r"\[\s*億円\s*\]", compact):
        return 100.0, "億円"
    if re.search(r"\[\s*百万円\s*\]", compact):
        return 1.0, "百万円"
    if re.search(r"\[\s*千円\s*\]", compact):
        return 0.001, "千円"
    return None, None


def _row_numbers(row: list[str], multiplier: float | None) -> list[float]:
    outputs: list[float] = []
    for cell in row:
        raw = norm(cell)
        if not raw or "%" in raw:
            continue
        value = legacy.parse_money(raw, multiplier)
        if value is None:
            continue
        digits = raw.replace(",", "").replace(".", "").replace("-", "")
        if digits.isdigit() and len(digits) == 4 and 1900 <= abs(value) <= 2100:
            continue
        outputs.append(value)
    return outputs


def _chart_candidates(pdf_bytes: bytes) -> list[Candidate]:
    outputs: list[Candidate] = []
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_number, page in enumerate(document, start=1):
        page_text = page.get_text("text")
        compact = norm(page_text)
        if not BACKLOG_RE.search(compact) or "四半期推移" not in compact:
            continue
        multiplier, unit = _page_multiplier(compact)
        if multiplier is None:
            continue
        for rows, strategy, _bbox in legacy.table_rows(page):
            table_text = "\n".join(" | ".join(row) for row in rows)
            if not BACKLOG_RE.search(table_text) and not BACKLOG_RE.search(compact):
                continue
            for row in rows:
                labels = [norm(cell) for cell in row]
                joined = "".join(labels[:4]).replace(" ", "")
                if "連結" not in joined:
                    continue
                values = _row_numbers(row, multiplier)
                if len(values) < 4:
                    continue
                if len(values) >= 8 and abs(values[3] - values[-1]) <= max(0.5, abs(values[-1]) * 0.002):
                    current = values[-1]
                    prior = values[2]
                else:
                    current = values[-1]
                    prior = values[-2] if len(values) >= 2 else None
                outputs.append(
                    _candidate(
                        current,
                        prior,
                        page_number,
                        f"{strategy}: " + " | ".join(row),
                        unit,
                    )
                )
    return outputs


def _support_counts(candidates: list[Candidate]) -> dict[int, int]:
    groups: dict[int, set[str]] = {}
    for candidate in candidates:
        key = round(candidate.current_backlog_m * 10)
        groups.setdefault(key, set()).add(candidate.method)
    return {key: len(methods) for key, methods in groups.items()}


def _credible_max(candidates: list[Candidate]) -> float:
    values = []
    for candidate in candidates:
        text = norm(candidate.evidence + " " + candidate.period_text)
        if candidate.current_backlog_m <= 0:
            continue
        if candidate.method == "SEPARATED_TEXT_PAIR":
            continue
        if candidate.scope == "SINGLE_SEGMENT":
            continue
        if FORECAST_RE.search(text) or START_BALANCE_RE.search(text):
            continue
        if not BACKLOG_RE.search(text) and "BACKLOG" not in candidate.method:
            continue
        values.append(candidate.current_backlog_m)
    return max(values) if values else 0.0


def selector_score(candidate: Candidate, candidates: list[Candidate]) -> float:
    support = _support_counts(candidates)
    score = v3._selector_score(candidate, candidates, support)
    text = norm(candidate.evidence + " " + candidate.period_text)

    if candidate.method == "CONSOLIDATED_QUARTERLY_CHART_BACKLOG":
        score += 850

    if CURRENT_PERIOD_RE.search(candidate.period_text):
        score += 330
    if PRIOR_PERIOD_RE.search(candidate.period_text) and not CURRENT_PERIOD_RE.search(candidate.period_text):
        score -= 330

    if FORECAST_RE.search(text):
        score -= 220
    if START_BALANCE_RE.search(text):
        score -= 260

    # The consolidated backlog cannot be smaller than a non-negative segment.
    # This bonus resolves documents that show the total first and segment pages later.
    credible_max = _credible_max(candidates)
    if credible_max > 0:
        if abs(candidate.current_backlog_m - credible_max) <= max(0.5, credible_max * 0.005):
            score += 250
        elif candidate.current_backlog_m < credible_max * 0.25 and candidate.scope != "TOTAL":
            score -= 180

    if candidate.method == "ROW_PERIOD_PAIR":
        normalized = text.lstrip(" |｜:")
        if re.match(r"受\s*注\s*残", normalized) and ("%" in text or "+" in text or "増減" in text):
            score += 170

    if candidate.method in {"BACKLOG_TIME_SERIES_LAST", "CONSOLIDATED_BACKLOG_SERIES_LAST"}:
        if SEGMENT_AFTER_LABEL_RE.search(text):
            score -= 420
        else:
            score += 100

    if candidate.method in {
        "TOTAL_THREE_PERIOD_ORDER_BACKLOG",
        "TOTAL_TWO_PERIOD_ORDER_SALES_BACKLOG",
    }:
        score += 350
        if candidate.order_intake_m not in (None, 0):
            for other in candidates:
                if (
                    other is not candidate
                    and other.scope == "TOTAL"
                    and other.current_backlog_m > 0
                    and abs(other.current_backlog_m - candidate.order_intake_m)
                    <= max(0.5, abs(candidate.order_intake_m) * 0.01)
                ):
                    score -= 650
                    break

    # Tables with three period groups often expose the year-end reference in the
    # final pair. The dedicated period parser is preferred over a last-column guess.
    if candidate.method == "TOTAL_BACKLOG_COLUMNS" and len(NUMBER_RE.findall(candidate.evidence)) >= 6:
        if any(
            other.method == "TOTAL_THREE_PERIOD_ORDER_BACKLOG"
            and other.page == candidate.page
            for other in candidates
        ):
            score -= 180

    # Independent extraction routes confirming the same value are retained as
    # evidence rather than treated as separate competing signals.
    score += 80 * max(0, support.get(round(candidate.current_backlog_m * 10), 1) - 1)
    return score


def _deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    outputs: dict[tuple[int, int, int], Candidate] = {}
    for candidate in candidates:
        key = (
            round(candidate.current_backlog_m * 1000),
            round((candidate.prior_backlog_m if candidate.prior_backlog_m is not None else -1) * 1000),
            candidate.page,
        )
        existing = outputs.get(key)
        if existing is None or selector_score(candidate, candidates) > selector_score(existing, candidates):
            outputs[key] = candidate
    return list(outputs.values())


def extract_candidates(pdf_bytes: bytes) -> tuple[list[Candidate], dict[str, Any]]:
    candidates, audit = v3.extract_candidates(pdf_bytes)
    chart = _chart_candidates(pdf_bytes)
    candidates.extend(chart)
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
        "v4_chart_candidates": len(chart),
        "v4_candidate_count": len(candidates),
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
