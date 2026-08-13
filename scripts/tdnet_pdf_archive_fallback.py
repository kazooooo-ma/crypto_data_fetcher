from __future__ import annotations

import functools
import json
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import requests

ARCHIVE_MANIFEST = "https://raw.githubusercontent.com/yukizi1113/tdnet/main/tekigikaizi/{date}/manifest.json"
ARCHIVE_RAW = "https://raw.githubusercontent.com/yukizi1113/tdnet/main/{path}"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 important-event-migration/3.0"})
_LOCK = threading.Lock()


def _get(url: str, timeout: int = 90) -> requests.Response | None:
    try:
        return _SESSION.get(url, timeout=timeout, allow_redirects=True)
    except Exception:
        return None


@functools.lru_cache(maxsize=800)
def load_manifest(date_yyyymmdd: str) -> tuple[dict[str, Any] | None, str | None]:
    url = ARCHIVE_MANIFEST.format(date=date_yyyymmdd)
    response = _get(url, 45)
    if response is None:
        return None, "ARCHIVE_MANIFEST_REQUEST_FAILED"
    if response.status_code == 404:
        return None, "ARCHIVE_MANIFEST_NOT_FOUND"
    if response.status_code != 200:
        return None, f"ARCHIVE_MANIFEST_HTTP_{response.status_code}"
    try:
        return response.json(), None
    except Exception as exc:
        return None, f"ARCHIVE_MANIFEST_JSON_{type(exc).__name__}"


def _file_id_from_url(url: str) -> str:
    match = re.search(r"/inbs/(\d{18})\.pdf", url or "")
    return match.group(1) if match else ""


def _archive_path(record: dict[str, Any]) -> tuple[str | None, str | None]:
    date = str(record.get("disclosure_date") or "")[:10].replace("-", "")
    file_id = str(record.get("file_id") or record.get("candidate_id") or "")
    if not re.fullmatch(r"\d{18}", file_id):
        file_id = _file_id_from_url(str(record.get("source_url") or ""))
    if not date or not file_id:
        return None, "ARCHIVE_LOOKUP_KEY_MISSING"
    manifest, err = load_manifest(date)
    if manifest is None:
        return None, err
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, list):
        return None, "ARCHIVE_MANIFEST_ITEMS_MISSING"
    for item in items:
        if str(item.get("file_id") or "") == file_id:
            path = item.get("github_path")
            if path:
                return str(path), None
    return None, "ARCHIVE_FILE_ID_NOT_FOUND"


def download_record_pdf(record: dict[str, Any]) -> tuple[bytes | None, str | None, str | None]:
    """Return bytes, error, source label.

    Direct TDnet is attempted once. When it is unavailable, the archived raw PDF
    is used if a daily manifest exists in yukizi1113/tdnet.
    """
    direct_url = str(record.get("source_url") or "")
    if direct_url:
        response = _get(direct_url)
        if response is not None and response.status_code == 200 and response.content.startswith(b"%PDF"):
            return response.content, None, "TDNET_DIRECT"
        direct_error = (
            "TDNET_REQUEST_FAILED" if response is None else f"TDNET_HTTP_{response.status_code}"
        )
    else:
        direct_error = "TDNET_URL_MISSING"

    path, archive_error = _archive_path(record)
    if path:
        raw_url = ARCHIVE_RAW.format(path=quote(path, safe="/()[]-_.~"))
        response = _get(raw_url)
        if response is not None and response.status_code == 200 and response.content.startswith(b"%PDF"):
            return response.content, None, "GITHUB_TDNET_ARCHIVE"
        archive_error = (
            "ARCHIVE_REQUEST_FAILED" if response is None else f"ARCHIVE_HTTP_{response.status_code}"
        )
    return None, f"{direct_error};{archive_error}", None
