#!/usr/bin/env python3
"""Checksum-verified Binance USD-M futures metrics collector.

Research-only. Preserves the native create_time as the actual known-at clock,
keeps missing metrics as missing, and never accesses credentials or orders.
"""
from __future__ import annotations

import argparse, csv, gzip, hashlib, json, math, shutil, tempfile, time
import urllib.error, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

BASE='https://data.binance.vision/data/futures/um/daily/metrics'
FIELDS=(
 'sum_open_interest','sum_open_interest_value',
 'count_toptrader_long_short_ratio','sum_toptrader_long_short_ratio',
 'count_long_short_ratio','sum_taker_long_short_vol_ratio')

@dataclass(frozen=True)
class Source:
    symbol:str; day:str; url:str; published_sha256:str; observed_sha256:str; rows:int


def sha256(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()

def days(a:str,b:str)->Iterator[str]:
    x=date.fromisoformat(a); y=date.fromisoformat(b)
    while x<=y:yield x.isoformat();x+=timedelta(days=1)

def get(url:str,path:Path,attempts:int=6)->bool:
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+'.part')
    for i in range(attempts):
        try:
            q=urllib.request.Request(url,headers={'User-Agent':'smc-ict-metrics-v3/1.0'})
            with urllib.request.urlopen(q,timeout=180) as r,tmp.open('wb') as o:shutil.copyfileobj(r,o,1<<20)
            tmp.replace(path);return True
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)
            if e.code==404:return False
            err=e
        except Exception as e:
            tmp.unlink(missing_ok=True);err=e
        if i+1<attempts:time.sleep(min(20,2**i))
    raise RuntimeError(f'{url}: {err!r}')

def checksum(path:Path,name:str)->str:
    text=path.read_text(encoding='utf-8-sig').strip()
    vals=[x.lower() for x in text.replace('*',' ').split() if len(x)==64 and all(c in '0123456789abcdefABCDEF' for c in x)]
    if name not in text or not vals:raise ValueError(f'bad checksum {name}')
    return vals[0]

def parse_time(raw:str)->int:
    s=raw.strip()
    try:
        v=int(float(s)); a=abs(v)
        if a<10**11:v*=1000
        elif a>=10**15:v//=1000
        if 10**12<=v<=10**14:return v
    except ValueError:pass
    # Source strings are naive UTC, never local time.
    dt=datetime.fromisoformat(s.replace('Z','+00:00'))
    if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp()*1000)

def parse_metric(raw:str)->str:
    s=raw.strip()
    if s=='':return ''
    v=float(s)
    if not math.isfinite(v) or v<0:raise ValueError(f'invalid metric {raw!r}')
    return format(v,'.17g')

def fetch_one(symbol:str,day:str,cache:Path):
    name=f'{symbol}-metrics-{day}.zip';url=f'{BASE}/{symbol}/{name}';z=cache/symbol/name;c=cache/symbol/(name+'.CHECKSUM')
    cz=get(url+'.CHECKSUM',c);zz=get(url,z)
    if cz!=zz:raise ValueError(f'archive/checksum availability mismatch {url}')
    if not zz:c.unlink(missing_ok=True);return None
    pub=checksum(c,name);obs=sha256(z);c.unlink(missing_ok=True)
    if pub!=obs:z.unlink(missing_ok=True);raise ValueError(f'checksum mismatch {name}')
    return symbol,day,url,z,pub,obs

