from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from numba import njit, prange, set_num_threads

OUT=Path('research/phase33_spot_perp_discovery/results');OUT.mkdir(parents=True,exist_ok=True)
CACHE=Path('/tmp/phase33-cache');CACHE.mkdir(parents=True,exist_ok=True)
SYMBOLS=('BTCUSDT','ETHUSDT');LS=(1,3,6,12);HORIZONS=np.array([3,6,12,24,48,96],np.int64)
PERIODS=(('dev','2022-01-01','2024-01-01'),('val','2024-01-01','2025-01-01'),('conf','2025-01-01','2026-01-01'),('hold','2026-01-01','2026-07-01'))
COLS=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_buy_base','taker_buy_quote','ignore']

def fetch(path:str)->Path:
    dst=CACHE/path.replace('/','__');dst.parent.mkdir(parents=True,exist_ok=True)
    if dst.exists():return dst
    url='https://data.binance.vision/'+path
    r=requests.get(url+'.CHECKSUM',timeout=60);r.raise_for_status();expected=r.text.strip().split()[0]
    z=requests.get(url,timeout=180);z.raise_for_status();dst.write_bytes(z.content)
    actual=hashlib.sha256(z.content).hexdigest()
    if actual!=expected:raise RuntimeError(f'checksum {path} {actual} {expected}')
    return dst

def read_kline(path:str)->pd.DataFrame:
    p=fetch(path)
    with zipfile.ZipFile(p) as zf:
        names=[n for n in zf.namelist() if n.endswith('.csv')];raw=zf.read(names[0])
    d=pd.read_csv(io.BytesIO(raw),header=None,names=COLS,usecols=range(12))
    x=pd.to_numeric(d.open_time,errors='coerce').to_numpy(np.float64);micro=x>1e14;x[micro]/=1000;d['open_time']=x.astype(np.int64)
    for c in ('open','high','low','close','quote_volume','taker_buy_quote'):d[c]=pd.to_numeric(d[c],errors='coerce')
    return d[['open_time','open','high','low','close','quote_volume','taker_buy_quote']]

def read_funding(path:str)->pd.DataFrame:
    try:p=fetch(path)
    except Exception:return pd.DataFrame(columns=['time','rate'])
    with zipfile.ZipFile(p) as zf:raw=zf.read([n for n in zf.namelist() if n.endswith('.csv')][0])
    first=raw.splitlines()[0].decode('utf-8-sig').split(',')[0]
    d=pd.read_csv(io.BytesIO(raw),header=0 if not first.lstrip('-').isdigit() else None)
    if 'calc_time' in d.columns:tc='calc_time';rc='funding_rate'
    elif 'calc_time_ms' in d.columns:tc='calc_time_ms';rc='funding_rate'
    else:tc=d.columns[0];rc=d.columns[-1]
    t=pd.to_numeric(d[tc],errors='coerce').to_numpy(float);t[t>1e14]/=1000
    return pd.DataFrame({'time':t.astype(np.int64),'rate':pd.to_numeric(d[rc],errors='coerce')}).dropna()

def month_paths(symbol:str,month:str,market:str)->str:
    if market=='spot':return f'data/spot/monthly/klines/{symbol}/5m/{symbol}-5m-{month}.zip'
    return f'data/futures/um/monthly/klines/{symbol}/5m/{symbol}-5m-{month}.zip'

def load_symbol(symbol:str)->pd.DataFrame:
    months=[str(x) for x in pd.period_range('2022-01','2026-06',freq='M')]
    spot=[];fut=[];fund=[]
    for mi,m in enumerate(months):
        spot.append(read_kline(month_paths(symbol,m,'spot')));fut.append(read_kline(month_paths(symbol,m,'fut')))
        fund.append(read_funding(f'data/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{m}.zip'))
        if mi%12==0:print(symbol,m,flush=True)
    s=pd.concat(spot).sort_values('open_time').drop_duplicates('open_time',keep='last').add_prefix('spot_').rename(columns={'spot_open_time':'open_time'})
    f=pd.concat(fut).sort_values('open_time').drop_duplicates('open_time',keep='last').add_prefix('fut_').rename(columns={'fut_open_time':'open_time'})
    d=s.merge(f,on='open_time',how='inner',validate='one_to_one').sort_values('open_time').reset_index(drop=True)
    ff=pd.concat(fund).sort_values('time').drop_duplicates('time',keep='last') if fund else pd.DataFrame(columns=['time','rate'])
    bar=np.zeros(len(d));ix=np.searchsorted(d.open_time.to_numpy(np.int64),ff.time.to_numpy(np.int64),side='left');ok=(ix>=0)&(ix<len(d));np.add.at(bar,ix[ok],ff.rate.to_numpy(float)[ok]);d['fund_cum']=np.cumsum(bar)
    return d

def prior_z(s:pd.Series,w=2016,minp=1000)->pd.Series:
    p=s.shift(1);return (s-p.rolling(w,min_periods=minp).mean())/p.rolling(w,min_periods=minp).std(ddof=0).replace(0,np.nan)

