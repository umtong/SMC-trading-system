from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import time
import urllib.request
import zipfile

import numpy as np
import pandas as pd

BYBIT_ROOT = 'https://public.bybit.com/trading'
BINANCE_ROOT = 'https://data.binance.vision/data/futures/um/daily/aggTrades'
SYMBOLS = ('BTCUSDT','ETHUSDT')


def fetch(url: str, attempts: int = 6, timeout: int = 300) -> bytes:
    err=None
    for attempt in range(attempts):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'smc-v196-crossvenue/1.0'})
            with urllib.request.urlopen(req,timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            err=exc
            if attempt+1<attempts: time.sleep(min(2**attempt,20))
    raise RuntimeError(f'fetch failed {url}: {err!r}')


def parse_binance(symbol: str, day: str) -> tuple[pd.DataFrame,dict]:
    name=f'{symbol}-aggTrades-{day}.zip'
    url=f'{BINANCE_ROOT}/{symbol}/{name}'
    checksum=fetch(url+'.CHECKSUM').decode('utf-8-sig').strip().split()[0].lower()
    payload=fetch(url)
    actual=hashlib.sha256(payload).hexdigest()
    if actual!=checksum: raise ValueError('Binance checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        members=[n for n in z.namelist() if n.endswith('.csv')]
        if len(members)!=1: raise ValueError('unexpected Binance archive')
        raw=pd.read_csv(z.open(members[0]),header=None,low_memory=False)
    if pd.isna(pd.to_numeric(raw.iloc[0,0],errors='coerce')): raw=raw.iloc[1:].copy()
    raw=raw.iloc[:,:7]
    raw.columns=['agg_id','price','quantity','first_id','last_id','time','buyer_maker']
    for c in ['price','quantity','time']: raw[c]=pd.to_numeric(raw[c],errors='raise')
    t=raw.time.astype('int64')
    unit='us' if int(t.max())>=10**14 else 'ms'
    ts=pd.to_datetime(t,unit=unit,utc=True)
    sec=ts.dt.floor('s')
    maker=raw.buyer_maker.astype(str).str.lower().isin(['true','1'])
    quote=raw.price.to_numpy(float)*raw.quantity.to_numpy(float)
    signed=np.where(maker,-quote,quote)
    d=pd.DataFrame({'second':sec,'price':raw.price.to_numpy(float),'quote':quote,'signed':signed,'count':1})
    grouped=d.groupby('second',sort=True).agg(
        binance_price=('price','last'),binance_quote=('quote','sum'),
        binance_signed=('signed','sum'),binance_count=('count','sum'))
    return grouped,{'url':url,'sha256':actual,'archive_bytes':len(payload),'raw_rows':len(raw)}


def parse_bybit(symbol: str, day: str) -> tuple[pd.DataFrame,dict]:
    name=f'{symbol}{day}.csv.gz'
    url=f'{BYBIT_ROOT}/{symbol}/{name}'
    payload=fetch(url)
    actual=hashlib.sha256(payload).hexdigest()
    raw=pd.read_csv(io.BytesIO(gzip.decompress(payload)),low_memory=False)
    required={'timestamp','side','size','price'}
    if not required.issubset(raw.columns): raise ValueError(f'Bybit columns {raw.columns}')
    for c in ['timestamp','size','price']: raw[c]=pd.to_numeric(raw[c],errors='raise')
    ts=pd.to_datetime(raw.timestamp,unit='s',utc=True)
    sec=ts.dt.floor('s')
    quote=raw['foreignNotional'].to_numpy(float) if 'foreignNotional' in raw else raw.price.to_numpy(float)*raw['size'].to_numpy(float)
    signed=np.where(raw.side.astype(str).str.lower().eq('buy'),quote,-quote)
    d=pd.DataFrame({'second':sec,'price':raw.price.to_numpy(float),'quote':quote,'signed':signed,'count':1})
    grouped=d.groupby('second',sort=True).agg(
        bybit_price=('price','last'),bybit_quote=('quote','sum'),
        bybit_signed=('signed','sum'),bybit_count=('count','sum'))
    return grouped,{'url':url,'sha256':actual,'archive_bytes':len(payload),'raw_rows':len(raw)}


def causal_z(series: pd.Series, window: int) -> pd.Series:
    mean=series.rolling(window,min_periods=window).mean().shift(1)
    std=series.rolling(window,min_periods=window).std(ddof=0).shift(1).replace(0,np.nan)
    return (series-mean)/std


def build_day(symbol: str, day: str, out: Path) -> None:
    b, bm=parse_binance(symbol,day)
    y, ym=parse_bybit(symbol,day)
    start=pd.Timestamp(day,tz='UTC'); end=start+pd.Timedelta(days=1)
    index=pd.date_range(start,end,freq='1s',inclusive='left')
    frame=pd.DataFrame(index=index)
    frame=frame.join(b).join(y)
    frame[['binance_price','bybit_price']]=frame[['binance_price','bybit_price']].ffill()
    for c in ['binance_quote','binance_signed','binance_count','bybit_quote','bybit_signed','bybit_count']:
        frame[c]=frame[c].fillna(0.0)
    frame=frame.dropna(subset=['binance_price','bybit_price']).copy()
    frame['bybit_imb']=frame.bybit_signed/frame.bybit_quote.replace(0,np.nan)
    frame['binance_imb']=frame.binance_signed/frame.binance_quote.replace(0,np.nan)
    frame['flow_gap']=frame.bybit_imb.fillna(0)-frame.binance_imb.fillna(0)
    frame['price_gap_bps']=np.log(frame.bybit_price/frame.binance_price)*1e4
    frame['bybit_ret_1s_bps']=np.log(frame.bybit_price/frame.bybit_price.shift(1))*1e4
    frame['binance_ret_1s_bps']=np.log(frame.binance_price/frame.binance_price.shift(1))*1e4
    frame['bybit_signed_z60']=causal_z(frame.bybit_signed,60)
    frame['flow_gap_z300']=causal_z(frame.flow_gap,300)
    frame['price_gap_z300']=causal_z(frame.price_gap_bps,300)
    frame['volume_z300']=causal_z(np.log1p(frame.bybit_quote),300)
    frame['entry_price']=frame.binance_price.shift(-1)
    frame['entry_time']=frame.index.to_series().shift(-1)
    for h in (1,2,5,10,30,60):
        frame[f'fwd_{h}s']=np.log(frame.binance_price.shift(-(h+1))/frame.entry_price)
    keep=['entry_time','entry_price','bybit_imb','binance_imb','flow_gap','price_gap_bps','bybit_ret_1s_bps','binance_ret_1s_bps','bybit_signed_z60','flow_gap_z300','price_gap_z300','volume_z300']+[f'fwd_{h}s' for h in (1,2,5,10,30,60)]
    frame=frame[keep].dropna().reset_index(names='signal_time')
    frame['symbol']=symbol; frame['day']=day
    out.mkdir(parents=True,exist_ok=True)
    path=out/f'{symbol}_{day}.csv.gz'
    frame.to_csv(path,index=False,compression={'method':'gzip','compresslevel':6,'mtime':0})
    manifest={'symbol':symbol,'day':day,'rows':len(frame),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'binance':bm,'bybit':ym}
    (out/f'{symbol}_{day}.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(manifest))


def greedy(entry_ns: np.ndarray, exit_ns: np.ndarray) -> np.ndarray:
    out=[]; free=-10**30
    for i,(e,x) in enumerate(zip(entry_ns,exit_ns)):
        if e>=free: out.append(i); free=x
    return np.asarray(out,dtype=int)


def metric(rets: np.ndarray, days: int) -> dict:
    if len(rets)==0: return {'trades':0,'net_return':0.0,'gday':0.0,'max_drawdown':0.0,'profit_factor':0.0,'avg_net_bps':0.0}
    eq=np.cumprod(1+np.maximum(rets,-.999)); curve=np.r_[1.,eq]; peak=np.maximum.accumulate(curve); dd=1-curve/peak
    pos=rets[rets>0].sum(); neg=-rets[rets<0].sum()
    return {'trades':len(rets),'net_return':float(eq[-1]-1),'gday':float(np.exp(np.log(eq[-1])/days)-1),'max_drawdown':float(dd.max()),'profit_factor':float(pos/neg) if neg>0 else 999.,'avg_net_bps':float(rets.mean()*1e4)}


def aggregate(input_dir: Path, output_dir: Path) -> None:
    files=sorted(input_dir.rglob('*.csv.gz'))
    frames=[]
    for p in files:
        d=pd.read_csv(p)
        d.signal_time=pd.to_datetime(d.signal_time,utc=True); d.entry_time=pd.to_datetime(d.entry_time,utc=True)
        frames.append(d)
    panel=pd.concat(frames,ignore_index=True).sort_values(['entry_time','symbol'],kind='mergesort')
    panel['year']=panel.entry_time.dt.year
    rules=[]
    for family in ('bybit_flow','flow_divergence','price_lead'):
      for z in (2.0,2.5,3.0):
       for confirm in ('none','same','binance_lag'):
        for h in (1,2,5,10,30,60):
         for mode in ('continuation','convergence'):
          if family!='price_lead' and mode=='convergence': continue
          x=panel.copy()
          if family=='bybit_flow':
            raw=x.bybit_signed_z60; direction=np.sign(raw); mask=raw.abs()>=z
          elif family=='flow_divergence':
            raw=x.flow_gap_z300; direction=np.sign(raw); mask=raw.abs()>=z
          else:
            raw=x.price_gap_z300; direction=np.sign(raw); mask=raw.abs()>=z
            if mode=='convergence': direction=-direction
          mask &= direction!=0
          if confirm=='same': mask &= np.sign(x.bybit_ret_1s_bps)==direction
          elif confirm=='binance_lag': mask &= (np.sign(x.bybit_ret_1s_bps)==direction)&(np.sign(x.binance_ret_1s_bps)!=direction)
          cand=x.loc[mask].copy(); cand['direction']=direction[mask]; cand['score']=raw[mask].abs()
          cand=cand.sort_values(['entry_time','score','symbol'],ascending=[True,False,True],kind='mergesort')
          entry=cand.entry_time.astype('int64').to_numpy(); exit_ns=(cand.entry_time+pd.to_timedelta(h,unit='s')).astype('int64').to_numpy()
          chosen=greedy(entry,exit_ns); cand=cand.iloc[chosen]
          gross=cand.direction.to_numpy()*cand[f'fwd_{h}s'].to_numpy()
          for cost in (6.,12.,18.,24.):
            net=gross-cost/1e4
            ident=f'{family}_z{z}_{confirm}_{mode}_h{h}_c{int(cost)}'
            for year,days in [(2022,12),(2023,12)]:
                use=cand.year.to_numpy()==year
                rules.append({'config':ident,'family':family,'z':z,'confirm':confirm,'mode':mode,'horizon_s':h,'cost_bps':cost,'year':year,**metric(net[use],days)})
    r=pd.DataFrame(rules); output_dir.mkdir(parents=True,exist_ok=True); r.to_csv(output_dir/'screen.csv',index=False)
    p=r.pivot_table(index=['config','family','z','confirm','mode','horizon_s','cost_bps'],columns='year',values=['trades','net_return','gday','max_drawdown','profit_factor','avg_net_bps'],aggfunc='first')
    p.columns=[f'{a}_{b}' for a,b in p.columns]; p=p.reset_index()
    p['min_gday']=p[['gday_2022','gday_2023']].min(axis=1); p['max_dd']=p[['max_drawdown_2022','max_drawdown_2023']].max(axis=1); p['min_trades']=p[['trades_2022','trades_2023']].min(axis=1)
    p=p.sort_values(['min_gday','max_dd'],ascending=[False,True]); p.to_csv(output_dir/'rank.csv',index=False)
    eligible=p[(p.cost_bps>=12)&(p.min_trades>=30)&(p.net_return_2022>0)&(p.net_return_2023>0)&(p.max_dd<.30)]
    eligible.to_csv(output_dir/'eligible.csv',index=False)
    summary={'files':len(files),'rows':len(panel),'eligible':len(eligible),'best':eligible.head(30).replace([np.inf,-np.inf],None).to_dict('records')}
    (output_dir/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))


def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('day'); d.add_argument('--symbol',required=True); d.add_argument('--day',required=True); d.add_argument('--out',type=Path,required=True)
    a=sub.add_parser('aggregate'); a.add_argument('--input',type=Path,required=True); a.add_argument('--out',type=Path,required=True)
    args=ap.parse_args()
    if args.cmd=='day': build_day(args.symbol,args.day,args.out)
    else: aggregate(args.input,args.out)
if __name__=='__main__': main()
