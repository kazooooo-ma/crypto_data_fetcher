from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote

import requests

START = dt.date(2025, 12, 30)
END = dt.date(2026, 7, 6)
PRICE_END = dt.date(2026, 8, 14)
OUT = Path("out_full")
TEXT_OUT = OUT / "text"
TEXT_OUT.mkdir(parents=True, exist_ok=True)
PDF_DIR = Path(tempfile.mkdtemp(prefix="tdnet-full-pdf-"))
TXT_DIR = Path(tempfile.mkdtemp(prefix="tdnet-full-txt-"))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 TDnet backlog research one-off/1.0"})

DOC_TITLE = re.compile(r"決算短信|決算説明|決算補足|決算概要|決算資料|ファクトブック|Financial Results|Earnings")
EXCLUDE_TITLE = re.compile(r"訂正|修正後|再訂正")
BACKLOG_KEYWORD = re.compile(
    r"受注残高|受注残|受注高|繰越工事高|手持工事高|繰越高|契約残高|バックログ|Book[- ]?to[- ]?Bill",
    re.IGNORECASE,
)
CODE_RE = re.compile(r"\d{4}|\d{3}[A-Z]")


def get_json(url: str, timeout: int = 60):
    response = SESSION.get(url, timeout=timeout)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def dates():
    day = START
    while day <= END:
        yield day
        day += dt.timedelta(days=1)


def fetch_manifest(day: dt.date):
    date_str = day.strftime("%Y%m%d")
    url = (
        "https://raw.githubusercontent.com/yukizi1113/tdnet/main/"
        f"tekigikaizi/{date_str}/manifest.json"
    )
    try:
        return date_str, get_json(url), None
    except Exception as exc:  # noqa: BLE001
        return date_str, None, f"{type(exc).__name__}: {exc}"


def candidate_items(items: list[dict]) -> list[dict]:
    output = []
    seen = set()
    for item in items:
        title = str(item.get("title") or "")
        if not DOC_TITLE.search(title):
            continue
        if EXCLUDE_TITLE.search(title):
            continue
        file_id = str(item.get("file_id") or "")
        key = file_id or str(item.get("github_path") or item.get("source_url") or title)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def download_pdf(item: dict, date_str: str) -> tuple[Path | None, str | None]:
    file_id = str(item.get("file_id") or "")
    code = str(item.get("ticker") or "")
    safe_name = re.sub(r"[^0-9A-Za-z._-]+", "_", f"{date_str}_{code}_{file_id}.pdf")
    target = PDF_DIR / safe_name
    urls: list[str] = []
    github_path = item.get("github_path")
    if github_path:
        urls.append(
            "https://raw.githubusercontent.com/yukizi1113/tdnet/main/"
            + quote(str(github_path), safe="/")
        )
    if item.get("source_url"):
        urls.append(str(item["source_url"]))
    last_error = "download failed"
    for url in urls:
        try:
            response = SESSION.get(url, timeout=120)
            if response.status_code == 200 and response.content.startswith(b"%PDF"):
                target.write_bytes(response.content)
                return target, None
            last_error = f"HTTP {response.status_code} from {url}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
    return None, last_error


def extract_item(item: dict, date_str: str) -> tuple[dict | None, dict]:
    meta = {
        "date": date_str,
        "code": str(item.get("ticker") or ""),
        "company": item.get("company") or "",
        "title": item.get("title") or "",
        "time": item.get("time") or "",
        "source_url": item.get("source_url") or "",
        "github_path": item.get("github_path") or "",
        "file_id": item.get("file_id") or "",
    }
    pdf_path, error = download_pdf(item, date_str)
    if not pdf_path:
        meta["status"] = "DOWNLOAD_ERROR"
        meta["error"] = error
        return None, meta
    text_path = TXT_DIR / f"{pdf_path.stem}.txt"
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), str(text_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0 or not text_path.exists():
            meta["status"] = "PDFTOTEXT_ERROR"
            meta["error"] = completed.stderr[-1000:]
            return None, meta
        text = text_path.read_text(encoding="utf-8", errors="replace")
        meta["text_chars"] = len(text)
        if not BACKLOG_KEYWORD.search(text):
            meta["status"] = "NO_BACKLOG_KEYWORD"
            return None, meta
        pages = [page.strip() for page in text.split("\f") if page.strip()]
        record = {
            "date": date_str,
            "code": meta["code"],
            "company": meta["company"],
            "title": meta["title"],
            "time": meta["time"],
            "file_id": meta["file_id"],
            "url": meta["source_url"],
            "github_path": meta["github_path"],
            "pages": pages,
        }
        meta["status"] = "MATCHED"
        meta["pages"] = len(pages)
        return record, meta
    except Exception as exc:  # noqa: BLE001
        meta["status"] = "EXTRACT_ERROR"
        meta["error"] = f"{type(exc).__name__}: {exc}"
        return None, meta
    finally:
        pdf_path.unlink(missing_ok=True)
        text_path.unlink(missing_ok=True)


def fetch_prices(codes: set[str]) -> dict:
    start_ts = int(dt.datetime(2025, 12, 15, tzinfo=dt.timezone.utc).timestamp())
    end_ts = int(dt.datetime.combine(PRICE_END, dt.time.min, tzinfo=dt.timezone.utc).timestamp())

    def one(code: str):
        symbols = ["^TOPX", "1306.T"] if code == "TOPIX" else [f"{code}.T"]
        attempts = []
        for symbol in symbols:
            url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + quote(symbol, safe="")
                + f"?period1={start_ts}&period2={end_ts}&interval=1d"
                "&events=div%2Csplits&includeAdjustedClose=true"
            )
            try:
                obj = get_json(url, timeout=60)
                results = ((obj or {}).get("chart") or {}).get("result")
                if not results:
                    attempts.append({"symbol": symbol, "status": "NO_DATA"})
                    continue
                result = results[0]
                timestamps = result.get("timestamp") or []
                quote_data = ((result.get("indicators") or {}).get("quote") or [{}])[0]
                adjusted = (
                    ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
                    or []
                )
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
                return code, {
                    "status": "OK",
                    "symbol": symbol,
                    "currency": (result.get("meta") or {}).get("currency"),
                    "exchange": (result.get("meta") or {}).get("exchangeName"),
                    "rows": rows,
                    "url": url,
                    "attempts": attempts,
                }
            except Exception as exc:  # noqa: BLE001
                attempts.append({"symbol": symbol, "status": "ERROR", "error": f"{type(exc).__name__}: {exc}"})
        return code, {"status": "ERROR", "attempts": attempts}

    output: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for code, value in executor.map(one, sorted(codes)):
            output[code] = value
    return output


