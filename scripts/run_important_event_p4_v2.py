from __future__ import annotations

import datetime as dt
import math
from typing import Any

import important_event_p4_price_importance_once as core

ORIGINAL_EVALUATE = core.evaluate


def corrected_timing(row: dict[str, Any]) -> str:
    start = core.parse_date(row.get("start_date"))
    end = core.parse_date(row.get("end_date"))
    if start and start > core.CUTOFF:
        return "START_WITHIN_5D" if (start - core.CUTOFF).days <= 7 else "FUTURE"
    if start and start <= core.CUTOFF and (not end or end >= core.CUTOFF):
        return "ACTIVE"
    if end and end < core.CUTOFF:
        return "PAST_END"
    if row.get("status") in {"ACTIVE", "PRICED"}:
        return "ACTIVE"
    latest = core.parse_date(row.get("latest_event_date"))
    return "RECENT" if latest and (core.CUTOFF - latest).days <= 7 else "OPEN_UNCONFIRMED"


def append_limitation(current: str | None, value: str) -> str:
    parts = [p for p in str(current or "").split("|") if p]
    if value and value not in parts:
        parts.append(value)
    return "|".join(parts)


def corrected_evaluate(row: dict[str, Any], price: dict[str, Any]) -> dict[str, Any]:
    result = ORIGINAL_EVALUATE(row, price)
    timing = corrected_timing(row)
    result["timing_state"] = timing

    family = row.get("event_family")
    method = str(row.get("acquisition_method") or "")
    confidence = str(row.get("extraction_confidence") or "C")
    is_orphan = bool(row.get("is_orphan"))
    price_ok = result.get("price_status") == "OK" and core.fnum(result.get("median_turnover_20d_yen")) is not None
    current_close = core.fnum(result.get("current_close"))
    median_volume = core.fnum(result.get("median_volume_20d"))
    median_turnover = core.fnum(result.get("median_turnover_20d_yen"))
    ratio = core.fnum(result.get("ratio_basis"))
    volume_days = core.fnum(result.get("volume_days"))
    amount_days = core.fnum(result.get("amount_days"))
    limitation = result.get("data_limitation") or ""
    numeric_inconsistency = False

    if family == "FINANCING_SUPPLY":
        share_candidates = [
            core.fnum(row.get("potential_shares")),
            core.fnum(row.get("issued_shares")),
            core.fnum(row.get("offered_shares")),
        ]
        parsed_shares = max([x for x in share_candidates if x is not None], default=None)
        proceeds = core.fnum(row.get("gross_proceeds_yen")) or core.fnum(row.get("net_proceeds_yen"))
        implied_shares = proceeds / current_close if proceeds is not None and current_close and current_close > 0 else None
        if parsed_shares is not None and implied_shares is not None:
            effective = min(parsed_shares, implied_shares)
            consistency_ratio = max(parsed_shares, implied_shares) / max(min(parsed_shares, implied_shares), 1)
            if consistency_ratio > 3:
                numeric_inconsistency = True
                limitation = append_limitation(limitation, "NUMERIC_INCONSISTENCY_SHARES_VS_PROCEEDS")
                ratio = None
        else:
            effective = parsed_shares if parsed_shares is not None else implied_shares
        volume_days = effective / median_volume if effective is not None and median_volume else None
        amount_days = proceeds / median_turnover if proceeds is not None and median_turnover else None
        result["effective_shares_or_supply"] = effective
        result["volume_days"] = volume_days
        result["amount_days"] = amount_days
        result["ratio_basis"] = ratio

    mat = core.materiality(ratio, volume_days, amount_days)
    result["materiality"] = mat
    verified = confidence in {"A", "B"} and not is_orphan and not numeric_inconsistency

    if family == "BUYBACK" and method == "TOSTNET3":
        latest = core.parse_date(row.get("latest_event_date"))
        days_from_event = (core.CUTOFF - latest).days if latest else None
        result["persistence_state"] = "ONE_OFF_TOSTNET"
        limitation = append_limitation(limitation, "TOSTNET_NOT_PERSISTENT_MARKET_DEMAND")
        if verified and price_ok and mat in {"VERY_HIGH", "HIGH"} and days_from_event is not None and days_from_event <= 1:
            importance = "B"
            action = "WATCH_5D"
        else:
            importance = "C"
            action = "NO_TRADE"
    else:
        result["persistence_state"] = "PERSISTENT_OR_OPEN"
        active_timing = timing in {"ACTIVE", "RECENT", "START_WITHIN_5D"}
        if family == "BUYBACK":
            if verified and price_ok and method == "MARKET_PURCHASE" and mat in {"VERY_HIGH", "HIGH"} and active_timing:
                importance = "A"
            elif verified and price_ok and mat in {"VERY_HIGH", "HIGH", "MEDIUM"}:
                importance = "B"
            else:
                importance = "C"
            if not method:
                importance = "B" if importance == "A" else importance
                limitation = append_limitation(limitation, "BUYBACK_METHOD_UNVERIFIED")
        else:
            if verified and price_ok and mat in {"VERY_HIGH", "HIGH"} and active_timing:
                importance = "A"
            elif verified and price_ok and mat in {"VERY_HIGH", "HIGH", "MEDIUM"}:
                importance = "B"
            else:
                importance = "C"

        age_trading = result.get("latest_event_trading_days")
        reacted = result.get("price_state") in {"REACTED_UP_DO_NOT_CHASE", "REACTED_DOWN_MARKET_SKEPTICISM"}
        if importance == "A" and family == "BUYBACK":
            progress_confirmed = not bool(result.get("upper_bound_only"))
            action = "RESEARCH_READY" if progress_confirmed and timing == "ACTIVE" and not reacted else "WATCH_5D"
        elif importance == "A" and family == "FINANCING_SUPPLY":
            action = "RESEARCH_READY" if age_trading is not None and age_trading >= 5 and timing in {"ACTIVE", "PRICED"} and not reacted else "WATCH_5D"
        elif importance == "B":
            action = "WATCH_5D"
        else:
            action = "NO_TRADE"

    if family == "BUYBACK" and result.get("upper_bound_only"):
        limitation = append_limitation(limitation, "AUTHORIZED_MAX_NOT_ACTUAL_REMAINING")
    result["importance_class"] = importance
    result["action_state"] = action
    result["data_limitation"] = limitation
    result["numeric_consistency"] = "FAIL" if numeric_inconsistency else "PASS_OR_NOT_TESTED"
    return result


core.timing_state = corrected_timing
core.evaluate = corrected_evaluate

if __name__ == "__main__":
    core.main()