def make_panel(d:pd.DataFrame,si:int)->pd.DataFrame:
    t=d.open_time.to_numpy(np.int64);sc=d.spot_close.to_numpy(float);fo=d.fut_open.to_numpy(float);fc=d.fut_close.to_numpy(float);fund=d.fund_cum.to_numpy(float)
    sl=pd.Series(np.log(sc));fl=pd.Series(np.log(fc));rows=[];basis=fl-sl;basis_z=prior_z(basis)
    for L in LS:
        sr=sl-sl.shift(L);fr=fl-fl.shift(L);gap=fr-sr
        ssig=sl.diff().rolling(2016,min_periods=1000).std(ddof=0).shift(1);fsig=fl.diff().rolling(2016,min_periods=1000).std(ddof=0).shift(1)
        srz=sr/(ssig*np.sqrt(L));frz=fr/(fsig*np.sqrt(L));sf=(d.spot_taker_buy_quote*2-d.spot_quote_volume).rolling(L,min_periods=L).sum()/d.spot_quote_volume.rolling(L,min_periods=L).sum().replace(0,np.nan);ff=(d.fut_taker_buy_quote*2-d.fut_quote_volume).rolling(L,min_periods=L).sum()/d.fut_quote_volume.rolling(L,min_periods=L).sum().replace(0,np.nan);sfz=prior_z(sf);ffz=prior_z(ff)
        fams={'SPOT_LEAD_CONT':(np.sign(srz),np.abs(srz),np.sign(srz)*(srz-frz),np.sign(srz)*sfz,np.sign(srz)*ffz),'FUT_LEAD_CONT':(np.sign(frz),np.abs(frz),np.sign(frz)*(frz-srz),np.sign(frz)*ffz,np.sign(frz)*sfz),'FUT_OVERREV':(-np.sign(frz),np.abs(frz),np.sign(frz)*(frz-srz),-np.sign(frz)*sfz,-np.sign(frz)*ffz),'SPOT_FLOW_LEAD':(np.sign(sfz),np.abs(sfz),np.sign(sfz)*(srz-frz),np.abs(sfz),np.sign(sfz)*ffz),'BASIS_REVERT':(-np.sign(basis_z),np.abs(basis_z),np.abs(basis_z),-np.sign(basis_z)*ffz,-np.sign(basis_z)*sfz)}
        for fam,(side,strength,gap_al,flow1,flow2) in fams.items():
            m=np.isfinite(strength)&np.isfinite(gap_al)&np.isfinite(flow1)&(strength>=1.5)&(gap_al>=.5)&(flow1>=0)&(side!=0);ii=np.flatnonzero(m.to_numpy() if hasattr(m,'to_numpy') else m)
            if not len(ii):continue
            sd=np.asarray(side)[ii].astype(np.int8);rec=pd.DataFrame({'time':t[ii],'symbol':si,'bar':ii,'family':fam,'L':L,'side':sd,'strength':np.asarray(strength)[ii],'gap':np.asarray(gap_al)[ii],'flow1':np.asarray(flow1)[ii],'flow2':np.asarray(flow2)[ii],'basis_z':basis_z.to_numpy()[ii]});rec['score']=rec.strength+rec.gap+rec.flow1.clip(lower=0)+rec.flow2.clip(lower=0)
            for hj,H in enumerate(HORIZONS):
                valid=ii+1+H<len(d);gross=np.full(len(ii),np.nan);fd=np.full(len(ii),np.nan);ex=np.full(len(ii),-1,np.int64);jj=ii[valid];gross[valid]=sd[valid]*(fo[jj+1+H]/fo[jj+1]-1)*1e4;fd[valid]=sd[valid]*(fund[jj+1+H]-fund[jj]);ex[valid]=t[jj+1+H];rec[f'g{hj}']=gross;rec[f'f{hj}']=fd;rec[f'x{hj}']=ex
            rows.append(rec)
    return pd.concat(rows,ignore_index=True)

@njit(cache=True,parallel=True)
def evaluate(starts,ends,time,period,symbol,family,L,strength,gap,flow1,gross,fund,exit_,configs):
    C=len(configs);cnt=np.zeros((C,4),np.int64);s18=np.zeros((C,4));s24=np.zeros((C,4));top=np.full((C,4,10),-1e300)
    for ci in prange(C):
        cf=configs[ci];fam=int(cf[0]);ll=int(cf[1]);sthr=cf[2];gthr=cf[3];fthr=cf[4];hj=int(cf[5]);cool=int(cf[6]);free=-9223372036854775807;sfree=np.full(2,-9223372036854775807,np.int64)
        for gi in range(len(starts)):
            a=starts[gi];b=ends[gi];tt=time[a]
            if tt<free:continue
            chosen=-1
            for q in range(a,b):
                si=symbol[q]
                if tt<sfree[si]:continue
                if family[q]==fam and L[q]==ll and strength[q]>=sthr and gap[q]>=gthr and flow1[q]>=fthr and np.isfinite(gross[q,hj]) and exit_[q,hj]>tt:chosen=q;break
            if chosen>=0:
                p=period[chosen]
                if p>=0:
                    v=gross[chosen,hj]-18.-fund[chosen,hj]*1e4;cnt[ci,p]+=1;s18[ci,p]+=v;s24[ci,p]+=v-6
                    if v>top[ci,p,0]:
                        top[ci,p,0]=v
                        for k in range(9):
                            if top[ci,p,k]>top[ci,p,k+1]:x=top[ci,p,k];top[ci,p,k]=top[ci,p,k+1];top[ci,p,k+1]=x
                            else:break
                free=exit_[chosen,hj];sfree[symbol[chosen]]=tt+cool*300000
    return cnt,s18,s24,top

