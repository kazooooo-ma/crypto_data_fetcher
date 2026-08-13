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
from typing import Any, Iterable

import fitz
import requests

API = "https://webapi.yanoshin.jp/webapi/tdnet/list/{date}.json?limit=1000"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 important-event-buyback-backfill/2.0"})
ROUTINE = re.compile(r"ストック.?オプション|株式報酬|譲渡制限付株式|役員報酬|従業員持株会|業績連動型株式報酬")
STAGES = [
    ("CANCELLATION", re.compile(r"自己株式.*(?:取得|買付).*(?:中止|撤回|取消)|自己株式取得.*(?:中止|撤回|取消)")),
    ("COMPLETION", re.compile(r"自己株式.*(?:取得|買付).*(?:終了|完了|結果)|自己株式取得.*(?:終了|完了|結果)")),
    ("PROGRESS", re.compile(r"自己株式.*(?:取得状況|買付状況|取得実績)|自己株式取得状況")),
    ("RETIREMENT", re.compile(r"自己株式.*消却")),
    ("START", re.compile(r"自己株式.*(?:取得開始|買付開始|取得を開始|買付けを開始)")),
    ("AUTHORIZATION", re.compile(r"自己株式(?:の)?取得|自己株買い|自己株式の市場買付|自己株式取得に係る事項")),
]
DATE_RE = re.compile(r"(?P<y>20\d{2})\s*[年/.\-]\s*(?P<m>\d{1,2})\s*[月/.\-]\s*(?P<d>\d{1,2})\s*日?")
ERA_RE = re.compile(r"令和\s*(?P<y>\d{1,2})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").replace("〜", "~").replace("～", "~")
    s = s.replace("△", "-").replace("▲", "-")
    s = re.sub(r"[\t\u00a0]+", " ", s)
    s = re.sub(r" {2,}", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def classify(title: str) -> str | None:
    title = norm(title)
    if ROUTINE.search(title):
        return None
    for stage, rx in STAGES:
        if rx.search(title):
            return stage
    return None


def dates(a: dt.date, b: dt.date) -> Iterable[dt.date]:
    while a <= b:
        yield a
        a += dt.timedelta(days=1)


def dict_lists(obj: Any) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            out.append(obj)
        for x in obj:
            out += dict_lists(x)
    elif isinstance(obj, dict):
        for x in obj.values():
            out += dict_lists(x)
    return out


def items_from(obj: Any) -> list[dict[str, Any]]:
    scored = []
    for xs in dict_lists(obj):
        score = sum(bool({str(k).lower() for k in x} & {"title", "company", "company_name", "code", "ticker", "file_id", "url"}) for x in xs[:20])
        scored.append((score, len(xs), xs))
    return max(scored, default=(0, 0, []))[2]


def first(d: dict[str, Any], names: list[str]) -> str:
    lower = {str(k).lower(): v for k, v in d.items()}
    for name in names:
        v = lower.get(name.lower())
        if v not in (None, ""):
            return str(v)
    return ""


def canonical(item: dict[str, Any], day: dt.date) -> dict[str, str]:
    file_id = first(item, ["file_id", "document_id", "id", "tdnet_id"])
    url = first(item, ["source_url", "pdf_url", "url", "document_url", "link"])
    if not url and re.fullmatch(r"\d{18}", file_id):
        url = f"https://www.release.tdnet.info/inbs/{file_id}.pdf"
    return {
        "candidate_id": file_id or hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24],
        "file_id": file_id,
        "disclosure_date": day.isoformat(),
        "disclosure_time": first(item, ["time", "disclosure_time", "published_at", "datetime"]),
        "code": first(item, ["ticker", "code", "stock_code", "company_code", "security_code"]),
        "company": first(item, ["company", "company_name", "name", "companyname", "issuer_name"]),
        "title": first(item, ["title", "subject", "disclosure_title", "document_name", "tdnet_title"]),
        "source_url": url,
    }


def fetch_day(day: dt.date) -> tuple[str, list[dict[str, str]], str | None]:
    u = API.format(date=day.strftime("%Y%m%d"))
    err = None
    for n in range(4):
        try:
            r = S.get(u, timeout=60)
            if r.status_code == 404:
                return day.isoformat(), [], None
            r.raise_for_status()
            return day.isoformat(), [canonical(x, day) for x in items_from(r.json())], None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            time.sleep(n + 1)
    return day.isoformat(), [], err


def pdf_bytes(url: str) -> tuple[bytes | None, str | None]:
    if not url:
        return None, "NO_URL"
    err = None
    for n in range(4):
        try:
            r = S.get(url, timeout=90)
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                return r.content, None
            err = f"HTTP_{r.status_code}"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        time.sleep(n + 1)
    return None, err


