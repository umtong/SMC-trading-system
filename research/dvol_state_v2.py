from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

BINANCE='https://data.binance.vision/data/futures/um/monthly/klines'
DERIBIT='https://www.deribit.com/api/v2/public/get_volatility_index_data'
SYMS=('BTCUSDT','ETHUSDT')
H=(4,16,48)
COSTS=(.0030,.0045,.0060)
ZS=(1.,1.5,2.,2.5)


def get(url,attempts=6):
    last=None
    for k in range(attempts):
        try:
            q=urllib.request.Request(url,headers={'User-Agent':'smc-dvol-v2/1.0'})
            with urllib.request.urlopen(q,timeout=600) as r:return r.read()
        except Exception as e:
            last=e
            if k+1<attempts:time.sleep(min(20,2**k))
    raise RuntimeError(f'{url}: {last!r}')
def verified(symbol,tag):
    name=f'{symbol}-15m-{tag}.zip';url=f'{BINANCE}/{symbol}/15m/{name}';check=get(url+'.CHECKSUM').decode('utf-8-sig').strip();expected=check.split()[0].lower();payload=get(url);actual=hashlib.sha256(payload).hexdigest()
    if actual!=expected:raise ValueError(f'checksum {name}')
    return payload,{'url':url,'sha256':actual,'bytes':len(payload)}
def epoch(v):
    v=int(float(v));return v//1000 if abs(v)>=10**15 else v
