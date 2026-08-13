from __future__ import annotations
import datetime as dt, json
from pathlib import Path
from urllib.parse import quote
import requests

CODES=["6232","6613","6345"]
OUT=Path("out_final_prices_2"); OUT.mkdir(exist_ok=True)
s=requests.Session(); s.headers.update({"User-Agent":"Mozilla/5.0 backlog-final-price/2.0"})
start=int(dt.datetime(2025,12,20,tzinfo=dt.timezone.utc).timestamp())
end=int(dt.datetime(2026,8,15,tzinfo=dt.timezone.utc).timestamp())
out={}
for code in CODES:
    symbol=f"{code}.T"
    url="https://query1.finance.yahoo.com/v8/finance/chart/"+quote(symbol,safe="")+f"?period1={start}&period2={end}&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
    try:
        r=s.get(url,timeout=60); r.raise_for_status(); obj=r.json(); result=((obj.get("chart") or {}).get("result") or [None])[0]
        if not result:
            out[code]={"status":"NO_DATA","url":url}; continue
        ts=result.get("timestamp") or []; q=((result.get("indicators") or {}).get("quote") or [{}])[0]; adj=(((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or [])
        rows=[]
        for i,t in enumerate(ts):
            def at(v): return v[i] if i<len(v) else None
            rows.append({"date":dt.datetime.fromtimestamp(t,tz=dt.timezone.utc).date().isoformat(),"open":at(q.get("open") or []),"high":at(q.get("high") or []),"low":at(q.get("low") or []),"close":at(q.get("close") or []),"adjclose":at(adj),"volume":at(q.get("volume") or [])})
        out[code]={"status":"OK","symbol":symbol,"rows":rows,"url":url}
    except Exception as e:
        out[code]={"status":"ERROR","error":f"{type(e).__name__}: {e}","url":url}
(OUT/"prices_3.json").write_text(json.dumps(out,ensure_ascii=False),encoding="utf-8")
(OUT/"summary.json").write_text(json.dumps({"ok":[k for k,v in out.items() if v.get('status')=='OK'],"failed":{k:v.get('status') for k,v in out.items() if v.get('status')!='OK'}},ensure_ascii=False,indent=2),encoding="utf-8")