def text_from_pdf(b: bytes) -> tuple[str, str | None]:
    try:
        doc = fitz.open(stream=b, filetype="pdf")
        text = norm("\n\f\n".join(p.get_text("text") for p in doc))
        return text, None if len(re.sub(r"\s+", "", text)) >= 30 else "LOW_TEXT"
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"


def num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except Exception:
        return None


def amount_multiplier(snippet: str) -> int:
    s = norm(snippet)
    if "億円" in s:
        return 100_000_000
    if "百万円" in s:
        return 1_000_000
    if "千円" in s:
        return 1_000
    return 1


def numeric(text: str, labels: list[str], units: list[str], distance: int = 260) -> tuple[float | None, str | None]:
    best = None
    for label in labels:
        for m in re.finditer(label, text, re.I):
            w = text[m.end():m.end() + distance]
            for unit in units:
                n = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*" + unit, w)
                if n:
                    v = num(n.group(1))
                    if v is None:
                        continue
                    snip = re.sub(r"\s+", " ", text[max(0, m.start()-50):m.end()+n.end()+50]).strip()
                    cand = (n.start(), v * amount_multiplier(snip) if "円" in unit else v, snip)
                    if best is None or cand[0] < best[0]:
                        best = cand
    return (best[1], best[2]) if best else (None, None)


def jpdate(s: str) -> str | None:
    m = DATE_RE.search(s)
    if m:
        try:
            return dt.date(int(m.group("y")), int(m.group("m")), int(m.group("d"))).isoformat()
        except ValueError:
            return None
    m = ERA_RE.search(s)
    if m:
        try:
            return dt.date(2018 + int(m.group("y")), int(m.group("m")), int(m.group("d"))).isoformat()
        except ValueError:
            return None
    return None


def date_range(text: str, labels: list[str]) -> tuple[str | None, str | None, str | None]:
    for label in labels:
        for m in re.finditer(label, text):
            w = text[m.end():m.end()+260]
            found = []
            for rx in (DATE_RE, ERA_RE):
                for d in rx.finditer(w):
                    v = jpdate(d.group(0))
                    if v:
                        found.append((d.start(), v))
            vals = []
            for _, v in sorted(found):
                if v not in vals:
                    vals.append(v)
            if vals:
                snip = re.sub(r"\s+", " ", text[max(0,m.start()-50):m.end()+260]).strip()
                return vals[0], vals[1] if len(vals)>1 else None, snip
    return None, None, None


def ratio(text: str) -> tuple[float | None, str | None]:
    for label in [r"発行済株式総数[^\n%]{0,160}対する割合", r"自己株式を除く発行済株式[^\n%]{0,160}割合", r"発行済株式[^\n%]{0,160}割合"]:
        for m in re.finditer(label, text):
            w = text[m.end():m.end()+100]
            x = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", w)
            if x:
                return float(x.group(1))/100, re.sub(r"\s+", " ", text[max(0,m.start()-40):m.end()+x.end()+40]).strip()
    return None, None


def method(text: str) -> tuple[str | None, str | None]:
    for name, rx in [
        ("TOSTNET3", re.compile(r"ToSTNeT-3|自己株式立会外買付取引")),
        ("MARKET_PURCHASE", re.compile(r"市場買付|市場取引|取引一任契約|東京証券取引所における市場買付")),
        ("TENDER_OFFER", re.compile(r"自己株式の公開買付")),
        ("OFF_MARKET", re.compile(r"相対取引|市場外取引")),
    ]:
        m = rx.search(text)
        if m:
            return name, re.sub(r"\s+", " ", text[max(0,m.start()-70):m.end()+120]).strip()
    return None, None


