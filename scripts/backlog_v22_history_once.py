from __future__ import annotations
import argparse, concurrent.futures as cf, csv, datetime as dt, hashlib, json, re, time, unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote
import fitz, requests

API='https://webapi.yanoshin.jp/webapi/tdnet/list/{d}.json?limit=1000'
S=requests.Session(); S.headers.update({'User-Agent':'Mozilla/5.0 backlog-v22/2.2'})
CODES=set('1434 1723 1724 1786 186A 1945 1950 1964 1968 1981 2153 2317 2385 261A 2760 3241 3267 3636 3741 3771 3774 3817 3842 402A 4299 4444 4743 478A 4847 5074 5133 5915 6018 6104 6125 6134 6155 6222 6227 6232 6235 6246 6255 6266 6282 6284 6292 6323 6339 6349 6363 6366 6455 6496 6505 6540 6652 6653 6656 6668 6702 6706 6744 6845 6858 6877 6888 6946 6999 7014 7122 7224 7377 7500 7949 8023 8052 8056 8137 8151 8850 9248 9658 9682 9716 9765'.split())
INC=re.compile(r'決算短信|四半期決算|中間期決算|決算説明|決算補足|決算概要|決算資料|業績説明|Financial Results|決算プレゼン|決算説明会|決算報告',re.I)
EXC=re.compile(r'訂正|再訂正|監査報告|招集通知|有価証券報告書|コーポレート.ガバナンス')
BTERM=re.compile(r'受注残|繰越工事|手持工事|受注済残|バックログ|backlog',re.I)
UNITS=[(re.compile(r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*兆円'),1e6),(re.compile(r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*億円'),100),(re.compile(r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*百万円'),1),(re.compile(r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*千円'),.001),(re.compile(r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*万円'),.01)]

def norm(s):
 s=unicodedata.normalize('NFKC',s or '').replace('〜','~').replace('～','~').replace('△','-').replace('▲','-')
 return re.sub(r'\n{3,}','\n\n',re.sub(r'[ \t\u00a0]{2,}',' ',s)).strip()
def days(a,b):
 while a<=b: yield a; a+=dt.timedelta(days=1)
def fetch_day(d):
 u=API.format(d=d.strftime('%Y%m%d')); err=''
 for n in range(4):
  try:
   r=S.get(u,timeout=60)
   if r.status_code==404:return d.isoformat(),[],''
   r.raise_for_status(); o=r.json(); return d.isoformat(),o.get('items',[]) if isinstance(o,dict) else [],''
  except Exception as e: err=f'{type(e).__name__}:{e}'; time.sleep(n+1)
 return d.isoformat(),[],err
def canon(x,day):
 raw=next((v for k,v in x.items() if str(k).lower()=='tdnet' and isinstance(v,dict)),x)
 code=str(raw.get('company_code') or raw.get('ticker') or raw.get('code') or '').strip(); title=norm(str(raw.get('title') or ''))
 if code not in CODES or not INC.search(title) or EXC.search(title): return None
 pub=str(raw.get('pubdate') or ''); ps=pub.split(); date=ps[0] if ps and re.fullmatch(r'20\d\d-\d\d-\d\d',ps[0]) else day; tm=ps[1][:5] if len(ps)>1 else ''
 api=str(raw.get('id') or ''); url=unquote(str(raw.get('document_url') or '').split('?',1)[-1]); m=re.search(r'/inbs/(\d{18})\.pdf',url); fid=m.group(1) if m else api
 if not url.startswith('http') and re.fullmatch(r'\d{18}',fid): url=f'https://www.release.tdnet.info/inbs/{fid}.pdf'
 return {'file_id':fid,'disclosure_date':date,'disclosure_time':tm,'code':code,'company':norm(str(raw.get('company_name') or '')),'title':title,'source_url':url}
def pdf(row):
 fid=str(row.get('file_id') or ''); date=row['disclosure_date'].replace('-',''); urls=[row.get('source_url','')]
 if re.fullmatch(r'\d{18}',fid): urls += [f'https://www.release.tdnet.info/inbs/{fid}.pdf',f'https://tdnet-pdf.kabutan.jp/{date}/{fid}.pdf']
 err=''
 for u in dict.fromkeys(x for x in urls if x):
  for n in range(3):
   try:
    r=S.get(u,timeout=90)
    if r.status_code==200 and r.content.startswith(b'%PDF'): return r.content,u,''
    err=f'HTTP_{r.status_code}:{u}'
   except Exception as e: err=f'{type(e).__name__}:{e}:{u}'
   time.sleep(n+1)
 return None,'',err or 'NO_URL'
def text(b):
 try:
  d=fitz.open(stream=b,filetype='pdf'); t=norm('\n\f\n'.join(p.get_text('text') for p in d)); return t,'' if len(re.sub(r'\s+','',t))>=80 else 'LOW_TEXT'
 except Exception as e:return '',f'{type(e).__name__}:{e}'
def period(title,t,disc):
 z=norm(title+'\n'+t[:3500]); m=re.search(r'(20\d{2})\s*年\s*(\d{1,2})\s*月期',z); fy=int(m.group(1)) if m else None; em=int(m.group(2)) if m else None
 q=1 if re.search(r'第\s*1\s*四半期|第1四半期',z) else 2 if re.search(r'第\s*2\s*四半期|第2四半期|中間期|中間決算',z) else 3 if re.search(r'第\s*3\s*四半期|第3四半期',z) else 4 if re.search(r'決算短信|通期|年度決算|決算概要|決算説明',title,re.I) else None
 pe=None
 if fy and em and q:
  month=em-3*(4-q); year=fy
  while month<=0:month+=12;year-=1
  nxt=dt.date(year+1,1,1) if month==12 else dt.date(year,month+1,1); pe=(nxt-dt.timedelta(days=1)).isoformat()
 return {'fiscal_year_end_year':fy,'fiscal_year_end_month':em,'fiscal_quarter':q,'period_end':pe}
def nums(s):
 a=[]
 for rx,mul in UNITS:
  for x in rx.finditer(s):
   try:a.append((x.start(),float(x.group(1).replace(',',''))*mul,x.group(0)))
   except:pass
 return sorted(a)
def pick(t,labels,win=500):
 best=[]
 for lab in labels:
  for lm in re.finditer(lab,t,re.I):
   st=max(0,lm.start()-60); ch=t[st:min(len(t),lm.end()+win)]
   for p,v,raw in nums(ch):
    ap=st+p; dist=abs(ap-lm.end())+(120 if ap<lm.start() else 0); ctx=re.sub(r'\s+',' ',t[max(0,lm.start()-100):min(len(t),lm.end()+win)]).strip(); best.append((dist,v,ctx,lab))
 if not best:return None,'',''
 _,v,c,l=min(best,key=lambda x:x[0]); return v,c,l
def table_total(t,label):
 for m in re.finditer(label,t,re.I):
  ch=t[m.start():m.start()+1800]
  for line in ch.splitlines():
   if re.search(r'合計|計\s',line):
    vs=nums(line)
    if vs:return vs[-1][1],re.sub(r'\s+',' ',line).strip()
 return None,''
def field(t,kind):
 labs={'backlog':[r'期末受注残高',r'受注残高',r'繰越工事高',r'次期繰越工事高',r'手持工事高',r'受注済残高',r'バックログ'], 'order':[r'受注高',r'受注金額',r'受注実績'], 'sales':[r'売上高',r'売上収益',r'売上額'], 'op':[r'営業利益',r'営業損失'], 'assets':[r'資産合計',r'総資産'], 'contract':[r'契約資産'], 'inventory':[r'棚卸資産',r'仕掛品'], 'receivables':[r'売上債権',r'受取手形及び売掛金'], 'ocf':[r'営業活動によるキャッシュ.フロー']}[kind]
 if kind in {'backlog','order'}:
  v,c=table_total(t,labs[0]);
  if v is not None:return v,c,'A_TABLE'
 v,c,l=pick(t,labs,650 if kind in {'backlog','order'} else 420); conf='A' if c and re.search(r'合計|当社グループ全体|連結|受注残高は|売上高は|資産合計',c) else 'B'
 if c and re.search(r'セグメント|事業部門|部門別',c) and not re.search(r'合計|全社|連結',c):conf='C_SCOPE'
 return v,c,conf
def process(r):
 b,u,e=pdf(r)
 if not b:return {**r,'status':'PDF_DOWNLOAD_FAILED','error':e}
 t,te=text(b); out={**r,'download_url':u,'pdf_sha256':hashlib.sha256(b).hexdigest(),'text_chars':len(t)}
 if not BTERM.search(t):return {**out,'status':'NO_BACKLOG_TERM','error':te}
 out.update(period(r['title'],t,r['disclosure_date']))
 mapping={'backlog_end_m':'backlog','order_cumulative_m':'order','sales_cumulative_m':'sales','operating_profit_cumulative_m':'op','total_assets_m':'assets','contract_assets_m':'contract','inventory_m':'inventory','receivables_m':'receivables','operating_cashflow_cumulative_m':'ocf'}; conf=[]; prov=[]
 for k,kind in mapping.items():
  v,c,q=field(t,kind); out[k]=v; conf.append(q); prov.append({'field':k,'value':v,'confidence':q,'context':c}) if v is not None else None
 out['field_provenance']=prov; out['data_confidence']='C' if any(x.startswith('C') for x in conf) else 'A' if conf and all(x.startswith('A') for x in conf[:5] if x) else 'B'; out['status']='EXTRACTED' if out.get('backlog_end_m') is not None else 'PARTIAL_EXTRACTED'; out['error']=te
 out['one_off_flag']=bool(re.search(r'大型案件|大口受注|単発|一過性|買収|子会社化|連結範囲|為替',t)); out['delay_flag']=bool(re.search(r'延期|遅延|後ろ倒し|検収遅れ|納期長期化|許認可|系統連系',t)); return out
def key(r):return (r.get('code'),r.get('fiscal_year_end_year'),r.get('fiscal_year_end_month'),r.get('fiscal_quarter'))
def enrich(rows):
 rank={'A':3,'B':2,'C':1}; best={}
 for r in rows:
  if r.get('status') not in {'EXTRACTED','PARTIAL_EXTRACTED'} or None in key(r):continue
  k=key(r); sc=(rank.get(r.get('data_confidence'),0),1 if '決算短信' in r.get('title','') else 0,r.get('text_chars',0))
  if k not in best or sc>(rank.get(best[k].get('data_confidence'),0),1 if '決算短信' in best[k].get('title','') else 0,best[k].get('text_chars',0)):best[k]=r
 sel=sorted(best.values(),key=lambda x:(x['code'],x.get('period_end') or '9999')); byfy=defaultdict(list)
 for r in sel:byfy[(r['code'],r['fiscal_year_end_year'],r['fiscal_year_end_month'])].append(r)
 for g in byfy.values():
  g.sort(key=lambda x:x['fiscal_quarter']); ps=po=pp=poc=None
  for r in g:
   q=r['fiscal_quarter']; cs,co,cp,coc=r.get('sales_cumulative_m'),r.get('order_cumulative_m'),r.get('operating_profit_cumulative_m'),r.get('operating_cashflow_cumulative_m')
   r['sales_quarter_m']=cs if q==1 else cs-ps if cs is not None and ps is not None else None; r['order_quarter_m']=co if q==1 else co-po if co is not None and po is not None else None; r['operating_profit_quarter_m']=cp if q==1 else cp-pp if cp is not None and pp is not None else None; r['operating_cashflow_quarter_m']=coc if q==1 else coc-poc if coc is not None and poc is not None else None; ps,po,pp,poc=cs,co,cp,coc
 by=defaultdict(list)
 for r in sel:by[r['code']].append(r)
 for g in by.values():
  g.sort(key=lambda x:x.get('period_end') or '9999')
  for i,r in enumerate(g):
   if i>=3:
    z=g[i-3:i+1]
    for src,dst in [('sales_quarter_m','ltm_sales_m'),('order_quarter_m','ltm_order_m'),('operating_profit_quarter_m','ltm_operating_profit_m')]:
     if all(x.get(src) is not None for x in z):r[dst]=sum(float(x[src]) for x in z)
   b,s,o,op=r.get('backlog_end_m'),r.get('sales_quarter_m'),r.get('order_quarter_m'),r.get('operating_profit_quarter_m'); ltm=r.get('ltm_sales_m')
   r['backlog_to_ltm_sales']=b/ltm if b is not None and ltm and ltm>0 else None; r['backlog_months']=12*r['backlog_to_ltm_sales'] if r.get('backlog_to_ltm_sales') is not None else None; r['book_to_bill_quarter']=o/s if o is not None and s and s>0 else None; r['op_margin_quarter']=op/s if op is not None and s and s>0 else None
   if i:
    pb=g[i-1].get('backlog_end_m'); r['backlog_qoq']=b/pb-1 if b is not None and pb and pb>0 else None; r['conversion_proxy']=s/pb if s is not None and pb and pb>0 else None
 return sel
def writecsv(p,rows):
 if not rows:p.write_text('',encoding='utf-8');return
 keys=[];seen=set()
 for r in rows:
  for k in r:
   if k not in seen:seen.add(k);keys.append(k)
 with p.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=keys);w.writeheader();
  for r in rows:w.writerow({k:json.dumps(v,ensure_ascii=False,separators=(',',':')) if isinstance(v,(list,dict)) else v for k,v in r.items()})
def segment(a,b,out,workers):
 out.mkdir(parents=True,exist_ok=True)
 with cf.ThreadPoolExecutor(max_workers=20) as ex: dr=list(ex.map(fetch_day,list(days(a,b))))
 aud=[]; cand=[]
 for day,items,err in sorted(dr):
  c=[r for x in items if (r:=canon(x,day))]; cand+=c; aud.append({'date':day,'status':'ERROR' if err else 'OK','error':err,'disclosures':len(items),'candidate_filings':len(c)})
 cand=list({str(r.get('file_id') or r.get('source_url')):r for r in cand}.values())
 with cf.ThreadPoolExecutor(max_workers=workers) as ex: rows=list(ex.map(process,cand))
 rows.sort(key=lambda r:(r.get('disclosure_date',''),r.get('code',''),r.get('file_id','')))
 with (out/'history_raw.jsonl').open('w',encoding='utf-8') as f:
  for r in rows:f.write(json.dumps(r,ensure_ascii=False)+'\n')
 (out/'source_audit.json').write_text(json.dumps(aud,ensure_ascii=False,indent=2),encoding='utf-8'); sm={'start':a.isoformat(),'end':b.isoformat(),'disclosures':sum(x['disclosures'] for x in aud),'candidate_filings':len(cand),'status_counts':dict(Counter(r.get('status','') for r in rows))};(out/'summary.json').write_text(json.dumps(sm,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(sm,ensure_ascii=False))
def aggregate(parts,out):
 out.mkdir(parents=True,exist_ok=True); raw=[]
 for p in parts.rglob('history_raw.jsonl'):
  with p.open(encoding='utf-8') as f:raw += [json.loads(x) for x in f if x.strip()]
 raw=list({str(r.get('file_id') or r.get('source_url')):r for r in raw}.values()); raw.sort(key=lambda r:(r.get('disclosure_date',''),r.get('code',''))); q=enrich(raw); writecsv(out/'backlog_history_raw.csv',raw); writecsv(out/'backlog_history_quarterly.csv',q); sm={'raw_filings':len(raw),'quarterly_observations':len(q),'codes':len(set(r['code'] for r in q)),'status_counts':dict(Counter(r.get('status','') for r in raw)),'confidence_counts':dict(Counter(r.get('data_confidence','') for r in q)),'backlog_ratio_available':sum(r.get('backlog_to_ltm_sales') is not None for r in q),'book_to_bill_available':sum(r.get('book_to_bill_quarter') is not None for r in q)};(out/'summary.json').write_text(json.dumps(sm,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(sm,ensure_ascii=False))
def main():
 p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True);a=s.add_parser('segment');a.add_argument('--start',required=True);a.add_argument('--end',required=True);a.add_argument('--out',required=True);a.add_argument('--workers',type=int,default=12);g=s.add_parser('aggregate');g.add_argument('--parts',required=True);g.add_argument('--out',required=True);x=p.parse_args(); segment(dt.date.fromisoformat(x.start),dt.date.fromisoformat(x.end),Path(x.out),x.workers) if x.cmd=='segment' else aggregate(Path(x.parts),Path(x.out))
if __name__=='__main__':main()
