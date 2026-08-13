from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from urllib.parse import quote

import requests

CODES = [
    "1306", "1726", "1758", "1780", "186A", "1950", "211A", "3443",
    "4444", "6235", "6279", "6379", "6702", "8061", "8869", "9619", "9960",
]
OUT = Path("out_final_prices")
OUT.mkdir(exist_ok=True)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 final-backlog-backfill/1.0"})

start_ts = int(dt.datetime(2025, 12, 20, tzinfo=dt.timezone.utc).timestamp())
end_ts = int(dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc).timestamp())


def fetch(code: str) -> dict:
    symbol = f"{code}.T"
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + quote(symbol, safe="")
        + f"?period1={start_ts}&period2={end_ts}&interval=1d"
        "&events=div%2Csplits&includeAdjustedClose=true"
    )
    try:
        response = SESSION.get(url, timeout=60)
        response.raise_for_status()
        obj = response.json()
        results = ((obj.get("chart") or {}).get("result") or [])
        if not results:
            return {"status": "NO_DATA", "symbol": symbol, "url": url}
        result = results[0]
        timestamps = result.get("timestamp") or []
        quote_data = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        adjusted = (((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or [])
        rows = []
        for index, timestamp in enumerate(timestamps):
            def at(values):
                return values[index] if index < len(values) else None
            rows.append({
                "date": dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date().isoformat(),
                "open": at(quote_data.get("open") or []),
                "high": at(quote_data.get("high") or []),
                "low": at(quote_data.get("low") or []),
                "close": at(quote_data.get("close") or []),
                "adjclose": at(adjusted),
                "volume": at(quote_data.get("volume") or []),
            })
        return {
            "status": "OK" if rows else "NO_ROWS",
            "symbol": symbol,
            "currency": (result.get("meta") or {}).get("currency"),
            "exchange": (result.get("meta") or {}).get("exchangeName"),
            "rows": rows,
            "url": url,
        }
    except Exception as exc:
        return {"status": "ERROR", "symbol": symbol, "error": f"{type(exc).__name__}: {exc}", "url": url}


output = {code: fetch(code) for code in CODES}
(OUT / "final_missing_prices.json").write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
summary = {
    "codes": len(CODES),
    "ok": sum(1 for value in output.values() if value.get("status") == "OK"),
    "failed": {code: value.get("status") for code, value in output.items() if value.get("status") != "OK"},
}
(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