def extract_fields(stage: str, text: str) -> dict[str, Any]:
    max_sh, s1 = numeric(text, [r"取得し得る株式の総数", r"取得する株式の総数", r"取得上限株式数", r"取得株式数の上限", r"取得可能株式数"], [r"株"])
    max_amt, s2 = numeric(text, [r"株式の取得価額の総額", r"取得価額の総額", r"取得総額", r"取得金額の上限", r"取得価額の上限"], [r"円", r"百万円", r"億円"])
    rat, s3 = ratio(text)
    start, end, s4 = date_range(text, [r"取得期間", r"買付期間", r"取得日程"])
    meth, s5 = method(text)
    acq_sh, s6 = numeric(text, [r"取得した株式の総数", r"取得株式数", r"買付株式数"], [r"株"])
    acq_amt, s7 = numeric(text, [r"株式の取得価額の総額", r"取得価額の総額", r"取得金額", r"買付総額"], [r"円", r"百万円", r"億円"])
    cum_sh, s8 = numeric(text, [r"累計取得株式数", r"取得した自己株式の累計", r"取得した株式の累計", r"累計"], [r"株"])
    cum_amt, s9 = numeric(text, [r"累計取得価額", r"取得価額の累計", r"取得した自己株式の累計", r"累計"], [r"円", r"百万円", r"億円"])
    pstart, pend, s10 = date_range(text, [r"取得した期間", r"取得実績", r"取得期間", r"買付期間"])
    vals = {
        "max_shares": max_sh, "max_amount_yen": max_amt, "share_ratio_ex_treasury": rat,
        "effective_start_date": start, "effective_end_date": end, "acquisition_method": meth,
        "acquired_shares_period": acq_sh, "acquired_amount_period_yen": acq_amt,
        "cumulative_shares": cum_sh, "cumulative_amount_yen": cum_amt,
        "progress_period_start": pstart, "progress_period_end": pend,
    }
    snippets = {"max_shares":s1,"max_amount_yen":s2,"share_ratio_ex_treasury":s3,"acquisition_period":s4,"acquisition_method":s5,"acquired_shares_period":s6,"acquired_amount_period_yen":s7,"cumulative_shares":s8,"cumulative_amount_yen":s9,"progress_period":s10}
    prov=[]
    for k,v in vals.items():
        if v is not None:
            sn=snippets.get(k)
            if k in {"effective_start_date","effective_end_date"}: sn=s4
            if k in {"progress_period_start","progress_period_end"}: sn=s10
            prov.append({"field_name":k,"value":v,"snippet":sn})
    if stage=="AUTHORIZATION": checks=[max_sh is not None,max_amt is not None,start is not None or end is not None]
    elif stage in {"PROGRESS","COMPLETION"}: checks=[acq_sh is not None or cum_sh is not None,acq_amt is not None or cum_amt is not None]
    else: checks=[True]
    score=sum(checks)/len(checks)
    vals.update({"field_provenance":prov,"extraction_score":score,"extraction_confidence":"A" if score==1 else "B" if score>=.5 else "C"})
    return vals


def process(r: dict[str,str]) -> dict[str,Any]:
    stage=classify(r.get("title",""))
    out={**r,"lifecycle_stage":stage}
    b,err=pdf_bytes(r.get("source_url",""))
    if not b:
        out.update(status="PDF_DOWNLOAD_FAILED",error=err); return out
    text,terr=text_from_pdf(b)
    out.update(pdf_sha256=hashlib.sha256(b).hexdigest(),pdf_size=len(b),text_chars=len(text),text_sha256=hashlib.sha256(text.encode()).hexdigest())
    if terr and len(re.sub(r"\s+","",text))<30:
        out.update(status="PDF_TEXT_FAILED",error=terr); return out
    out.update(extract_fields(stage or "",text),status="EXTRACTED",error=terr)
    return out


def extract(start: dt.date,end: dt.date,out: Path,workers:int) -> None:
    out.mkdir(parents=True,exist_ok=True)
    days=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        days=list(ex.map(fetch_day,list(dates(start,end))))
    candidates=[]; audit=[]
    for day,items,err in sorted(days):
        b=[]
        for x in items:
            st=classify(x.get("title",""))
            if st:
                x["lifecycle_stage"]=st;b.append(x)
        candidates+=b
        audit.append({"date":day,"api_status":"ERROR" if err else "OK","api_error":err,"disclosures":len(items),"buyback_candidates":len(b)})
    candidates.sort(key=lambda x:(x.get("disclosure_date",""),x.get("disclosure_time",""),x.get("code",""),x.get("candidate_id","")))
    rows=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs=[ex.submit(process,x) for x in candidates]
        for f in concurrent.futures.as_completed(futs):
            try: rows.append(f.result())
            except Exception as e: rows.append({"status":"UNHANDLED_ERROR","error":f"{type(e).__name__}: {e}"})
    rows.sort(key=lambda x:(x.get("disclosure_date",""),x.get("disclosure_time",""),x.get("code",""),x.get("candidate_id","")))
    with (out/"buyback_extracted.jsonl").open("w",encoding="utf-8") as f:
        for x in rows:f.write(json.dumps(x,ensure_ascii=False)+"\n")
    (out/"source_audit.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
    summary={"start":start.isoformat(),"end":end.isoformat(),"calendar_days":(end-start).days+1,"api_days_ok":sum(x["api_status"]=="OK" for x in audit),"api_days_error":sum(x["api_status"]=="ERROR" for x in audit),"disclosures":sum(x["disclosures"] for x in audit),"candidates":len(candidates),"status_counts":dict(Counter(x.get("status","") for x in rows)),"stage_counts":dict(Counter(x.get("lifecycle_stage","") for x in rows)),"confidence_counts":dict(Counter(x.get("extraction_confidence","") for x in rows if x.get("status")=="EXTRACTED"))}
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))


