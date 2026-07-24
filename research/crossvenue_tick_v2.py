from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import shutil
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

BINANCE = 'https://data.binance.vision/data/futures/um/daily/aggTrades'
BYBIT = 'https://public.bybit.com/trading'
DEV_DAYS = ('2022-01-15','2022-03-15','2022-05-15','2022-07-15','2022-09-15','2022-11-15')
VAL_DAYS = ('2023-01-15','2023-03-15','2023-05-15','2023-07-15','2023-09-15','2023-11-15')
FINAL_DAYS = ('2024-01-15','2024-03-15','2024-05-15','2024-07-15','2024-09-15','2024-11-15')
LATENCIES = (100,250,500,1000)
HORIZONS = (2_000,5_000,15_000,30_000)
COSTS = (.0012,.0018,.0024)
ZS = (1.5,2.0,2.5,3.0)


def get(url: str, attempts: int = 6) -> bytes:
    last=None
    for k in range(attempts):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'smc-crossvenue-v2/1.0'})
            with urllib.request.urlopen(req,timeout=600) as r:return r.read()
        except Exception as exc:
            last=exc
            if k+1<attempts:time.sleep(min(20,2**k))
    raise RuntimeError(f'{url}: {last!r}')


def binance_payload(symbol: str, day: str) -> tuple[bytes,dict]:
    name=f'{symbol}-aggTrades-{day}.zip';url=f'{BINANCE}/{symbol}/{name}'
    check=get(url+'.CHECKSUM').decode('utf-8-sig').strip();expected=check.split()[0].lower();payload=get(url);actual=hashlib.sha256(payload).hexdigest()
    if actual!=expected:raise ValueError(f'checksum {name}')
    return payload,{'url':url,'sha256':actual,'bytes':len(payload)}


