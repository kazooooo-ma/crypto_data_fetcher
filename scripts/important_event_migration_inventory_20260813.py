from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import requests

START = dt.date(2025, 1, 1)
END = dt.date(2026, 8, 12)
OUT = Path("out_important_event_inventory")
OUT.mkdir(parents=True, exist_ok=True)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 important-event-migration/1.0"})

ROUTINE_COMPENSATION = re.compile(
    r"ストック.?オプション|株式報酬|譲渡制限付株式|役員報酬|従業員持株会|業績連動型株式報酬"
)

PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("BUYBACK", "BUYBACK", re.compile(r"自己株式(?:の)?取得|自己株買い")),
    ("BUYBACK", "CANCELLATION", re.compile(r"自己株式.*(?:取得)?(?:中止|取消|撤回)")),
    ("BUYBACK", "COMPLETION", re.compile(r"自己株式.*(?:取得終了|取得完了|取得結果|取得状況及び取得終了)")),
    ("BUYBACK", "PROGRESS", re.compile(r"自己株式.*取得状況")),
    ("BUYBACK", "RETIREMENT", re.compile(r"自己株式.*消却")),
    ("OFFERING_SUPPLY", "PUBLIC_OFFERING", re.compile(r"公募.*(?:新株|増資)|募集による新株式発行|海外募集")),
    ("OFFERING_SUPPLY", "SECONDARY_OFFERING", re.compile(r"株式の売出し|売出価格|売出しに関する")),
    ("OFFERING_SUPPLY", "THIRD_PARTY_ALLOTMENT", re.compile(r"第三者割当.*(?:新株|株式|増資)")),
    ("CB_WARRANT", "WARRANT", re.compile(r"新株予約権")),
    ("CB_WARRANT", "CONVERTIBLE_BOND", re.compile(r"転換社債|転換社債型新株予約権付社債|ＣＢ|CB")),
    ("CB_WARRANT", "EXERCISE_PROGRESS", re.compile(r"(?:新株予約権|転換社債).*(?:行使状況|月間行使状況|大量行使)")),
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


def get_json(url: str, timeout: int = 45):
    response = SESSION.get(url, timeout=timeout)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def fetch_manifest(day: dt.date):
    date_raw = day.strftime("%Y%m%d")
    url = f"https://raw.githubusercontent.com/yukizi1113/tdnet/main/tekigikaizi/{date_raw}/manifest.json"
    try:
        return date_raw, get_json(url), None
    except Exception as exc:  # noqa: BLE001
        return date_raw, None, f"{type(exc).__name__}: {exc}"


def lifecycle_stage(title: str, event_type: str, subtype: str) -> str:
    if re.search(r"中止|取消|撤回", title):
        return "CANCELLATION"
    if re.search(r"完了|終了|結果", title):
        return "COMPLETION"
    if re.search(r"取得状況|行使状況|大量行使|進捗", title):
        return "PROGRESS"
    if re.search(r"開始|買付けの開始|公開買付け開始", title):
        return "START"
    if subtype in {"RETIREMENT", "SPLIT", "CONSOLIDATION", "SHARE_EXCHANGE", "SHARE_TRANSFER", "MERGER"}:
        return "AUTHORIZATION"
    return "AUTHORIZATION"


def classify(title: str) -> tuple[str, str, str, str] | None:
    matches: list[tuple[str, str]] = []
    for event_type, subtype, pattern in PATTERNS:
        if pattern.search(title):
            matches.append((event_type, subtype))
    if not matches:
        return None

    event_type, subtype = matches[0]
    if event_type == "BUYBACK":
        if re.search(r"中止|取消|撤回", title):
            subtype = "CANCELLATION"
        elif re.search(r"取得終了|取得完了|取得結果|取得状況及び取得終了", title):
            subtype = "COMPLETION"
        elif re.search(r"取得状況", title):
            subtype = "PROGRESS"
        elif re.search(r"消却", title):
            subtype = "RETIREMENT"
        else:
            subtype = "AUTHORIZATION"

    routine = bool(ROUTINE_COMPENSATION.search(title))
    if routine and event_type in {"CB_WARRANT", "OFFERING_SUPPLY"}:
        materiality_scope = "ROUTINE_COMPENSATION_EXCLUDED"
    else:
        materiality_scope = "MIGRATION_CANDIDATE"

    stage = lifecycle_stage(title, event_type, subtype)
    return event_type, subtype, stage, materiality_scope


def direction(event_type: str, subtype: str) -> str:
    if event_type == "BUYBACK" and subtype not in {"CANCELLATION"}:
        return "DEMAND"
    if event_type in {"OFFERING_SUPPLY", "CB_WARRANT", "HOLDER_SALE"}:
        return "SUPPLY"
    if event_type == "TOB_MBO":
        return "DEMAND_OR_EXIT"
    return "CORPORATE_ACTION"