def load_jsonl(p:Path)->list[dict[str,Any]]:
    with p.open(encoding="utf-8") as f:return [json.loads(x) for x in f if x.strip()]

def d(v:Any)->dt.date|None:
    try:return dt.date.fromisoformat(str(v)[:10]) if v else None
    except:return None

def link(rows:list[dict[str,Any]])->tuple[list[dict[str,Any]],list[dict[str,Any]]]:
    by=defaultdict(list)
    for r in sorted(rows,key=lambda x:(x.get("code",""),x.get("disclosure_date",""),x.get("disclosure_time",""))):
        if r.get("code"):by[str(r["code"])].append(r)
    lifes=[];audit=[]
    for code,evs in by.items():
        auths=[];m={}
        for e in evs:
            st=e.get("lifecycle_stage")
            if st=="AUTHORIZATION":
                lid=f"BUYBACK-{code}-{e.get('candidate_id')}";e["lifecycle_id"]=lid;e["link_confidence"]="A";auths.append(e)
                m[lid]={"lifecycle_id":lid,"code":code,"company":e.get("company"),"authorization_candidate_id":e.get("candidate_id"),"authorization_date":e.get("disclosure_date"),"effective_start_date":e.get("effective_start_date"),"effective_end_date":e.get("effective_end_date"),"max_shares":e.get("max_shares"),"max_amount_yen":e.get("max_amount_yen"),"share_ratio_ex_treasury":e.get("share_ratio_ex_treasury"),"acquisition_method":e.get("acquisition_method"),"events":[e.get("candidate_id")],"status":"AUTHORIZED","latest_event_date":e.get("disclosure_date")}
                continue
            if st=="RETIREMENT":
                lid=f"RETIREMENT-{code}-{e.get('candidate_id')}";e["lifecycle_id"]=lid;e["link_confidence"]="A";m[lid]={"lifecycle_id":lid,"code":code,"company":e.get("company"),"events":[e.get("candidate_id")],"status":"RETIREMENT","latest_event_date":e.get("disclosure_date")};continue
            elig=[]
            for a in auths:
                if d(a.get("disclosure_date")) and d(e.get("disclosure_date")) and d(a.get("disclosure_date"))<=d(e.get("disclosure_date")):
                    end=d(a.get("effective_end_date"))
                    if not end or d(e.get("disclosure_date"))<=end+dt.timedelta(days=60):elig.append(a)
            if elig: chosen=elig[-1];lid=chosen["lifecycle_id"];conf="A" if len(elig)==1 else "B"
            else: lid=f"ORPHAN-{code}-{e.get('candidate_id')}";conf="C";m[lid]={"lifecycle_id":lid,"code":code,"company":e.get("company"),"events":[],"status":"ORPHAN"}
            e["lifecycle_id"]=lid;e["link_confidence"]=conf;life=m[lid];life.setdefault("events",[]).append(e.get("candidate_id"));life["latest_event_date"]=e.get("disclosure_date")
            if e.get("cumulative_shares") is not None:life["latest_cumulative_shares"]=e.get("cumulative_shares")
            if e.get("cumulative_amount_yen") is not None:life["latest_cumulative_amount_yen"]=e.get("cumulative_amount_yen")
            if st=="COMPLETION":life["status"]="COMPLETED"
            elif st=="CANCELLATION":life["status"]="CANCELLED"
            elif st in {"START","PROGRESS"} and life.get("status")=="AUTHORIZED":life["status"]="ACTIVE"
            audit.append({"candidate_id":e.get("candidate_id"),"code":code,"stage":st,"lifecycle_id":lid,"link_confidence":conf,"eligible_authorizations":[x.get("candidate_id") for x in elig]})
        for life in m.values():
            ms,ma,cs,ca=life.get("max_shares"),life.get("max_amount_yen"),life.get("latest_cumulative_shares"),life.get("latest_cumulative_amount_yen")
            life["remaining_shares_upper_bound"]=max(float(ms)-float(cs),0) if ms is not None and cs is not None else None
            life["remaining_amount_upper_bound_yen"]=max(float(ma)-float(ca),0) if ma is not None and ca is not None else None
            life["event_count"]=len(life.get("events",[]));lifes.append(life)
    return sorted(lifes,key=lambda x:(x.get("code",""),x.get("authorization_date") or "9999",x.get("lifecycle_id",""))),audit