def main():
    audit: list[dict] = []
    manifest_catalog: list[dict] = []
    matched_by_day: dict[str, list[dict]] = {}
    codes: set[str] = set()

    manifest_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for result in executor.map(fetch_manifest, list(dates())):
            manifest_results.append(result)
    manifest_results.sort()

    tasks: list[tuple[dict, str]] = []
    for date_str, obj, error in manifest_results:
        if error:
            audit.append({"date": date_str, "status": "MANIFEST_ERROR", "error": error})
            continue
        if not obj:
            audit.append({"date": date_str, "status": "NO_MANIFEST"})
            continue
        items = obj.get("items") or []
        candidates = candidate_items(items)
        audit.append({"date": date_str, "status": "MANIFEST_OK", "items": len(items), "document_candidates": len(candidates)})
        for item in candidates:
            manifest_catalog.append({
                "date": date_str,
                "code": str(item.get("ticker") or ""),
                "company": item.get("company") or "",
                "title": item.get("title") or "",
                "time": item.get("time") or "",
                "file_id": item.get("file_id") or "",
                "source_url": item.get("source_url") or "",
                "github_path": item.get("github_path") or "",
            })
            tasks.append((item, date_str))

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(extract_item, item, date_str) for item, date_str in tasks]
        for future in concurrent.futures.as_completed(futures):
            record, meta = future.result()
            audit.append(meta)
            if record:
                date_str = meta["date"]
                matched_by_day.setdefault(date_str, []).append(record)
                if CODE_RE.fullmatch(meta["code"]):
                    codes.add(meta["code"])

    for date_str, files in sorted(matched_by_day.items()):
        files.sort(key=lambda item: (item["code"], item["title"], item["file_id"]))
        (TEXT_OUT / f"text_{date_str}.json").write_text(
            json.dumps({"date": date_str, "files": files}, ensure_ascii=False),
            encoding="utf-8",
        )

    extra_codes = Path("scripts/backfill_price_codes.txt")
    if extra_codes.exists():
        codes.update(line.strip() for line in extra_codes.read_text(encoding="utf-8").splitlines() if CODE_RE.fullmatch(line.strip()))
    codes.add("TOPIX")
    prices = fetch_prices(codes)

    (OUT / "manifest_catalog.json").write_text(json.dumps(manifest_catalog, ensure_ascii=False), encoding="utf-8")
    (OUT / "prices_yahoo.json").write_text(json.dumps(prices, ensure_ascii=False), encoding="utf-8")
    (OUT / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "period": [START.isoformat(), END.isoformat()],
        "manifest_days": sum(1 for item in audit if item.get("status") == "MANIFEST_OK"),
        "document_candidates": len(tasks),
        "matched_documents": sum(len(items) for items in matched_by_day.values()),
        "matched_days": len(matched_by_day),
        "price_codes": len(codes),
        "price_ok": sum(1 for item in prices.values() if item.get("status") == "OK"),
        "download_errors": sum(1 for item in audit if item.get("status") == "DOWNLOAD_ERROR"),
        "extract_errors": sum(1 for item in audit if item.get("status") in {"PDFTOTEXT_ERROR", "EXTRACT_ERROR"}),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
