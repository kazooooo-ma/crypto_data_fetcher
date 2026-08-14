from __future__ import annotations

import re
from typing import Any

import fitz

import backlog_structured_parser as legacy

Candidate = legacy.Candidate
BACKLOG_RE = legacy.BACKLOG_RE
ORDER_RE = legacy.ORDER_RE
SALES_RE = legacy.SALES_RE
TOTAL_RE = legacy.TOTAL_RE
norm = legacy.norm
parse_money = legacy.parse_money
parse_percent = legacy.parse_percent
unit_multiplier = legacy.unit_multiplier
mixed_jpy_values = legacy.mixed_jpy_values

FORECAST_RE = re.compile(
    r"業績予想|通期予想|連結業績予想|業績見通し|予想値|計画値|修正予想|次期予想"
)
START_BALANCE_RE = re.compile(r"期首受注残|期首繰越|前期末受注残")
MONEY_EXPR = (
    r"(?:[0-9][0-9,]*(?:\.[0-9]+)?\s*億\s*"
    r"[0-9][0-9,]*(?:\.[0-9]+)?\s*百万円|"
    r"[0-9][0-9,]*(?:\.[0-9]+)?\s*億\s*"
    r"[0-9][0-9,]*(?:\.[0-9]+)?\s*万円|"
    r"[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:兆円|億円|百万円|千円|万円))"
)


def _money(expression: str) -> float | None:
    values = mixed_jpy_values(expression)
    return values[0][0] if values else None


def _make(
    current: float,
    prior: float | None,
    order: float | None,
    sales: float | None,
    method: str,
    confidence: str,
    score: float,
    page: int,
    evidence: str,
    period_text: str = "",
    unit: str | None = None,
    yoy: float | None = None,
    scope: str = "TOTAL",
) -> Candidate:
    return Candidate(
        current,
        prior,
        order,
        sales,
        method,
        confidence,
        score,
        page,
        evidence,
        period_text,
        unit,
        yoy,
        scope,
    )


def _precise_narratives(page_number: int, text: str) -> list[Candidate]:
    compact = norm(text)
    outputs: list[Candidate] = []
    for match in BACKLOG_RE.finditer(compact):
        sentence = compact[match.start() : match.end() + 550]
        after = compact[match.end() : match.end() + 500]

        final_match = re.search(
            rf"(?:増|減)(?:加|少)?(?:額)?[^。]{{0,45}}?(?:の|して|となり|となった結果)\s*({MONEY_EXPR})",
            after,
        )
        direct_match = re.search(
            rf"^(?:は|が|を|、|:|：|\s)*({MONEY_EXPR})",
            after,
        )
        become_matches = list(
            re.finditer(
                rf"({MONEY_EXPR})[^。]{{0,35}}?(?:となりました|となります|に増加|に減少|であります|でした)",
                after,
            )
        )
        chosen = None
        if final_match:
            chosen = final_match.group(1)
        elif direct_match:
            chosen = direct_match.group(1)
        elif become_matches:
            chosen = become_matches[-1].group(1)
        if not chosen:
            continue
        current = _money(chosen)
        if current is None or current <= 0:
            continue
        yoy = parse_percent(after[:260])
        prior = current / (1 + yoy) if yoy is not None and yoy > -0.999 else None
        scope = "TOTAL" if re.search(r"連結|全社|合計|総額", sentence) else "NARRATIVE"
        score = 225 if final_match else 215
        outputs.append(
            _make(
                current,
                prior,
                None,
                None,
                "NARRATIVE_FINAL_BALANCE",
                "A" if scope == "TOTAL" else "B",
                score,
                page_number,
                sentence,
                "",
                None,
                yoy,
                scope,
            )
        )
    return outputs


def _numeric_row_values(row: list[str], multiplier: float | None) -> list[float]:
    values: list[float] = []
    for cell in row:
        raw = norm(cell)
        if not raw or "%" in raw or re.search(r"前年同期比|構成比|増減率", raw):
            continue
        value = parse_money(raw, multiplier)
        if value is None:
            continue
        normalized_digits = raw.replace(",", "").replace(".", "").replace("-", "")
        if 1900 <= abs(value) <= 2100 and normalized_digits.isdigit() and len(normalized_digits) == 4:
            continue
        values.append(value)
    return values


