from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import fitz
import requests

API = "https://webapi.yanoshin.jp/webapi/tdnet/list/{date}.json?limit=1000"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 backlog-signal-daily/1.0"})

EARNINGS_RX = re.compile(
    r"決算短信|決算説明|決算補足|決算概要|業績予想|業績.*修正|通期業績|四半期決算|中間決算|決算発表|決算資料|決算説明会|Financial Results",
    re.I,
)
DOC_RX = re.compile(r"決算|業績|受注|説明資料|補足資料|説明会|Financial|Results|Presentation", re.I)
BACKLOG_RX = re.compile(r"受注残(?:高)?|受注高|受注額|受注状況|受注実績|手持(?:工事|工事高)|受注工事高|Book[- ]?to[- ]?Bill|BBレシオ|B/B", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").replace("〜", "~").replace("～", "~")
    s = s.replace("△", "-").replace("▲", "-")
    s = re.sub(r"[\t\u00a0]+", " ", s)
    s = re.sub(r" {2,}", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def items_from(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, dict) and isinstance(obj.get("items"), list):
        return [x for x in obj["items"] if isinstance(x, dict)]
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    return []


def lower_get(d: dict[str, Any], *names: str) -> str:
    m = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = m.get(n.lower())
        if v not in (None, ""):
            return str(v)
    return ""


def canonical(item: dict[str, Any], day: dt.date) -> dict[str, str]:
    raw = item
    for k, v in item.items():
        if str(k).lower() == "tdnet" and isinstance(v, dict):
            raw = v
            break
    url = lower_get(raw, "document_url", "source_url", "pdf_url", "url", "link")
    if "rd.php?" in url:
        url = unquote(url.split("?", 1)[1])
    pubdate = lower_get(raw, "pubdate", "published_at", "datetime", "time")
    dd, tt = day.isoformat(), ""
    if pubdate:
        p = pubdate.strip().split()
        if p and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", p[0]):
            dd = p[0]
        if len(p) > 1:
            tt = p[1][:5]
    file_id = ""
    m = re.search(r"/inbs/(\d{18})\.pdf", url)
    if m:
        file_id = m.group(1)
    if not file_id:
        file_id = lower_get(raw, "id", "file_id", "document_id", "tdnet_id")
    return {
        "date": dd,
        "time": tt,
        "code": lower_get(raw, "company_code", "ticker", "code", "stock_code", "security_code"),
        "company": lower_get(raw, "company_name", "company", "name", "issuer_name"),
        "title": lower_get(raw, "title", "subject", "document_name"),
        "source_url": url,
        "file_id": file_id,
    }


def fetch_day(day: dt.date) -> tuple[dt.date, list[dict[str, str]], str | None]:
    err = None
    for n in range(4):
        try:
            r = S.get(API.format(date=day.strftime("%Y%m%d")), timeout=60)
            if r.status_code == 404:
                return day, [], None
            r.raise_for_status()
            rows = [canonical(x, day) for x in items_from(r.json())]
            return day, rows, None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            time.sleep(n + 1)
    return day, [], err


def get_pdf_text(row: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = dict(row)
    url = row.get("source_url", "")
    if not url:
        out.update(download_status="NO_URL", text="")
        return out
    err = None
    for n in range(4):
        try:
            r = S.get(url, timeout=90)
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                doc = fitz.open(stream=r.content, filetype="pdf")
                text = norm("\n\f\n".join(p.get_text("text") for p in doc))
                out.update(
                    download_status="OK",
                    pdf_sha256=hashlib.sha256(r.content).hexdigest(),
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    pages=len(doc),
                    text=text,
                )
                return out
            err = f"HTTP_{r.status_code}"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        time.sleep(n + 1)
    out.update(download_status="FAILED", error=err, text="")
    return out


def snippets(text: str) -> list[str]:
    out: list[str] = []
    for m in BACKLOG_RX.finditer(text):
        s = re.sub(r"\s+", " ", text[max(0, m.start()-240):m.end()+520]).strip()
        if s not in out:
            out.append(s)
        if len(out) >= 12:
            break
    return out


def num_tokens(s: str) -> list[str]:
    return re.findall(r"(?<!\d)(?:-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*(?:百万円|千円|億円|円|株|%|倍)?", s)


def scan(start: dt.date, end: dt.date, outdir: Path, workers: int) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    days = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += dt.timedelta(days=1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, 12)) as ex:
        day_results = list(ex.map(fetch_day, days))

    all_rows: list[dict[str, str]] = []
    source_audit = []
    for d, rows, err in sorted(day_results, key=lambda x: x[0]):
        all_rows.extend(rows)
        source_audit.append({"date": d.isoformat(), "disclosures": len(rows), "status": "ERROR" if err else "OK", "error": err})

    earnings_rows = [r for r in all_rows if EARNINGS_RX.search(r.get("title", ""))]
    universe_codes = {r["code"] for r in earnings_rows if r.get("code")}
    universe = {}
    for r in earnings_rows:
        if r.get("code"):
            universe.setdefault(r["code"], {"code": r["code"], "company": r.get("company", ""), "first_date": r["date"], "earnings_titles": []})
            universe[r["code"]]["earnings_titles"].append(f"{r['date']} {r.get('time','')} {r.get('title','')}")

    scan_docs = [r for r in all_rows if r.get("code") in universe_codes and DOC_RX.search(r.get("title", ""))]
    fetched: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(get_pdf_text, r) for r in scan_docs]
        for f in concurrent.futures.as_completed(futs):
            fetched.append(f.result())

    hits = []
    company_hits = defaultdict(list)
    for r in fetched:
        text = r.pop("text", "")
        ss = snippets(text)
        if ss:
            rec = {**r, "snippet_count": len(ss), "snippets": ss, "numeric_tokens": [num_tokens(x) for x in ss]}
            hits.append(rec)
            company_hits[r.get("code", "")].append(rec)

    universe_rows = []
    for code, u in sorted(universe.items()):
        h = company_hits.get(code, [])
        universe_rows.append({
            "code": code,
            "company": u["company"],
            "first_date": u["first_date"],
            "earnings_disclosure_count": len(u["earnings_titles"]),
            "earnings_titles": " || ".join(u["earnings_titles"]),
            "backlog_hit_docs": len(h),
            "backlog_terms_found": bool(h),
        })

    def write_csv(path: Path, rows: list[dict[str, Any]]):
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        keys = []
        seen = set()
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k); keys.append(k)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in r.items()})

    write_csv(outdir / "universe.csv", universe_rows)
    write_csv(outdir / "backlog_hits.csv", [{k:v for k,v in r.items() if k not in {"snippets","numeric_tokens"}} for r in hits])
    with (outdir / "backlog_hits.jsonl").open("w", encoding="utf-8") as f:
        for r in sorted(hits, key=lambda x:(x.get("date",""), x.get("code",""), x.get("time",""))):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (outdir / "source_audit.json").write_text(json.dumps(source_audit, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "start": start.isoformat(), "end": end.isoformat(),
        "calendar_days": len(days),
        "api_days_ok": sum(x["status"] == "OK" for x in source_audit),
        "api_days_error": sum(x["status"] == "ERROR" for x in source_audit),
        "all_disclosures": len(all_rows),
        "earnings_related_disclosures": len(earnings_rows),
        "earnings_universe_companies": len(universe),
        "scanned_documents": len(scan_docs),
        "download_status": dict(Counter(r.get("download_status", "") for r in fetched)),
        "backlog_hit_documents": len(hits),
        "backlog_hit_companies": len({r.get("code") for r in hits if r.get("code")}),
        "hit_companies": sorted([{"code": c, "company": universe.get(c,{}).get("company", ""), "docs": len(rs)} for c, rs in company_hits.items()], key=lambda x:(-x["docs"], x["code"])),
    }
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=20)
    a = ap.parse_args()
    scan(dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end), Path(a.out), a.workers)

if __name__ == "__main__":
    main()