def bybit_payload(symbol: str, day: str) -> tuple[bytes,dict]:
    urls=(f'{BYBIT}/{symbol}/{symbol}{day}.csv.gz',f'{BYBIT}/{symbol}/{symbol}{day}.csv')
    last=None
    for url in urls:
        try:
            payload=get(url);return payload,{'url':url,'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)}
        except Exception as exc:last=exc
    raise RuntimeError(f'Bybit unavailable {symbol} {day}: {last!r}')


def norm_ms(v: np.ndarray) -> np.ndarray:
    v=np.asarray(v,dtype=float)
    med=np.nanmedian(np.abs(v))
    if med<1e11:v=v*1000
    elif med>=1e15:v=np.floor(v/1000)
    return v.astype(np.int64)


def read_binance(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        member=[n for n in z.namelist() if n.endswith('.csv')][0]
        d=pd.read_csv(z.open(member),header=None)
    if isinstance(d.iloc[0,0],str) and 'agg' in str(d.iloc[0,0]).lower():d=d.iloc[1:].copy()
    d=d.iloc[:,:7];d.columns=['agg','price','qty','first','last','time','maker']
    p=pd.to_numeric(d.price,errors='raise').to_numpy(float);q=pd.to_numeric(d.qty,errors='raise').to_numpy(float);t=norm_ms(pd.to_numeric(d.time,errors='raise').to_numpy());maker=d.maker.astype(str).str.lower().isin(['true','1']).to_numpy();value=p*q
    return pd.DataFrame({'time_ms':t,'price':p,'quote':value,'signed':np.where(~maker,value,-value)}).sort_values('time_ms',kind='mergesort')


def read_bybit(payload: bytes) -> pd.DataFrame:
    raw=gzip.decompress(payload) if payload[:2]==b'\x1f\x8b' else payload
    d=pd.read_csv(io.BytesIO(raw))
    lower={c.lower():c for c in d.columns}
    tc=next(lower[k] for k in ('timestamp','time','trade_time_ms') if k in lower)
    pc=next(lower[k] for k in ('price','trade_price') if k in lower)
    qc=next(lower[k] for k in ('size','qty','quantity') if k in lower)
    sc=next((lower[k] for k in ('side','takerside') if k in lower),None)
    t=norm_ms(pd.to_numeric(d[tc],errors='raise').to_numpy());p=pd.to_numeric(d[pc],errors='raise').to_numpy(float);q=pd.to_numeric(d[qc],errors='raise').to_numpy(float);value=p*q
    if sc is not None:buy=d[sc].astype(str).str.lower().str.startswith('b').to_numpy()
    else:buy=np.ones(len(d),dtype=bool)
    return pd.DataFrame({'time_ms':t,'price':p,'quote':value,'signed':np.where(buy,value,-value)}).sort_values('time_ms',kind='mergesort')


def bins(d: pd.DataFrame, width: int = 100) -> pd.DataFrame:
    b=(d.time_ms.to_numpy(np.int64)//width)*width
    x=pd.DataFrame({'bin_ms':b,'quote':d.quote.to_numpy(float),'signed':d.signed.to_numpy(float),'price':d.price.to_numpy(float)})
    return x.groupby('bin_ms',sort=True).agg(quote=('quote','sum'),signed=('signed','sum'),first=('price','first'),last=('price','last'),count=('price','size')).reset_index()


def panel(symbol: str, day: str) -> tuple[pd.DataFrame,dict]:
    bp,bm=binance_payload(symbol,day);yp,ym=bybit_payload(symbol,day);b=read_binance(bp);y=read_bybit(yp);bb=bins(b);yb=bins(y)
    start=max(bb.bin_ms.min(),yb.bin_ms.min());end=min(bb.bin_ms.max(),yb.bin_ms.max());idx=np.arange(start,end+100,100,dtype=np.int64)
    z=pd.DataFrame({'bin_ms':idx}).merge(bb,on='bin_ms',how='left',suffixes=('','_b')).rename(columns={'quote':'b_quote','signed':'b_signed','first':'b_first','last':'b_last','count':'b_count'})
    z=z.merge(yb,on='bin_ms',how='left').rename(columns={'quote':'y_quote','signed':'y_signed','first':'y_first','last':'y_last','count':'y_count'})
    for prefix in ('b','y'):
        z[f'{prefix}_quote']=z[f'{prefix}_quote'].fillna(0.);z[f'{prefix}_signed']=z[f'{prefix}_signed'].fillna(0.);z[f'{prefix}_count']=z[f'{prefix}_count'].fillna(0.)
        z[f'{prefix}_last']=z[f'{prefix}_last'].ffill();z[f'{prefix}_first']=z[f'{prefix}_first'].fillna(z[f'{prefix}_last'])
        z[f'{prefix}_ret_500']=np.log(z[f'{prefix}_last']/z[f'{prefix}_last'].shift(5));z[f'{prefix}_ret_1000']=np.log(z[f'{prefix}_last']/z[f'{prefix}_last'].shift(10))
        z[f'{prefix}_flow_500']=z[f'{prefix}_signed'].rolling(5,min_periods=3).sum()/z[f'{prefix}_quote'].rolling(5,min_periods=3).sum().replace(0,np.nan)
        z[f'{prefix}_flow_1000']=z[f'{prefix}_signed'].rolling(10,min_periods=5).sum()/z[f'{prefix}_quote'].rolling(10,min_periods=5).sum().replace(0,np.nan)
    for c in ('y_ret_500','y_ret_1000','y_flow_500','y_flow_1000'):
        r=z[c].rolling(18_000,min_periods=3_600);z[c+'_z']=(z[c]-r.mean().shift(1))/r.std(ddof=0).shift(1).replace(0,np.nan)
    z['under_500']=z.y_ret_500-z.b_ret_500;z['under_1000']=z.y_ret_1000-z.b_ret_1000;z['symbol']=symbol;z['day']=day
    return z.replace([np.inf,-np.inf],np.nan),{'binance':bm,'bybit':ym,'rows':len(z)}


def actual_price(times: np.ndarray, prices: np.ndarray, target: np.ndarray) -> np.ndarray:
    i=np.searchsorted(times,target,side='left');out=np.full(len(target),np.nan);ok=i<len(times);out[ok]=prices[i[ok]];return out


def candidate_trades(z: pd.DataFrame, b: pd.DataFrame, rule: str, window: int, zthr: float, latency: int, horizon: int, cost: float) -> pd.DataFrame:
    suffix='500' if window==500 else '1000';shock=z[f'y_ret_{suffix}_z'];flow=z[f'y_flow_{suffix}_z'];under=z[f'under_{suffix}']
    if rule=='price_under':side=np.where((shock.abs()>=zthr)&(np.sign(shock)==np.sign(under)),np.sign(shock),0)
    elif rule=='flow_confirm':side=np.where((shock.abs()>=zthr)&(flow.abs()>=zthr)&(np.sign(shock)==np.sign(flow)),np.sign(shock),0)
    elif rule=='flow_diverge_revert':side=np.where((shock.abs()>=zthr)&(flow.abs()>=zthr)&(np.sign(shock)!=np.sign(flow)),-np.sign(shock),0)
    else:side=np.where((shock.abs()>=zthr)&(np.abs(z[f'b_ret_{suffix}'])<np.abs(z[f'y_ret_{suffix}'])*.35),np.sign(shock),0)
    q=z.loc[side!=0,['bin_ms','symbol','day']].copy();q['side']=side[side!=0]
    if q.empty:return pd.DataFrame(columns=['entry_ms','exit_ms','symbol','day','side','net'])
    bt=b.time_ms.to_numpy(np.int64);bp=b.price.to_numpy(float);entry_target=q.bin_ms.to_numpy(np.int64)+100+latency;exit_target=entry_target+horizon
    ep=actual_price(bt,bp,entry_target);xp=actual_price(bt,bp,exit_target);q['entry_ms']=entry_target;q['exit_ms']=exit_target;q['net']=q.side.to_numpy(float)*(xp/ep-1)-cost
    return q.dropna(subset=['net'])[['entry_ms','exit_ms','symbol','day','side','net']]


def route_global(frames: list[pd.DataFrame]) -> pd.DataFrame:
    q=pd.concat(frames,ignore_index=True).sort_values(['entry_ms','symbol'],kind='mergesort') if frames else pd.DataFrame()
    if q.empty:return q
    rows=[];free=-1
    for t,g in q.groupby('entry_ms',sort=True):
        if int(t)<free:continue
        r=g.iloc[0];rows.append(r);free=int(r.exit_ms)
    return pd.DataFrame(rows)


def stats(z: pd.DataFrame) -> dict:
    if z.empty:return {'n':0,'mean_bps':-999.,'trim10_bps':-999.,'pf':0.,'log_growth':-999.,'top10_conc':1.}
    v=z.net.to_numpy(float);pos=v[v>0].sum();neg=-v[v<0].sum();sv=np.sort(v);trim=sv[:-10].mean() if len(v)>10 else v.mean();p=np.sort(v[v>0])[::-1]
    return {'n':len(v),'mean_bps':v.mean()*1e4,'trim10_bps':trim*1e4,'pf':pos/neg if neg else 999.,'log_growth':float(np.log1p(v).sum()),'top10_conc':float(p[:10].sum()/max(p.sum(),1e-12))}


def main() -> int:
    ap=argparse.ArgumentParser();ap.add_argument('--symbols',nargs='+',default=('BTCUSDT','ETHUSDT'));ap.add_argument('--output-dir',type=Path,required=True);args=ap.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
    data={};sources=[]
    for phase,days in (('dev',DEV_DAYS),('val',VAL_DAYS)):
        for s in args.symbols:
            for day in days:
                z,meta=panel(s,day);bp,_=binance_payload(s,day);b=read_binance(bp);data[(s,day)]=(z,b);sources.append({'symbol':s,'day':day,**meta})
    rows=[]
    for rule in ('price_under','flow_confirm','flow_diverge_revert','strict_under'):
      for window in (500,1000):
       for zthr in ZS:
        for latency in LATENCIES:
         for horizon in HORIZONS:
          rec={'rule':rule,'window_ms':window,'z':zthr,'latency_ms':latency,'horizon_ms':horizon}
          for cost in COSTS:
           for label,days in (('dev',DEV_DAYS),('val',VAL_DAYS)):
            legs=[]
            for s in args.symbols:
             for day in days:
              z,b=data[(s,day)];legs.append(candidate_trades(z,b,rule,window,zthr,latency,horizon,cost))
            rec.update({f'{label}_{int(cost*1e4)}bp_{k}':v for k,v in stats(route_global(legs)).items()})
          rec['eligible']=rec['dev_12bp_n']>=150 and rec['val_12bp_n']>=150 and rec['dev_12bp_trim10_bps']>0 and rec['val_12bp_trim10_bps']>0 and rec['dev_18bp_trim10_bps']>0 and rec['val_18bp_trim10_bps']>0 and rec['dev_12bp_pf']>=1.1 and rec['val_12bp_pf']>=1.1 and rec['dev_12bp_top10_conc']<=.35 and rec['val_12bp_top10_conc']<=.35
          rows.append(rec)
    screen=pd.DataFrame(rows);screen.to_csv(args.output_dir/'screen_pre_final.csv',index=False);eligible=screen[screen.eligible].copy()
    final_opened=not eligible.empty;final_rows=[]
    if final_opened:
        final_data={}
        for s in args.symbols:
            for day in FINAL_DAYS:
                z,meta=panel(s,day);bp,_=binance_payload(s,day);b=read_binance(bp);final_data[(s,day)]=(z,b);sources.append({'symbol':s,'day':day,**meta})
        for _,r in eligible.iterrows():
            rec=r.to_dict()
            for cost in COSTS:
                legs=[]
                for s in args.symbols:
                    for day in FINAL_DAYS:
                        z,b=final_data[(s,day)];legs.append(candidate_trades(z,b,r.rule,int(r.window_ms),float(r.z),int(r.latency_ms),int(r.horizon_ms),cost))
                rec.update({f'final_{int(cost*1e4)}bp_{k}':v for k,v in stats(route_global(legs)).items()})
            final_rows.append(rec)
    final=pd.DataFrame(final_rows);final.to_csv(args.output_dir/'screen_final.csv',index=False)
    target=final[(final.final_12bp_log_growth>=math.log(1.01))&(final.final_18bp_trim10_bps>0)&(final.final_12bp_n>=150)] if not final.empty else final
    summary={'version':'CROSSVENUE_TICK_V2','screened':len(screen),'eligible_pre_final':len(eligible),'final_opened':final_opened,'target_1pct_final_sample':len(target),'sources':sources,'orders_submitted':False,'paper_or_live_started':False,'validated':False}
    (args.output_dir/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
