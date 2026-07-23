from __future__ import annotations
import argparse,hashlib,io,json,math,time,urllib.error,urllib.request,zipfile
from pathlib import Path
import numpy as np
import pandas as pd
BASE='https://data.binance.vision/data/futures/um/monthly'
SYMBOLS=('BTCUSDT','ETHUSDT')
UA='smc-v250-funding-settlement/1.0'

def fetch(url,attempts=6):
    err=None
    for i in range(attempts):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':UA})
            with urllib.request.urlopen(req,timeout=240) as r:return r.read()
        except Exception as e:
            err=e
            if i+1<attempts:time.sleep(min(2**i,20))
    raise RuntimeError(f'{url}: {err!r}')

def verified(url,cache):
    name=url.rsplit('/',1)[-1];p=cache/name
    if p.exists() and (p.with_suffix(p.suffix+'.CHECKSUM')).exists():
        payload=p.read_bytes();expected=(p.with_suffix(p.suffix+'.CHECKSUM')).read_text().split()[0].lower()
    else:
        chk=fetch(url+'.CHECKSUM');expected=chk.decode('utf-8-sig').strip().split()[0].lower();payload=fetch(url);p.write_bytes(payload);p.with_suffix(p.suffix+'.CHECKSUM').write_text(f'{expected}  {name}\n')
    actual=hashlib.sha256(payload).hexdigest()
    if actual!=expected:raise ValueError(f'checksum mismatch {url}')
    return payload,actual

def csvzip(payload,header=None):
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        ns=[n for n in z.namelist() if n.lower().endswith('.csv')]
        if len(ns)!=1:raise ValueError(ns)
        return pd.read_csv(z.open(ns[0]),header=header,low_memory=False)

def epoch(s):
    x=pd.to_numeric(s,errors='coerce');m=float(x.dropna().abs().median()) if x.notna().any() else 0;u='ns' if m>1e17 else 'us' if m>1e14 else 'ms' if m>1e11 else 's';return pd.to_datetime(x,unit=u,utc=True,errors='coerce')

def months(a,b):
    p=pd.Period(a,freq='M');e=pd.Period(b,freq='M')
    while p<=e:yield str(p);p+=1

