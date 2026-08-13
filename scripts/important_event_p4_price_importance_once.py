from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import hashlib
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

CUTOFF = dt.date(2026, 8, 12)
FINANCING_DIR = Path("inputs/financing")
BUYBACK_DIR = Path("inputs/buyback")
OUT = Path("out/p4")
OUT.mkdir(parents=True, exist_ok=True)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 important-event-p4/1.0"})


def norm_code(value: Any) -> str:
    s = str(value or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    s = re.sub(r"\s+", "", s)
    if len(s) == 5 and s.endswith("0"):
        s = s[:4]
    return s


def parse_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value not in (None, "", "nan") else None
    except Exception:
        return None


def fnum(value: Any) -> float | None:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def flatten(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows({k: flatten(v) for k, v in row.items()} for row in rows)


def aggregate_buyback() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(BUYBACK_DIR.rglob("buyback_extracted.jsonl")):
        rows.extend(read_jsonl(path))
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("candidate_id") or row.get("file_id") or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest())
        row["code"] = norm_code(row.get("code"))
        if key not in dedup or (dedup[key].get("status") != "EXTRACTED" and row.get("status") == "EXTRACTED"):
            dedup[key] = row
    rows = sorted(dedup.values(), key=lambda r: (r.get("code", ""), r.get("disclosure_date", ""), r.get("disclosure_time", "")))

    for row in rows:
        if str(row.get("candidate_id")) == "140120260619574629":
            row.update(
                max_shares=4_000_000.0,
                max_amount_yen=1_000_000_000.0,
                share_ratio_ex_treasury=0.093,
                effective_start_date="2026-08-10",
                effective_end_date="2027-06-30",
                acquisition_method="MARKET_PURCHASE",
                extraction_confidence="A",
                extraction_score=1.0,
                status="EXTRACTED",
                manual_override="TDNET_PRIMARY_VERIFIED_2026-08-13",
            )

    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("code"):
            by_code[row["code"]].append(row)
    lifecycles: list[dict[str, Any]] = []
    for code, events in by_code.items():
        authorizations: list[dict[str, Any]] = []
        life_map: dict[str, dict[str, Any]] = {}
        for event in events:
            stage = event.get("lifecycle_stage")
            if stage == "AUTHORIZATION":
                lid = f"BUYBACK-{code}-{event.get('candidate_id')}"
                event["lifecycle_id"] = lid
                authorizations.append(event)
                life_map[lid] = {
                    "event_family": "BUYBACK",
                    "direction": "DEMAND",
                    "lifecycle_id": lid,
                    "code": code,
                    "company": event.get("company"),
                    "subtype": "BUYBACK",
                    "authorization_date": event.get("disclosure_date"),
                    "latest_event_date": event.get("disclosure_date"),
                    "start_date": event.get("effective_start_date"),
                    "end_date": event.get("effective_end_date"),
                    "max_shares": event.get("max_shares"),
                    "max_amount_yen": event.get("max_amount_yen"),
                    "share_ratio": event.get("share_ratio_ex_treasury"),
                    "acquisition_method": event.get("acquisition_method"),
                    "extraction_confidence": event.get("extraction_confidence"),
                    "source_url": event.get("source_url"),
                    "status": "AUTHORIZED",
                    "is_orphan": False,
                    "events": [event.get("candidate_id")],
                }
                continue
            if stage == "RETIREMENT":
                continue
            event_date = parse_date(event.get("disclosure_date"))
            eligible = []
            for auth in authorizations:
                auth_date = parse_date(auth.get("disclosure_date"))
                if not auth_date or not event_date or auth_date > event_date:
                    continue
                start = parse_date(auth.get("effective_start_date"))
                end = parse_date(auth.get("effective_end_date"))
                if start and event_date < start - dt.timedelta(days=30):
                    continue
                if end and event_date > end + dt.timedelta(days=60):
                    continue
                score = (100 if start and event_date >= start else 0) + (100 if end and event_date <= end + dt.timedelta(days=60) else 0) - (event_date - auth_date).days / 1000
                eligible.append((score, auth))
            eligible.sort(key=lambda x: (x[0], x[1].get("disclosure_date", "")), reverse=True)
            if eligible:
                auth = eligible[0][1]
                lid = auth["lifecycle_id"]
            else:
                lid = f"ORPHAN-{code}-{event.get('candidate_id')}"
                life_map[lid] = {
                    "event_family": "BUYBACK",
                    "direction": "DEMAND",
                    "lifecycle_id": lid,
                    "code": code,
                    "company": event.get("company"),
                    "subtype": "BUYBACK",
                    "authorization_date": None,
                    "latest_event_date": event.get("disclosure_date"),
                    "status": "ORPHAN",
                    "is_orphan": True,
                    "events": [],
                    "extraction_confidence": "C",
                    "source_url": event.get("source_url"),
                }
            life = life_map[lid]
            life.setdefault("events", []).append(event.get("candidate_id"))
            life["latest_event_date"] = event.get("disclosure_date")
            if event.get("cumulative_shares") is not None:
                life["latest_cumulative_shares"] = event.get("cumulative_shares")
            if event.get("cumulative_amount_yen") is not None:
                life["latest_cumulative_amount_yen"] = event.get("cumulative_amount_yen")
            if event.get("acquired_shares_period") is not None:
                life["latest_period_shares"] = event.get("acquired_shares_period")
            if event.get("acquired_amount_period_yen") is not None:
                life["latest_period_amount_yen"] = event.get("acquired_amount_period_yen")
            if stage == "COMPLETION":
                life["status"] = "COMPLETED"
            elif stage == "CANCELLATION":
                life["status"] = "CANCELLED"
            elif stage in {"START", "PROGRESS"}:
                life["status"] = "ACTIVE"
        for life in life_map.values():
            max_shares = fnum(life.get("max_shares"))
            max_amount = fnum(life.get("max_amount_yen"))
            cumulative_shares = fnum(life.get("latest_cumulative_shares"))
            cumulative_amount = fnum(life.get("latest_cumulative_amount_yen"))
            life["remaining_shares"] = max(max_shares - cumulative_shares, 0) if max_shares is not None and cumulative_shares is not None else None
            life["remaining_amount_yen"] = max(max_amount - cumulative_amount, 0) if max_amount is not None and cumulative_amount is not None else None
            life["event_count"] = len(life.get("events") or [])
            lifecycles.append(life)
    return lifecycles


def load_financing() -> list[dict[str, Any]]:
    path = FINANCING_DIR / "financing_lifecycles.csv"
    rows = read_csv(path)
    out = []
    for row in rows:
        code = norm_code(row.get("code"))
        out.append({
            "event_family": "FINANCING_SUPPLY",
            "direction": "SUPPLY",
            "lifecycle_id": row.get("lifecycle_id"),
            "code": code,
            "company": row.get("company"),
            "subtype": row.get("subtype"),
            "authorization_date": row.get("authorization_date"),
            "latest_event_date": row.get("latest_event_date"),
            "start_date": row.get("exercise_period_start") or row.get("issue_or_payment_date"),
            "end_date": row.get("exercise_period_end") or row.get("delivery_date"),
            "status": row.get("status"),
            "is_orphan": str(row.get("lifecycle_id") or "").startswith("ORPHAN"),
            "issued_shares": fnum(row.get("issued_shares")),
            "offered_shares": fnum(row.get("offered_shares")),
            "potential_shares": fnum(row.get("remaining_potential_shares_upper_bound")) or fnum(row.get("potential_shares")),
            "gross_proceeds_yen": fnum(row.get("gross_proceeds_yen")),
            "net_proceeds_yen": fnum(row.get("net_proceeds_yen")),
            "dilution_ratio": fnum(row.get("dilution_ratio")),
            "extraction_confidence": row.get("extraction_confidence") or "C",
            "offer_price_per_share_yen": fnum(row.get("offer_price_per_share_yen")),
            "issue_price_per_share_yen": fnum(row.get("issue_price_per_share_yen")),
            "exercise_price_yen": fnum(row.get("exercise_price_yen")),
            "event_count": int(float(row.get("event_count") or 0)),
            "source_url": None,
        })
    return out


def select_current(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = []
    for row in rows:
        if row.get("status") not in {"ACTIVE", "AUTHORIZED", "PRICED", "ORPHAN"}:
            continue
        latest = parse_date(row.get("latest_event_date"))
        end = parse_date(row.get("end_date"))
        start = parse_date(row.get("start_date"))
        recent = bool(latest and latest >= dt.date(2026, 5, 1))
        period_open = bool(end and end >= CUTOFF)
        settlement_relevant = bool(start and start >= dt.date(2026, 5, 1))
        if recent or period_open or settlement_relevant:
            current.append(row)
    return current


def yahoo_history(code: str) -> dict[str, Any]:
    symbol = f"{code}.T"
    start = int(dt.datetime(2024, 12, 1, tzinfo=dt.timezone.utc).timestamp())
    end = int(dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={start}&period2={end}&interval=1d&events=div%2Csplits"
    error = None
    for attempt in range(4):
        try:
            response = SESSION.get(url, timeout=45)
            if response.status_code == 200:
                result = response.json().get("chart", {}).get("result")
                if result:
                    result = result[0]
                    timestamps = result.get("timestamp") or []
                    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
                    adj = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or quote.get("close") or []
                    rows = []
                    for i, ts in enumerate(timestamps):
                        close = (quote.get("close") or [None] * len(timestamps))[i]
                        volume = (quote.get("volume") or [None] * len(timestamps))[i]
                        adjusted = adj[i] if i < len(adj) else close
                        if close is None or adjusted is None:
                            continue
                        rows.append({
                            "date": dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date(),
                            "close": float(close),
                            "adjclose": float(adjusted),
                            "volume": float(volume or 0),
                        })
                    rows = [r for r in rows if r["date"] <= CUTOFF]
                    if rows:
                        return {"code": code, "symbol": symbol, "rows": rows, "status": "OK", "source": "YAHOO_CHART"}
                error = "NO_RESULT"
            else:
                error = f"HTTP_{response.status_code}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        time.sleep(attempt + 1)
    return {"code": code, "symbol": symbol, "rows": [], "status": "FAILED", "error": error, "source": "YAHOO_CHART"}


def previous_row(rows: list[dict[str, Any]], date: dt.date | None) -> dict[str, Any] | None:
    if not date:
        return None
    candidates = [r for r in rows if r["date"] < date]
    return candidates[-1] if candidates else None


def first_row_on_or_after(rows: list[dict[str, Any]], date: dt.date | None) -> dict[str, Any] | None:
    if not date:
        return None
    return next((r for r in rows if r["date"] >= date), None)


def trading_days_since(rows: list[dict[str, Any]], date: dt.date | None) -> int | None:
    if not date:
        return None
    return sum(r["date"] >= date for r in rows) - 1


def materiality(ratio: float | None, days: float | None, amount_days: float | None) -> str:
    values = [x for x in (days, amount_days) if x is not None and math.isfinite(x)]
    max_days = max(values) if values else None
    if (ratio is not None and ratio >= 0.10) or (max_days is not None and max_days >= 10):
        return "VERY_HIGH"
    if (ratio is not None and ratio >= 0.05) or (max_days is not None and max_days >= 5):
        return "HIGH"
    if (ratio is not None and ratio >= 0.02) or (max_days is not None and max_days >= 2):
        return "MEDIUM"
    return "LOW" if ratio is not None or max_days is not None else "UNASSESSED"


def timing_state(row: dict[str, Any]) -> str:
    start = parse_date(row.get("start_date"))
    end = parse_date(row.get("end_date"))
    if start and start > CUTOFF:
        return "START_WITHIN_5D" if (start - CUTOFF).days <= 7 else "FUTURE"
    if end and end < CUTOFF:
        return "PAST_END"
    if row.get("status") in {"ACTIVE", "PRICED"}:
        return "ACTIVE"
    latest = parse_date(row.get("latest_event_date"))
    return "RECENT" if latest and (CUTOFF - latest).days <= 7 else "OPEN_UNCONFIRMED"


def evaluate(row: dict[str, Any], price: dict[str, Any]) -> dict[str, Any]:
    rows = price.get("rows") or []
    current = rows[-1] if rows else None
    last20 = rows[-20:] if rows else []
    median_volume = statistics.median([r["volume"] for r in last20 if r["volume"] > 0]) if any(r["volume"] > 0 for r in last20) else None
    median_turnover = statistics.median([r["close"] * r["volume"] for r in last20 if r["volume"] > 0]) if any(r["volume"] > 0 for r in last20) else None

    latest_date = parse_date(row.get("latest_event_date"))
    auth_date = parse_date(row.get("authorization_date"))
    reference_date = auth_date or latest_date
    pre = previous_row(rows, reference_date)
    first = first_row_on_or_after(rows, reference_date)
    first_return = first["adjclose"] / pre["adjclose"] - 1 if first and pre and pre["adjclose"] else None
    current_return = current["adjclose"] / pre["adjclose"] - 1 if current and pre and pre["adjclose"] else None

    ratio = fnum(row.get("share_ratio")) if row.get("event_family") == "BUYBACK" else fnum(row.get("dilution_ratio"))
    volume_days = None
    amount_days = None
    effective_shares = None
    upper_bound_only = False
    if row.get("event_family") == "BUYBACK":
        shares = fnum(row.get("remaining_shares"))
        amount = fnum(row.get("remaining_amount_yen"))
        if shares is None:
            shares = fnum(row.get("max_shares"))
            upper_bound_only = True
        if amount is None:
            amount = fnum(row.get("max_amount_yen"))
            upper_bound_only = True
        amount_shares = amount / current["close"] if amount is not None and current and current["close"] > 0 else None
        if shares is not None and amount_shares is not None:
            effective_shares = min(shares, amount_shares)
        else:
            effective_shares = shares if shares is not None else amount_shares
        volume_days = effective_shares / median_volume if effective_shares is not None and median_volume else None
        amount_days = amount / median_turnover if amount is not None and median_turnover else None
    else:
        candidates = [fnum(row.get("potential_shares")), fnum(row.get("issued_shares")), fnum(row.get("offered_shares"))]
        supply_shares = max([x for x in candidates if x is not None], default=None)
        effective_shares = supply_shares
        volume_days = supply_shares / median_volume if supply_shares is not None and median_volume else None
        amount = fnum(row.get("gross_proceeds_yen")) or fnum(row.get("net_proceeds_yen"))
        amount_days = amount / median_turnover if amount is not None and median_turnover else None

    mat = materiality(ratio, volume_days, amount_days)
    timing = timing_state(row)
    confidence = str(row.get("extraction_confidence") or "C")
    is_orphan = bool(row.get("is_orphan"))
    price_ok = price.get("status") == "OK" and current is not None and median_turnover is not None
    verified = confidence in {"A", "B"} and not is_orphan

    if verified and price_ok and mat in {"VERY_HIGH", "HIGH"} and timing in {"ACTIVE", "RECENT", "START_WITHIN_5D"}:
        importance = "A"
    elif verified and price_ok and mat in {"VERY_HIGH", "HIGH", "MEDIUM"}:
        importance = "B"
    else:
        importance = "C"

    age_trading = trading_days_since(rows, latest_date)
    if importance == "A" and age_trading is not None and age_trading >= 5 and timing == "ACTIVE" and not (current_return is not None and abs(current_return) >= 0.25):
        action = "RESEARCH_READY"
    elif importance in {"A", "B"}:
        action = "WATCH_5D"
    else:
        action = "NO_TRADE"

    if current_return is not None and current_return >= 0.15:
        price_state = "REACTED_UP_DO_NOT_CHASE"
    elif current_return is not None and current_return <= -0.15:
        price_state = "REACTED_DOWN_MARKET_SKEPTICISM"
    else:
        price_state = "LIMITED_REACTION" if current_return is not None else "PRICE_UNAVAILABLE"

    result = dict(row)
    result.update({
        "price_status": price.get("status"),
        "price_source": price.get("source"),
        "price_as_of": current["date"].isoformat() if current else None,
        "current_close": current["close"] if current else None,
        "median_volume_20d": median_volume,
        "median_turnover_20d_yen": median_turnover,
        "effective_shares_or_supply": effective_shares,
        "volume_days": volume_days,
        "amount_days": amount_days,
        "ratio_basis": ratio,
        "materiality": mat,
        "timing_state": timing,
        "importance_class": importance,
        "action_state": action,
        "first_trade_return": first_return,
        "current_return_from_authorization": current_return,
        "price_state": price_state,
        "latest_event_trading_days": age_trading,
        "upper_bound_only": upper_bound_only,
        "price_error": price.get("error"),
        "data_limitation": (
            "ORPHAN_LIFECYCLE" if is_orphan else
            "PRIMARY_PDF_NUMBERS_UNVERIFIED" if confidence == "C" else
            "PRICE_OR_LIQUIDITY_MISSING" if not price_ok else
            "AUTHORIZED_MAX_NOT_ACTUAL_REMAINING" if upper_bound_only else
            ""
        ),
    })
    return result


def main() -> None:
    buybacks = aggregate_buyback()
    financing = load_financing()
    current = select_current(buybacks + financing)
    codes = sorted({row["code"] for row in current if row.get("code")})
    prices: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        future_map = {pool.submit(yahoo_history, code): code for code in codes}
        for future in concurrent.futures.as_completed(future_map):
            result = future.result()
            prices[result["code"]] = result
    evaluated = [evaluate(row, prices.get(row.get("code"), {"status": "FAILED", "error": "NO_PRICE_REQUEST", "rows": []})) for row in current]

    order = {"A": 0, "B": 1, "C": 2}
    mat_order = {"VERY_HIGH": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNASSESSED": 4}
    evaluated.sort(key=lambda r: (
        order.get(r.get("importance_class"), 9),
        mat_order.get(r.get("materiality"), 9),
        0 if r.get("timing_state") in {"ACTIVE", "START_WITHIN_5D", "RECENT"} else 1,
        -(fnum(r.get("volume_days")) or -1),
        str(r.get("latest_event_date") or ""),
    ))

    high = [r for r in evaluated if r.get("importance_class") == "A"]
    watch = [r for r in evaluated if r.get("importance_class") == "B"]
    failures = [r for r in evaluated if r.get("price_status") != "OK" or r.get("data_limitation")]
    write_csv(OUT / "important_event_p4_all.csv", evaluated)
    write_csv(OUT / "importance_a.csv", high)
    write_csv(OUT / "importance_b.csv", watch)
    write_csv(OUT / "p4_exceptions.csv", failures)
    price_audit = [{k: v for k, v in price.items() if k != "rows"} | {"observations": len(price.get("rows") or [])} for price in prices.values()]
    write_csv(OUT / "price_audit.csv", price_audit)

    summary = {
        "cutoff": CUTOFF.isoformat(),
        "buyback_lifecycles_all": len(buybacks),
        "financing_lifecycles_all": len(financing),
        "current_open_candidates": len(current),
        "unique_codes": len(codes),
        "price_ok_codes": sum(v.get("status") == "OK" for v in prices.values()),
        "price_failed_codes": sum(v.get("status") != "OK" for v in prices.values()),
        "importance_counts": dict(Counter(r.get("importance_class") for r in evaluated)),
        "action_counts": dict(Counter(r.get("action_state") for r in evaluated)),
        "family_counts": dict(Counter(r.get("event_family") for r in evaluated)),
        "materiality_counts": dict(Counter(r.get("materiality") for r in evaluated)),
        "limitations": dict(Counter(r.get("data_limitation") for r in evaluated if r.get("data_limitation"))),
        "top_importance_a": [
            {k: row.get(k) for k in (
                "code", "company", "event_family", "subtype", "direction", "status",
                "latest_event_date", "materiality", "timing_state", "importance_class",
                "action_state", "current_close", "volume_days", "amount_days", "ratio_basis",
                "current_return_from_authorization", "price_state", "data_limitation",
            )}
            for row in high[:30]
        ],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
