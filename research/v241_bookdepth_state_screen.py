from __future__ import annotations
import argparse,csv,hashlib,io,json,math,re,time,urllib.error,urllib.request,zipfile
from dataclasses import dataclass
from datetime import date,timedelta
from pathlib import Path
import numpy as np
import pandas as pd
BASE='https://data.binance.vision/data/futures/um/daily'
MONTHLY='https://data.binance.vision/data/futures/um/monthly/fundingRate'
SYMBOLS=('BTCUSDT','ETHUSDT')
UA='smc-v241-bookdepth/1.0'

def fetch(url,attempts=5):
    err=None
    for i in range(attempts):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':UA})
            with urllib.request.urlopen(req,timeout=180) as r:return r.read()
        except urllib.error.HTTPError as e:
            if e.code==404:raise
            err=e
        except Exception as e:err=e
        if i+1<attempts:time.sleep(min(2**i,20))
    raise RuntimeError(f'{url}: {err!r}')

def verified(url):
    chk=fetch(url+'.CHECKSUM').decode('utf-8-sig').strip().split()[0].lower();payload=fetch(url);actual=hashlib.sha256(payload).hexdigest()
    if actual!=chk:raise ValueError(f'checksum mismatch {url}')
    return payload,actual

def epoch(v):
    x=pd.to_numeric(v,errors='coerce');m=float(x.dropna().abs().median());unit='ns' if m>1e17 else 'us' if m>1e14 else 'ms' if m>1e11 else 's';return pd.to_datetime(x,unit=unit,utc=True,errors='coerce')

def csv_from_zip(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        names=[n for n in z.namelist() if n.lower().endswith('.csv')]
        if len(names)!=1:raise ValueError(names)
        raw=z.read(names[0])
    # header detection
    first=raw.splitlines()[0].decode('utf-8-sig','replace').split(',')
    header=0 if any(re.search('[A-Za-z]',x) for x in first) else None
    return pd.read_csv(io.BytesIO(raw),header=header,low_memory=False)

def parse_depth(symbol,day,cache,manifest):
    name=f'{symbol}-bookDepth-{day}.zip';url=f'{BASE}/bookDepth/{symbol}/{name}'
    try:payload,sha=verified(url)
    except urllib.error.HTTPError as e:
        if e.code==404:
            manifest.append({'symbol':symbol,'day':day,'kind':'bookDepth','status':'not_available','url':url});return None
        raise
    p=cache/name;p.write_bytes(payload);d=csv_from_zip(payload);d.columns=[str(c).strip().lower().replace(' ','_') for c in d.columns]
    if all(str(c).isdigit() for c in d.columns):
        if d.shape[1]>=4:d=d.iloc[:,:4];d.columns=['timestamp','percentage','depth','notional']
    tcol=next((c for c in d if 'time' in c),d.columns[0]);d['time']=epoch(d[tcol])
    pct=next((c for c in d if 'percent' in c or c in ('level','range')),None)
    side=next((c for c in d if c in ('side','bid_ask')),None)
    val=next((c for c in d if 'notional' in c),None) or next((c for c in d if c=='depth' or 'quantity' in c),None)
    if pct is None or val is None:raise ValueError(f'unrecognized depth schema {d.columns.tolist()}')
    d[pct]=pd.to_numeric(d[pct].astype(str).str.replace('%','',regex=False),errors='coerce');d[val]=pd.to_numeric(d[val],errors='coerce')
    if side:
        ss=d[side].astype(str).str.lower();d['signed_level']=np.where(ss.str.contains('bid'),-d[pct].abs(),d[pct].abs())
    else:d['signed_level']=d[pct]
    d=d.dropna(subset=['time','signed_level',val]);d['abs_level']=d.signed_level.abs().round(4);d['side2']=np.where(d.signed_level<0,'bid','ask')
    piv=d.pivot_table(index='time',columns=['side2','abs_level'],values=val,aggfunc='last').sort_index()
    piv.columns=[f'{a}_{b:g}' for a,b in piv.columns];piv=piv.resample('5min').last().dropna(how='all')
    manifest.append({'symbol':symbol,'day':day,'kind':'bookDepth','status':'verified','url':url,'sha256':sha,'rows':len(d),'columns':list(d.columns)})
    return piv

def parse_klines(symbol,day,cache,manifest):
    name=f'{symbol}-1m-{day}.zip';url=f'{BASE}/klines/{symbol}/1m/{name}';payload,sha=verified(url);(cache/name).write_bytes(payload);d=csv_from_zip(payload)
    if d.shape[1]<11:raise ValueError('kline width')
    d=d.iloc[:,:11];d.columns=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_buy_base','taker_buy_quote']
    d['time']=epoch(d.open_time)
    for c in d.columns[1:11]:d[c]=pd.to_numeric(d[c],errors='coerce')
    d=d.dropna(subset=['time','open','close']).set_index('time').sort_index();d['signed_quote']=2*d.taker_buy_quote-d.quote_volume
    q=d.resample('5min',label='left',closed='left').agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),quote=('quote_volume','sum'),signed=('signed_quote','sum'))
    manifest.append({'symbol':symbol,'day':day,'kind':'klines','status':'verified','url':url,'sha256':sha,'rows':len(d)})
    return q.dropna(subset=['open','close'])