def _period_group_tables(
    page_number: int, page: fitz.Page, page_text: str
) -> list[Candidate]:
    outputs: list[Candidate] = []
    for rows, strategy, _bbox in legacy.table_rows(page):
        joined = "\n".join(" | ".join(row) for row in rows)
        multiplier, unit = unit_multiplier(joined + "\n" + page_text[:4000])
        order_count = len(ORDER_RE.findall(joined))
        backlog_count = len(BACKLOG_RE.findall(joined))
        sales_count = len(SALES_RE.findall(joined))
        for row in rows:
            labels = [norm(cell) for cell in row]
            if not any(TOTAL_RE.match(cell.replace(" ", "")) for cell in labels[:3] if cell):
                continue
            values = _numeric_row_values(row, multiplier)
            if len(values) < 4 or backlog_count < 2:
                continue

            prior = current = order = sales = None
            method = ""
            if len(values) >= 6 and order_count >= 2 and sales_count >= 2:
                prior, current = values[2], values[5]
                order, sales = values[3], values[4]
                method = "TOTAL_TWO_PERIOD_ORDER_SALES_BACKLOG"
            elif len(values) >= 6 and order_count >= 3 and backlog_count >= 3:
                prior, current = values[1], values[3]
                order = values[2]
                method = "TOTAL_THREE_PERIOD_ORDER_BACKLOG"
            elif len(values) >= 4 and order_count >= 2 and backlog_count >= 2:
                prior, current = values[1], values[3]
                order = values[2]
                method = "TOTAL_TWO_PERIOD_ORDER_BACKLOG"
            if current is None or current <= 0:
                continue
            evidence = f"{strategy}: " + " | ".join(row)
            outputs.append(
                _make(
                    current,
                    prior,
                    order,
                    sales,
                    method,
                    "A",
                    255,
                    page_number,
                    evidence,
                    "",
                    unit,
                    None,
                    "TOTAL",
                )
            )
    return outputs


def _series_tables(
    page_number: int, page: fitz.Page, page_text: str
) -> list[Candidate]:
    outputs: list[Candidate] = []
    for rows, strategy, _bbox in legacy.table_rows(page):
        joined = "\n".join(" | ".join(row) for row in rows)
        multiplier, unit = unit_multiplier(joined + "\n" + page_text[:4000])
        table_has_backlog = bool(BACKLOG_RE.search(joined))
        table_has_order = bool(ORDER_RE.search(joined))
        for row in rows:
            row_text = " | ".join(row)
            values = _numeric_row_values(row, multiplier)
            if BACKLOG_RE.search(row_text) and len(values) >= 5:
                outputs.append(
                    _make(
                        values[-1],
                        values[-2],
                        None,
                        None,
                        "BACKLOG_TIME_SERIES_LAST",
                        "A",
                        245,
                        page_number,
                        f"{strategy}: {row_text}",
                        "",
                        unit,
                        None,
                        "METRIC_ROW",
                    )
                )
            elif (
                table_has_backlog
                and not table_has_order
                and len(values) >= 4
                and re.search(r"連結|全社|total", norm(" ".join(row[:2])), re.I)
            ):
                outputs.append(
                    _make(
                        values[-1],
                        values[-2],
                        None,
                        None,
                        "CONSOLIDATED_BACKLOG_SERIES_LAST",
                        "A",
                        242,
                        page_number,
                        f"{strategy}: {row_text}",
                        "",
                        unit,
                        None,
                        "TOTAL",
                    )
                )
    return outputs


def _ratio_section_totals(page_number: int, text: str) -> list[Candidate]:
    compact = norm(text)
    outputs: list[Candidate] = []
    for match in BACKLOG_RE.finditer(compact):
        section = compact[match.start() : match.end() + 2400]
        total = re.search(
            r"合\s*計\s+([0-9][0-9,]*(?:\.[0-9]+)?)\s+100(?:\.0+)?\s+"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+100(?:\.0+)?\s+"
            r"[-+]?[0-9][0-9,]*(?:\.[0-9]+)?",
            section,
        )
        if total:
            multiplier, unit = unit_multiplier(section)
            prior = parse_money(total.group(1), multiplier)
            current = parse_money(total.group(2), multiplier)
            if current is not None and current > 0:
                outputs.append(
                    _make(
                        current,
                        prior,
                        None,
                        None,
                        "BACKLOG_SECTION_TOTAL_WITH_RATIOS",
                        "A",
                        250,
                        page_number,
                        section[:900],
                        "",
                        unit,
                        None,
                        "TOTAL",
                    )
                )
    return outputs