def load_symbol(symbol,cache,manifest):
    ks=[];ps=[];fs=[]
    for m in months('2021-01','2025-12'):
        for kind,container in [('klines',ks),('premiumIndexKlines',ps),('fundingRate',fs)]:
            if kind=='fundingRate':name=f'{symbol}-fundingRate-{m}.zip';url=f'{BASE}/{kind}/{symbol}/{name}'
            else:name=f'{symbol}-5m-{m}.zip';url=f'{BASE}/{kind}/{symbol}/5m/{name}'
            try:payload,sha=verified(url,cache/symbol/kind)
            except Exception as e:
                manifest.append({'symbol':symbol,'month':m,'kind':kind,'status':'error','error':repr(e)});continue
            d=csvzip(payload,header=None)
            if pd.isna(pd.to_numeric(d.iloc[0,0],errors='coerce')):d=d.iloc[1:].copy()
            if kind=='klines':
                d=d.iloc[:,:11];d.columns=['time','open','high','low','close','volume','close_time','quote','trades','taker_buy_base','taker_buy_quote'];d['time']=epoch(d.time)
                for c in d.columns[1:]:d[c]=pd.to_numeric(d[c],errors='coerce')
                d['signed']=2*d.taker_buy_quote-d.quote;container.append(d[['time','open','high','low','close','quote','signed']])
            elif kind=='premiumIndexKlines':
                d=d.iloc[:,:7];d.columns=['time','popen','phigh','plow','pclose','ignore','close_time'];d['time']=epoch(d.time)
                for c in ('popen','phigh','plow','pclose'):d[c]=pd.to_numeric(d[c],errors='coerce')
                container.append(d[['time','popen','phigh','plow','pclose']])
            else:
                # archive may have header or 3 columns calc_time, interval, rate, mark
                d.columns=[str(c).lower() for c in d.columns]
                if all(c.isdigit() for c in d.columns):
                    names=['time','funding_interval_hours','funding_rate','mark_price'];d=d.iloc[:,:min(len(names),d.shape[1])];d.columns=names[:d.shape[1]]
                tc=next((c for c in d if 'time' in c),d.columns[0]);rc=next((c for c in d if 'funding' in c and 'time' not in c and 'interval' not in c),None)
                if rc is None:rc=d.columns[-2] if d.shape[1]>=2 else d.columns[-1]
                q=pd.DataFrame({'time':epoch(d[tc]),'funding_rate':pd.to_numeric(d[rc],errors='coerce')});container.append(q)
            manifest.append({'symbol':symbol,'month':m,'kind':kind,'status':'verified','sha256':sha,'rows':len(d)})
    if not ks or not ps:return None,None
    k=pd.concat(ks).dropna(subset=['time','open','close']).sort_values('time').drop_duplicates('time').set_index('time')
    p=pd.concat(ps).dropna(subset=['time','pclose']).sort_values('time').drop_duplicates('time').set_index('time')
    x=k.join(p,how='inner')
    f=pd.concat(fs).dropna().sort_values('time').drop_duplicates('time').set_index('time') if fs else pd.DataFrame(columns=['funding_rate'])
    # causal state
    x['ret5']=np.log(x.close/x.close.shift(1));x['ret15']=np.log(x.close/x.close.shift(3));x['ret1h']=np.log(x.close/x.close.shift(12));x['flow15']=x.signed.rolling(3,min_periods=2).sum()/x.quote.rolling(3,min_periods=2).sum().replace(0,np.nan)
    tr=pd.concat([x.high-x.low,(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1);x['atr1h']=tr.rolling(12,min_periods=6).mean().shift(1);x['rv1d']=x.ret5.rolling(288,min_periods=96).std(ddof=0).shift(1)*math.sqrt(288)
    def z(s,w=288*90):
        return (s-s.rolling(w,min_periods=288*14).mean().shift(1))/s.rolling(w,min_periods=288*14).std(ddof=0).shift(1).replace(0,np.nan)
    x['prem_z']=z(x.pclose);x['flow_z']=z(x.flow15);x['ret_z']=z(x.ret15);x['rv_z']=z(x.rv1d)
    # known clock and running premium average in current 8h settlement window
    ns=x.index.view('i8');settle_id=(ns//(8*3600*10**9)).astype(np.int64);x['settle_id']=settle_id;x['premium_running_mean']=x.groupby('settle_id').pclose.expanding().mean().reset_index(level=0,drop=True)
    next_settle=pd.to_datetime((settle_id+1)*(8*3600*10**9),utc=True);x['minutes_to_settlement']=(next_settle-x.index)/pd.Timedelta(minutes=1);x['next_settlement']=next_settle
    if len(f):
        x['last_funding']=f.funding_rate.reindex(x.index,method='ffill').fillna(0)
    else:x['last_funding']=0.
    x['estimated_funding_proxy']=x.premium_running_mean+0.25*x.last_funding
    return x,f

def simulate(frames,funds,family,delay,post_hold,z,stop_atr,risk,target_vol,cost):
    # event list created only from completed bars; entry is next 5m open
    events=[]
    for sym,x in frames.items():
        proxy=x.estimated_funding_proxy;ext=(x.prem_z.abs()>=z)
        if family=='carry_capture':
            sig=ext&(x.minutes_to_settlement==delay);direction=-np.sign(proxy);score=x.prem_z.abs()+np.abs(proxy)*1e4;exit_time=x.next_settlement+pd.to_timedelta(post_hold,unit='m')
        elif family=='post_reset':
            sig=ext&(x.minutes_to_settlement==0)&(np.sign(x.flow15)==-np.sign(proxy));direction=-np.sign(proxy);score=x.prem_z.abs()+x.flow_z.abs();exit_time=x.index+pd.to_timedelta(post_hold,unit='m')
        else:
            sig=ext&(x.minutes_to_settlement==delay)&(np.sign(x.flow15)==np.sign(proxy))&(x.ret_z.abs()>=.5);direction=np.sign(proxy);score=x.prem_z.abs()+x.flow_z.abs();exit_time=x.next_settlement
        q=pd.DataFrame({'signal_time':x.index[sig],'entry_time':x.index.to_series().shift(-1)[sig].values,'exit_hint':exit_time[sig],'symbol':sym,'direction':direction[sig].astype(int),'score':score[sig].values})
        events.append(q.dropna())
    ev=pd.concat(events,ignore_index=True).sort_values(['entry_time','score','symbol'],ascending=[True,False,True],kind='mergesort') if events else pd.DataFrame()
    # merge union timeline
    times=pd.DatetimeIndex(sorted(set().union(*[set(x.index) for x in frames.values()])))
    eq=1.;curve=[];trades=[];free=pd.Timestamp.min.tz_localize('UTC');half=cost/20000.
    for _,e in ev.iterrows():
        et=pd.Timestamp(e.entry_time);xh=pd.Timestamp(e.exit_hint);sym=e.symbol;di=int(e.direction)
        if et<free or et not in frames[sym].index or di==0:continue
        x=frames[sym];p=float(x.at[et,'open']);a=float(x.at[et,'atr1h']);v=float(x.at[et,'rv1d'])
        if not (p>0 and a>0 and v>0):continue
        lev=min(10.,risk/(stop_atr*a/p+half+.0002),target_vol/v)
        if lev<.05:continue
        path=x[(x.index>=et)&(x.index<=xh)]
        if len(path)<2:continue
        stop=p*(1-di*stop_atr*a/p);exitp=float(path.iloc[-1].close);xt=path.index[-1];reason='economic_horizon'
        for t,r in path.iterrows():
            if (di>0 and r.low<=stop) or (di<0 and r.high>=stop):exitp=stop*(1-.0002 if di>0 else 1+.0002);xt=t;reason='emergency_stop';break
            if family in ('post_reset','squeeze') and t>et and np.sign(r.pclose)!=np.sign(x.at[et,'pclose']):exitp=float(r.open);xt=t;reason='premium_normalized';break
        gross=di*(exitp/p-1)*lev;fund=0.
        f=funds.get(sym)
        if f is not None and len(f):
            rr=f[(f.index>et)&(f.index<=xt)].funding_rate
            if len(rr):fund=-di*lev*float(rr.sum())
        net=gross+fund-lev*cost/1e4;eq*=max(1+net,1e-9);trades.append({'entry_time':et,'exit_time':xt,'symbol':sym,'direction':di,'leverage':lev,'gross':gross,'funding':fund,'net':net,'equity':eq,'reason':reason});free=xt
    return pd.DataFrame(trades)

def metrics(t,a,b):
    if len(t)==0:return {'trades':0,'net':0.,'gday':0.,'mdd':0.}
    q=t[(t.entry_time>=a)&(t.entry_time<b)].copy()
    if len(q)==0:return {'trades':0,'net':0.,'gday':0.,'mdd':0.}
    e=(1+q.net.clip(lower=-.999)).cumprod();days=(pd.Timestamp(b)-pd.Timestamp(a)).days;g=math.exp(math.log(max(float(e.iloc[-1]),1e-12))/days)-1;dd=float((1-e/e.cummax()).max());return {'trades':len(q),'net':float(e.iloc[-1]-1),'gday':g,'mdd':dd}

def run(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);cache=out/'raw';manifest=[];frames={};funds={}
    for s in SYMBOLS:
        (cache/s/'klines').mkdir(parents=True,exist_ok=True);(cache/s/'premiumIndexKlines').mkdir(parents=True,exist_ok=True);(cache/s/'fundingRate').mkdir(parents=True,exist_ok=True)
        x,f=load_symbol(s,cache,manifest)
        if x is not None:frames[s]=x;funds[s]=f
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    rows=[]
    for fam in ('carry_capture','post_reset','squeeze'):
      for delay in (5,15,30,60):
       for hold in (5,15,30,60,120,240):
        if fam=='squeeze' and hold!=5:continue
        for z in (1.5,2.,2.5,3.):
         for sm in (2.,3.,4.):
          for risk in (.005,.01,.02,.03):
           for tv in (.02,.04,.06,.08):
            for cost in (12.,24.,36.,48.):
             t=simulate(frames,funds,fam,delay,hold,z,sm,risk,tv,cost)
             row={'family':fam,'delay_min':delay,'post_hold_min':hold,'z':z,'stop_atr':sm,'risk':risk,'target_daily_vol':tv,'cost_bps':cost}
             for name,a,b in [('Y2022','2022-01-01','2023-01-01'),('Y2023','2023-01-01','2024-01-01'),('Y2024','2024-01-01','2025-01-01'),('Y2025','2025-01-01','2026-01-01')]:
              m=metrics(t,pd.Timestamp(a,tz='UTC'),pd.Timestamp(b,tz='UTC'));row.update({f'{name}_{k}':v for k,v in m.items()})
             rows.append(row)
    r=pd.DataFrame(rows);r['min_gday']=r[[f'Y{y}_gday' for y in (2022,2023,2024,2025)]].min(axis=1);r['max_mdd']=r[[f'Y{y}_mdd' for y in (2022,2023,2024,2025)]].max(axis=1);r['min_trades']=r[[f'Y{y}_trades' for y in (2022,2023,2024,2025)]].min(axis=1);r=r.sort_values(['min_gday','max_mdd'],ascending=[False,True]);r.to_csv(out/'rank.csv',index=False);r.to_csv(out/'screen.csv',index=False)
    e=r[(r.min_gday>0)&(r.max_mdd<=.4)&(r.min_trades>=3)];e.to_csv(out/'eligible.csv',index=False);target=r[(r.min_gday>=.01)&(r.max_mdd<=.4)&(r.min_trades>=30)];target.to_csv(out/'target_met.csv',index=False);(out/'summary.json').write_text(json.dumps({'status':'COMPLETE','rows':len(r),'eligible':len(e),'target_met':len(target),'best':r.head(30).replace([np.nan,np.inf,-np.inf],None).to_dict('records')},indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);run(p.parse_args().out)