def read_kline(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        member=[n for n in z.namelist() if n.endswith('.csv')][0];rows=[]
        for r in csv.reader(line.decode('utf-8-sig') for line in z.open(member)):
            if not r or r[0].lower().replace(' ','_')=='open_time':continue
            rows.append(r[:12])
    d=pd.DataFrame(rows,columns=['time','open','high','low','close','volume','close_time','quote','count','taker_base','taker_quote','ignore']);d['time']=pd.to_datetime([epoch(x) for x in d.time],unit='ms',utc=True)
    for c in ('open','high','low','close','quote','count','taker_quote'):d[c]=pd.to_numeric(d[c],errors='coerce')
    return d.dropna(subset=['time','open','close']).sort_values('time').drop_duplicates('time')
def dvol(currency,start,end):
    rows=[];cursor=start
    while cursor<end:
        stop=min(end,cursor+pd.Timedelta(days=90));params=urllib.parse.urlencode({'currency':currency,'start_timestamp':int(cursor.timestamp()*1000),'end_timestamp':int(stop.timestamp()*1000),'resolution':3600});raw=get(DERIBIT+'?'+params);obj=json.loads(raw);data=obj.get('result',{}).get('data',[])
        for r in data:
            if len(r)>=5:rows.append(r[:5])
        cursor=stop
    d=pd.DataFrame(rows,columns=['time','open','high','low','close']);d['time']=pd.to_datetime(d.time,unit='ms',utc=True);d=d.sort_values('time').drop_duplicates('time')
    # Timestamp represents the volatility candle tick; use only after the full hourly interval closes.
    d['known_at']=d.time+pd.Timedelta(hours=1)
    for c in ('open','high','low','close'):d[c]=pd.to_numeric(d[c],errors='coerce')
    return d
def prior_z(s,w=24*90,minp=24*30):
    r=s.rolling(w,min_periods=minp);return (s-r.mean().shift(1))/r.std(ddof=0).shift(1).replace(0,np.nan)
def build(symbol,price,dv):
    x=pd.merge_asof(price.sort_values('time'),dv[['known_at','close']].rename(columns={'known_at':'time','close':'dvol'}).sort_values('time'),on='time',direction='backward',tolerance=pd.Timedelta('2h'))
    x['r1']=np.log(x.close/x.close.shift(1));x['r4']=np.log(x.close/x.close.shift(4));x['rv16']=x.r1.rolling(16,min_periods=8).std(ddof=0).shift(1);x['rv96']=x.r1.rolling(96,min_periods=48).std(ddof=0).shift(1)
    x['dvol_ret']=np.log(x.dvol/x.dvol.shift(4));x['dvol_z']=prior_z(x.dvol_ret);x['price_z']=prior_z(x.r4);x['vrp']=x.dvol/100-x.rv96*np.sqrt(365*24*4);x['vrp_z']=prior_z(x.vrp)
    x['flow']=2*x.taker_quote/x.quote.replace(0,np.nan)-1;x['flow_z']=prior_z(x.flow,96*30,96*10);x['entry']=x.open.shift(-1)
    for h in H:x[f'exit_{h}']=x.open.shift(-(1+h));x[f'raw_{h}']=np.log(x[f'exit_{h}']/x.entry)
    x['symbol']=symbol;return x.replace([np.inf,-np.inf],np.nan)
def route(x,side,h,cost):
    q=x.loc[(side!=0)&x.entry.notna()&x[f'raw_{h}'].notna(),['time','symbol','entry',f'raw_{h}']].copy();q['side']=side[side!=0];q=q.sort_values(['time','symbol']);rows=[];free=pd.Timestamp.min.tz_localize('UTC')
    for ts,g in q.groupby('time',sort=True):
        ent=ts+pd.Timedelta(minutes=15)
        if ent<free:continue
        r=g.iloc[0];s=int(np.sign(r.side));rows.append({'entry_ts':ent,'exit_ts':ent+pd.Timedelta(minutes=15*h),'symbol':r.symbol,'side':s,'net':s*float(r[f'raw_{h}'])-cost});free=ent+pd.Timedelta(minutes=15*h)
    return pd.DataFrame(rows)
def global_route(legs):
    q=pd.concat(legs,ignore_index=True).sort_values(['entry_ts','symbol']) if legs else pd.DataFrame();
    if q.empty:return q
    rows=[];free=pd.Timestamp.min.tz_localize('UTC')
    for ts,g in q.groupby('entry_ts',sort=True):
        if ts<free:continue
        r=g.iloc[0];rows.append(r);free=r.exit_ts
    return pd.DataFrame(rows)
def stats(z):
    if z.empty:return {'n':0,'mean_bps':-999.,'trim10_bps':-999.,'pf':0.,'log_growth':-999.,'top10_conc':1.}
    v=z.net.to_numpy(float);pos=v[v>0].sum();neg=-v[v<0].sum();sv=np.sort(v);trim=sv[:-10].mean() if len(v)>10 else v.mean();p=np.sort(v[v>0])[::-1]
    return {'n':len(v),'mean_bps':v.mean()*1e4,'trim10_bps':trim*1e4,'pf':pos/neg if neg else 999.,'log_growth':float(np.log1p(v).sum()),'top10_conc':float(p[:10].sum()/max(p.sum(),1e-12))}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output-dir',type=Path,required=True);args=ap.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True);prices={};sources=[]
    for s in SYMS:
        frames=[]
        for y in range(2021,2026):
            for m in range(1,13):
                tag=f'{y:04d}-{m:02d}';p,meta=verified(s,tag);frames.append(read_kline(p));sources.append({'symbol':s,'month':tag,**meta})
        prices[s]=pd.concat(frames,ignore_index=True)
    dvs={'BTCUSDT':dvol('BTC',pd.Timestamp('2021-01-01',tz='UTC'),pd.Timestamp('2026-01-01',tz='UTC')),'ETHUSDT':dvol('ETH',pd.Timestamp('2021-01-01',tz='UTC'),pd.Timestamp('2026-01-01',tz='UTC'))};panels={s:build(s,prices[s],dvs[s]) for s in SYMS}
    # BTC DVOL also becomes a known cross-asset state for ETH.
    btc=panels['BTCUSDT'][['time','dvol_z','dvol_ret']].rename(columns={'dvol_z':'btc_dvol_z','dvol_ret':'btc_dvol_ret'});panels['ETHUSDT']=panels['ETHUSDT'].merge(btc,on='time',how='left')
    rules=[]
    for z in ZS:
      for h in H:
       rules += [(f'vol_price_continue_z{z}_h{h}',h,lambda x,z=z:np.where((x.dvol_z>=z)&(x.price_z.abs()>=1),np.sign(x.price_z),0)),(f'vol_price_revert_z{z}_h{h}',h,lambda x,z=z:np.where((x.dvol_z>=z)&(x.price_z.abs()>=1),-np.sign(x.price_z),0)),(f'vol_only_breakout_z{z}_h{h}',h,lambda x,z=z:np.where((x.dvol_z>=z)&(x.price_z.abs()<.5),np.sign(x.flow_z),0)),(f'vrp_revert_z{z}_h{h}',h,lambda x,z=z:np.where(x.vrp_z.abs()>=z,-np.sign(x.vrp_z)*np.sign(x.price_z.replace(0,np.nan)),0)),(f'vrp_continue_z{z}_h{h}',h,lambda x,z=z:np.where(x.vrp_z.abs()>=z,np.sign(x.vrp_z)*np.sign(x.price_z.replace(0,np.nan)),0))]
       rules.append((f'btc_dvol_eth_continue_z{z}_h{h}',h,lambda x,z=z:np.where((x.symbol=='ETHUSDT')&(x.get('btc_dvol_z',pd.Series(index=x.index,dtype=float))>=z),np.sign(x.get('btc_dvol_ret',pd.Series(index=x.index,dtype=float))),0)))
       rules.append((f'btc_dvol_eth_revert_z{z}_h{h}',h,lambda x,z=z:np.where((x.symbol=='ETHUSDT')&(x.get('btc_dvol_z',pd.Series(index=x.index,dtype=float))>=z),-np.sign(x.get('btc_dvol_ret',pd.Series(index=x.index,dtype=float))),0)))
    periods={'dev21_22':(pd.Timestamp('2021-01-01',tz='UTC'),pd.Timestamp('2023-01-01',tz='UTC')),'val2023':(pd.Timestamp('2023-01-01',tz='UTC'),pd.Timestamp('2024-01-01',tz='UTC')),'confirm2024':(pd.Timestamp('2024-01-01',tz='UTC'),pd.Timestamp('2025-01-01',tz='UTC')),'final2025':(pd.Timestamp('2025-01-01',tz='UTC'),pd.Timestamp('2026-01-01',tz='UTC'))};rows=[]
    for n,h,fn in rules:
      for cl,cost in [('base',COSTS[0]),('stress',COSTS[1]),('hard',COSTS[2])]:
       legs=[route(x,fn(x),h,cost) for x in panels.values()];L=global_route(legs);r={'candidate':n,'horizon':h,'cost':cl}
       for pn,(a,b) in periods.items():r.update({f'{pn}_{k}':v for k,v in stats(L[(L.exit_ts>=a)&(L.exit_ts<b)]).items()})
       rows.append(r)
    s=pd.DataFrame(rows);s.to_csv(args.output_dir/'screen.csv',index=False);b=s[s.cost=='base'];t=s[s.cost=='stress'];m=b.merge(t,on=['candidate','horizon'],suffixes=('_b','_s'));eligible=m[(m.dev21_22_n_b>=40)&(m.val2023_n_b>=20)&(m.dev21_22_log_growth_b>0)&(m.val2023_log_growth_b>0)&(m.dev21_22_log_growth_s>0)&(m.val2023_log_growth_s>0)&(m.dev21_22_top10_conc_b<=.5)&(m.val2023_top10_conc_b<=.5)];confirmed=eligible[(eligible.confirm2024_log_growth_b>0)&(eligible.confirm2024_log_growth_s>0)];final=confirmed[(confirmed.final2025_log_growth_b>0)&(confirmed.final2025_log_growth_s>0)]
    summary={'version':'DVOL_STATE_V2','candidates':len(rules),'dev_validation_survivors':len(eligible),'confirm2024_survivors':len(confirmed),'final2025_survivors':len(final),'sources':sources,'2026_opened':False,'orders_submitted':False,'validated':False};(args.output_dir/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,indent=2))
if __name__=='__main__':main()