def stable_candidate_id(item: dict, date_raw: str, event_type: str, subtype: str) -> str:
    file_id = str(item.get("file_id") or "")
    if file_id:
        return file_id
    basis = "|".join(
        [date_raw, str(item.get("ticker") or ""), event_type, subtype, str(item.get("title") or "")]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def main():
    manifest_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        for result in executor.map(fetch_manifest, list(iter_dates())):
            manifest_results.append(result)
    manifest_results.sort()

    candidates: list[dict] = []
    audit_rows: list[dict] = []
    monthly = defaultdict(lambda: {
        "manifest_days": 0,
        "missing_manifest_days": 0,
        "disclosures": 0,
        "candidates": 0,
        "routine_compensation_excluded": 0,
        "by_event_type": Counter(),
        "by_lifecycle_stage": Counter(),
    })
    duplicate_ids = Counter()

    for date_raw, manifest, error in manifest_results:
        month = f"{date_raw[:4]}-{date_raw[4:6]}"
        if error:
            audit_rows.append({"date": date_raw, "status": "MANIFEST_ERROR", "error": error})
            monthly[month]["missing_manifest_days"] += 1
            continue
        if not manifest:
            audit_rows.append({"date": date_raw, "status": "NO_MANIFEST"})
            monthly[month]["missing_manifest_days"] += 1
            continue

        items = manifest.get("items") or []
        monthly[month]["manifest_days"] += 1
        monthly[month]["disclosures"] += len(items)
        audit_rows.append({"date": date_raw, "status": "MANIFEST_OK", "items": len(items)})

        for item in items:
            title = str(item.get("title") or "")
            classified = classify(title)
            if not classified:
                continue
            event_type, subtype, stage, materiality_scope = classified
            candidate_id = stable_candidate_id(item, date_raw, event_type, subtype)
            duplicate_ids[candidate_id] += 1
            record = {
                "candidate_id": candidate_id,
                "disclosure_date": f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}",
                "disclosure_time": item.get("time") or "",
                "code": str(item.get("ticker") or ""),
                "company": item.get("company") or "",
                "title": title,
                "event_type": event_type,
                "event_subtype": subtype,
                "lifecycle_stage": stage,
                "direction": direction(event_type, subtype),
                "materiality_scope": materiality_scope,
                "file_id": str(item.get("file_id") or ""),
                "source_url": item.get("source_url") or "",
                "github_path": item.get("github_path") or "",
                "manifest_status": item.get("status") or "",
                "month": month,
            }
            candidates.append(record)
            monthly[month]["candidates"] += 1
            monthly[month]["by_event_type"][event_type] += 1
            monthly[month]["by_lifecycle_stage"][stage] += 1
            if materiality_scope == "ROUTINE_COMPENSATION_EXCLUDED":
                monthly[month]["routine_compensation_excluded"] += 1

    candidates.sort(key=lambda r: (r["disclosure_date"], r["disclosure_time"], r["code"], r["candidate_id"]))
    unique_candidates = []
    seen = set()
    for record in candidates:
        if record["candidate_id"] in seen:
            continue
        seen.add(record["candidate_id"])
        unique_candidates.append(record)

    with (OUT / "candidates.jsonl").open("w", encoding="utf-8") as handle:
        for record in unique_candidates:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    monthly_rows = []
    for month in sorted(monthly):
        value = monthly[month]
        monthly_rows.append({
            "month": month,
            "manifest_days": value["manifest_days"],
            "missing_manifest_days": value["missing_manifest_days"],
            "disclosures": value["disclosures"],
            "candidates": value["candidates"],
            "routine_compensation_excluded": value["routine_compensation_excluded"],
            "by_event_type": dict(value["by_event_type"]),
            "by_lifecycle_stage": dict(value["by_lifecycle_stage"]),
        })
    (OUT / "monthly_summary.json").write_text(
        json.dumps(monthly_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "manifest_audit.json").write_text(
        json.dumps(audit_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    enechange = [
        r for r in unique_candidates
        if r["code"] == "4169" and r["disclosure_date"] == "2026-06-22" and r["event_type"] == "BUYBACK"
    ]
    enechange_regression = {
        "expected": {
            "code": "4169",
            "date": "2026-06-22",
            "time": "12:00",
            "file_id": "140120260619574629",
        },
        "found": enechange,
        "pass": any(
            r.get("disclosure_time") == "12:00" and r.get("file_id") == "140120260619574629"
            for r in enechange
        ),
    }
    (OUT / "enechange_regression.json").write_text(
        json.dumps(enechange_regression, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    event_counts = Counter(r["event_type"] for r in unique_candidates)
    stage_counts = Counter(r["lifecycle_stage"] for r in unique_candidates)
    summary = {
        "period": [START.isoformat(), END.isoformat()],
        "manifest_days_ok": sum(1 for r in audit_rows if r.get("status") == "MANIFEST_OK"),
        "manifest_days_missing_or_error": sum(1 for r in audit_rows if r.get("status") != "MANIFEST_OK"),
        "disclosures": sum(row["disclosures"] for row in monthly_rows),
        "candidates_raw": len(candidates),
        "candidates_unique": len(unique_candidates),
        "routine_compensation_excluded": sum(
            1 for r in unique_candidates if r["materiality_scope"] == "ROUTINE_COMPENSATION_EXCLUDED"
        ),
        "duplicate_candidate_ids": sum(1 for count in duplicate_ids.values() if count > 1),
        "event_type_counts": dict(event_counts),
        "lifecycle_stage_counts": dict(stage_counts),
        "enechange_regression_pass": enechange_regression["pass"],
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