def months(start,end):
    p=pd.Period(start,freq='M');e=pd.Period(end,freq='M')
    while p<=e:yield str(p);p+=1

def funding(symbol,cache,manifest):
    chunks=[]
    for month in months('2023-01','2025-12'):
        name=f'{symbol}-fundingRate-{month}.zip';url=f'{MONTHLY}/{symbol}/{name}'
        try:payload,sha=verified(url)
        except urllib.error.HTTPError as e:
            if e.code==404:continue
            raise
        (cache/name).write_bytes(payload);d=csv_from_zip(payload);d.columns=[str(c).lower() for c in d.columns]
        t=next(c for c in d if 'time' in c);r=next(c for c in d if 'funding' in c and 'time' not in c);chunks.append(pd.DataFrame({'time':epoch(d[t]),'rate':pd.to_numeric(d[r],errors='coerce')}));manifest.append({'symbol':symbol,'month':month,'kind':'funding','sha256':sha})
    return pd.concat(chunks).dropna().sort_values('time').drop_duplicates('time') if chunks else pd.DataFrame(columns=['time','rate'])

def zprior(s,w):
    return (s-s.rolling(w,min_periods=max(w//3,12)).mean().shift(1))/s.rolling(w,min_periods=max(w//3,12)).std(ddof=0).shift(1).replace(0,np.nan)

def build(symbol,cache,manifest):
    chunks=[]
    for year in (2023,2024,2025):
        for month in range(1,13):
            target=date(year,month,15)
            local=[]
            for k in (2,1,0):
                day=(target-timedelta(days=k)).isoformat();dep=parse_depth(symbol,day,cache,manifest)
                if dep is None:continue
                kli=parse_klines(symbol,day,cache,manifest);local.append(kli.join(dep,how='inner'))
            if local:
                x=pd.concat(local).sort_index();x['trade_day']=target.isoformat();chunks.append(x[x.index.date==target])
    if not chunks:return None
    x=pd.concat(chunks).sort_index();bid=[c for c in x if c.startswith('bid_')];ask=[c for c in x if c.startswith('ask_')]
    if not bid or not ask:raise ValueError('no bid/ask depth fields')
    # nearest common level or aggregate across all reported bands
    x['bid_depth']=x[bid].sum(axis=1,min_count=1);x['ask_depth']=x[ask].sum(axis=1,min_count=1);x['depth_imb']=(x.bid_depth-x.ask_depth)/(x.bid_depth+x.ask_depth)
    x['bid_chg']=np.log(x.bid_depth.replace(0,np.nan)/x.bid_depth.shift(1));x['ask_chg']=np.log(x.ask_depth.replace(0,np.nan)/x.ask_depth.shift(1));x['flow']=x.signed/x.quote.replace(0,np.nan);x['ret']=np.log(x.close/x.close.shift(1));x['eff_1h']=np.log(x.close/x.close.shift(12)).abs()/(x.ret.abs().rolling(12).sum()+1e-12)
    for c in ['bid_chg','ask_chg','flow','depth_imb','ret']:
        x[c+'_z']=zprior(x[c],48)
    x['symbol']=symbol;return x

def signals(x,family,z):
    if family=='vacuum_long':di=pd.Series(1,index=x.index);mask=(x.ask_chg_z<=-z)&(x.bid_chg_z>-.5)&(x.flow_z>=z)&(x.ret_z>0);active=(x.ask_chg_z<0)&(x.flow>0);score=-x.ask_chg_z+x.flow_z
    elif family=='vacuum_short':di=pd.Series(-1,index=x.index);mask=(x.bid_chg_z<=-z)&(x.ask_chg_z>-.5)&(x.flow_z<=-z)&(x.ret_z<0);active=(x.bid_chg_z<0)&(x.flow<0);score=-x.bid_chg_z-x.flow_z
    elif family=='replenish_short':di=pd.Series(-1,index=x.index);mask=(x.flow_z>=z)&(x.ask_chg_z>=z)&(x.eff_1h<.25);active=(x.ask_chg_z>0)&(x.flow>0);score=x.flow_z+x.ask_chg_z
    else:di=pd.Series(1,index=x.index);mask=(x.flow_z<=-z)&(x.bid_chg_z>=z)&(x.eff_1h<.25);active=(x.bid_chg_z>0)&(x.flow<0);score=-x.flow_z+x.bid_chg_z
    return di.where(mask,0),active.fillna(False),score

def run(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);cache=out/'raw';cache.mkdir(exist_ok=True);manifest=[];frames={}
    for s in SYMBOLS:
        try:frames[s]=build(s,cache,manifest)
        except Exception as e:manifest.append({'symbol':s,'kind':'build','status':'error','error':repr(e)})
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    frames={s:x for s,x in frames.items() if x is not None}
    if not frames:
        (out/'summary.json').write_text(json.dumps({'status':'NO_OFFICIAL_BOOKDEPTH','eligible':0},indent=2));return
    funds={s:funding(s,cache,manifest).set_index('time').rate for s in frames};times=pd.DatetimeIndex(sorted(set().union(*[set(x.index) for x in frames.values()])));opens=pd.DataFrame(index=times,columns=frames,dtype=float)
    for s,x in frames.items():opens[s]=x.open.reindex(times)
    rows=[]
    for fam in ('vacuum_long','vacuum_short','replenish_short','replenish_long'):
      for z in (1.5,2.,2.5,3.):
       maps={};events=[]
       for s,x in frames.items():
        d,a,sc=signals(x,fam,z);maps[s]=(d,a,sc);m=(d!=0)&d.shift(1).fillna(0).ne(d)
        if m.any():events.append(pd.DataFrame({'time':x.index[m],'symbol':s,'direction':d[m].astype(int),'score':sc[m]}))
       if not events:continue
       best=pd.concat(events).sort_values(['time','score','symbol'],ascending=[True,False,True]).groupby('time',as_index=False).first().set_index('time')
       for cost in (24.,36.,48.):
        eq=1.;pos=None;last=None;curve=[];trades=[];half=cost/2/1e4
        for t in times:
         if last is not None and pos:
          s,di=pos;p0=opens.at[last,s];p1=opens.at[t,s]
          if pd.notna(p0) and pd.notna(p1):eq*=max(1+di*(p1/p0-1),1e-9)
          fr=funds.get(s)
          if fr is not None:
           v=fr[(fr.index>last)&(fr.index<=t)]
           if len(v):eq*=max(1-di*float(v.sum()),1e-9)
         if pos:
          s,di=pos;d,a,sc=maps[s];same=t in a.index and bool(a.loc[t])
          if not same:eq*=1-half;trades[-1]['exit']=t;pos=None
         if pos is None and t in best.index:
          r=best.loc[t];s=str(r.symbol);di=int(r.direction)
          if pd.notna(opens.at[t,s]):eq*=1-half;pos=(s,di);trades.append({'entry':t,'symbol':s,'direction':di})
         curve.append((t,eq));last=t
        c=pd.DataFrame(curve,columns=['time','equity']).set_index('time');l=pd.DataFrame(trades)
        row={'family':fam,'z':z,'cost_bps':cost}
        for name,a,b in [('DEV_2023','2023-01-01','2024-01-01'),('SELECT_2024','2024-01-01','2025-01-01'),('VALID_2025','2025-01-01','2026-01-01')]:
         q=c[(c.index>=a)&(c.index<b)]
         if len(q)>1:
          e=q.equity/q.equity.iloc[0];days=(pd.Timestamp(b)-pd.Timestamp(a)).days;g=np.exp(np.log(max(e.iloc[-1],1e-12))/days)-1;dd=(1-e/e.cummax()).max();net=e.iloc[-1]-1;nt=int(((pd.to_datetime(l.entry,utc=True)>=pd.Timestamp(a,tz='UTC'))&(pd.to_datetime(l.entry,utc=True)<pd.Timestamp(b,tz='UTC'))).sum()) if len(l) else 0
         else:g=net=dd=np.nan;nt=0
         row.update({f'{name}_gday':g,f'{name}_net':net,f'{name}_mdd':dd,f'{name}_trades':nt})
        rows.append(row)
    r=pd.DataFrame(rows);r.to_csv(out/'screen.csv',index=False)
    if len(r):
      r['min_gday']=r[['DEV_2023_gday','SELECT_2024_gday','VALID_2025_gday']].min(axis=1);r['max_mdd']=r[['DEV_2023_mdd','SELECT_2024_mdd','VALID_2025_mdd']].max(axis=1);r=r.sort_values(['min_gday','max_mdd'],ascending=[False,True]);r.to_csv(out/'rank.csv',index=False);e=r[(r.min_gday>0)&(r.max_mdd<.4)&(r.VALID_2025_trades>=5)];e.to_csv(out/'eligible.csv',index=False);target=r[(r.min_gday>=.01)&(r.max_mdd<=.4)];target.to_csv(out/'target_met.csv',index=False);summary={'status':'COMPLETE','rows':len(r),'eligible':len(e),'target_met':len(target),'best':r.head(20).replace([np.nan,np.inf,-np.inf],None).to_dict('records')}
    else:summary={'status':'NO_SIGNALS','eligible':0,'target_met':0}
    (out/'summary.json').write_text(json.dumps(summary,indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);run(p.parse_args().out)