def _vertical_grid_candidates(page_number: int, text: str) -> list[Candidate]:
    compact = norm(text)
    outputs: list[Candidate] = []
    if re.search(r"1\.\s*受注高", compact) and re.search(r"2\.\s*受注残高", compact):
        boundary = re.search(r"3\.\s*補足情報|3\.\s*その他", compact)
        prefix = compact[: boundary.start()] if boundary else compact
        totals = list(
            re.finditer(
                r"合\s*計\s+([0-9][0-9,]*(?:\.[0-9]+)?)\s+"
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+"
                r"([0-9]+(?:\.[0-9]+)?)",
                prefix,
            )
        )
        if totals:
            multiplier, unit = unit_multiplier(compact)
            chosen = totals[-1]
            order = parse_money(chosen.group(1), multiplier)
            current = parse_money(chosen.group(2), multiplier)
            yoy = float(chosen.group(3)) / 100.0
            prior = current / (1 + yoy) if current and yoy > -0.999 else None
            if current is not None and current > 0:
                outputs.append(
                    _make(
                        current,
                        prior,
                        order,
                        None,
                        "VERTICAL_TOTAL_ORDER_BACKLOG",
                        "B",
                        238,
                        page_number,
                        prefix[max(0, chosen.start() - 250) : chosen.end() + 250],
                        "",
                        unit,
                        yoy,
                        "TOTAL",
                    )
                )
    return outputs


def _page_adjustments(candidates: list[Candidate], page_texts: dict[int, str]) -> None:
    method_bonus = {
        "TOTAL_BACKLOG_COLUMNS": 45,
        "ROW_PERIOD_PAIR": 20,
        "ROW_CURRENT": 5,
        "NARRATIVE_BACKLOG": 20,
        "SEPARATED_TEXT_PAIR": 0,
    }
    for candidate in candidates:
        candidate.score += method_bonus.get(candidate.method, 0)
        text = norm(
            candidate.evidence
            + " "
            + candidate.period_text
            + " "
            + page_texts.get(candidate.page, "")[:2000]
        )
        if FORECAST_RE.search(text):
            candidate.score -= 120
        if START_BALANCE_RE.search(candidate.evidence):
            candidate.score -= 120
        if candidate.scope == "TOTAL":
            candidate.score += 25
        elif candidate.scope in {"SINGLE_SEGMENT", "METRIC_ROW", "NARRATIVE"}:
            candidate.score -= 10
        if candidate.confidence == "A":
            candidate.score += 20
        elif candidate.confidence == "B":
            candidate.score += 5
        else:
            candidate.score -= 20
        if (
            candidate.method == "ROW_PERIOD_PAIR"
            and candidate.prior_backlog_m
            and candidate.current_backlog_m < 200
            and candidate.prior_backlog_m > 1000
        ):
            candidate.score -= 160


def _deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    selected: dict[tuple[int, int, int], Candidate] = {}
    for candidate in candidates:
        key = (
            round(candidate.current_backlog_m * 1000),
            round((candidate.prior_backlog_m or -1) * 1000),
            candidate.page,
        )
        existing = selected.get(key)
        if existing is None or candidate.score > existing.score:
            selected[key] = candidate
    return sorted(
        selected.values(),
        key=lambda candidate: (
            candidate.score,
            candidate.scope == "TOTAL",
            candidate.confidence == "A",
            candidate.current_backlog_m,
            -candidate.page,
        ),
        reverse=True,
    )


def extract_candidates(pdf_bytes: bytes) -> tuple[list[Candidate], dict[str, Any]]:
    legacy_candidates, legacy_audit = legacy.extract_candidates(pdf_bytes)
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_texts = {
        page_number: page.get_text("text")
        for page_number, page in enumerate(document, start=1)
    }
    _page_adjustments(legacy_candidates, page_texts)

    added: list[Candidate] = []
    for page_number, page in enumerate(document, start=1):
        text = page_texts[page_number]
        if not BACKLOG_RE.search(norm(text)):
            continue
        added.extend(_precise_narratives(page_number, text))
        added.extend(_period_group_tables(page_number, page, text))
        added.extend(_series_tables(page_number, page, text))
        added.extend(_ratio_section_totals(page_number, text))
        added.extend(_vertical_grid_candidates(page_number, text))

    candidates = _deduplicate(legacy_candidates + added)
    audit = {
        **legacy_audit,
        "legacy_candidate_count": len(legacy_candidates),
        "added_candidate_count": len(added),
        "candidate_count": len(candidates),
    }
    return candidates, audit


def select_candidate(candidates: list[Candidate]) -> Candidate | None:
    return candidates[0] if candidates else None
