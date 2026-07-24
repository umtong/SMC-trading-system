from __future__ import annotations
import argparse, hashlib, json, time, zipfile
from pathlib import Path
import pandas as pd
import requests
BASE='https://data.binance.vision/data/futures/um/monthly/markPriceKlines'
COLS=['open_time','open','high','low','close','ignore','close_time','x1','x2','x3','x4','x5']
def digest(p:Path)->str:
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(8<<20),b''):h.update(b)
 return h.hexdigest()
def infer(s):
 x=pd.to_numeric(s,errors='raise');m=float(x.abs().median());return 'ns' if m>1e17 else 'us' if m>1e14 else 'ms' if m>1e11 else 's'
def get(session,url,out,attempts=6):
 out.mkdir(parents=True,exist_ok=True);name=url.rsplit('/',1)[-1];p=out/name;c=out/(name+'.CHECKSUM');err=None
 for i in range(attempts):
  try:
   if not c.exists():
    r=session.get(url+'.CHECKSUM',timeout=(30,180));r.raise_for_status();c.write_bytes(r.content)
   exp=c.read_text(encoding='utf-8-sig').strip().split()[0].lower()
   if not p.exists() or digest(p)!=exp:
    tmp=p.with_suffix('.part');tmp.unlink(missing_ok=True)
    with session.get(url,stream=True,timeout=(30,600)) as r:
     r.raise_for_status()
     with tmp.open('wb') as f:
      for b in r.iter_content(8<<20):
       if b:f.write(b)
    tmp.replace(p)
   got=digest(p)
   if got!=exp:raise RuntimeError((got,exp,url))
   return p,{'url':url,'bytes':p.stat().st_size,'sha256':got}
  except Exception as e:
   err=e
   if i+1<attempts:time.sleep(min(30,2**i))
 raise RuntimeError(f'{url}: {err!r}')
def parse(p):
 with zipfile.ZipFile(p) as z:
  names=[n for n in z.namelist() if n.lower().endswith('.csv')]
  if len(names)!=1:raise RuntimeError(names)
  with z.open(names[0]) as f:
   first=f.readline().decode('utf-8','replace').split(',')[0].strip().lower();f.seek(0);header=first in {'open_time','open time'};d=pd.read_csv(f,header=0 if header else None,names=None if header else COLS)
 d.columns=[str(c).strip().lower().replace(' ','_') for c in d.columns]
 if 'open_time' not in d.columns:d.columns=COLS[:len(d.columns)]
 out=pd.DataFrame({'timestamp':pd.to_datetime(pd.to_numeric(d.open_time,errors='raise'),unit=infer(d.open_time),utc=True)})
 for c in ('open','high','low','close'):out[c]=pd.to_numeric(d[c],errors='raise')
 return out
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--symbol',required=True);ap.add_argument('--year',type=int,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--cache',type=Path,required=True);a=ap.parse_args();assert a.year<=2024
 a.output.mkdir(parents=True,exist_ok=True);s=requests.Session();s.headers['User-Agent']='SMC-DV-AII-mark/1.0';parts=[];records=[]
 for m in range(1,13):
  stamp=f'{a.year}-{m:02d}';name=f'{a.symbol}-1m-{stamp}.zip';url=f'{BASE}/{a.symbol}/1m/{name}';p,r=get(s,url,a.cache/a.symbol);r['period']=stamp;records.append(r);parts.append(parse(p));print('MARK',a.symbol,stamp,r['bytes'],flush=True)
 d=pd.concat(parts,ignore_index=True).sort_values('timestamp').drop_duplicates('timestamp');start=pd.Timestamp(f'{a.year}-01-01',tz='UTC');end=pd.Timestamp(f'{a.year+1}-01-01',tz='UTC');d=d[(d.timestamp>=start)&(d.timestamp<end)]
 expected=int((end-start)/pd.Timedelta(minutes=1));assert len(d)==expected,(len(d),expected);p=a.output/f'{a.symbol}_{a.year}_mark_1m.parquet';d.to_parquet(p,index=False,compression='zstd');manifest={'symbol':a.symbol,'year':a.year,'rows':len(d),'path':str(p),'sha256':digest(p),'archives':records,'candidate_pnl_observed':False,'orders_submitted':False};(a.output/f'{a.symbol}_{a.year}_mark_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print(json.dumps({k:manifest[k] for k in ('symbol','year','rows','sha256')},indent=2))
if __name__=='__main__':main()