def read_day(item):
    symbol,day,url,z,pub,obs=item; rows=[]
    with zipfile.ZipFile(z) as a:
        names=[n for n in a.namelist() if not n.endswith('/')]
        if len(names)!=1:raise ValueError(f'one csv expected {z}')
        with a.open(names[0]) as raw:
            r=csv.reader(line.decode('utf-8-sig') for line in raw)
            header=tuple(x.strip().lower().replace(' ','_') for x in next(r))
            expected=('create_time','symbol',*FIELDS)
            if header!=expected:raise ValueError(f'header {header} != {expected}')
            for line_no,row in enumerate(r,2):
                if not row:continue
                if len(row)!=8:raise ValueError(f'field count {z}:{line_no}')
                if row[1].strip().upper()!=symbol:raise ValueError(f'symbol {z}:{line_no}')
                t=parse_time(row[0]);slot=t//300000*300000
                rows.append((t,slot,symbol,*[parse_metric(x) for x in row[2:]]))
    z.unlink(missing_ok=True)
    return Source(symbol,day,url,pub,obs,len(rows)),rows

def main()->int:
    p=argparse.ArgumentParser();p.add_argument('--symbols',nargs='+',default=['BTCUSDT','ETHUSDT']);p.add_argument('--start-date',default='2023-01-01');p.add_argument('--end-date',default='2025-12-31');p.add_argument('--workers',type=int,default=16);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
    a.output_dir.mkdir(parents=True,exist_ok=True);cache=a.output_dir/'.cache';req=[(s.upper(),d) for s in a.symbols for d in days(a.start_date,a.end_date)]
    got={s.upper():[] for s in a.symbols};missing={s.upper():[] for s in a.symbols}
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        fs={ex.submit(fetch_one,s,d,cache):(s,d) for s,d in req}
        for i,f in enumerate(as_completed(fs),1):
            s,d=fs[f];x=f.result();(missing[s] if x is None else got[s]).append(d if x is None else x)
            if i%100==0 or i==len(fs):print(f'download {i}/{len(fs)}',flush=True)
    sources=[];meta={}
    for s in got:
        allrows=[]
        for item in sorted(got[s],key=lambda x:x[1]):
            src,rows=read_day(item);sources.append(src);allrows.extend(rows)
        allrows.sort(key=lambda x:(x[0],x[1]))
        exact_dups=0;slot_dups=0;outrows=[];prev_t=None;prev_slot=None
        for row in allrows:
            t,slot=row[0],row[1]
            if t==prev_t:exact_dups+=1;outrows[-1]=row
            else:
                if slot==prev_slot:slot_dups+=1
                outrows.append(row)
            prev_t=t;prev_slot=slot
        out=a.output_dir/f'{s}_metrics_5m.csv.gz'
        with gzip.open(out,'wt',encoding='utf-8',newline='',compresslevel=6) as h:
            w=csv.writer(h);w.writerow(('create_time_ms','slot_start_ms','symbol',*FIELDS));w.writerows(outrows)
        slots=[r[1] for r in outrows];unique=len(set(slots));expected=(max(slots)-min(slots))//300000+1 if slots else 0
        nulls={f:sum(r[3+i]=='' for r in outrows) for i,f in enumerate(FIELDS)}
        meta[s]={'rows':len(outrows),'first_known_at_ms':outrows[0][0] if outrows else None,'last_known_at_ms':outrows[-1][0] if outrows else None,'unique_nominal_slots':unique,'expected_slots_between_endpoints':expected,'missing_nominal_slots':expected-unique,'exact_timestamp_duplicates':exact_dups,'multiple_rows_same_nominal_slot':slot_dups,'null_counts':nulls,'missing_daily_files':sorted(missing[s]),'output':out.name,'output_sha256':sha256(out),'output_bytes':out.stat().st_size}
    shutil.rmtree(cache,ignore_errors=True)
    manifest={'contract':{'source':'Binance Vision USD-M daily metrics','actual_create_time_preserved':True,'nominal_slot_used_for_audit_only':True,'missing_values_preserved':True,'credentials_used':False,'orders_submitted':False,'start_date':a.start_date,'end_date':a.end_date},'datasets':meta,'sources':[asdict(x) for x in sources]}
    (a.output_dir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(meta,indent=2),flush=True);return 0
if __name__=='__main__':raise SystemExit(main())