def main():
    panels=[]
    for si,s in enumerate(SYMBOLS):d=load_symbol(s);panels.append(make_panel(d,si));print('panel',s,len(panels[-1]),flush=True)
    p=pd.concat(panels,ignore_index=True);fams={x:i for i,x in enumerate(sorted(p.family.unique()))};p['family_i']=p.family.map(fams);p=p.sort_values(['time','score','symbol'],ascending=[True,False,True]).reset_index(drop=True);p.to_csv(OUT/'EVENT_PANEL.csv',index=False)
    time=p.time.to_numpy(np.int64);starts=np.r_[0,np.flatnonzero(np.diff(time)!=0)+1].astype(np.int64);ends=np.r_[starts[1:],len(p)].astype(np.int64);period=np.full(len(p),-1,np.int8)
    for i,(_,a,b) in enumerate(PERIODS):lo=int(pd.Timestamp(a,tz='UTC').timestamp()*1000);hi=int(pd.Timestamp(b,tz='UTC').timestamp()*1000);period[(time>=lo)&(time<hi)]=i
    configs=[];meta=[]
    for fam,L,st,gap,flow,hj,cool in itertools.product(range(len(fams)),LS,(1.5,2.,2.5),(.5,1.,1.5),(0.,.5,1.),range(len(HORIZONS)),(3,12)):configs.append((fam,L,st,gap,flow,hj,cool));meta.append({'family':next(k for k,v in fams.items() if v==fam),'L':L,'strength_min':st,'gap_min':gap,'flow_min':flow,'horizon':int(HORIZONS[hj]),'cooldown_bars':cool})
    gross=np.column_stack([p[f'g{i}'].to_numpy(float) for i in range(len(HORIZONS))]);fund=np.column_stack([p[f'f{i}'].to_numpy(float) for i in range(len(HORIZONS))]);ex=np.column_stack([p[f'x{i}'].fillna(-1).to_numpy(np.int64) for i in range(len(HORIZONS))]);set_num_threads(4);cnt,s18,s24,top=evaluate(starts,ends,time,period,p.symbol.to_numpy(np.int8),p.family_i.to_numpy(np.int8),p.L.to_numpy(np.int8),p.strength.to_numpy(float),p.gap.to_numpy(float),p.flow1.to_numpy(float),gross,fund,ex,np.asarray(configs,float))
    rows=[]
    for ci,m in enumerate(meta):
        r=dict(m)
        for j,(name,_,_) in enumerate(PERIODS):
            n=int(cnt[ci,j]);r[f'{name}_n']=n;r[f'{name}_mean18']=float(s18[ci,j]/n) if n else math.nan;r[f'{name}_mean24']=float(s24[ci,j]/n) if n else math.nan;tt=top[ci,j][top[ci,j]>-1e200];r[f'{name}_top10_18']=float((s18[ci,j]-tt.sum())/(n-len(tt))) if n>len(tt) else math.nan
        vals=[r['dev_mean18'],r['val_mean18'],r['dev_top10_18'],r['val_top10_18']];r['score']=min(vals) if np.isfinite(vals).all() else -1e9;rows.append(r)
    grid=pd.DataFrame(rows).sort_values(['score','conf_mean18'],ascending=False);grid.to_csv(OUT/'GRID.csv',index=False);rob=grid[(grid.dev_n>=100)&(grid.val_n>=30)&(grid.dev_mean18>0)&(grid.val_mean18>0)&(grid.dev_top10_18>0)&(grid.val_top10_18>0)&(grid.dev_mean24>0)&(grid.val_mean24>0)].copy();rob.to_csv(OUT/'ROBUST.csv',index=False)
    summary={'status':'CANDIDATE' if len(rob) else 'CASH','event_rows':len(p),'grid_rows':len(grid),'robust_rows':len(rob),'family_map':fams,'top':grid.head(30).replace({np.nan:None}).to_dict('records'),'top_robust':rob.head(30).replace({np.nan:None}).to_dict('records'),'data':'official Binance spot and USD-M 5m archives with checksum','causality':'current completed bars; next future open; chronological periods; no future cluster maxima; one global slot'}; (OUT/'SUMMARY.json').write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n');print(json.dumps({k:summary[k] for k in ('status','event_rows','grid_rows','robust_rows')},indent=2))
if __name__=='__main__':main()
