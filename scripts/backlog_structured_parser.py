from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

import fitz

BACKLOG_RE = re.compile(
    r"受\s*注\s*残(?:高)?|繰\s*越(?:工事|受注|高)?|次\s*期\s*繰\s*越(?:高|工事高)?|"
    r"手\s*持(?:工事|受注|高)?|注文残|契約残|backlog",
    re.I,
)
ORDER_RE = re.compile(r"受\s*注\s*高|受\s*注\s*額|新規受注|orders?", re.I)
SALES_RE = re.compile(r"売\s*上\s*高|完成工事高|売上収益|sales|revenue", re.I)
TOTAL_RE = re.compile(
    r"^(?:合計|合\s*計|計|総計|全社|連結合計|総合計|total|合\s*計\s*欄)$",
    re.I,
)
CURRENT_RE = re.compile(
    r"当第|当期|今回|現在|直近|202[5-9]|令和[7-9]|FY ?202[5-9]",
    re.I,
)
PRIOR_RE = re.compile(
    r"前第|前期|前年|比較期|202[0-5]|令和[2-7]|FY ?202[0-5]",
    re.I,
)
PERCENT_RE = re.compile(
    r"[-+]?\d+(?:\.\d+)?\s*%|前年差|増減率|前年同期比|前期比", re.I
)
MONEY_UNIT_RE = re.compile(
    r"単位\s*[:：]?\s*(千円|百万円|億円|万円)|[（(](千円|百万円|億円|万円)[）)]"
)
NUMBER_RE = re.compile(r"[-+]?\s*[0-9][0-9,]*(?:\.[0-9]+)?")
YEAR_RE = re.compile(r"20\d{2}|令和\s*([1-9]\d*)")
QUARTER_RE = re.compile(r"第?\s*([1-4])\s*四半期|([1-4])\s*Q|通期|中間", re.I)


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    for old, new in [
        ("△", "-"),
        ("▲", "-"),
        ("−", "-"),
        ("–", "-"),
        ("―", "-"),
        ("〜", "~"),
        ("～", "~"),
    ]:
        text = text.replace(old, new)
    text = re.sub(r"[\t\u00a0]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_label(value: Any) -> str:
    return re.sub(r"[\s:：()（）\[\]【】]", "", norm(value)).lower()


def unit_multiplier(text: str) -> tuple[float | None, str | None]:
    match = MONEY_UNIT_RE.search(norm(text))
    if not match:
        return None, None
    unit = next(value for value in match.groups() if value)
    return {"千円": 0.001, "百万円": 1.0, "億円": 100.0, "万円": 0.01}[unit], unit


def parse_number(value: Any) -> float | None:
    text = norm(value).replace(",", "")
    if not text or text in {"-", "―", "—"}:
        return None
    match = NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group().replace(" ", ""))
    except ValueError:
        return None


def parse_money(value: Any, default_multiplier: float | None = None) -> float | None:
    text = norm(value).replace(",", "")
    if (
        not text
        or text in {"-", "―", "—"}
        or "%" in text
        or "pt" in text.lower()
        or re.fullmatch(r"20\d{2}", text)
    ):
        return None
    number = parse_number(text)
    if number is None:
        return None
    explicit = None
    for unit, multiplier in [
        ("兆円", 1_000_000.0),
        ("億円", 100.0),
        ("百万円", 1.0),
        ("千円", 0.001),
        ("万円", 0.01),
    ]:
        if unit in text:
            explicit = multiplier
            break
    multiplier = explicit if explicit is not None else default_multiplier
    if multiplier is None:
        multiplier = 1.0
    return number * multiplier


def parse_percent(value: Any) -> float | None:
    text = norm(value).replace(",", "")
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*%", text)
    return float(match.group(1)) / 100.0 if match else None


