from __future__ import annotations
import datetime as dt, json, re, time
from pathlib import Path
import fitz, requests

OUT=Path('out/targeted-20260814'); OUT.mkdir(parents=True, exist_ok=True)
CANDS=[
 ('6266','タツモ','2026-08-10','15:30','https://www.release.tdnet.info/inbs/140120260810516594.pdf'),
 ('7721','東京計器','2026-08-10','16:00','https://www.release.tdnet.info/inbs/140120260807515817.pdf'),
 ('3671','ソフトＭＡＸ','2026-08-12','15:30','https://www.release.tdnet.info/inbs/140120260806513010.pdf'),
 ('407A','UNICON HD','2026-08-12','15:30','https://www.release.tdnet.info/inbs/140120260812517878.pdf'),
 ('478A','フツパー','2026-08-12','15:30','https://www.release.tdnet.info/inbs/140120260812518067.pdf'),
 ('6540','船場','2026-08-12','15:30','https://www.release.tdnet.info/inbs/140120260812517657.pdf'),
 ('6846','中央製作所','2026-08-12','14:40','https://www.release.tdnet.info/inbs/140120260810516054.pdf'),
 ('7369','メイホーHD','2026-08-13','13:00','https://www.release.tdnet.info/inbs/140120260813519149.pdf'),
 ('4299','ハイマックス','2026-08-14','13:00','https://www.release.tdnet.info/inbs/140120260813519687.pdf'),
 ('4371','CCT','2026-08-14','16:00','https://www.release.tdnet.info/inbs/140120260814520530.pdf'),
 ('5074','テスHD','2026-08-14','15:00','https://www.release.tdnet.info/inbs/140120260814521035.pdf'),
 ('5133','テリロジーHD','2026-08-14','15:30','https://www.release.tdnet.info/inbs/140120260813519972.pdf'),
 ('6232','ACSL','2026-08-14','15:30','https://www.release.tdnet.info/inbs/140120260814520365.pdf'),
 ('6268','ナブテスコ','2026-08-14','16:00','https://www.release.tdnet.info/inbs/140120260814521255.pdf'),
 ('9248','人・夢・技術G','2026-08-14','16:00','https://www.release.tdnet.info/inbs/140120260812518313.pdf'),
]
S=requests.Session(); S.headers.update({'User-Agent':'Mozilla/5.0 backlog-targeted/1.0'})
TERMS=['資産合計','総資産','受注残高','受注高','売上高','営業利益','純資産','Book-to-Bill','BBレシオ']

def ctx(text,term):
 out=[]
 for m in re.finditer(re.escape(term),text):
  s=re.sub(r'\s+',' ',text[max(0,m.start()-500):m.end()+900]).strip()
  if s not in out: out.append(s)
  if len(out)>=4: break
 return out

def pdf_info(code,name,date,tim,url):
 r=S.get(url,timeout=90); ok=r.status_code==200 and r.content.startswith(b'%PDF')
 rec={'code':code,'company':name,'date':date,'time':tim,'url':url,'http':r.status_code,'pdf':ok}
 if ok:
  d=fitz.open(stream=r.content,filetype='pdf'); text='\n'.join(p.get_text('text') for p in d)
  rec['pages']=len(d); rec['contexts']={t:ctx(text,t) for t in TERMS if t in text}
 return rec

def yahoo(code):
 start=int(dt.datetime(2026,8,3,tzinfo=dt.timezone.utc).timestamp()); end=int(dt.datetime(2026,8,16,tzinfo=dt.timezone.utc).timestamp())
 u=f'https://query1.finance.yahoo.com/v8/finance/chart/{code}.T?period1={start}&period2={end}&interval=1d&events=div%2Csplits&includeAdjustedClose=true'
 r=S.get(u,timeout=30); r.raise_for_status(); j=r.json()['chart']['result'][0]
 ts=j['timestamp']; q=j['indicators']['quote'][0]; adj=j['indicators'].get('adjclose',[{}])[0].get('adjclose',q['close'])
 return [{'date':dt.datetime.fromtimestamp(t,dt.timezone.utc).date().isoformat(),'close':c,'adjclose':a,'volume':v} for t,c,a,v in zip(ts,q['close'],adj,q['volume']) if c is not None]

records=[]
for c in CANDS:
 try: records.append(pdf_info(*c))
 except Exception as e: records.append({'code':c[0],'company':c[1],'error':f'{type(e).__name__}: {e}'})
prices={}
for c in CANDS:
 try: prices[c[0]]=yahoo(c[0])
 except Exception as e: prices[c[0]]={'error':f'{type(e).__name__}: {e}'}
(OUT/'pdf_contexts.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
(OUT/'prices.json').write_text(json.dumps(prices,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'pdf_ok':sum(r.get('pdf') for r in records),'records':len(records),'price_ok':sum(isinstance(v,list) for v in prices.values())},ensure_ascii=False))
