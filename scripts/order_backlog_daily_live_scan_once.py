from __future__ import annotations
import concurrent.futures, csv, datetime as dt, json, re, time, unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import unquote
import fitz, requests

S=requests.Session(); S.headers.update({'User-Agent':'Mozilla/5.0 order-backlog-daily-scan/1.0'})
API='https://webapi.yanoshin.jp/webapi/tdnet/list/{date}.json?limit=1000'
EARN=re.compile(r'決算短信|決算説明|決算補足|業績予想|業績.*修正|決算概要|決算資料|四半期.*決算|中間期.*決算|通期.*決算',re.I)
TERM=re.compile(r'受注残高|受注残|受注高|受注状況|繰越工事高|次期繰越工事高|手持工事高|受注残.*売上|Book.?to.?Bill|BBレシオ|ＢＢレシオ',re.I)


def norm(s:str)->str:
    s=unicodedata.normalize('NFKC',s or '').replace('\u3000',' ')
    return re.sub(r'[ \t]+',' ',s)

def first(d:dict,names:list[str])->str:
    low={str(k).lower():v for k,v in d.items()}
    for n in names:
        v=low.get(n.lower())
        if v not in (None,''): return str(v)
    return ''
def unwrap(x:dict)->dict:
    for k,v in x.items():
        if str(k).lower()=='tdnet' and isinstance(v,dict): return v
    return x

def items(obj:Any)->list[dict]:
    if isinstance(obj,dict) and isinstance(obj.get('items'),list): return [unwrap(x) for x in obj['items'] if isinstance(x,dict)]
    return []
def fetch_day(day:dt.date):
    u=API.format(date=day.strftime('%Y%m%d')); err=''
    for n in range(4):
        try:
            r=S.get(u,timeout=60); r.raise_for_status(); xs=items(r.json()); return day.isoformat(),xs,''
        except Exception as e:
            err=f'{type(e).__name__}: {e}'; time.sleep(n+1)
    return day.isoformat(),[],err

def canon(x:dict,day:str)->dict:
    url=first(x,['document_url','source_url','pdf_url','url','link'])
    if 'rd.php?' in url: url=unquote(url.split('?',1)[1])
    title=norm(first(x,['title','subject','document_name']))
    pub=first(x,['pubdate','published_at','datetime','time'])
    t=''
    if pub:
        ps=str(pub).split();
        if len(ps)>1:t=ps[1][:5]
        elif re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?',ps[0]):t=ps[0][:5]
    return {'date':day,'time':t,'code':first(x,['company_code','ticker','code','stock_code'])[:5].rstrip('0'),'company':norm(first(x,['company_name','company','name'])),'title':title,'url':url,'earnings_related':bool(EARN.search(title))}
def pdf_text(url:str)->tuple[str,str]:
    if not url:return '', 'NO_URL'
    err=''
    for n in range(3):
        try:
            r=S.get(url,timeout=90)
            if r.status_code==200 and r.content.startswith(b'%PDF'):
                doc=fitz.open(stream=r.content,filetype='pdf'); tx='\n'.join(p.get_text('text') for p in doc); return norm(tx),''
            err=f'HTTP_{r.status_code}'
        except Exception as e: err=f'{type(e).__name__}: {e}'
        time.sleep(n+1)
    return '',err

def inspect(r:dict)->dict:
    tx,err=pdf_text(r['url']); ms=list(TERM.finditer(tx)); snippets=[]
    for m in ms[:20]: snippets.append(re.sub(r'\s+',' ',tx[max(0,m.start()-220):min(len(tx),m.end()+500)]).strip())
    return {**r,'pdf_status':'OK' if tx else 'ERROR','pdf_error':err,'text_chars':len(tx),'term_hits':len(ms),'snippets':snippets}
def main():
    out=Path('out/order_backlog_20260813');out.mkdir(parents=True,exist_ok=True)
    start=dt.date(2026,8,7);end=dt.date(2026,8,13);ds=[];d=start
    while d<=end: ds.append(d);d+=dt.timedelta(days=1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex: days=list(ex.map(fetch_day,ds))
    allr=[];audit=[]
    for day,xs,err in sorted(days):
        cr=[canon(x,day) for x in xs]; er=[r for r in cr if r['earnings_related']]
        allr+=er;audit.append({'date':day,'tdnet_disclosures':len(cr),'earnings_related':len(er),'error':err})
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex: rows=list(ex.map(inspect,allr))
    hits=[r for r in rows if r['term_hits']>0]
    for name,data in [('all_earnings.jsonl',rows),('backlog_hits.jsonl',hits)]:
        with (out/name).open('w',encoding='utf-8') as f:
            for r in data:f.write(json.dumps(r,ensure_ascii=False)+'\n')
    fields=['date','time','code','company','title','url','pdf_status','term_hits','snippets']
    with (out/'backlog_hits.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();
        for r in hits:w.writerow({k:json.dumps(r[k],ensure_ascii=False) if isinstance(r[k],list) else r.get(k,'') for k in fields})
    summary={'window':['2026-08-07','2026-08-13'],'audit':audit,'tdnet_total':sum(a['tdnet_disclosures'] for a in audit),'earnings_related_total':len(rows),'pdf_ok':sum(r['pdf_status']=='OK' for r in rows),'pdf_failed':sum(r['pdf_status']!='OK' for r in rows),'backlog_term_hits_companies':len({r['code'] for r in hits if r['code']}),'backlog_documents':len(hits)}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
