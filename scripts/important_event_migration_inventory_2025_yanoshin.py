from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

START = dt.date(2025, 1, 1)
END = dt.date(2025, 12, 31)
OUT = Path("out_important_event_inventory_2025")
OUT.mkdir(parents=True, exist_ok=True)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 important-event-migration/1.0"})

ROUTINE_COMPENSATION = re.compile(
    r"ストック.?オプション|株式報酬|譲渡制限付株式|役員報酬|従業員持株会|業績連動型株式報酬"
)
PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("BUYBACK", "BUYBACK", re.compile(r"自己株式(?:の)?取得|自己株買い|自己株式の市場買付|ToSTNeT-3.*自己株式")),
    ("BUYBACK", "RETIREMENT", re.compile(r"自己株式.*消却")),
    ("OFFERING_SUPPLY", "PUBLIC_OFFERING", re.compile(r"公募.*(?:新株|増資)|募集による新株式発行|海外募集")),
    ("OFFERING_SUPPLY", "SECONDARY_OFFERING", re.compile(r"株式の売出し|売出価格|売出しに関する")),
    ("OFFERING_SUPPLY", "THIRD_PARTY_ALLOTMENT", re.compile(r"第三者割当.*(?:新株|株式|増資)")),
    ("CB_WARRANT", "WARRANT", re.compile(r"新株予約権")),
    ("CB_WARRANT", "CONVERTIBLE_BOND", re.compile(r"転換社債|転換社債型新株予約権付社債|ＣＢ|CB")),
    ("TOB_MBO", "TOB", re.compile(r"公開買付|TOB|ＴＯＢ")),
    ("TOB_MBO", "MBO", re.compile(r"MBO|ＭＢＯ|マネジメント.?バイアウト")),
    ("REORGANIZATION", "SHARE_EXCHANGE", re.compile(r"株式交換")),
    ("REORGANIZATION", "SHARE_TRANSFER", re.compile(r"株式移転")),
    ("REORGANIZATION", "MERGER", re.compile(r"吸収合併|新設合併|合併契約")),
    ("REORGANIZATION", "FULL_SUBSIDIARY", re.compile(r"完全子会社化")),
    ("HOLDER_SALE", "DISTRIBUTION", re.compile(r"立会外分売")),
    ("HOLDER_SALE", "MAJOR_HOLDER_CHANGE", re.compile(r"主要株主.*異動|大株主.*異動|筆頭株主.*異動")),
    ("HOLDER_SALE", "HOLDER_SALE", re.compile(r"(?:大株主|主要株主|保有株式).*(?:売却|売出し)|株式売却意向")),
    ("SPLIT_CONSOLIDATION", "SPLIT", re.compile(r"株式分割")),
    ("SPLIT_CONSOLIDATION", "CONSOLIDATION", re.compile(r"株式併合")),
    ("INDEX_ACTION", "INDEX", re.compile(r"TOPIX|ＴＯＰＩＸ|日経平均|JPX日経|指数採用|指数除外")),
]


def iter_dates():
    day = START
    while day <= END:
        yield day
        day += dt.timedelta(days=1)


def classify(title: str):
    matches = [(event_type, subtype) for event_type, subtype, p in PATTERNS if p.search(title)]
    if not matches:
        return None
    event_type, subtype = matches[0]
    if event_type == "BUYBACK":
        if re.search(r"中止|取消|撤回", title):
            subtype = "CANCELLATION"
        elif re.search(r"取得終了|取得完了|取得結果|取得状況及び取得終了|取得状況および取得終了", title):
            subtype = "COMPLETION"
        elif re.search(r"取得状況|市場買付", title):
            subtype = "PROGRESS"
        elif re.search(r"消却", title):
            subtype = "RETIREMENT"
        else:
            subtype = "AUTHORIZATION"
    routine = bool(ROUTINE_COMPENSATION.search(title))
    materiality_scope = (
        "ROUTINE_COMPENSATION_EXCLUDED"
        if routine and event_type in {"CB_WARRANT", "OFFERING_SUPPLY"}
        else "MIGRATION_CANDIDATE"
    )
    if re.search(r"中止|取消|撤回", title):
        stage = "CANCELLATION"
    elif re.search(r"完了|終了|結果", title):
        stage = "COMPLETION"
    elif re.search(r"取得状況|行使状況|大量行使|市場買付|進捗", title):
        stage = "PROGRESS"
    elif re.search(r"開始|買付けの開始", title):
        stage = "START"
    else:
        stage = "AUTHORIZATION"
    return event_type, subtype, stage, materiality_scope


def direction(event_type: str, subtype: str) -> str:
    if event_type == "BUYBACK" and subtype != "CANCELLATION":
        return "DEMAND"
    if event_type in {"OFFERING_SUPPLY", "CB_WARRANT", "HOLDER_SALE"}:
        return "SUPPLY"
    if event_type == "TOB_MBO":
        return "DEMAND_OR_EXIT"
    return "CORPORATE_ACTION"


def decode_source_url(href: str) -> str:
    absolute = urljoin("https://contents.webapi.yanoshin.jp", href)
    decoded = unquote(absolute)
    marker = "webapi.yanoshin.jp/rd.php?"
    if marker in decoded:
        candidate = decoded.split(marker, 1)[1]
        while candidate.endswith("="):
            candidate = candidate[:-1]
        return candidate
    if "release.tdnet.info" in decoded and decoded.endswith(".pdf"):
        return decoded
    return absolute


