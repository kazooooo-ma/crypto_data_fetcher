from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import important_event_buyback_p2_p3_once as core
import run_buyback_p2_p3_corrected  # patches Yanoshin parser in core

ROUTINE = re.compile(
    r"ストック.?オプション|株式報酬|譲渡制限付株式|役員報酬|従業員持株会|業績連動型株式報酬|役職員向け"
)

CANCEL_RX = re.compile(r"(?:中止|撤回|取消|失権|消却|発行を行わない|発行中止)")
COMPLETE_RX = re.compile(r"(?:払込完了|発行完了|行使完了|全て.*行使完了|売出し.*終了|募集.*終了|償還完了|取得及び消却|処分完了)")
PROGRESS_RX = re.compile(r"(?:月間行使状況|大量行使|行使状況|転換状況|行使価額.*修正|下限行使価額.*修正|取得状況)")
PRICING_RX = re.compile(r"(?:発行価格.*決定|売出価格.*決定|条件.*決定|払込金額.*確定|発行条件等の決定|価格等の決定)")
START_RX = re.compile(r"(?:売出し開始|募集開始|行使開始|発行開始)")

CB_RX = re.compile(r"(?:転換社債型新株予約権付社債|転換社債|ユーロ円建[^\n]{0,50}新株予約権付社債)")
WARRANT_RX = re.compile(r"新株予約権")
THIRD_SHARE_RX = re.compile(r"第三者割当[^\n]{0,90}(?:新株式|株式発行|増資|自己株式の処分)|第三者割当増資")
SECONDARY_RX = re.compile(r"(?:株式の売出し|売出価格|売出株式|オーバーアロットメント)")
PUBLIC_RX = re.compile(r"(?:公募増資|公募による新株式発行|募集による新株式発行|海外募集|一般募集)")

INSTRUMENT_RX = [
    re.compile(r"第\s*[0-9０-９一二三四五六七八九十百]+\s*回[^\n、。]{0,35}転換社債型新株予約権付社債"),
    re.compile(r"第\s*[0-9０-９一二三四五六七八九十百]+\s*回[^\n、。]{0,25}新株予約権"),
    re.compile(r"20\d{2}年満期[^\n、。]{0,80}転換社債型新株予約権付社債"),
]


def normalize_title(title: str) -> str:
    return core.norm(title or "")


def classify(title: str) -> tuple[str, str] | None:
    t = normalize_title(title)
    if ROUTINE.search(t):
        return None
    signals = []
    if CB_RX.search(t):
        signals.append("CONVERTIBLE_BOND")
    if WARRANT_RX.search(t):
        signals.append("WARRANT")
    if THIRD_SHARE_RX.search(t):
        signals.append("THIRD_PARTY_SHARES")
    if SECONDARY_RX.search(t):
        signals.append("SECONDARY_OFFERING")
    if PUBLIC_RX.search(t):
        signals.append("PUBLIC_OFFERING")
    if not signals:
        return None
    subtype = signals[0] if len(set(signals)) == 1 else "MIXED_" + "+".join(sorted(set(signals)))
    if CANCEL_RX.search(t):
        stage = "CANCELLATION"
    elif COMPLETE_RX.search(t):
        stage = "COMPLETION"
    elif PROGRESS_RX.search(t):
        stage = "PROGRESS"
    elif PRICING_RX.search(t):
        stage = "PRICING"
    elif START_RX.search(t):
        stage = "START"
    else:
        stage = "AUTHORIZATION"
    return subtype, stage


def instrument_keys(title: str) -> list[str]:
    t = normalize_title(title)
    keys: list[str] = []
    for rx in INSTRUMENT_RX:
        for m in rx.finditer(t):
            key = re.sub(r"\s+", "", m.group(0))
            if key not in keys:
                keys.append(key)
    if not keys:
        if "株式の売出し" in t or "売出価格" in t:
            keys.append("SECONDARY_OFFERING")
        elif PUBLIC_RX.search(t):
            keys.append("PUBLIC_OFFERING")
        elif THIRD_SHARE_RX.search(t) and not WARRANT_RX.search(t) and not CB_RX.search(t):
            keys.append("THIRD_PARTY_SHARES")
    return keys


