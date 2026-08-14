from __future__ import annotations
import datetime as dt, json, re
from pathlib import Path
import fitz, requests

OUT=Path('out/more-20260814'); OUT.mkdir(parents=True, exist_ok=True)
CANDS=[
 ('4657','環境管理センター','2026-08-14','14:00','https://www.release.tdnet.info/inbs/140120260814520276.pdf','4657.T'),
 ('6298','ワイエイシイHD','2026-08-13','15:30','https://www.release.tdnet.info/inbs/140120260807515542.pdf','6298.T'),
 ('3446','JTEC CORPORATION','2026-08-10','14:30','https://www.release.tdnet.info/inbs/140120260810516071.pdf','3446.T'),
 ('1724','シンクレイヤ','2026-08-12','15:30','https://www.release.tdnet.info/inbs/140120260812517868.pdf','1724.T'),
 ('3763','プロシップ','2026-08-14','16:00','https://www.release.tdnet.info/inbs/140120260813520053.pdf','3763.T'),
 ('3968','セグエグループ','2026-08-13','11:30','https://www.release.tdnet.info/inbs/140120260812517955.pdf','3968.T'),
 ('290A','Synspective','2026-08-14','15:30','https://www.release.tdnet.info/inbs/140120260813519889.pdf','290A.T'),
 ('6846','中央製作所','2026-08-12','14:40','https://www.release.tdnet.info/inbs/140120260810516054.pdf','6846.N'),
]
S=requests.Session(); S.headers.update({'User-Agent':'Mozilla/5.0 backlog-more/1.0'})
TERMS=['資産合計','総資産','受注残高','受注残','受注高','売上高','営業利益','純資産','自己資本','１株当たり当期純利益','1株当たり当期純利益','EPS']

def ctx(text,term):
 out=[]
 for m in re.finditer(re.escape(term),text):
  s=re.sub(r'\s+',' ',text[max(0,m.start()-650):m.end()+1200]).strip()
  if s not in out: out.append(s)
  if len(out)>=6: break
 return out

def pdf_info(c):
 code,name,date,tim,url,sym=c
 r=S.get(url,timeout=90); ok=r.status_code==200 and r.content.startswith(b'%PDF')
 rec={'code':code,'company':name,'date':date,'time':tim,'url':url,'symbol':sym,'http':r.status_code,'pdf':ok}
 if ok:
  d=fitz.open(stream=r.content,filetype='pdf'); text='\n'.join(p.get_text('text') for p in d)
  rec['pages']=len(d); rec['contexts']={t:ctx(text,t) for t in TERMS if t in text}
 return rec

def yahoo(sym):
 start=int(dt.datetime(2026,8,3,tzinfo=dt.timezone.utc).timestamp()); end=int(dt.datetime(2026,8,16,tzinfo=dt.timezone.utc).timestamp())
 u=f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={start}&period2={end}&interval=1d&events=div%2Csplits&includeAdjustedClose=true'
 r=S.get(u,timeout=30); r.raise_for_status(); j=r.json()['chart']['result'][0]
 ts=j['timestamp']; q=j['indicators']['quote'][0]; adj=j['indicators'].get('adjclose',[{}])[0].get('adjclose',q['close'])
 return [{'date':dt.datetime.fromtimestamp(t,dt.timezone.utc).date().isoformat(),'close':c,'adjclose':a,'volume':v} for t,c,a,v in zip(ts,q['close'],adj,q['volume']) if c is not None]

records=[]
for c in CANDS:
 try: records.append(pdf_info(c))
 except Exception as e: records.append({'code':c[0],'company':c[1],'error':f'{type(e).__name__}: {e}'})
prices={}
for c in CANDS:
 try: prices[c[0]]=yahoo(c[5])
 except Exception as e: prices[c[0]]={'error':f'{type(e).__name__}: {e}'}
try: prices['1306']=yahoo('1306.T')
except Exception as e: prices['1306']={'error':f'{type(e).__name__}: {e}'}
(OUT/'pdf_contexts.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
(OUT/'prices.json').write_text(json.dumps(prices,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'pdf_ok':sum(bool(r.get('pdf')) for r in records),'records':len(records),'price_ok':sum(isinstance(v,list) for v in prices.values())},ensure_ascii=False))
