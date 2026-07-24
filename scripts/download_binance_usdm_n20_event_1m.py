from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

BASE_URL = 'https://data.binance.vision/data/futures/um/daily/klines'
COLUMNS = [
    'open_time','open','high','low','close','volume','close_time','quote_volume',
    'trades','taker_buy_base','taker_buy_quote','ignore',
]

@dataclass(frozen=True, slots=True)
class ArchiveSpec:
    symbol: str
    date: str
    @property
    def filename(self) -> str:
        return f'{self.symbol}-1m-{self.date}.zip'
    @property
    def url(self) -> str:
        return f'{BASE_URL}/{self.symbol}/1m/{self.filename}'

def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def parse_checksum(text: str) -> str:
    match=re.search(r'\b([0-9a-fA-F]{64})\b',text)
    if match is None:raise ValueError(f'invalid checksum payload: {text[:120]!r}')
    return match.group(1).lower()

def fetch(spec: ArchiveSpec, *, timeout: int=120, retries: int=5) -> tuple[ArchiveSpec,bytes,dict[str,object]]:
    headers={'User-Agent':'smc-ict-n20-event-1m/1.0'};last=None
    for attempt in range(1,retries+1):
        try:
            r=requests.get(spec.url,headers=headers,timeout=timeout)
            if r.status_code==404:raise FileNotFoundError(spec.url)
            r.raise_for_status();blob=r.content
            c=requests.get(spec.url+'.CHECKSUM',headers=headers,timeout=timeout);c.raise_for_status()
            expected=parse_checksum(c.text);actual=sha256_bytes(blob)
            if expected!=actual:raise ValueError(f'checksum mismatch {spec.filename}: {actual} != {expected}')
            return spec,blob,{'url':spec.url,'filename':spec.filename,'bytes':len(blob),'sha256':actual,'attempt':attempt}
        except FileNotFoundError:raise
        except Exception as exc:
            last=exc;time.sleep(min(2**attempt,20))
    assert last is not None;raise last

def timestamp_unit(values: pd.Series) -> str:
    numeric=pd.to_numeric(values,errors='raise')
    return 'us' if numeric.abs().median()>10**14 else 'ms'

def parse_zip(blob: bytes, spec: ArchiveSpec) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names=[n for n in z.namelist() if n.lower().endswith('.csv')]
        if len(names)!=1:raise ValueError(f'{spec.filename}: expected one CSV, got {names}')
        raw=z.read(names[0])
    first=raw.splitlines()[0].decode('utf-8',errors='replace').lower()
    frame=pd.read_csv(io.BytesIO(raw),header=0 if 'open_time' in first else None)
    if frame.shape[1]<12:raise ValueError(f'{spec.filename}: expected 12 columns, got {frame.shape[1]}')
    frame=frame.iloc[:,:12].copy();frame.columns=COLUMNS
    for c in COLUMNS:frame[c]=pd.to_numeric(frame[c],errors='raise')
    unit=timestamp_unit(frame.open_time)
    frame['open_time']=pd.to_datetime(frame.open_time,unit=unit,utc=True)
    frame['close_time']=pd.to_datetime(frame.close_time,unit=unit,utc=True)
    frame['symbol']=spec.symbol;frame['source_date']=spec.date
    return frame

def validate(frame: pd.DataFrame) -> dict[str,object]:
    q=frame.sort_values('open_time');dup=int(q.open_time.duplicated().sum())
    if dup:raise ValueError(f'duplicate 1m timestamps: {dup}')
    bad=int(((q.high<q[['open','low','close']].max(axis=1))|(q.low>q[['open','high','close']].min(axis=1))|(q[['open','high','low','close']]<=0).any(axis=1)|(q.volume<0)).sum())
    if bad:raise ValueError(f'invalid OHLCV rows: {bad}')
    delta=q.open_time.diff().dropna();irregular=delta[delta!=pd.Timedelta(minutes=1)]
    return {'rows':len(q),'start':q.open_time.min().isoformat(),'end':q.open_time.max().isoformat(),'duplicates':dup,'irregular_intervals':int(len(irregular)),'zero_volume_rows':int((q.volume==0).sum())}

