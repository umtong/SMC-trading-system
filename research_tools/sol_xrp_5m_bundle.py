#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,gzip,hashlib,json,math,shutil,time,urllib.request,zipfile
from dataclasses import asdict,dataclass
from pathlib import Path
BASE='https://data.binance.vision/data/futures/um/monthly/klines'
COLS=('open_time','open','high','low','close','volume','close_time','quote_asset_volume','number_of_trades','taker_buy_base_asset_volume','taker_buy_quote_asset_volume','ignore')
@dataclass(frozen=True)
class Source: symbol:str;month:str;url:str;published_sha256:str;observed_sha256:str;rows:int;first_open_time_ms:int|None;last_open_time_ms:int|None
def months(a,b):
 y,m=map(int,a.split('-'));ey,em=map(int,b.split('-'))
 while (y,m)<=(ey,em):yield f'{y:04d}-{m:02d}';m+=1; y,m=(y+1,1) if m==13 else (y,m)
def sha(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()
def get(u,p,attempts=6):
 e=None
 for k in range(attempts):
  try:
   q=urllib.request.Request(u,headers={'User-Agent':'smc-sol-xrp-5m/1.0'})
   with urllib.request.urlopen(q,timeout=300) as r,Path(p).open('wb') as o:shutil.copyfileobj(r,o,1<<20)
   return
  except Exception as x:e=x;Path(p).unlink(missing_ok=True);time.sleep(min(20,2**k))
 raise RuntimeError(f'{u}: {e!r}')
def check(p,n):
 s=Path(p).read_text(encoding='utf-8-sig').strip();xs=[x.lower() for x in s.replace('*',' ').split() if len(x)==64]
 if n not in s or not xs:raise ValueError(f'bad checksum {n}')
 return xs[0]
def epoch(x):
 v=int(float(x));return v//1000 if abs(v)>=10**15 else v
def rows(p):
 with zipfile.ZipFile(p) as z:
  ns=[n for n in z.namelist() if n.endswith('.csv')]
  if len(ns)!=1:raise ValueError(ns)
  for r in csv.reader(line.decode('utf-8-sig') for line in z.open(ns[0])):
   if r:yield [x.strip() for x in r]
def build(sym,mons,out):
 cache=out/'.cache'/sym;cache.mkdir(parents=True,exist_ok=True);target=out/f'{sym}_5m.csv.gz';src=[];prior=None;gaps=dups=total=0;first=last=None
 with gzip.open(target,'wt',newline='',compresslevel=6) as f:
  w=csv.writer(f);w.writerow(COLS)
  for mo in mons:
   n=f'{sym}-5m-{mo}.zip';u=f'{BASE}/{sym}/5m/{n}';z=cache/n;c=cache/(n+'.CHECKSUM');get(u+'.CHECKSUM',c);get(u,z);pub=check(c,n);obs=sha(z)
   if pub!=obs:raise ValueError(f'checksum {n}')
   cnt=0;mf=ml=None
   for i,r in enumerate(rows(z),1):
    if r[0].lower().replace(' ','_')=='open_time':continue
    if len(r)<12:raise ValueError(f'short {n}:{i}')
    q=r[:12];ot=epoch(q[0]);ct=epoch(q[6]);o,h,l,cl=map(float,q[1:5]);vol=float(q[5]);qv=float(q[7]);tr=int(float(q[8]));tb=float(q[9]);tbq=float(q[10])
    if not all(math.isfinite(v) for v in (o,h,l,cl,vol,qv,tb,tbq)) or min(o,h,l,cl)<=0 or h<max(o,cl) or l>min(o,cl) or h<l or min(vol,qv,tb,tbq)<0 or tr<0:raise ValueError(f'bad row {n}:{i}')
    if prior is not None:
     d=ot-prior;dups+=int(d==0);gaps+=int(d!=300000)
     if d<=0:raise ValueError(f'nonmonotonic {n}:{i}')
    prior=ot;first=ot if first is None else first;last=ot;mf=ot if mf is None else mf;ml=ot;q[0]=str(ot);q[6]=str(ct);q[8]=str(tr);w.writerow(q);cnt+=1;total+=1
   src.append(Source(sym,mo,u,pub,obs,cnt,mf,ml));z.unlink();c.unlink()
 return {'rows':total,'first_open_time_ms':first,'last_open_time_ms':last,'gap_transitions':gaps,'duplicate_open_times':dups,'output':target.name,'output_sha256':sha(target),'output_bytes':target.stat().st_size},src
def main():
 a=argparse.ArgumentParser();a.add_argument('--symbols',nargs='+',default=['SOLUSDT','XRPUSDT']);a.add_argument('--start',default='2022-04');a.add_argument('--end',default='2025-12');a.add_argument('--out',type=Path,required=True);z=a.parse_args();z.out.mkdir(parents=True,exist_ok=True);mons=tuple(months(z.start,z.end));ds={};ss=[]
 for s in z.symbols:ds[s],r=build(s.upper(),mons,z.out);ss+=r
 shutil.rmtree(z.out/'.cache',ignore_errors=True);man={'contract':{'source':'Binance Vision USD-M monthly 5m klines','symbols':[s.upper() for s in z.symbols],'start':z.start,'end':z.end,'checksum':'published SHA-256 verified per archive','credentials_used':False,'orders_submitted':False},'datasets':ds,'sources':[asdict(x) for x in ss]};(z.out/'manifest.json').write_text(json.dumps(man,indent=2));print(json.dumps(ds,indent=2))
if __name__=='__main__':main()
