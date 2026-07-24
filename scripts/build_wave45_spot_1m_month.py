from __future__ import annotations
import argparse, hashlib, io, json, time, urllib.error, urllib.request, zipfile
from pathlib import Path
import pandas as pd

ROOT='https://data.binance.vision/data/spot/monthly/klines'
COLS=['open_time','open','high','low','close','volume','close_time','quote_volume','num_trades','taker_buy_base_volume','taker_buy_quote_volume','ignore']

def parse_args():
 p=argparse.ArgumentParser(); p.add_argument('--symbol',required=True); p.add_argument('--month',required=True); p.add_argument('--output-dir',type=Path,required=True); p.add_argument('--retries',type=int,default=5); return p.parse_args()

def fetch(url,retries):
 req=urllib.request.Request(url,headers={'User-Agent':'wave45-spot-flow/1.0'}); last=None
 for i in range(retries):
  try:
   with urllib.request.urlopen(req,timeout=240) as r:return r.read()
  except (OSError,urllib.error.URLError,urllib.error.HTTPError) as e:
   last=e
   if i+1<retries:time.sleep(2**i)
 raise RuntimeError(url) from last

def verified(url,retries):
 token=fetch(url+'.CHECKSUM',retries).decode('utf-8-sig').strip().split()[0].lower(); payload=fetch(url,retries); actual=hashlib.sha256(payload).hexdigest()
 if token!=actual:raise ValueError(f'checksum mismatch {actual} {token}')
 return payload,{'url':url,'sha256':actual,'bytes':len(payload)}

def main():
 a=parse_args(); s=a.symbol.upper(); m=a.month; a.output_dir.mkdir(parents=True,exist_ok=True)
 url=f'{ROOT}/{s}/1m/{s}-1m-{m}.zip'; payload,src=verified(url,a.retries)
 with zipfile.ZipFile(io.BytesIO(payload)) as z:
  names=[n for n in z.namelist() if n.lower().endswith('.csv')]
  if len(names)!=1:raise ValueError(names)
  with z.open(names[0]) as f:raw=pd.read_csv(f,header=None,low_memory=False)
 raw=raw.iloc[:,:12].copy(); raw.columns=COLS; t=pd.to_numeric(raw.open_time,errors='coerce'); raw=raw[t.notna()].copy(); t=pd.to_numeric(raw.open_time,errors='raise').astype('int64')
 unit='ms' if int(t.max())<10**14 else 'us'; raw['timestamp']=pd.to_datetime(t,unit=unit,utc=True)
 for c in ['open','high','low','close','volume','quote_volume','num_trades','taker_buy_base_volume','taker_buy_quote_volume']:raw[c]=pd.to_numeric(raw[c],errors='raise')
 raw.sort_values('timestamp',inplace=True)
 if raw.timestamp.duplicated().any():raise ValueError('duplicate minute')
 start=pd.Timestamp(m+'-01',tz='UTC'); end=start+pd.offsets.MonthBegin(1); expected=pd.date_range(start,end,freq='1min',inclusive='left'); missing=expected.difference(pd.DatetimeIndex(raw.timestamp))
 if len(missing):raise ValueError(f'missing minutes={len(missing)} first={missing[:5].tolist()}')
 if len(raw)!=len(expected):raise ValueError(f'rows {len(raw)} != {len(expected)}')
 bad=(raw.high<raw[['open','close','low']].max(axis=1))|(raw.low>raw[['open','close','high']].min(axis=1))
 if bad.any():raise ValueError(f'invalid OHLC {int(bad.sum())}')
 out=raw[['timestamp','open','high','low','close','volume','quote_volume','num_trades','taker_buy_base_volume','taker_buy_quote_volume']].copy(); out.insert(0,'symbol',s)
 path=a.output_dir/f'{s}_spot_1m_{m}.parquet'; out.to_parquet(path,index=False,compression='zstd'); digest=hashlib.sha256(path.read_bytes()).hexdigest()
 manifest={'schema':'wave45-official-spot-1m-v1','symbol':s,'month':m,'rows':len(out),'start':out.timestamp.min().isoformat(),'end':out.timestamp.max().isoformat(),'source':src,'output':{'path':path.name,'bytes':path.stat().st_size,'sha256':digest},'holdout_protection':'2022 only'}
 (a.output_dir/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8'); print(json.dumps(manifest,indent=2)); return 0
if __name__=='__main__':raise SystemExit(main())