def event_coverage(events: pd.DataFrame, datasets: dict[str,pd.DataFrame]) -> pd.DataFrame:
    rows=[]
    for e in events.itertuples(index=False):
        t=pd.Timestamp(e.signal_time)
        if t.tz is None:t=t.tz_localize('UTC')
        else:t=t.tz_convert('UTC')
        hold=36 if e.quality_tier=='HIGH_QUALITY' else 24
        start=t-pd.Timedelta(minutes=60);end=t+pd.Timedelta(hours=hold)
        q=datasets[e.symbol];window=q[(q.open_time>=start)&(q.open_time<end)]
        expected=int((end-start)/pd.Timedelta(minutes=1))
        contiguous=(len(window)==expected and (window.open_time.diff().dropna()==pd.Timedelta(minutes=1)).all())
        rows.append({'symbol':e.symbol,'signal_time':t,'quality_tier':e.quality_tier,'window_start':start,'window_end':end,'expected_rows':expected,'available_rows':len(window),'complete_contiguous':bool(contiguous),'right_censored':bool(q.open_time.max()<end-pd.Timedelta(minutes=1))})
    return pd.DataFrame(rows)

def main() -> int:
    p=argparse.ArgumentParser();p.add_argument('--archive-dates',type=Path,required=True);p.add_argument('--events',type=Path,required=True);p.add_argument('--output',type=Path,default=Path('artifacts/binance_usdm_n20_event_1m'));p.add_argument('--workers',type=int,default=16);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    dates=pd.read_csv(a.archive_dates,dtype=str);events=pd.read_csv(a.events)
    specs=[ArchiveSpec(str(r.symbol),str(r.date)) for r in dates.itertuples(index=False)]
    chunks:dict[str,list[pd.DataFrame]]={};archives=[];missing=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures={pool.submit(fetch,s):s for s in specs}
        for i,future in enumerate(concurrent.futures.as_completed(futures),1):
            spec=futures[future]
            try:returned,blob,meta=future.result()
            except FileNotFoundError:missing.append(spec.url);continue
            frame=parse_zip(blob,returned);chunks.setdefault(returned.symbol,[]).append(frame);archives.append({**asdict(returned),**meta})
            if i%100==0:print(f'processed {i}/{len(specs)}',flush=True)
    datasets={};frames={}
    for symbol,parts in sorted(chunks.items()):
        frame=pd.concat(parts,ignore_index=True).sort_values('open_time').drop_duplicates('open_time',keep='last').reset_index(drop=True)
        frames[symbol]=frame
        target=a.output/f'{symbol}_1m.parquet';frame.to_parquet(target,index=False,compression='zstd')
        csv=a.output/f'{symbol}_1m.csv.gz';frame.to_csv(csv,index=False,compression={'method':'gzip','compresslevel':6,'mtime':0},float_format='%.12g')
        datasets[symbol]={**validate(frame),'parquet':target.name,'parquet_bytes':target.stat().st_size,'parquet_sha256':sha256_file(target),'csv_gz':csv.name,'csv_gz_bytes':csv.stat().st_size,'csv_gz_sha256':sha256_file(csv)}
    coverage=event_coverage(events,frames);coverage.to_csv(a.output/'event_coverage.csv',index=False)
    events.to_csv(a.output/'events.csv',index=False)
    payload={'schema_version':1,'created_at':datetime.now(timezone.utc).isoformat(),'source':BASE_URL,'archive_dates_sha256':sha256_file(a.archive_dates),'events_sha256':sha256_file(a.events),'requested_archives':len(specs),'verified_archives':len(archives),'missing_archives':sorted(missing),'archives':sorted(archives,key=lambda x:(x['symbol'],x['date'])),'datasets':datasets,'coverage':{'events':len(coverage),'complete_contiguous':int(coverage.complete_contiguous.sum()),'right_censored':int(coverage.right_censored.sum())}}
    (a.output/'manifest.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({'requested':len(specs),'verified':len(archives),'missing':len(missing),'datasets':{k:v['rows'] for k,v in datasets.items()},'coverage':payload['coverage']},indent=2),flush=True)
    if set(dates.symbol.unique())-set(datasets):raise SystemExit('one or more requested symbols produced no dataset')
    return 0
if __name__=='__main__':raise SystemExit(main())
