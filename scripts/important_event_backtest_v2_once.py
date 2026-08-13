from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import json
import math
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

START = dt.date(2025, 1, 1)
CUTOFF = dt.date(2026, 8, 12)
BENCHMARK = "1306.T"
HORIZONS = (1, 5, 20, 60)
PERIODS = {
    "FULL": (START, CUTOFF),
    "TRAIN": (START, dt.date(2026, 3, 31)),
    "VALIDATION": (dt.date(2026, 4, 1), dt.date(2026, 6, 30)),
    "OOS": (dt.date(2026, 7, 1), CUTOFF),
}
FIN_ROOT = Path("inputs/financing")
BUYBACK_ROOT = Path("inputs/buyback")
OUT = Path("out/backtest_v2")
OUT.mkdir(parents=True, exist_ok=True)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 important-event-backtest/2.0"})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()})


def find_one(root: Path, name: str) -> Path:
    matches = list(root.rglob(name))
    if not matches:
        raise FileNotFoundError(f"{name} not found under {root}")
    return matches[0]


def d(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value not in (None, "", "None", "null", "NA", "N/A") else None
    except (ValueError, TypeError):
        return None


def n(value: Any) -> float | None:
    try:
        x = float(str(value).replace(",", ""))
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def code(value: Any) -> str:
    text = str(value or "").upper().replace(".T", "").strip()
    text = re.sub(r"[^0-9A-Z]", "", text)
    if len(text) == 5 and text.endswith("0"):
        text = text[:4]
    return text


def manual_enechange(row: dict[str, Any]) -> None:
    row.update({
        "candidate_id": "140120260619574629",
        "file_id": "140120260619574629",
        "code": "4169",
        "company": "ENECHANGE",
        "disclosure_date": "2026-06-22",
        "disclosure_time": "12:00",
        "lifecycle_stage": "AUTHORIZATION",
        "max_shares": 4_000_000,
        "max_amount_yen": 1_000_000_000,
        "share_ratio_ex_treasury": 0.093,
        "effective_start_date": "2026-08-10",
        "effective_end_date": "2027-06-30",
        "acquisition_method": "MARKET_PURCHASE",
        "status": "MANUAL_PRIMARY_OVERRIDE",
        "extraction_confidence": "A_MANUAL_PRIMARY",
        "source_url": "https://www.release.tdnet.info/inbs/140120260619574629.pdf",
    })


def load_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    fin_path = find_one(FIN_ROOT, "financing_lifecycles.csv")
    for row in read_csv(fin_path):
        event_date = d(row.get("authorization_date"))
        ticker = code(row.get("code"))
        if not ticker or not event_date or not (START <= event_date <= CUTOFF):
            continue
        confidence = str(row.get("extraction_confidence") or "C")
        events.append({
            "event_id": row.get("lifecycle_id"),
            "event_family": "FINANCING_SUPPLY",
            "direction": -1,
            "code": ticker,
            "company": row.get("company"),
            "subtype": row.get("subtype") or "UNKNOWN_FINANCING",
            "event_date": event_date,
            "event_time": None,
            "confidence": confidence,
            "source_status": row.get("status"),
            "start_date": d(row.get("exercise_period_start")) or d(row.get("issue_or_payment_date")) or d(row.get("delivery_date")),
            "end_date": d(row.get("exercise_period_end")),
            "shares": max([x for x in (n(row.get("issued_shares")), n(row.get("offered_shares")), n(row.get("potential_shares"))) if x is not None], default=None),
            "amount_yen": n(row.get("gross_proceeds_yen")) or n(row.get("net_proceeds_yen")) or n(row.get("cb_face_amount_yen")),
            "ratio": n(row.get("dilution_ratio")),
            "method": None,
            "source_url": None,
        })

    buyback_rows: list[dict[str, Any]] = []
    for path in BUYBACK_ROOT.rglob("buyback_extracted.jsonl"):
        buyback_rows.extend(read_jsonl(path))
    dedup: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(buyback_rows):
        key = str(row.get("candidate_id") or row.get("file_id") or f"missing-{idx}")
        old = dedup.get(key)
        if old is None or (old.get("status") != "EXTRACTED" and row.get("status") == "EXTRACTED"):
            dedup[key] = row
    ene = dedup.get("140120260619574629")
    if ene is None:
        ene = {}
        dedup["140120260619574629"] = ene
    manual_enechange(ene)

    for row in dedup.values():
        if row.get("lifecycle_stage") != "AUTHORIZATION":
            continue
        event_date = d(row.get("disclosure_date"))
        ticker = code(row.get("code"))
        if not ticker or not event_date or not (START <= event_date <= CUTOFF):
            continue
        confidence = str(row.get("extraction_confidence") or "C")
        events.append({
            "event_id": row.get("candidate_id") or row.get("file_id"),
            "event_family": "BUYBACK",
            "direction": 1,
            "code": ticker,
            "company": row.get("company"),
            "subtype": "BUYBACK",
            "event_date": event_date,
            "event_time": str(row.get("disclosure_time") or ""),
            "confidence": confidence,
            "source_status": row.get("status"),
            "start_date": d(row.get("effective_start_date")),
            "end_date": d(row.get("effective_end_date")),
            "shares": n(row.get("max_shares")),
            "amount_yen": n(row.get("max_amount_yen")),
            "ratio": n(row.get("share_ratio_ex_treasury")),
            "method": str(row.get("acquisition_method") or "UNKNOWN"),
            "source_url": row.get("source_url"),
        })
    return events


def yahoo(symbol: str) -> dict[str, Any]:
    start = int(dt.datetime(2024, 10, 1, tzinfo=dt.timezone.utc).timestamp())
    end = int(dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={start}&period2={end}&interval=1d&events=div%2Csplits"
    error = None
    for attempt in range(5):
        try:
            response = SESSION.get(url, timeout=45)
            if response.status_code == 200:
                result = response.json().get("chart", {}).get("result")
                if result:
                    obj = result[0]
                    ts = obj.get("timestamp") or []
                    quote = (obj.get("indicators", {}).get("quote") or [{}])[0]
                    adj = (obj.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
                    rows = []
                    for i, stamp in enumerate(ts):
                        qopen = (quote.get("open") or [None] * len(ts))[i]
                        qclose = (quote.get("close") or [None] * len(ts))[i]
                        volume = (quote.get("volume") or [None] * len(ts))[i]
                        aclose = adj[i] if i < len(adj) else qclose
                        if qopen is None or qclose is None or aclose is None:
                            continue
                        factor = float(aclose) / float(qclose) if qclose else 1.0
                        rows.append({
                            "date": dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc).date(),
                            "open": float(qopen) * factor,
                            "close": float(aclose),
                            "raw_close": float(qclose),
                            "volume": float(volume or 0),
                        })
                    rows = [r for r in rows if r["date"] <= CUTOFF]
                    if rows:
                        return {"status": "OK", "symbol": symbol, "rows": rows}
                error = "NO_RESULT"
            else:
                error = f"HTTP_{response.status_code}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        time.sleep(attempt + 1)
    return {"status": "FAILED", "symbol": symbol, "rows": [], "error": error}


def before(rows: list[dict[str, Any]], event_date: dt.date, count: int = 20) -> list[dict[str, Any]]:
    return [r for r in rows if r["date"] < event_date][-count:]


def next_index(rows: list[dict[str, Any]], event_date: dt.date) -> int | None:
    for i, row in enumerate(rows):
        if row["date"] > event_date:
            return i
    return None


def materiality(ratio: float | None, volume_days: float | None, amount_days: float | None) -> str:
    days = max([x for x in (volume_days, amount_days) if x is not None and math.isfinite(x)], default=None)
    if (ratio is not None and ratio >= 0.10) or (days is not None and days >= 10):
        return "VERY_HIGH"
    if (ratio is not None and ratio >= 0.05) or (days is not None and days >= 5):
        return "HIGH"
    if (ratio is not None and ratio >= 0.02) or (days is not None and days >= 2):
        return "MEDIUM"
    return "LOW" if ratio is not None or days is not None else "UNASSESSED"


def verified(confidence: str) -> bool:
    return confidence.startswith("A") or confidence.startswith("B")


def persistent(event: dict[str, Any]) -> bool:
    if event["event_family"] == "BUYBACK":
        return event.get("method") == "MARKET_PURCHASE"
    subtype = str(event.get("subtype") or "")
    return "WARRANT" in subtype or "CONVERTIBLE_BOND" in subtype


def event_time_importance(event: dict[str, Any], mat: str, volume_ok: bool) -> str:
    if not verified(str(event.get("confidence") or "")) or not volume_ok:
        return "C"
    event_date = event["event_date"]
    start = event.get("start_date")
    near = start is not None and -5 <= (start - event_date).days <= 14
    if event["event_family"] == "BUYBACK":
        if mat in {"VERY_HIGH", "HIGH"} and persistent(event) and near:
            return "A"
    else:
        subtype = str(event.get("subtype") or "")
        if mat in {"VERY_HIGH", "HIGH"} and (near or "WARRANT" in subtype or "CONVERTIBLE_BOND" in subtype):
            return "A"
    return "B" if mat in {"VERY_HIGH", "HIGH", "MEDIUM"} else "C"


def evaluate(event: dict[str, Any], stock: dict[str, Any], benchmark: dict[str, Any]) -> dict[str, Any]:
    result = dict(event)
    rows = stock.get("rows") or []
    bench_rows = benchmark.get("rows") or []
    result["price_status"] = stock.get("status")
    if stock.get("status") != "OK" or benchmark.get("status") != "OK":
        result["error"] = stock.get("error") or benchmark.get("error")
        return result
    pre = before(rows, event["event_date"], 20)
    median_volume = statistics.median([r["volume"] for r in pre if r["volume"] > 0]) if any(r["volume"] > 0 for r in pre) else None
    median_turnover = statistics.median([r["raw_close"] * r["volume"] for r in pre if r["volume"] > 0]) if any(r["volume"] > 0 for r in pre) else None
    reference_close = pre[-1]["close"] if pre else None
    shares = event.get("shares")
    amount = event.get("amount_yen")
    event_price = pre[-1]["raw_close"] if pre else None
    if event["event_family"] == "BUYBACK" and event_price:
        amount_shares = amount / event_price if amount is not None else None
        effective_shares = min(shares, amount_shares) if shares is not None and amount_shares is not None else shares if shares is not None else amount_shares
    else:
        effective_shares = shares
    volume_days = effective_shares / median_volume if effective_shares is not None and median_volume else None
    amount_days = amount / median_turnover if amount is not None and median_turnover else None
    mat = materiality(event.get("ratio"), volume_days, amount_days)
    imp = event_time_importance(event, mat, median_turnover is not None)
    result.update({
        "pre20_median_volume": median_volume,
        "pre20_median_turnover_yen": median_turnover,
        "reference_close": reference_close,
        "effective_shares": effective_shares,
        "volume_days": volume_days,
        "amount_days": amount_days,
        "materiality": mat,
        "importance_event_time": imp,
        "persistent": persistent(event),
    })
    entry_idx = next_index(rows, event["event_date"])
    if entry_idx is None:
        result["error"] = "NO_POST_EVENT_TRADING_DAY"
        return result
    entry = rows[entry_idx]
    bench_entry_idx = next((i for i, r in enumerate(bench_rows) if r["date"] == entry["date"]), None)
    if bench_entry_idx is None:
        result["error"] = "NO_BENCHMARK_ENTRY"
        return result
    result["entry_date"] = entry["date"].isoformat()
    result["entry_open"] = entry["open"]
    result["benchmark_entry_open"] = bench_rows[bench_entry_idx]["open"]
    for h in HORIZONS:
        exit_idx = entry_idx + h - 1
        bench_exit_idx = bench_entry_idx + h - 1
        if exit_idx >= len(rows) or bench_exit_idx >= len(bench_rows):
            continue
        exit_row = rows[exit_idx]
        bench_exit = bench_rows[bench_exit_idx]
        if exit_row["date"] != bench_exit["date"]:
            bench_exit = next((r for r in bench_rows if r["date"] == exit_row["date"]), None)
            if bench_exit is None:
                continue
        stock_return = exit_row["close"] / entry["open"] - 1
        benchmark_return = bench_exit["close"] / bench_rows[bench_entry_idx]["open"] - 1
        excess = stock_return - benchmark_return
        directional = event["direction"] * excess
        cost = 0.002 if event["direction"] == 1 else 0.004 + 0.05 * h / 252
        result.update({
            f"exit_{h}d_date": exit_row["date"].isoformat(),
            f"return_{h}d": stock_return,
            f"benchmark_return_{h}d": benchmark_return,
            f"excess_{h}d": excess,
            f"directional_excess_{h}d": directional,
            f"net_directional_excess_{h}d": directional - cost,
        })
    if entry_idx + 19 < len(rows):
        fifth = rows[entry_idx + 4] if entry_idx + 4 < len(rows) else None
        twentieth = rows[entry_idx + 19]
        bench_fifth = next((r for r in bench_rows if fifth and r["date"] == fifth["date"]), None)
        bench_twentieth = next((r for r in bench_rows if r["date"] == twentieth["date"]), None)
        if fifth and bench_fifth and bench_twentieth:
            stock_5_20 = twentieth["close"] / fifth["close"] - 1
            bench_5_20 = bench_twentieth["close"] / bench_fifth["close"] - 1
            result["watch5_to20_directional_excess"] = event["direction"] * (stock_5_20 - bench_5_20)
    return result


def period(event_date: dt.date) -> str:
    if event_date <= dt.date(2026, 3, 31):
        return "TRAIN"
    if event_date <= dt.date(2026, 6, 30):
        return "VALIDATION"
    return "OOS"


def cluster(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("price_status") == "OK":
            by[(row["code"], row["event_family"])].append(row)
    for group in by.values():
        last: dt.date | None = None
        for row in sorted(group, key=lambda r: r["event_date"]):
            if last is None or (row["event_date"] - last).days > 20:
                kept.append(row)
                last = row["event_date"]
    return sorted(kept, key=lambda r: (r["event_date"], r["code"], r["event_family"]))


def bootstrap_ci(rows: list[dict[str, Any]], field: str, reps: int = 3000) -> tuple[float | None, float | None]:
    usable = [r for r in rows if n(r.get(field)) is not None]
    by_code: dict[str, list[float]] = defaultdict(list)
    for row in usable:
        by_code[row["code"]].append(float(row[field]))
    codes = list(by_code)
    if len(codes) < 3:
        return None, None
    rng = random.Random(20260813)
    means = []
    for _ in range(reps):
        sampled = [rng.choice(codes) for _ in codes]
        values = [v for ticker in sampled for v in by_code[ticker]]
        means.append(statistics.mean(values))
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def summarize(rows: list[dict[str, Any]], group_name: str, group_filter) -> list[dict[str, Any]]:
    output = []
    selected = [r for r in rows if group_filter(r)]
    for period_name, (start, end) in PERIODS.items():
        subset = [r for r in selected if start <= r["event_date"] <= end]
        for h in HORIZONS:
            field = f"net_directional_excess_{h}d"
            usable = [r for r in subset if n(r.get(field)) is not None]
            if not usable:
                continue
            values = [float(r[field]) for r in usable]
            ci_lo, ci_hi = bootstrap_ci(usable, field)
            output.append({
                "group": group_name,
                "period": period_name,
                "horizon": f"{h}D",
                "n": len(values),
                "unique_codes": len({r["code"] for r in usable}),
                "mean": statistics.mean(values),
                "median": statistics.median(values),
                "win_rate": sum(v > 0 for v in values) / len(values),
                "ci_lo": ci_lo,
                "ci_hi": ci_hi,
                "best": max(values),
                "worst": min(values),
            })
    return output


def main() -> None:
    events = load_events()
    symbols = sorted({f"{event['code']}.T" for event in events} | {BENCHMARK})
    prices: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=18) as pool:
        future_map = {pool.submit(yahoo, symbol): symbol for symbol in symbols}
        for future in concurrent.futures.as_completed(future_map):
            result = future.result()
            prices[result["symbol"]] = result
    benchmark = prices[BENCHMARK]
    evaluated = [evaluate(event, prices.get(f"{event['code']}.T", {"status": "FAILED", "rows": [], "error": "NOT_FETCHED"}), benchmark) for event in events]
    for row in evaluated:
        row["period"] = period(row["event_date"])
        row["event_date"] = row["event_date"].isoformat()
        if isinstance(row.get("start_date"), dt.date):
            row["start_date"] = row["start_date"].isoformat()
        if isinstance(row.get("end_date"), dt.date):
            row["end_date"] = row["end_date"].isoformat()
    clustered_input = []
    for row in evaluated:
        restored = dict(row)
        restored["event_date"] = d(row.get("event_date"))
        clustered_input.append(restored)
    clustered = cluster(clustered_input)

    groups = {
        "EVENT_ALL_VERIFIED": lambda r: verified(str(r.get("confidence") or "")),
        "BUYBACK_VERIFIED": lambda r: r["event_family"] == "BUYBACK" and verified(str(r.get("confidence") or "")),
        "BUYBACK_PERSISTENT": lambda r: r["event_family"] == "BUYBACK" and verified(str(r.get("confidence") or "")) and bool(r.get("persistent")),
        "BUYBACK_IMPORTANCE_A": lambda r: r["event_family"] == "BUYBACK" and r.get("importance_event_time") == "A",
        "FINANCING_VERIFIED": lambda r: r["event_family"] == "FINANCING_SUPPLY" and verified(str(r.get("confidence") or "")),
        "FINANCING_IMPORTANCE_A": lambda r: r["event_family"] == "FINANCING_SUPPLY" and r.get("importance_event_time") == "A",
    }
    subtypes = sorted({str(r.get("subtype")) for r in clustered if r["event_family"] == "FINANCING_SUPPLY"})
    for subtype in subtypes:
        groups[f"FIN_{subtype}"] = lambda r, subtype=subtype: r["event_family"] == "FINANCING_SUPPLY" and str(r.get("subtype")) == subtype and verified(str(r.get("confidence") or ""))
    summary: list[dict[str, Any]] = []
    for name, filt in groups.items():
        summary.extend(summarize(clustered, name, filt))
    top_bottom = sorted([r for r in clustered if n(r.get("net_directional_excess_20d")) is not None], key=lambda r: float(r["net_directional_excess_20d"]), reverse=True)
    price_audit = [{"symbol": symbol, "status": value.get("status"), "observations": len(value.get("rows") or []), "error": value.get("error")} for symbol, value in sorted(prices.items())]
    for row in clustered:
        row["event_date"] = row["event_date"].isoformat()
    write_csv(OUT / "event_backtest_all_raw.csv", evaluated)
    write_csv(OUT / "event_backtest_clustered.csv", clustered)
    write_csv(OUT / "event_backtest_summary.csv", summary)
    write_csv(OUT / "event_backtest_top_bottom.csv", top_bottom[:30] + top_bottom[-30:])
    write_csv(OUT / "price_audit.csv", price_audit)
    audit = {
        "cutoff": CUTOFF.isoformat(),
        "events_input": len(events),
        "events_evaluated": len(evaluated),
        "events_clustered": len(clustered),
        "unique_codes": len({e["code"] for e in events}),
        "price_ok_symbols": sum(v.get("status") == "OK" for v in prices.values()),
        "price_failed_symbols": sum(v.get("status") != "OK" for v in prices.values()),
        "benchmark": BENCHMARK,
        "entry_rule": "first trading day strictly after disclosure date, adjusted open",
        "exit_rule": "1/5/20/60th trading day adjusted close",
        "costs": {"long_round_trip": 0.002, "short_round_trip": 0.004, "short_borrow_annual": 0.05},
        "lookahead_controls": [
            "Only authorization-stage buyback rows and authorization fields from financing lifecycles are used.",
            "Later completion, cancellation and progress are not selection inputs.",
            "Importance uses pre-event 20-day liquidity and event-time disclosed size.",
            "Same code and family within 20 calendar days is clustered.",
        ],
        "group_counts": dict(Counter(r["event_family"] for r in clustered)),
    }
    (OUT / "event_backtest_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"audit": audit, "summary_20d_full": [r for r in summary if r["period"] == "FULL" and r["horizon"] == "20D"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