def mixed_jpy_values(text: str) -> list[tuple[float, str, int, int]]:
    text = norm(text)
    outputs: list[tuple[float, str, int, int]] = []
    patterns = [
        (
            re.compile(
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*億\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*百万円"
            ),
            lambda a, b: float(a.replace(",", "")) * 100
            + float(b.replace(",", "")),
        ),
        (
            re.compile(
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*億\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*万円"
            ),
            lambda a, b: float(a.replace(",", "")) * 100
            + float(b.replace(",", "")) * 0.01,
        ),
        (
            re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*兆円"),
            lambda a, _b: float(a.replace(",", "")) * 1_000_000,
        ),
        (
            re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*億円"),
            lambda a, _b: float(a.replace(",", "")) * 100,
        ),
        (
            re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*百万円"),
            lambda a, _b: float(a.replace(",", "")),
        ),
        (
            re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*千円"),
            lambda a, _b: float(a.replace(",", "")) * 0.001,
        ),
        (
            re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*万円"),
            lambda a, _b: float(a.replace(",", "")) * 0.01,
        ),
    ]
    occupied: list[tuple[int, int]] = []
    for pattern, converter in patterns:
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in occupied):
                continue
            groups = match.groups()
            value = converter(groups[0], groups[1] if len(groups) > 1 else None)
            outputs.append((value, match.group(0), match.start(), match.end()))
            occupied.append((match.start(), match.end()))
    return sorted(outputs, key=lambda item: item[2])


def years_in(text: str) -> list[int]:
    years: list[int] = []
    for match in YEAR_RE.finditer(norm(text)):
        year = int(match.group(0)) if match.group(0).startswith("20") else 2018 + int(match.group(1))
        years.append(year)
    return years


def quarter_tokens(text: str) -> list[int]:
    outputs: list[int] = []
    for match in QUARTER_RE.finditer(norm(text)):
        token = match.group(0)
        if "通期" in token:
            outputs.append(4)
        elif "中間" in token:
            outputs.append(2)
        else:
            outputs.append(int(match.group(1) or match.group(2)))
    return outputs


def approx_equal(
    left: float, right: float, rel: float = 0.015, abs_tol: float = 1.0
) -> bool:
    return abs(left - right) <= max(abs_tol, abs(right) * rel)


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
    current_role: str = "UNKNOWN"
    diagnostic: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def table_rows(
    page: fitz.Page,
) -> list[tuple[list[list[str]], str, tuple[float, float, float, float]]]:
    outputs = []
    seen: set[str] = set()
    for vertical, horizontal in [("lines", "lines"), ("text", "text")]:
        try:
            finder = page.find_tables(
                vertical_strategy=vertical,
                horizontal_strategy=horizontal,
            )
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
            fingerprint = hashlib_text(joined)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            outputs.append((rows, f"{vertical}/{horizontal}", table.bbox))
    return outputs


