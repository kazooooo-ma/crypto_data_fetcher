from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import unquote

import important_event_buyback_p2_p3_once as core


def items_from(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, dict) and isinstance(obj.get("items"), list):
        return [x for x in obj["items"] if isinstance(x, dict)]
    return core.items_from(obj)


def canonical(item: dict[str, Any], day):
    raw = item
    for key, value in item.items():
        if str(key).lower() == "tdnet" and isinstance(value, dict):
            raw = value
            break

    api_id = core.first(raw, ["id", "file_id", "document_id", "tdnet_id"])
    url = core.first(raw, ["document_url", "source_url", "pdf_url", "url", "link"])
    if "rd.php?" in url:
        url = unquote(url.split("?", 1)[1])
    official = re.search(r"/inbs/(\d{18})\.pdf", url)
    file_id = official.group(1) if official else api_id
    if not url and re.fullmatch(r"\d{18}", file_id):
        url = f"https://www.release.tdnet.info/inbs/{file_id}.pdf"

    pubdate = core.first(raw, ["pubdate", "published_at", "datetime", "time", "disclosure_time"])
    disclosure_date = day.isoformat()
    disclosure_time = ""
    if pubdate:
        parts = str(pubdate).strip().split()
        if parts and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", parts[0]):
            disclosure_date = parts[0]
        if len(parts) > 1:
            disclosure_time = parts[1][:5]
        elif re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", parts[0]):
            disclosure_time = parts[0][:5]

    candidate_id = file_id or hashlib.sha256(
        json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:24]
    return {
        "candidate_id": candidate_id,
        "file_id": file_id,
        "api_id": api_id,
        "disclosure_date": disclosure_date,
        "disclosure_time": disclosure_time,
        "code": core.first(raw, ["company_code", "ticker", "code", "stock_code", "security_code"]),
        "company": core.first(raw, ["company_name", "company", "name", "issuer_name"]),
        "title": core.first(raw, ["title", "subject", "disclosure_title", "document_name"]),
        "source_url": url,
        "markets_string": core.first(raw, ["markets_string", "market"]),
    }


core.items_from = items_from
core.canonical = canonical

if __name__ == "__main__":
    core.main()
