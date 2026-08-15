from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import backlog_history_collection_v5 as collector
import backlog_structured_parser_v6 as parser


CATR_PDF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Referer": "https://catr.jp/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-site",
    "Upgrade-Insecure-Requests": "1",
}


def mixed_money_to_million(text: str | None) -> float | None:
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text).replace(",", "").replace(" ", "")
    units = {
        "兆円": 1_000_000.0,
        "億円": 100.0,
        "億": 100.0,
        "百万円": 1.0,
        "千円": 0.001,
        "万円": 0.01,
        "円": 0.000001,
    }
    total = 0.0
    matches = list(
        re.finditer(
            r"([0-9]+(?:\.[0-9]+)?)(兆円|億円|億|百万円|千円|万円|円)",
            normalized,
        )
    )
    if not matches:
        return None
    for match in matches:
        total += float(match.group(1)) * units[match.group(2)]
    return total


def fetch_pdf_with_catr_headers(
    detail: dict[str, Any],
) -> tuple[bytes | None, str | None, list[dict[str, Any]]]:
    queue: list[str] = []
    for key in ["pdf_url", "detail_url"]:
        value = detail.get(key)
        if value:
            queue.append(str(value))
    queue.extend(str(value) for value in detail.get("pdf_candidates") or [] if value)
    seen: set[str] = set()
    audit: list[dict[str, Any]] = []
    while queue:
        url = queue.pop(0)
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            headers = CATR_PDF_HEADERS if "pdf.catr.jp" in url else None
            response = collector.base.get(url, headers=headers)
            audit.append(
                {
                    "url": url,
                    "final_url": response.url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "bytes": len(response.content),
                    "catr_browser_headers": bool(headers),
                }
            )
            pdf = collector._extract_pdf(response.content)
            if pdf is not None:
                return pdf, response.url, audit
            if response.content.lstrip().startswith(b"<"):
                soup = BeautifulSoup(response.text, "html.parser")
                for tag in soup.find_all(["a", "iframe", "embed", "object"]):
                    href = tag.get("href") or tag.get("src") or tag.get("data")
                    if not href:
                        continue
                    resolved = urljoin(response.url, href)
                    if "pdf" in resolved.lower() or "tdnet" in resolved.lower():
                        queue.append(resolved)
        except Exception as exc:
            audit.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
    return None, None, audit


collector.parser = parser
collector.inventory.money_to_million = mixed_money_to_million
collector.fetch_pdf = fetch_pdf_with_catr_headers

if __name__ == "__main__":
    collector.base.main()