def extract_ratio(text: str, labels: list[str]) -> tuple[float | None, str | None]:
    for label in labels:
        for m in re.finditer(label, text, re.I):
            w = text[m.end():m.end() + 220]
            x = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", w)
            if x:
                snip = re.sub(r"\s+", " ", text[max(0, m.start() - 50):m.end() + x.end() + 50]).strip()
                return float(x.group(1)) / 100, snip
    return None, None


def extract_text_field(text: str, labels: list[str], distance: int = 220) -> tuple[str | None, str | None]:
    for label in labels:
        for m in re.finditer(label, text, re.I):
            w = text[m.end():m.end() + distance]
            line = re.split(r"[\n。]", w, maxsplit=1)[0].strip(" :：・")
            if line:
                snip = re.sub(r"\s+", " ", text[max(0, m.start() - 40):m.end() + min(distance, len(w))]).strip()
                return line[:180], snip
    return None, None


def numeric(text: str, labels: list[str], units: list[str], distance: int = 300):
    return core.numeric(text, labels, units, distance)


def extract_fields(subtype: str, stage: str, text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    snippets: dict[str, str | None] = {}

    specs = {
        "issued_shares": (
            [r"発行新株式数", r"募集株式数", r"発行する株式の数", r"新たに発行する株式数", r"処分する自己株式の数", r"割当株式数"],
            [r"株"],
        ),
        "offered_shares": (
            [r"売出株式数", r"売出しに係る株式の数", r"引受人の買取引受による売出し", r"売出数"],
            [r"株"],
        ),
        "over_allotment_shares": ([r"オーバーアロットメントによる売出し", r"追加的な売出し"], [r"株"]),
        "warrants_count": ([r"発行する新株予約権の総数", r"新株予約権の総数", r"発行新株予約権数"], [r"個", r"個分"]),
        "potential_shares": (
            [r"潜在株式数", r"交付株式数", r"新株予約権の目的である株式の数", r"目的である株式の総数", r"転換により交付する株式数"],
            [r"株"],
        ),
        "cb_face_amount_yen": ([r"社債の総額", r"本社債の総額", r"発行総額"], [r"円", r"百万円", r"億円"]),
        "gross_proceeds_yen": ([r"払込金額の総額", r"発行価額の総額", r"調達する資金の額", r"発行による手取金"], [r"円", r"百万円", r"億円"]),
        "net_proceeds_yen": ([r"差引手取概算額", r"手取概算額", r"差引手取額"], [r"円", r"百万円", r"億円"]),
        "issue_price_per_share_yen": ([r"1株当たりの発行価額", r"発行価格", r"払込金額", r"処分価額"], [r"円"]),
        "offer_price_per_share_yen": ([r"売出価格", r"1株当たりの売出価格"], [r"円"]),
        "exercise_price_yen": ([r"行使価額", r"転換価額"], [r"円"]),
        "lower_exercise_price_yen": ([r"下限行使価額", r"下限転換価額"], [r"円"]),
        "exercised_warrants_period": ([r"対象月間における行使個数", r"月間行使個数", r"行使された新株予約権の数", r"交付株式数"], [r"個", r"株"]),
        "shares_delivered_period": ([r"対象月間における交付株式数", r"行使により交付した株式数", r"交付株式数"], [r"株"]),
        "exercise_amount_period_yen": ([r"対象月間における行使価額の総額", r"行使価額の総額", r"払込金額の総額"], [r"円", r"百万円", r"億円"]),
        "remaining_warrants": ([r"未行使の新株予約権の数", r"未行使残存個数", r"残存する新株予約権の数"], [r"個"]),
        "remaining_potential_shares": ([r"未行使の新株予約権の目的となる株式数", r"残存潜在株式数", r"未行使残存株式数"], [r"株"]),
        "cumulative_exercised_shares": ([r"累計交付株式数", r"累計行使株式数", r"累計"], [r"株"]),
        "cumulative_exercise_amount_yen": ([r"累計行使価額", r"累計払込金額", r"累計"], [r"円", r"百万円", r"億円"]),
    }
    for name, (labels, units) in specs.items():
        value, snippet = numeric(text, labels, units)
        fields[name] = value
        snippets[name] = snippet

    dilution, ds = extract_ratio(text, [
        r"希薄化率", r"発行済株式総数に対する割合", r"議決権総数に対する割合", r"最大希薄化率", r"潜在株式比率"
    ])
    fields["dilution_ratio"] = dilution
    snippets["dilution_ratio"] = ds

    issue_date, _, s_issue = core.date_range(text, [r"払込期日", r"発行日", r"払込日"])
    delivery_date, _, s_delivery = core.date_range(text, [r"受渡期日", r"受渡日", r"株式の受渡し"])
    ex_start, ex_end, s_ex = core.date_range(text, [r"行使期間", r"新株予約権を行使することができる期間", r"転換期間"])
    pricing_start, pricing_end, s_price = core.date_range(text, [r"条件決定期間", r"発行価格等決定日", r"売出価格等決定日"])
    fields.update({
        "issue_or_payment_date": issue_date,
        "delivery_date": delivery_date,
        "exercise_period_start": ex_start,
        "exercise_period_end": ex_end,
        "pricing_window_start": pricing_start,
        "pricing_window_end": pricing_end,
    })
    snippets.update({
        "issue_or_payment_date": s_issue,
        "delivery_date": s_delivery,
        "exercise_period_start": s_ex,
        "exercise_period_end": s_ex,
        "pricing_window_start": s_price,
        "pricing_window_end": s_price,
    })

    allottee, s_allottee = extract_text_field(text, [r"割当予定先", r"割当先", r"引受人", r"売出人"])
    fields["counterparty_or_seller"] = allottee
    snippets["counterparty_or_seller"] = s_allottee

    provenance = []
    for name, value in fields.items():
        if value not in (None, ""):
            provenance.append({"field_name": name, "value": value, "snippet": snippets.get(name)})

    if stage == "AUTHORIZATION":
        if "WARRANT" in subtype or "CONVERTIBLE_BOND" in subtype:
            checks = [
                fields.get("potential_shares") is not None or fields.get("warrants_count") is not None or fields.get("cb_face_amount_yen") is not None,
                fields.get("gross_proceeds_yen") is not None or fields.get("net_proceeds_yen") is not None or fields.get("cb_face_amount_yen") is not None,
                fields.get("exercise_period_end") is not None or fields.get("issue_or_payment_date") is not None,
            ]
        else:
            checks = [
                fields.get("issued_shares") is not None or fields.get("offered_shares") is not None,
                fields.get("gross_proceeds_yen") is not None or fields.get("offer_price_per_share_yen") is not None or fields.get("issue_price_per_share_yen") is not None,
                fields.get("issue_or_payment_date") is not None or fields.get("delivery_date") is not None or fields.get("pricing_window_start") is not None,
            ]
    elif stage == "PROGRESS":
        checks = [
            fields.get("shares_delivered_period") is not None or fields.get("exercised_warrants_period") is not None or fields.get("remaining_warrants") is not None,
            fields.get("exercise_amount_period_yen") is not None or fields.get("cumulative_exercise_amount_yen") is not None,
        ]
    elif stage == "PRICING":
        checks = [
            fields.get("offer_price_per_share_yen") is not None or fields.get("issue_price_per_share_yen") is not None or fields.get("exercise_price_yen") is not None,
        ]
    else:
        checks = [True]
    score = sum(checks) / len(checks)
    fields.update({
        "field_provenance": provenance,
        "extraction_score": score,
        "extraction_confidence": "A" if score == 1 else "B" if score >= 0.5 else "C",
    })
    return fields


def process(record: dict[str, str]) -> dict[str, Any]:
    cls = classify(record.get("title", ""))
    out: dict[str, Any] = {**record}
    if cls is None:
        out.update(status="EXCLUDED_ROUTINE_OR_UNMATCHED")
        return out
    subtype, stage = cls
    out.update(event_type="FINANCING_SUPPLY", event_subtype=subtype, lifecycle_stage=stage, instrument_keys=instrument_keys(record.get("title", "")))
    blob, err = core.pdf_bytes(record.get("source_url", ""))
    if not blob:
        out.update(status="PDF_DOWNLOAD_FAILED", error=err)
        return out
    text, text_err = core.text_from_pdf(blob)
    out.update(
        pdf_sha256=hashlib.sha256(blob).hexdigest(),
        pdf_size=len(blob),
        text_chars=len(text),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    if text_err and len(re.sub(r"\s+", "", text)) < 30:
        out.update(status="PDF_TEXT_FAILED", error=text_err)
        return out
    out.update(extract_fields(subtype, stage, text), status="EXTRACTED", error=text_err)
    return out


def daterange(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    return core.dates(start, end)


def extract(start: dt.date, end: dt.date, out: Path, workers: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        days = list(pool.map(core.fetch_day, list(daterange(start, end))))
    candidates: list[dict[str, str]] = []
    audit = []
    for day, items, err in sorted(days):
        matched = []
        for item in items:
            if classify(item.get("title", "")):
                matched.append(item)
        candidates.extend(matched)
        audit.append({
            "date": day,
            "api_status": "ERROR" if err else "OK",
            "api_error": err,
            "disclosures": len(items),
            "financing_candidates": len(matched),
        })
    candidates.sort(key=lambda r: (r.get("disclosure_date", ""), r.get("disclosure_time", ""), r.get("code", ""), r.get("candidate_id", "")))
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process, r) for r in candidates]
        for future in concurrent.futures.as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as exc:
                rows.append({"status": "UNHANDLED_ERROR", "error": f"{type(exc).__name__}: {exc}"})
    rows.sort(key=lambda r: (r.get("disclosure_date", ""), r.get("disclosure_time", ""), r.get("code", ""), r.get("candidate_id", "")))
    with (out / "financing_extracted.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out / "source_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "calendar_days": (end - start).days + 1,
        "api_days_ok": sum(x["api_status"] == "OK" for x in audit),
        "api_days_error": sum(x["api_status"] == "ERROR" for x in audit),
        "disclosures": sum(x["disclosures"] for x in audit),
        "candidates": len(candidates),
        "status_counts": dict(Counter(r.get("status", "") for r in rows)),
        "stage_counts": dict(Counter(r.get("lifecycle_stage", "") for r in rows)),
        "subtype_counts": dict(Counter(r.get("event_subtype", "") for r in rows)),
        "confidence_counts": dict(Counter(r.get("extraction_confidence", "") for r in rows if r.get("status") == "EXTRACTED")),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def parse_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except Exception:
        return None


def key_overlap(a: dict[str, Any], b: dict[str, Any]) -> int:
    return len(set(a.get("instrument_keys") or []) & set(b.get("instrument_keys") or []))


def link_lifecycles(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r.get("code", ""), r.get("disclosure_date", ""), r.get("disclosure_time", ""))):
        if row.get("code"):
            by_code[str(row["code"])].append(row)
    lifecycles: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for code, events in by_code.items():
        auths: list[dict[str, Any]] = []
        life_map: dict[str, dict[str, Any]] = {}
        for event in events:
            stage = event.get("lifecycle_stage")
            if stage == "AUTHORIZATION":
                lid = f"FIN-{code}-{event.get('candidate_id')}"
                event["lifecycle_id"] = lid
                event["link_confidence"] = "A"
                auths.append(event)
                life_map[lid] = {
                    "lifecycle_id": lid,
                    "code": code,
                    "company": event.get("company"),
                    "subtype": event.get("event_subtype"),
                    "instrument_keys": event.get("instrument_keys"),
                    "authorization_candidate_id": event.get("candidate_id"),
                    "authorization_date": event.get("disclosure_date"),
                    "issue_or_payment_date": event.get("issue_or_payment_date"),
                    "delivery_date": event.get("delivery_date"),
                    "exercise_period_start": event.get("exercise_period_start"),
                    "exercise_period_end": event.get("exercise_period_end"),
                    "issued_shares": event.get("issued_shares"),
                    "offered_shares": event.get("offered_shares"),
                    "potential_shares": event.get("potential_shares"),
                    "warrants_count": event.get("warrants_count"),
                    "cb_face_amount_yen": event.get("cb_face_amount_yen"),
                    "gross_proceeds_yen": event.get("gross_proceeds_yen"),
                    "net_proceeds_yen": event.get("net_proceeds_yen"),
                    "dilution_ratio": event.get("dilution_ratio"),
                    "events": [event.get("candidate_id")],
                    "status": "AUTHORIZED",
                    "latest_event_date": event.get("disclosure_date"),
                    "extraction_confidence": event.get("extraction_confidence"),
                }
                continue

            candidates = []
            event_date = parse_date(event.get("disclosure_date"))
            for auth in auths:
                auth_date = parse_date(auth.get("disclosure_date"))
                if not auth_date or not event_date or auth_date > event_date:
                    continue
                days = (event_date - auth_date).days
                max_days = 900 if ("WARRANT" in str(auth.get("event_subtype")) or "CONVERTIBLE_BOND" in str(auth.get("event_subtype"))) else 240
                if days > max_days:
                    continue
                overlap = key_overlap(auth, event)
                same_subtype = auth.get("event_subtype") == event.get("event_subtype")
                score = overlap * 100 + (25 if same_subtype else 0) + max(0, max_days - days) / max_days
                if overlap or same_subtype or not (event.get("instrument_keys") or []):
                    candidates.append((score, auth))
            candidates.sort(key=lambda x: (x[0], x[1].get("disclosure_date", "")), reverse=True)
            if candidates:
                best_score = candidates[0][0]
                tied = [a for score, a in candidates if abs(score - best_score) < 1e-9]
                chosen = candidates[0][1]
                lid = chosen["lifecycle_id"]
                confidence = "A" if len(tied) == 1 and (key_overlap(chosen, event) > 0 or chosen.get("event_subtype") == event.get("event_subtype")) else "B"
            else:
                lid = f"ORPHAN-{code}-{event.get('candidate_id')}"
                confidence = "C"
                life_map[lid] = {
                    "lifecycle_id": lid,
                    "code": code,
                    "company": event.get("company"),
                    "subtype": event.get("event_subtype"),
                    "instrument_keys": event.get("instrument_keys"),
                    "events": [],
                    "status": "ORPHAN",
                    "latest_event_date": event.get("disclosure_date"),
                }
            event["lifecycle_id"] = lid
            event["link_confidence"] = confidence
            life = life_map[lid]
            life.setdefault("events", []).append(event.get("candidate_id"))
            life["latest_event_date"] = event.get("disclosure_date")
            if event.get("offer_price_per_share_yen") is not None:
                life["offer_price_per_share_yen"] = event.get("offer_price_per_share_yen")
            if event.get("issue_price_per_share_yen") is not None:
                life["issue_price_per_share_yen"] = event.get("issue_price_per_share_yen")
            if event.get("exercise_price_yen") is not None:
                life["exercise_price_yen"] = event.get("exercise_price_yen")
            if event.get("remaining_warrants") is not None:
                life["latest_remaining_warrants"] = event.get("remaining_warrants")
            if event.get("remaining_potential_shares") is not None:
                life["latest_remaining_potential_shares"] = event.get("remaining_potential_shares")
            if event.get("cumulative_exercised_shares") is not None:
                life["latest_cumulative_exercised_shares"] = event.get("cumulative_exercised_shares")
            if event.get("cumulative_exercise_amount_yen") is not None:
                life["latest_cumulative_exercise_amount_yen"] = event.get("cumulative_exercise_amount_yen")
            if stage == "PRICING":
                life["status"] = "PRICED"
            elif stage in {"START", "PROGRESS"}:
                life["status"] = "ACTIVE"
            elif stage == "COMPLETION":
                life["status"] = "COMPLETED"
            elif stage == "CANCELLATION":
                life["status"] = "CANCELLED"
            audit.append({
                "candidate_id": event.get("candidate_id"),
                "code": code,
                "stage": stage,
                "lifecycle_id": lid,
                "link_confidence": confidence,
                "best_score": candidates[0][0] if candidates else None,
                "eligible_authorizations": [a.get("candidate_id") for _, a in candidates[:10]],
            })

        for life in life_map.values():
            potential = life.get("potential_shares")
            remaining = life.get("latest_remaining_potential_shares")
            cumulative = life.get("latest_cumulative_exercised_shares")
            if remaining is None and potential is not None and cumulative is not None:
                remaining = max(float(potential) - float(cumulative), 0)
            life["remaining_potential_shares_upper_bound"] = remaining
            life["event_count"] = len(life.get("events") or [])
            lifecycles.append(life)
    lifecycles.sort(key=lambda r: (r.get("code", ""), r.get("authorization_date") or "9999", r.get("lifecycle_id", "")))
    return lifecycles, audit


def flatten(row: dict[str, Any]) -> dict[str, Any]:
    return {k: json.dumps(v, ensure_ascii=False, separators=(",", ":")) if isinstance(v, (list, dict)) else v for k, v in row.items()}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(flatten(row) for row in rows)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def aggregate(parts: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, Any]] = []
    for path in sorted(parts.rglob("financing_extracted.jsonl")):
        all_rows.extend(load_jsonl(path))
    dedup: dict[str, dict[str, Any]] = {}
    for row in all_rows:
        key = str(row.get("candidate_id") or row.get("file_id") or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest())
        if key not in dedup or dedup[key].get("status") != "EXTRACTED":
            dedup[key] = row
    rows = sorted(dedup.values(), key=lambda r: (r.get("disclosure_date", ""), r.get("disclosure_time", ""), r.get("code", ""), r.get("candidate_id", "")))
    lifecycles, links = link_lifecycles(rows)
    provenance = []
    for row in rows:
        for field in row.get("field_provenance") or []:
            provenance.append({
                "candidate_id": row.get("candidate_id"),
                "lifecycle_id": row.get("lifecycle_id"),
                "code": row.get("code"),
                "company": row.get("company"),
                "disclosure_date": row.get("disclosure_date"),
                "event_subtype": row.get("event_subtype"),
                "lifecycle_stage": row.get("lifecycle_stage"),
                "field_name": field.get("field_name"),
                "value": field.get("value"),
                "source_url": row.get("source_url"),
                "snippet": field.get("snippet"),
                "extraction_confidence": row.get("extraction_confidence"),
                "pdf_sha256": row.get("pdf_sha256"),
            })
    write_csv(out / "financing_extracted.csv", rows)
    write_csv(out / "financing_lifecycles.csv", lifecycles)
    write_csv(out / "field_provenance.csv", provenance)
    write_csv(out / "lifecycle_link_audit.csv", links)
    with (out / "financing_extracted.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    regressions = {}
    for candidate_id, expected in {
        "140120241226544987": ("SECONDARY_OFFERING", "AUTHORIZATION"),
        "140120250105546162": ("WARRANT", "PROGRESS"),
    }.items():
        matches = [r for r in rows if str(r.get("candidate_id")) == candidate_id]
        regressions[candidate_id] = {
            "found": bool(matches),
            "event_subtype": matches[0].get("event_subtype") if matches else None,
            "lifecycle_stage": matches[0].get("lifecycle_stage") if matches else None,
            "pdf_status": matches[0].get("status") if matches else None,
            "pass": bool(matches and expected[0] in str(matches[0].get("event_subtype")) and matches[0].get("lifecycle_stage") == expected[1]),
        }
    summary = {
        "candidates_unique": len(rows),
        "status_counts": dict(Counter(r.get("status", "") for r in rows)),
        "stage_counts": dict(Counter(r.get("lifecycle_stage", "") for r in rows)),
        "subtype_counts": dict(Counter(r.get("event_subtype", "") for r in rows)),
        "confidence_counts": dict(Counter(r.get("extraction_confidence", "") for r in rows if r.get("status") == "EXTRACTED")),
        "lifecycle_count": len(lifecycles),
        "lifecycle_status_counts": dict(Counter(r.get("status", "") for r in lifecycles)),
        "orphan_links": sum(r.get("link_confidence") == "C" for r in links),
        "ambiguous_links": sum(r.get("link_confidence") == "B" for r in links),
        "field_provenance_rows": len(provenance),
        "regressions": regressions,
        "regression_pass": all(x["pass"] for x in regressions.values()),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["regression_pass"]:
        raise SystemExit("financing regression failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    ext = sub.add_parser("extract")
    ext.add_argument("--start", required=True)
    ext.add_argument("--end", required=True)
    ext.add_argument("--out", required=True)
    ext.add_argument("--workers", type=int, default=12)
    agg = sub.add_parser("aggregate")
    agg.add_argument("--parts", required=True)
    agg.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.cmd == "extract":
        extract(dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end), Path(args.out), args.workers)
    else:
        aggregate(Path(args.parts), Path(args.out))


if __name__ == "__main__":
    main()