def hashlib_text(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def row_numbers(
    row: list[str], multiplier: float | None
) -> list[tuple[int, float, str]]:
    outputs: list[tuple[int, float, str]] = []
    for index, cell in enumerate(row):
        text = norm(cell)
        if not text or "%" in text or PERCENT_RE.search(text):
            continue
        value = parse_money(text, multiplier)
        if value is None:
            continue
        plain = text.replace(",", "").replace(" ", "")
        if re.fullmatch(r"20\d{2}", plain):
            continue
        outputs.append((index, value, text))
    return outputs


def is_data_row(row: list[str], multiplier: float | None) -> bool:
    if not row:
        return False
    label = compact_label(" ".join(row[:2]))
    if TOTAL_RE.match(label):
        return True
    values = row_numbers(row, multiplier)
    return bool(values) and any(
        re.search(r"事業|部門|セグメント|工事|装置|製品|合計|計$", norm(cell))
        for cell in row[:2]
    )


def expand_header_row(row: list[str]) -> list[str]:
    outputs = []
    last = ""
    for cell in row:
        value = norm(cell)
        if value:
            last = value
        outputs.append(last)
    return outputs


def header_end_index(rows: list[list[str]], multiplier: float | None) -> int:
    for index, row in enumerate(rows):
        if is_data_row(row, multiplier):
            return index
    return min(len(rows), 5)


def column_headers(rows: list[list[str]], end: int) -> list[str]:
    width = max((len(row) for row in rows), default=0)
    expanded = [expand_header_row(row + [""] * (width - len(row))) for row in rows[:end]]
    headers: list[str] = []
    for col in range(width):
        pieces = []
        for row in expanded:
            value = norm(row[col])
            if value and value not in pieces:
                pieces.append(value)
        headers.append(" ".join(pieces))
    return headers


def role_for_header(
    header: str, all_headers: list[str], as_of_date: str | None
) -> str:
    header = norm(header)
    years = years_in(header)
    current_year = int(as_of_date[:4]) if as_of_date else None
    if current_year and years:
        if max(years) >= current_year:
            return "CURRENT"
        if max(years) == current_year - 1:
            return "PRIOR"
    if CURRENT_RE.search(header) and not PRIOR_RE.search(header):
        return "CURRENT"
    if PRIOR_RE.search(header) and not CURRENT_RE.search(header):
        return "PRIOR"
    header_quarters = quarter_tokens(header)
    all_quarters = [quarter for value in all_headers for quarter in quarter_tokens(value)]
    if header_quarters and all_quarters:
        maximum = max(all_quarters)
        if max(header_quarters) == maximum:
            return "CURRENT"
        if max(header_quarters) < maximum:
            return "PRIOR"
    return "UNKNOWN"


def choose_period_pair(
    values: list[tuple[int, float]],
    headers: list[str],
    as_of_date: str | None,
) -> tuple[float, float | None, str, str]:
    if not values:
        raise ValueError("values required")
    roles = {
        col: role_for_header(headers[col] if col < len(headers) else "", headers, as_of_date)
        for col, _value in values
    }
    current_values = [(col, value) for col, value in values if roles[col] == "CURRENT"]
    prior_values = [(col, value) for col, value in values if roles[col] == "PRIOR"]
    if current_values:
        current_col, current = current_values[-1]
        prior = prior_values[-1][1] if prior_values else None
        return current, prior, "EXPLICIT_PERIOD", headers[current_col]
    if len(values) >= 2:
        # Most Japanese quarterly tables place prior on the left and current on the right.
        prior = values[-2][1]
        current = values[-1][1]
        return current, prior, "POSITIONAL_PAIR", ""
    return values[0][1], None, "SINGLE_PERIOD", ""


def metric_columns(headers: list[str], regex: re.Pattern[str]) -> list[int]:
    return [index for index, header in enumerate(headers) if regex.search(norm(header))]


def metric_current_value(
    row: list[str],
    columns: list[int],
    headers: list[str],
    multiplier: float | None,
    as_of_date: str | None,
) -> float | None:
    values: list[tuple[int, float]] = []
    for column in columns:
        if column >= len(row):
            continue
        value = parse_money(row[column], multiplier)
        if value is not None:
            values.append((column, value))
    if not values:
        return None
    return choose_period_pair(values, headers, as_of_date)[0]


def total_table_candidates(
    page_number: int,
    rows: list[list[str]],
    page_text: str,
    as_of_date: str | None,
) -> list[Candidate]:
    joined = "\n".join(" | ".join(row) for row in rows)
    multiplier, unit = unit_multiplier(joined + "\n" + page_text[:5000])
    end = header_end_index(rows, multiplier)
    headers = column_headers(rows, end)
    backlog_cols = metric_columns(headers, BACKLOG_RE)
    order_cols = metric_columns(headers, ORDER_RE)
    sales_cols = metric_columns(headers, SALES_RE)
    if not backlog_cols:
        return []
    candidates: list[Candidate] = []
    data_rows = [(index, row) for index, row in enumerate(rows[end:], start=end)]
    total_rows = []
    segment_rows = []
    for row_index, row in data_rows:
        label = compact_label(" ".join(row[:2]))
        if TOTAL_RE.match(label):
            total_rows.append((row_index, row))
        elif row_numbers(row, multiplier):
            segment_rows.append((row_index, row))

    for row_index, row in total_rows:
        backlog_values = []
        for column in backlog_cols:
            if column < len(row):
                value = parse_money(row[column], multiplier)
                if value is not None:
                    backlog_values.append((column, value))
        if not backlog_values:
            continue
        current, prior, role, period_text = choose_period_pair(
            backlog_values, headers, as_of_date
        )
        order = metric_current_value(
            row, order_cols, headers, multiplier, as_of_date
        )
        sales = metric_current_value(
            row, sales_cols, headers, multiplier, as_of_date
        )
        evidence = " | ".join(row)
        candidates.append(
            Candidate(
                current,
                prior,
                order,
                sales,
                "TOTAL_TABLE",
                "A",
                145 + (8 if role == "EXPLICIT_PERIOD" else 0),
                page_number,
                evidence,
                period_text,
                unit,
                parse_percent(evidence),
                "TOTAL",
                role,
                "",
            )
        )

    # Some disclosures omit a total line but list all operating segments in a single table.
    if not total_rows and 1 < len(segment_rows) <= 12:
        current_values: list[float] = []
        prior_values: list[float] = []
        order_values: list[float] = []
        sales_values: list[float] = []
        evidence_rows: list[str] = []
        role = "UNKNOWN"
        for _row_index, row in segment_rows:
            values = []
            for column in backlog_cols:
                if column < len(row):
                    value = parse_money(row[column], multiplier)
                    if value is not None:
                        values.append((column, value))
            if not values:
                continue
            current, prior, item_role, _period_text = choose_period_pair(
                values, headers, as_of_date
            )
            current_values.append(current)
            if prior is not None:
                prior_values.append(prior)
            order = metric_current_value(
                row, order_cols, headers, multiplier, as_of_date
            )
            sales = metric_current_value(
                row, sales_cols, headers, multiplier, as_of_date
            )
            if order is not None:
                order_values.append(order)
            if sales is not None:
                sales_values.append(sales)
            evidence_rows.append(" | ".join(row))
            if item_role == "EXPLICIT_PERIOD":
                role = item_role
        if len(current_values) >= 2:
            candidates.append(
                Candidate(
                    sum(current_values),
                    sum(prior_values) if len(prior_values) == len(current_values) else None,
                    sum(order_values) if len(order_values) == len(current_values) else None,
                    sum(sales_values) if len(sales_values) == len(current_values) else None,
                    "SEGMENT_SUM_TABLE",
                    "B",
                    128 + (5 if role == "EXPLICIT_PERIOD" else 0),
                    page_number,
                    " || ".join(evidence_rows),
                    "",
                    unit,
                    None,
                    "TOTAL_SUM",
                    role,
                    f"segments={len(current_values)}",
                )
            )
    return candidates


def metric_row_candidates(
    page_number: int,
    rows: list[list[str]],
    page_text: str,
    as_of_date: str | None,
) -> list[Candidate]:
    joined = "\n".join(" | ".join(row) for row in rows)
    multiplier, unit = unit_multiplier(joined + "\n" + page_text[:5000])
    candidates: list[Candidate] = []
    for row_index, row in enumerate(rows):
        label_positions = [
            index for index, cell in enumerate(row) if BACKLOG_RE.search(norm(cell))
        ]
        if not label_positions:
            continue
        label_index = min(label_positions)
        values = [
            (column, value)
            for column, value, _text in row_numbers(row, multiplier)
            if column > label_index
        ]
        if len(values) < 1:
            continue
        # Percentage columns are excluded by row_numbers. A pair normally represents prior/current.
        current, prior, role, period_text = choose_period_pair(
            values, [norm(cell) for cell in row], as_of_date
        )
        evidence = " | ".join(row)
        scope = "TOTAL" if TOTAL_RE.search(compact_label(evidence)) else "METRIC_ROW"
        score = 135 if scope == "TOTAL" else 102
        if role == "EXPLICIT_PERIOD":
            score += 5
        if len(values) > 4:
            score -= 15
        candidates.append(
            Candidate(
                current,
                prior,
                None,
                None,
                "METRIC_ROW_TABLE",
                "A" if scope == "TOTAL" else "B",
                score,
                page_number,
                evidence,
                period_text,
                unit,
                parse_percent(evidence),
                scope,
                role,
                f"value_count={len(values)}",
            )
        )
    return candidates


def parse_total_line_numbers(
    text: str, multiplier: float | None
) -> tuple[float, float] | None:
    # Expected layouts include: 合計 受注高 前年比 売上高 前年比 受注残 前年比.
    numbers: list[float] = []
    for token in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?(?:\s*(?:百万円|千円|億円|万円))?", text):
        if "%" in token:
            continue
        value = parse_money(token, multiplier)
        if value is not None and not (1900 <= value <= 2100):
            numbers.append(value)
    if len(numbers) >= 3:
        return numbers[-1], numbers[-2]
    return None


def text_total_row_candidates(page_number: int, text: str) -> list[Candidate]:
    normalized = norm(text)
    multiplier, unit = unit_multiplier(normalized)
    outputs: list[Candidate] = []
    lines = [norm(line) for line in text.splitlines() if norm(line)]
    for index, line in enumerate(lines):
        block = " ".join(lines[max(0, index - 3) : min(len(lines), index + 4)])
        if not BACKLOG_RE.search(block):
            continue
        compact = compact_label(line)
        if not (compact.startswith("合計") or compact.startswith("計") or "total" in compact):
            continue
        values = []
        for _start, value, token in [
            (match.start(), parse_money(match.group(0), multiplier), match.group(0))
            for match in re.finditer(r"[-+]?\d[\d,]*(?:\.\d+)?", line)
        ]:
            if value is not None and not (1900 <= value <= 2100):
                values.append(value)
        if not values:
            continue
        # If a table row includes ratios, choose the final large monetary value before the final ratio.
        money_like = [value for value in values if value >= 100]
        current = money_like[-1] if money_like else values[-1]
        outputs.append(
            Candidate(
                current,
                None,
                None,
                None,
                "TEXT_TOTAL_ROW",
                "B",
                105,
                page_number,
                block,
                "",
                unit,
                parse_percent(block),
                "TOTAL",
                "TEXT_TABLE",
                f"raw_values={values}",
            )
        )
    # Common normalized table pattern with metric labels before the total row.
    if BACKLOG_RE.search(normalized) and TOTAL_RE.search(normalized):
        for match in re.finditer(r"(?:合\s*計|総\s*計|Total)[^\n]{0,250}", normalized, re.I):
            block = match.group(0)
            numbers = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", block)
            parsed = [parse_money(token, multiplier) for token in numbers]
            parsed = [value for value in parsed if value is not None and not (1900 <= value <= 2100)]
            if len(parsed) >= 3:
                # When alternating monetary value and percent, monetary values are normally the larger entries.
                money = [value for value in parsed if value >= 100]
                if money:
                    outputs.append(
                        Candidate(
                            money[-1],
                            None,
                            None,
                            None,
                            "TEXT_TOTAL_PATTERN",
                            "C",
                            65,
                            page_number,
                            block,
                            "",
                            unit,
                            None,
                            "TOTAL",
                            "TEXT_TABLE",
                            f"raw_values={parsed}",
                        )
                    )
    return outputs


def narrative_candidates(page_number: int, text: str) -> list[Candidate]:
    compact = norm(text)
    outputs: list[Candidate] = []
    for match in BACKLOG_RE.finditer(compact):
        after = compact[match.end() : match.end() + 550]
        before = compact[max(0, match.start() - 120) : match.start()]
        values = mixed_jpy_values(after)
        if not values:
            continue
        # Phrases such as "前年から2,916百万円増の27,443百万円" contain an increment first.
        selected = values[0]
        if len(values) >= 2:
            segment = after[: values[1][3] + 30]
            if re.search(r"増(?:加)?\s*の|減(?:少)?\s*の|となり|残高は", segment):
                selected = values[-1]
            elif re.search(r"前年[^。]{0,80}(?:増|減)", segment):
                selected = values[-1]
        current = selected[0]
        yoy = parse_percent(after)
        prior = current / (1 + yoy) if yoy is not None and yoy > -0.999 else None
        context = before + compact[match.start() : match.end()] + after
        outputs.append(
            Candidate(
                current,
                prior,
                None,
                None,
                "NARRATIVE_BACKLOG",
                "B",
                118 + (7 if yoy is not None else 0),
                page_number,
                context[:800],
                "",
                None,
                yoy,
                "NARRATIVE",
                "NARRATIVE_CURRENT",
                f"selected={selected[1]}",
            )
        )
    return outputs


def chart_trend_candidates(page_number: int, text: str) -> list[Candidate]:
    normalized = norm(text)
    outputs: list[Candidate] = []
    for match in BACKLOG_RE.finditer(normalized):
        window = normalized[max(0, match.start() - 500) : match.end() + 1200]
        years = years_in(window)
        if len(set(years)) < 2:
            continue
        multiplier, unit = unit_multiplier(window)
        values: list[float] = []
        for token in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", window):
            value = parse_money(token, multiplier)
            if value is None or 1900 <= value <= 2100:
                continue
            if value < 10 and len(set(years)) >= 3:
                continue
            values.append(value)
        # Keep plausible monetary levels and choose the final two points of a chart series.
        filtered = [value for value in values if value >= 100]
        if len(filtered) < 2:
            continue
        current = filtered[-1]
        prior = filtered[-2]
        outputs.append(
            Candidate(
                current,
                prior,
                None,
                None,
                "CHART_TREND",
                "C",
                55,
                page_number,
                window[:1000],
                " ".join(str(year) for year in years[-6:]),
                unit,
                None,
                "TOTAL_BY_MAX_SERIES",
                "LATEST_TREND",
                f"value_count={len(filtered)}",
            )
        )
    return outputs


def deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    selected: dict[tuple[int, int, int, str], Candidate] = {}
    for candidate in candidates:
        if not math.isfinite(candidate.current_backlog_m) or candidate.current_backlog_m <= 0:
            continue
        key = (
            round(candidate.current_backlog_m * 1000),
            round((candidate.prior_backlog_m or -1) * 1000),
            candidate.page,
            candidate.method,
        )
        existing = selected.get(key)
        if existing is None or candidate.score > existing.score:
            selected[key] = candidate
    return sorted(
        selected.values(),
        key=lambda candidate: (
            -candidate.score,
            candidate.confidence not in {"A", "B"},
            candidate.scope not in {"TOTAL", "TOTAL_SUM", "TOTAL_BY_MAX_SERIES"},
            -candidate.current_backlog_m,
            candidate.page,
        ),
    )


def select_candidate(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    eligible = [
        candidate
        for candidate in candidates
        if candidate.confidence in {"A", "B"} and candidate.current_backlog_m > 0
    ]
    if not eligible:
        return candidates[0]
    eligible.sort(
        key=lambda candidate: (
            candidate.score,
            candidate.scope in {"TOTAL", "TOTAL_SUM", "TOTAL_BY_MAX_SERIES"},
            candidate.current_role in {
                "EXPLICIT_PERIOD",
                "LATEST_TREND",
                "NARRATIVE_CURRENT",
                "TEXT_TABLE",
            },
            candidate.prior_backlog_m is not None,
            candidate.current_backlog_m,
            -candidate.page,
        ),
        reverse=True,
    )
    return eligible[0]


def extract_candidates(
    pdf_bytes: bytes,
    as_of_date: str | None = None,
) -> tuple[list[Candidate], dict[str, Any]]:
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    candidates: list[Candidate] = []
    pages_with_backlog = 0
    tables_seen = 0
    for page_number, page in enumerate(document, start=1):
        page_text = page.get_text("text")
        normalized = norm(page_text)
        if not BACKLOG_RE.search(normalized):
            continue
        pages_with_backlog += 1
        tables = table_rows(page)
        tables_seen += len(tables)
        for rows, _strategy, _bbox in tables:
            candidates.extend(total_table_candidates(page_number, rows, page_text, as_of_date))
            candidates.extend(metric_row_candidates(page_number, rows, page_text, as_of_date))
        candidates.extend(text_total_row_candidates(page_number, page_text))
        candidates.extend(narrative_candidates(page_number, page_text))
        candidates.extend(chart_trend_candidates(page_number, page_text))
    candidates = deduplicate(candidates)
    audit = {
        "page_count": len(document),
        "pages_with_backlog": pages_with_backlog,
        "tables_seen": tables_seen,
        "candidate_count": len(candidates),
        "methods": {
            method: sum(candidate.method == method for candidate in candidates)
            for method in sorted({candidate.method for candidate in candidates})
        },
    }
    return candidates, audit
