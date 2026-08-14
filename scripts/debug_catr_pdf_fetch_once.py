from __future__ import annotations

import json
from pathlib import Path

import requests

OUT = Path("out/debug-catr-pdf")
DETAIL = "https://disclosure.catr.jp/companies/a6384/42410/tdnet/9801c/681174"
PDF = "https://pdf.catr.jp/2025/07/0000681174_3ef6c31de92c4f85802ea28ffc1f2493.pdf"


def record(session: requests.Session, label: str, url: str, headers: dict[str, str] | None = None) -> dict:
    try:
        response = session.get(url, timeout=60, allow_redirects=True, headers=headers or {})
        return {
            "label": label,
            "url": url,
            "status": response.status_code,
            "final_url": response.url,
            "history": [{"status": item.status_code, "url": item.url, "location": item.headers.get("location")} for item in response.history],
            "headers": dict(response.headers),
            "cookies": session.cookies.get_dict(),
            "bytes": len(response.content),
            "first_hex": response.content[:64].hex(),
            "first_text": response.content[:1000].decode("utf-8", errors="replace"),
        }
    except Exception as exc:
        return {"label": label, "url": url, "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    browser = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    }
    session = requests.Session()
    results = []
    results.append(record(session, "plain", PDF))
    results.append(record(session, "browser", PDF, browser))
    results.append(record(session, "detail", DETAIL, browser))
    referer_headers = {**browser, "Referer": DETAIL, "Sec-Fetch-Site": "same-site", "Sec-Fetch-Dest": "document"}
    results.append(record(session, "after_detail_referer", PDF, referer_headers))
    results.append(record(session, "download_query", PDF + "?download=1", referer_headers))
    results.append(record(session, "raw_query", PDF + "?raw=1", referer_headers))
    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
