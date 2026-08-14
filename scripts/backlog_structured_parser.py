from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, asdict
from typing import Any

import fitz

BACKLOG_RE = re.compile(r"受\s*注\s*残|繰\s*越[^\n]{0,12}受\s*注\s*残|手\s*持[^\n]{0,12}受\s*注|注文残|backlog", re.I)
ORDER_RE = re.compile(r"受\s*注\s*高|受\s*注\s*額|新規受注|orders?", re.I)
SALES_RE = re.compile(r"売\s*上\s*高|完成工事高|売上収益|sales|revenue", re.I)
TOTAL_RE = re.compile(r"^(?:合計|合\s*計|計|総計|全社|連結合計|total)$", re.I)
CURRENT_RE = re.compile(r"当第|当期|今回|現在|202[5-9]|令和[7-9]", re.I)
PRIOR_RE = re.compile(r"前第|前期|前年|202[0-5]|令和[2-7]", re.I)
PERCENT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?\s*%|前年差|増減率|前年同期比", re.I)
MONEY_UNIT_RE = re.compile(r"単位\s*[:：]?\s*(千円|百万円|億円|万円)|[（(](千円|百万円|億円|万円)[）)]")
NUMBER_RE = re.compile(r"[-+]?\s*[0-9][0-9,]*(?:\.[0-9]+)?")


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    for old, new in [("△", "-"), ("▲", "-"), ("−", "-"), ("–", "-"), ("〜", "~"), ("～", "~")]:
        text = text.replace(old, new)
    text = re.sub(r"[\t\u00a0]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def unit_multiplier(text: str) -> tuple[float | None, str | None]:
    match = MONEY_UNIT_RE.search(norm(text))
    if not match:
        return None, None
    unit = next(x for x in match.groups() if x)
    return {"千円": 0.001, "百万円": 1.0, "億円": 100.0, "万円": 0.01}[unit], unit


def parse_money(value: Any, default_multiplier: float | None = None) -> float | None:
    text = norm(value).replace(",", "")
    if not text or text in {"-", "―", "—"} or "%" in text or "pt" in text.lower():
        return None
    match = NUMBER_RE.search(text)
    if not match:
        return None
    try:
        number = float(match.group().replace(" ", ""))
    except ValueError:
        return None
    explicit = None
    for unit, multiplier in [("兆円", 1_000_000.0), ("億円", 100.0), ("百万円", 1.0), ("千円", 0.001), ("万円", 0.01)]:
        if unit in text:
            explicit = multiplier
            break
    return number * (explicit if explicit is not None else (default_multiplier if default_multiplier is not None else 1.0))


def parse_percent(value: Any) -> float | None:
    text = norm(value).replace(",", "")
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*%", text)
    return float(match.group(1)) / 100.0 if match else None


def mixed_jpy_values(text: str) -> list[tuple[float, str]]:
    text = norm(text)
    outputs: list[tuple[float, str]] = []
    patterns = [
        (re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*億\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*百万円"), lambda a, b: float(a.replace(',', '')) * 100 + float(b.replace(',', ''))),
        (re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*億\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*万円"), lambda a, b: float(a.replace(',', '')) * 100 + float(b.replace(',', '')) * 0.01),
        (re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*億円"), lambda a, _b: float(a.replace(',', '')) * 100),
        (re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*百万円"), lambda a, _b: float(a.replace(',', ''))),
        (re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*千円"), lambda a, _b: float(a.replace(',', '')) * 0.001),
    ]
    for pattern, converter in patterns:
        for match in pattern.finditer(text):
            groups = match.groups()
            outputs.append((converter(groups[0], groups[1] if len(groups) > 1 else None), match.group(0)))
    return outputs


@dataclass
class Candidate:
    current_backlog_m: float
    prior_backlog_m: float | None
    order_intake_m: float | None
    sales_m: float | None
    method: str
    confidence: str
    score: float
    page: int
    evidence: str
    period_text: str = ""
    unit: str | None = None
    yoy: float | None = None
    scope: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def table_rows(page: fitz.Page) -> list[tuple[list[list[str]], str, tuple[float, float, float, float]]]:
    outputs = []
    seen: set[str] = set()
    for vertical, horizontal in [("lines", "lines"), ("text", "text")]:
        try:
            finder = page.find_tables(vertical_strategy=vertical, horizontal_strategy=horizontal)
        except Exception:
            continue
        for table in finder.tables:
            try:
                rows = [[norm(cell) for cell in row] for row in table.extract()]
            except Exception:
                continue
            rows = [row for row in rows if any(row)]
            joined = "\n".join(" | ".join(row) for row in rows)
            if not BACKLOG_RE.search(joined):
                continue
            fingerprint = str(hash(joined))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            outputs.append((rows, f"{vertical}/{horizontal}", table.bbox))
    return outputs


def numeric_cells(row: list[str], multiplier: float | None) -> list[tuple[int, float]]:
    outputs = []
    for index, cell in enumerate(row):
        value = parse_money(cell, multiplier)
        if value is not None and not (1900 <= abs(value) <= 2100 and len(norm(cell).replace(',', '')) == 4):
            outputs.append((index, value))
    return outputs


def data_start(rows: list[list[str]], multiplier: float | None) -> int:
    for index, row in enumerate(rows):
        label = norm(" ".join(row[:2])).replace(" ", "")
        if numeric_cells(row, multiplier) and (TOTAL_RE.match(label) or index >= 2):
            return index
    return min(3, len(rows))


def header_for_columns(rows: list[list[str]], end: int) -> list[str]:
    width = max((len(row) for row in rows), default=0)
    headers = []
    for col in range(width):
        values = []
        for row in rows[:end]:
            if col < len(row) and norm(row[col]):
                values.append(norm(row[col]))
        headers.append(" ".join(values))
    return headers


def find_metric_columns(headers: list[str], regex: re.Pattern[str]) -> list[int]:
    return [index for index, header in enumerate(headers) if regex.search(header)]


def period_context(rows: list[list[str]], row_index: int) -> str:
    context = []
    for index in range(max(0, row_index - 5), row_index + 1):
        if rows[index]:
            first = norm(rows[index][0])
            if first:
                context.append(first)
    return " ".join(context)


def table_candidates(page_number: int, rows: list[list[str]], page_text: str) -> list[Candidate]:
    joined = "\n".join(" | ".join(row) for row in rows)
    multiplier, unit = unit_multiplier(joined + "\n" + page_text[:4000])
    start = data_start(rows, multiplier)
    headers = header_for_columns(rows, start)
    backlog_cols = find_metric_columns(headers, BACKLOG_RE)
    order_cols = find_metric_columns(headers, ORDER_RE)
    sales_cols = find_metric_columns(headers, SALES_RE)
    candidates: list[Candidate] = []

    # Column-oriented tables. Forward-fill the period cell so current and prior blocks are distinguished.
    last_period = ""
    for row_index, row in enumerate(rows):
        if row and norm(row[0]):
            first = norm(row[0])
            if re.search(r"四半期|会計年度|期間|自\s*20|至\s*20|当第|前第", first):
                last_period = first
        labels = [norm(cell) for cell in row]
        label = "".join(labels[:2]).replace(" ", "")
        is_total = any(TOTAL_RE.match(cell.replace(" ", "")) for cell in labels[:2] if cell)
        is_single_segment = len(rows) <= start + 2 and row_index >= start
        if (is_total or is_single_segment) and backlog_cols:
            backlog_values = []
            for col in backlog_cols:
                if col < len(row):
                    value = parse_money(row[col], multiplier)
                    if value is not None:
                        backlog_values.append((col, value))
            if backlog_values:
                backlog_values.sort()
                current = backlog_values[-1][1]
                prior = backlog_values[-2][1] if len(backlog_values) >= 2 else None
                order_value = None
                sales_value = None
                for cols, target in [(order_cols, "order"), (sales_cols, "sales")]:
                    values = []
                    for col in cols:
                        if col < len(row):
                            value = parse_money(row[col], multiplier)
                            if value is not None:
                                values.append((col, value))
                    if values:
                        values.sort()
                        if target == "order":
                            order_value = values[-1][1]
                        else:
                            sales_value = values[-1][1]
                yoy = None
                if len(backlog_values) == 1:
                    col = backlog_values[0][0]
                    for nearby in range(col + 1, min(len(row), col + 4)):
                        yoy = parse_percent(row[nearby])
                        if yoy is not None:
                            prior = current / (1 + yoy)
                            break
                context = last_period or period_context(rows, row_index)
                current_bonus = 15 if CURRENT_RE.search(context) else 0
                prior_penalty = 20 if PRIOR_RE.search(context) and not CURRENT_RE.search(context) else 0
                method = "TOTAL_BACKLOG_COLUMNS" if is_total else "SINGLE_SEGMENT_BACKLOG"
                candidates.append(Candidate(
                    current, prior, order_value, sales_value, method,
                    "A" if is_total else "B",
                    100 + current_bonus - prior_penalty + (5 if prior is not None else 0),
                    page_number, " | ".join(row), context, unit, yoy,
                    "TOTAL" if is_total else "SINGLE_SEGMENT",
                ))

    # Row-oriented tables where backlog is the metric row and periods are columns.
    for row_index, row in enumerate(rows):
        label_positions = [i for i, cell in enumerate(row) if BACKLOG_RE.search(norm(cell))]
        if not label_positions:
            continue
        label_index = min(label_positions)
        values = [(col, value) for col, value in numeric_cells(row, multiplier) if col > label_index]
        if not values:
            # Some tables put the label in a merged cell followed by values in the same text cell.
            text_values = mixed_jpy_values(" ".join(row))
            values = [(i + label_index + 1, value) for i, (value, _raw) in enumerate(text_values)]
        if len(values) >= 2:
            money_values = [value for _col, value in values]
            # The first two values are normally prior/current. Later values are absolute/percentage changes.
            prior, current = money_values[0], money_values[1]
            evidence = " | ".join(row)
            candidates.append(Candidate(
                current, prior, None, None, "ROW_PERIOD_PAIR", "A", 110,
                page_number, evidence, period_context(rows, row_index), unit,
                parse_percent(evidence), "TOTAL" if TOTAL_RE.search(evidence.replace(" ", "")) else "METRIC_ROW",
            ))
        elif len(values) == 1:
            current = values[0][1]
            evidence = " | ".join(row)
            yoy = parse_percent(evidence)
            candidates.append(Candidate(
                current, current / (1 + yoy) if yoy is not None else None,
                None, None, "ROW_CURRENT", "B", 75,
                page_number, evidence, period_context(rows, row_index), unit, yoy, "METRIC_ROW",
            ))

    return candidates


def narrative_candidates(page_number: int, text: str) -> list[Candidate]:
    compact = norm(text)
    outputs: list[Candidate] = []
    for match in BACKLOG_RE.finditer(compact):
        window = compact[max(0, match.start() - 100): match.end() + 450]
        values = mixed_jpy_values(window)
        if not values:
            continue
        # Prefer the first money expression after the backlog label.
        after = compact[match.end(): match.end() + 450]
        after_values = mixed_jpy_values(after)
        value, raw = after_values[0] if after_values else values[0]
        yoy = parse_percent(after)
        prior = value / (1 + yoy) if yoy is not None and yoy > -0.999 else None
        outputs.append(Candidate(
            value, prior, None, None, "NARRATIVE_BACKLOG", "B", 80 + (5 if yoy is not None else 0),
            page_number, window, "", None, yoy, "NARRATIVE",
        ))
    return outputs


def separated_text_candidates(page_number: int, text: str) -> list[Candidate]:
    # Handles PDFs whose table text is emitted as vertical labels followed by a numeric grid.
    compact = norm(text)
    outputs: list[Candidate] = []
    for match in BACKLOG_RE.finditer(compact):
        window = compact[match.end(): match.end() + 900]
        numbers = []
        for token in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", window):
            value = parse_money(token)
            if value is not None and not (1900 <= abs(value) <= 2100):
                numbers.append(value)
        if len(numbers) < 2:
            continue
        multiplier, unit = unit_multiplier(compact[max(0, match.start()-500):match.end()+200])
        if multiplier is not None:
            numbers = [number * multiplier for number in numbers]
        # Candidate pairs are emitted rather than selecting by proximity; reconciliation decides whether the table is usable.
        for index in range(min(len(numbers) - 1, 20)):
            prior, current = numbers[index], numbers[index + 1]
            if prior > 0 and current > 0:
                outputs.append(Candidate(
                    current, prior, None, None, "SEPARATED_TEXT_PAIR", "C", 35,
                    page_number, compact[max(0, match.start()-100):match.end()+900], "", unit, None, "UNKNOWN",
                ))
    return outputs


def deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    selected: dict[tuple[int, int, int, str], Candidate] = {}
    for candidate in candidates:
        key = (
            round(candidate.current_backlog_m * 1000),
            round((candidate.prior_backlog_m or -1) * 1000),
            candidate.page,
            candidate.method,
        )
        existing = selected.get(key)
        if existing is None or candidate.score > existing.score:
            selected[key] = candidate
    return sorted(selected.values(), key=lambda c: (-c.score, -c.current_backlog_m, c.page))


def select_candidate(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    eligible = [c for c in candidates if c.confidence in {"A", "B"} and c.current_backlog_m > 0]
    if not eligible:
        return candidates[0]
    # Prefer current-period totals. When equal-confidence totals disagree, the larger full-company total is safer than a segment subtotal.
    eligible.sort(key=lambda c: (c.score, c.scope == "TOTAL", c.current_backlog_m, c.page), reverse=True)
    return eligible[0]


def extract_candidates(pdf_bytes: bytes) -> tuple[list[Candidate], dict[str, Any]]:
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    candidates: list[Candidate] = []
    pages_with_backlog = 0
    tables_seen = 0
    for page_number, page in enumerate(document, start=1):
        page_text = page.get_text("text")
        if not BACKLOG_RE.search(norm(page_text)):
            continue
        pages_with_backlog += 1
        tables = table_rows(page)
        tables_seen += len(tables)
        for rows, _strategy, _bbox in tables:
            candidates.extend(table_candidates(page_number, rows, page_text))
        candidates.extend(narrative_candidates(page_number, page_text))
        candidates.extend(separated_text_candidates(page_number, page_text))
    candidates = deduplicate(candidates)
    audit = {
        "page_count": len(document),
        "pages_with_backlog": pages_with_backlog,
        "tables_seen": tables_seen,
        "candidate_count": len(candidates),
    }
    return candidates, audit
