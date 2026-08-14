from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin

import fitz
import requests
from bs4 import BeautifulSoup

FIXTURES = Path("data/backlog_v22_gold_fixtures.json")
OUT = Path("out/backlog-table-probe")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
})
BACKLOG_RE = re.compile(r"受注残|繰越受注|手持工事|受注済残|注文残|backlog", re.I)
ORDER_RE = re.compile(r"受注高|受注額|新規受注|orders?", re.I)
SALES_RE = re.compile(r"売上高|完成工事高|sales|revenue", re.I)
TOTAL_RE = re.compile(r"合計|総計|全社|連結|total", re.I)
UNIT_RE = re.compile(r"単位\s*[:：]?\s*([^\n\)）]{1,20})|\((百万円|千円|億円|万円)\)|（(百万円|千円|億円|万円)）")
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-△▲]?\s*[0-9][0-9,]*(?:\.[0-9]+)?(?:\s*(?:百万円|千円|億円|万円|円|%))?")


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    text = text.replace("△", "-").replace("▲", "-").replace("〜", "~").replace("～", "~")
    text = re.sub(r"[\t\u00a0]+", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def tdnet_id(url: str) -> str | None:
    m = re.search(r"(\d{18})\.pdf", unquote(url or ""))
    return m.group(1) if m else None


def candidate_urls(url: str) -> list[str]:
    urls = [url]
    fid = tdnet_id(url)
    if fid:
        urls += [
            f"https://www.release.tdnet.info/inbs/{fid}.pdf",
            f"https://tdnet-pdf.kabutan.jp/{fid[:8]}/{fid}.pdf",
        ]
    seen = set()
    return [u for u in urls if u and not (u in seen or seen.add(u))]


def pdf_payload(content: bytes) -> bytes | None:
    pos = content[:4096].find(b"%PDF")
    return content[pos:] if pos >= 0 else None


def fetch_pdf(url: str) -> tuple[bytes | None, str | None, str | None, list[dict[str, Any]]]:
    audit: list[dict[str, Any]] = []
    queue = candidate_urls(url)
    seen: set[str] = set()
    for current in queue:
        if current in seen:
            continue
        seen.add(current)
        for attempt in range(3):
            try:
                response = SESSION.get(current, timeout=90, allow_redirects=True)
                audit.append({
                    "url": current,
                    "final_url": response.url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "bytes": len(response.content),
                })
                if response.status_code != 200:
                    time.sleep(attempt + 1)
                    continue
                payload = pdf_payload(response.content)
                if payload:
                    return payload, response.url, None, audit
                content_type = (response.headers.get("content-type") or "").lower()
                if "html" in content_type or response.content.lstrip().startswith(b"<"):
                    soup = BeautifulSoup(response.text, "html.parser")
                    for tag in soup.find_all(["a", "iframe", "embed"]):
                        href = tag.get("href") or tag.get("src")
                        if href and ("pdf" in href.lower() or "document" in href.lower()):
                            queue.append(urljoin(response.url, href))
                break
            except Exception as exc:
                audit.append({"url": current, "error": f"{type(exc).__name__}: {exc}"})
                time.sleep(attempt + 1)
    return None, None, "PDF_FETCH_FAILED", audit


def page_tables(page: fitz.Page) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    strategies = [("lines", "lines"), ("text", "text")]
    for vertical, horizontal in strategies:
        try:
            finder = page.find_tables(vertical_strategy=vertical, horizontal_strategy=horizontal)
        except Exception:
            continue
        for idx, table in enumerate(finder.tables):
            try:
                rows = [[norm(cell) for cell in row] for row in table.extract()]
            except Exception:
                continue
            rows = [row for row in rows if any(row)]
            joined = "\n".join(" | ".join(row) for row in rows)
            if not BACKLOG_RE.search(joined):
                continue
            fp = hashlib.sha256(joined.encode("utf-8")).hexdigest()
            if fp in fingerprints:
                continue
            fingerprints.add(fp)
            outputs.append({
                "strategy": f"{vertical}/{horizontal}",
                "table_index": idx,
                "bbox": list(table.bbox),
                "rows": rows,
                "has_order": bool(ORDER_RE.search(joined)),
                "has_sales": bool(SALES_RE.search(joined)),
                "has_total": bool(TOTAL_RE.search(joined)),
                "unit_mentions": [norm("".join(x for x in m.groups() if x)) for m in UNIT_RE.finditer(joined)],
            })
    return outputs


def relevant_blocks(page: fitz.Page) -> list[dict[str, Any]]:
    blocks = []
    raw = page.get_text("blocks")
    for i, block in enumerate(raw):
        text = norm(block[4])
        if BACKLOG_RE.search(text):
            blocks.append({"block_index": i, "bbox": list(block[:4]), "text": text})
    return blocks


def text_windows(text: str) -> list[str]:
    windows = []
    for match in BACKLOG_RE.finditer(text):
        window = norm(text[max(0, match.start() - 450): match.end() + 900])
        if window not in windows:
            windows.append(window)
    return windows[:20]


def numeric_tokens(text: str) -> list[str]:
    return [norm(m.group(0)) for m in NUMBER_RE.finditer(text)]


def process(fixture: dict[str, Any]) -> dict[str, Any]:
    result = {"fixture": fixture}
    payload, final_url, error, fetch_audit = fetch_pdf(fixture["source_url"])
    result["fetch_audit"] = fetch_audit
    result["download_url"] = final_url
    if payload is None:
        result["status"] = "PDF_FETCH_FAILED"
        result["error"] = error
        return result
    result["pdf_sha256"] = hashlib.sha256(payload).hexdigest()
    result["pdf_bytes"] = len(payload)
    try:
        doc = fitz.open(stream=payload, filetype="pdf")
    except Exception as exc:
        result["status"] = "PDF_OPEN_FAILED"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    pages = []
    full_text = []
    for page_no, page in enumerate(doc):
        text = page.get_text("text")
        full_text.append(text)
        tables = page_tables(page)
        blocks = relevant_blocks(page)
        if tables or blocks:
            pages.append({
                "page": page_no + 1,
                "tables": tables,
                "blocks": blocks,
            })
    all_text = norm("\n\f\n".join(full_text))
    windows = text_windows(all_text)
    result.update({
        "status": "PROBED",
        "page_count": len(doc),
        "text_chars": len(all_text),
        "pages": pages,
        "text_windows": windows,
        "window_numeric_tokens": [numeric_tokens(w) for w in windows],
    })
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    results = []
    with cf.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(process, fixture) for fixture in fixtures]
        for future in cf.as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda x: (x["fixture"]["ir_date"], x["fixture"]["code"]))
    with (OUT / "probe_results.jsonl").open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "fixtures": len(results),
        "status_counts": {},
        "pdf_downloaded": sum(row.get("status") == "PROBED" for row in results),
        "with_relevant_table": sum(any(p.get("tables") for p in row.get("pages", [])) for row in results),
        "with_relevant_block": sum(any(p.get("blocks") for p in row.get("pages", [])) for row in results),
    }
    for row in results:
        summary["status_counts"][row.get("status", "UNKNOWN")] = summary["status_counts"].get(row.get("status", "UNKNOWN"), 0) + 1
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