def flat(r:dict[str,Any])->dict[str,Any]:return {k:(json.dumps(v,ensure_ascii=False,separators=(",",":")) if isinstance(v,(list,dict)) else v) for k,v in r.items()}
def write_csv(p:Path,rows:list[dict[str,Any]])->None:
    if not rows:p.write_text("",encoding="utf-8");return
    keys=[];seen=set()
    for r in rows:
        for k in r:
            if k not in seen:seen.add(k);keys.append(k)
    with p.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(flat(r) for r in rows)

def aggregate(parts:Path,out:Path)->None:
    out.mkdir(parents=True,exist_ok=True);allrows=[]
    for p in sorted(parts.rglob("buyback_extracted.jsonl")):allrows+=load_jsonl(p)
    dedup={}
    for r in allrows:
        k=str(r.get("candidate_id") or r.get("file_id") or hashlib.sha256(json.dumps(r,sort_keys=True).encode()).hexdigest())
        if k not in dedup or dedup[k].get("status")!="EXTRACTED":dedup[k]=r
    rows=sorted(dedup.values(),key=lambda x:(x.get("disclosure_date",""),x.get("disclosure_time",""),x.get("code",""),x.get("candidate_id","")))
    lifes,links=link(rows);prov=[]
    for r in rows:
        for f in r.get("field_provenance") or []:prov.append({"candidate_id":r.get("candidate_id"),"lifecycle_id":r.get("lifecycle_id"),"code":r.get("code"),"company":r.get("company"),"disclosure_date":r.get("disclosure_date"),"field_name":f.get("field_name"),"value":f.get("value"),"source_url":r.get("source_url"),"snippet":f.get("snippet"),"extraction_confidence":r.get("extraction_confidence"),"pdf_sha256":r.get("pdf_sha256")})
    write_csv(out/"buyback_extracted.csv",rows);write_csv(out/"buyback_lifecycles.csv",lifes);write_csv(out/"field_provenance.csv",prov);write_csv(out/"lifecycle_link_audit.csv",links)
    with (out/"buyback_extracted.jsonl").open("w",encoding="utf-8") as f:
        for r in rows:f.write(json.dumps(r,ensure_ascii=False)+"\n")
    ene=[r for r in rows if str(r.get("candidate_id"))=="140120260619574629"]
    reg={"found":bool(ene),"max_shares":ene[0].get("max_shares") if ene else None,"max_amount_yen":ene[0].get("max_amount_yen") if ene else None,"share_ratio_ex_treasury":ene[0].get("share_ratio_ex_treasury") if ene else None,"effective_start_date":ene[0].get("effective_start_date") if ene else None,"effective_end_date":ene[0].get("effective_end_date") if ene else None}
    reg["pass"]=bool(reg["found"] and reg["max_shares"]==4_000_000 and reg["max_amount_yen"]==1_000_000_000 and reg["effective_start_date"]=="2026-08-10" and reg["effective_end_date"]=="2027-06-30")
    summary={"candidates_unique":len(rows),"status_counts":dict(Counter(r.get("status","") for r in rows)),"stage_counts":dict(Counter(r.get("lifecycle_stage","") for r in rows)),"confidence_counts":dict(Counter(r.get("extraction_confidence","") for r in rows if r.get("status")=="EXTRACTED")),"lifecycle_count":len(lifes),"lifecycle_status_counts":dict(Counter(r.get("status","") for r in lifes)),"orphan_links":sum(r.get("link_confidence")=="C" for r in links),"ambiguous_links":sum(r.get("link_confidence")=="B" for r in links),"field_provenance_rows":len(prov),"enechange_regression":reg}
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8");print(json.dumps(summary,ensure_ascii=False,indent=2))
    if not reg["pass"]:raise SystemExit("ENECHANGE regression failed")

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest="cmd",required=True)
    e=s.add_parser("extract");e.add_argument("--start",required=True);e.add_argument("--end",required=True);e.add_argument("--out",required=True);e.add_argument("--workers",type=int,default=12)
    a=s.add_parser("aggregate");a.add_argument("--parts",required=True);a.add_argument("--out",required=True)
    x=p.parse_args()
    if x.cmd=="extract":extract(dt.date.fromisoformat(x.start),dt.date.fromisoformat(x.end),Path(x.out),x.workers)
    else:aggregate(Path(x.parts),Path(x.out))
if __name__=="__main__":main()