def fetch_day(day: dt.date):
    date_raw = day.strftime("%Y%m%d")
    url = f"https://contents.webapi.yanoshin.jp/contents/tdnet_date/{date_raw}"
    try:
        response = SESSION.get(url, timeout=45)
        if response.status_code == 404:
            return date_raw, [], "NO_PAGE", url
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 3:
                continue
            time_text = cells[0].get_text(" ", strip=True)
            company_text = cells[1].get_text(" ", strip=True)
            title_text = cells[2].get_text(" ", strip=True)
            if not re.fullmatch(r"\d{1,2}:\d{2}", time_text):
                continue
            match = re.search(r"[（(]([0-9]{4}|[0-9]{3}[A-Z])[）)]", company_text)
            if not match:
                continue
            code = match.group(1)
            company = re.sub(r"\s*[（(](?:[0-9]{4}|[0-9]{3}[A-Z])[）)]\s*$", "", company_text).strip()
            title_link = cells[2].find("a", href=True)
            href = title_link.get("href") if title_link else ""
            source_url = decode_source_url(href) if href else ""
            file_match = re.search(r"/(\d+)\.pdf", source_url)
            file_id = file_match.group(1) if file_match else ""
            records.append({
                "disclosure_date": day.isoformat(),
                "disclosure_time": time_text,
                "code": code,
                "company": company,
                "title": title_text,
                "file_id": file_id,
                "source_url": source_url,
                "source_page": url,
            })
        return date_raw, records, None, url
    except Exception as exc:  # noqa: BLE001
        return date_raw, [], f"{type(exc).__name__}: {exc}", url


def candidate_id(record: dict, event_type: str, subtype: str) -> str:
    if record.get("file_id"):
        return record["file_id"]
    basis = "|".join([
        record["disclosure_date"], record["code"], event_type, subtype, record["title"]
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def main():
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for result in executor.map(fetch_day, list(iter_dates())):
            results.append(result)
    results.sort()

    candidates = []
    audit = []
    monthly = defaultdict(lambda: {
        "pages_ok": 0,
        "pages_missing_or_error": 0,
        "disclosures": 0,
        "candidates": 0,
        "routine_compensation_excluded": 0,
        "by_event_type": Counter(),
        "by_lifecycle_stage": Counter(),
    })

    for date_raw, records, error, url in results:
        month = f"{date_raw[:4]}-{date_raw[4:6]}"
        if error:
            monthly[month]["pages_missing_or_error"] += 1
            audit.append({"date": date_raw, "status": error, "url": url})
            continue
        monthly[month]["pages_ok"] += 1
        monthly[month]["disclosures"] += len(records)
        audit.append({"date": date_raw, "status": "PAGE_OK", "items": len(records), "url": url})
        for record in records:
            classified = classify(record["title"])
            if not classified:
                continue
            event_type, subtype, stage, materiality_scope = classified
            out = {
                "candidate_id": candidate_id(record, event_type, subtype),
                **record,
                "event_type": event_type,
                "event_subtype": subtype,
                "lifecycle_stage": stage,
                "direction": direction(event_type, subtype),
                "materiality_scope": materiality_scope,
                "manifest_status": "YANOSHIN_ARCHIVE",
                "month": month,
            }
            candidates.append(out)
            monthly[month]["candidates"] += 1
            monthly[month]["by_event_type"][event_type] += 1
            monthly[month]["by_lifecycle_stage"][stage] += 1
            if materiality_scope == "ROUTINE_COMPENSATION_EXCLUDED":
                monthly[month]["routine_compensation_excluded"] += 1

    candidates.sort(key=lambda r: (r["disclosure_date"], r["disclosure_time"], r["code"], r["candidate_id"]))
    unique = []
    seen = set()
    for record in candidates:
        if record["candidate_id"] in seen:
            continue
        seen.add(record["candidate_id"])
        unique.append(record)

    with (OUT / "candidates_2025.jsonl").open("w", encoding="utf-8") as handle:
        for record in unique:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    monthly_rows = []
    for month in sorted(monthly):
        value = monthly[month]
        monthly_rows.append({
            "month": month,
            "pages_ok": value["pages_ok"],
            "pages_missing_or_error": value["pages_missing_or_error"],
            "disclosures": value["disclosures"],
            "candidates": value["candidates"],
            "routine_compensation_excluded": value["routine_compensation_excluded"],
            "by_event_type": dict(value["by_event_type"]),
            "by_lifecycle_stage": dict(value["by_lifecycle_stage"]),
        })
    (OUT / "monthly_summary_2025.json").write_text(
        json.dumps(monthly_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "page_audit_2025.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    counts = Counter(r["event_type"] for r in unique)
    summary = {
        "period": [START.isoformat(), END.isoformat()],
        "pages_ok": sum(1 for r in audit if r.get("status") == "PAGE_OK"),
        "pages_missing_or_error": sum(1 for r in audit if r.get("status") != "PAGE_OK"),
        "disclosures": sum(r["disclosures"] for r in monthly_rows),
        "candidates_unique": len(unique),
        "routine_compensation_excluded": sum(
            1 for r in unique if r["materiality_scope"] == "ROUTINE_COMPENSATION_EXCLUDED"
        ),
        "event_type_counts": dict(counts),
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (OUT / "summary_2025.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
