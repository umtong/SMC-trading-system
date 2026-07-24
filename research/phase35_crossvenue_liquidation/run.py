from __future__ import annotations
import ast,json,math,re
from pathlib import Path
import numpy as np
import pandas as pd
from huggingface_hub import hf_hub_download,list_repo_files

REPO='azulcoder/btc-quant-ticks';VENUES=('binancef','bybit','okx');TABLES=('trades','liquidations','depth_snapshots')
OUT=Path('research/phase35_crossvenue_liquidation/results');OUT.mkdir(parents=True,exist_ok=True)

def levels(x):
    if isinstance(x,str):
        try:x=json.loads(x)
        except Exception:
            try:x=ast.literal_eval(x)
            except Exception:return []
    if isinstance(x,np.ndarray):x=x.tolist()
    out=[]
    if isinstance(x,(list,tuple)):
        for r in x:
            if isinstance(r,str):
                try:r=json.loads(r)
                except Exception:continue
            if isinstance(r,(list,tuple)) and len(r)>=2:
                try:
                    p,q=float(r[0]),float(r[1])
                    if p>0 and q>=0 and np.isfinite(p+q):out.append((p,q))
                except Exception:pass
    return out

def depth_row(r):
    b=sorted(levels(r.bids),reverse=True);a=sorted(levels(r.asks))
    if not b or not a:return None
    bp,bq=b[0];ap,aq=a[0]
    if ap<=bp:return None
    bd=sum(p*q for p,q in b);ad=sum(p*q for p,q in a);tot=bd+ad;mid=(bp+ap)/2
    micro=(ap*bq+bp*aq)/(bq+aq) if bq+aq>0 else mid
    return dict(sec=int(r.ts_ms)//1000,exchange=str(r.exchange),bid=bp,ask=ap,mid=mid,spread_bps=(ap/bp-1)*1e4,bid_depth=bd,ask_depth=ad,imbalance=(bd-ad)/tot if tot>0 else 0,micro_bps=(micro/mid-1)*1e4)

def download(days=10):
    files=list_repo_files(REPO,repo_type='dataset');pat=re.compile(r'^data/date=(\d{4}-\d{2}-\d{2})/(trades|liquidations|depth_snapshots)\.parquet$');found={};by={}
    for f in files:
        m=pat.match(f)
        if m:
            d,t=m.groups();found[d,t]=f;by.setdefault(d,set()).add(t)
    dates=sorted(d for d,x in by.items() if set(TABLES)<=x)[-days:]
    if len(dates)<3:raise RuntimeError(dates)
    paths={t:[] for t in TABLES}
    for d in dates:
        for t in TABLES:
            paths[t].append(Path(hf_hub_download(REPO,filename=found[d,t],repo_type='dataset',cache_dir='.hf-cache')))
    return dates,paths

def panel(paths):
    raw={k:pd.concat([pd.read_parquet(p) for p in v],ignore_index=True) for k,v in paths.items()}
    for k in raw:raw[k]=raw[k][raw[k].exchange.isin(VENUES)].sort_values(['ts_ms','exchange']).reset_index(drop=True)
    dep=[]
    for r in raw['depth_snapshots'].itertuples(index=False):
        x=depth_row(r)
        if x:dep.append(x)
    d=pd.DataFrame(dep).sort_values(['exchange','sec']).groupby(['exchange','sec'],as_index=False).last()
    tr=raw['trades'].copy();tr['sec']=tr.ts_ms.astype('int64')//1000;tr['notional']=pd.to_numeric(tr.price)*pd.to_numeric(tr.qty);tr['signed']=np.where(tr.aggressor_buy.astype(bool),tr.notional,-tr.notional)
    t=tr.groupby(['exchange','sec']).agg(trade_notional=('notional','sum'),signed_trade=('signed','sum')).reset_index()
    li=raw['liquidations'].copy();li['sec']=li.ts_ms.astype('int64')//1000
    if 'notional_usd' not in li:li['notional_usd']=pd.to_numeric(li.price)*pd.to_numeric(li.qty)
    li['long_liq']=np.where(li.side.astype(str).str.lower().eq('long'),li.notional_usd,0.);li['short_liq']=np.where(li.side.astype(str).str.lower().eq('short'),li.notional_usd,0.)
    l=li.groupby(['exchange','sec']).agg(long_liq=('long_liq','sum'),short_liq=('short_liq','sum')).reset_index()
    x=d.merge(t,on=['exchange','sec'],how='left').merge(l,on=['exchange','sec'],how='left')
    for c in ['trade_notional','signed_trade','long_liq','short_liq']:x[c]=x[c].fillna(0.)
    parts=[]
    for v,g in x.groupby('exchange',sort=False):
        g=g.sort_values('sec').copy();log=np.log(g.mid)
        for w in (5,10,30,60):
            g[f'ret{w}']=(log-log.shift(w))*1e4;q=g.signed_trade.rolling(w,min_periods=max(2,w//2)).sum();z=g.trade_notional.rolling(w,min_periods=max(2,w//2)).sum();g[f'flow{w}']=q/z.replace(0,np.nan)
        for c in ('bid_depth','ask_depth'):
            med=g[c].rolling(3600,min_periods=600).median().shift(1);g[c+'_ratio']=g[c]/med.replace(0,np.nan)
        total=g.long_liq+g.short_liq;p=np.log1p(total).shift(1);mu=p.rolling(21600,min_periods=1800).mean();sd=p.rolling(21600,min_periods=1800).std(ddof=0);g['liq_z']=(np.log1p(total)-mu)/sd.replace(0,np.nan);g['liq_usd']=total;g['liq_side']=np.where(g.short_liq>g.long_liq,1,np.where(g.long_liq>g.short_liq,-1,0));parts.append(g)
    return pd.concat(parts,ignore_index=True).sort_values(['sec','exchange']).reset_index(drop=True)

def events(x):
    rows=[]
    for v,g in x.groupby('exchange'):
        last=-10**30
        for r in g.itertuples(index=False):
            if np.isfinite(r.liq_z) and r.liq_side and r.liq_z>=1.5 and r.liq_usd>=25000 and r.sec-last>=30:
                rows.append(dict(event_sec=int(r.sec),shock_venue=v,side=int(r.liq_side),liq_z=float(r.liq_z),liq_usd=float(r.liq_usd)));last=int(r.sec)
    return pd.DataFrame(rows).sort_values(['event_sec','liq_z'],ascending=[True,False])

def candidates(ev,by):
    rows=[]
    for r in ev.itertuples(index=False):
        if r.event_sec not in by[r.shock_venue].index:continue
        b=by[r.shock_venue];r0=b.loc[r.event_sec];pre=r0.ask_depth_ratio if r.side>0 else r0.bid_depth_ratio
        for fam,lz,usd,frag,repl,flow,delay,h,mode in __import__('itertools').product(('CONT','RECLAIM','TRANSFER'),(1.5,2,2.5,3),(25000.,100000.,250000.),(.5,.75,1.),(1.,1.25,1.5),(0.,.25,.5),(3,5,10,20),(15,30,60,120,300),('SHOCK','LAG')):
            if fam=='RECLAIM' and mode=='LAG':continue
            if r.liq_z<lz or r.liq_usd<usd or not np.isfinite(pre) or pre>frag:continue
            t=r.event_sec+delay
            if t not in b.index:continue
            q=b.loc[t];move=r.side*(q.mid/r0.mid-1)*1e4;fa=r.side*q.get(f'flow{max(5,min(60,delay))}',np.nan);dr=q.ask_depth_ratio if r.side>0 else q.bid_depth_ratio;opp=q.bid_depth_ratio if r.side>0 else q.ask_depth_ratio;tv=r.shock_venue;side=r.side
            if fam=='CONT':
                if not(np.isfinite(fa) and fa>=flow and dr<=frag and move>=0):continue
            elif fam=='RECLAIM':
                side=-r.side
                if not(np.isfinite(fa) and fa<=-flow and opp>=repl and move<=0):continue
            else:
                vals=[]
                for v,g in by.items():
                    if r.event_sec in g.index and t in g.index:vals.append((r.side*(g.loc[t].mid/g.loc[r.event_sec].mid-1)*1e4,v))
                if len(vals)<2:continue
                vals.sort();tv=vals[0][1] if vals[0][1]!=r.shock_venue else vals[1][1];tg=by[tv];qq=tg.loc[t];tf=r.side*qq.get(f'flow{max(5,min(60,delay))}',np.nan);td=qq.ask_depth_ratio if r.side>0 else qq.bid_depth_ratio
                if not(np.isfinite(tf) and tf>=flow and td<=frag):continue
            pid=f'{fam}_{lz}_{usd}_{frag}_{repl}_{flow}_{delay}_{h}_{mode}';score=r.liq_z+math.log1p(r.liq_usd/25000)+max(fa,0)+max(1-dr,0)+max(opp-1,0);rows.append(dict(policy_id=pid,family=fam,decision_sec=t,target_venue=tv,side=side,score=score,horizon=h))
    return pd.DataFrame(rows)

def execute(c,by,cost):
    if c.empty:return c
    rows=[];free=-10**30
    for t,g in c.sort_values(['decision_sec','score'],ascending=[True,False]).groupby('decision_sec'):
        if t<free:continue
        r=g.iloc[0];book=by[r.target_venue];e=int(t)+1;x=e+int(r.horizon)
        if e not in book.index or x not in book.index:continue
        a=book.loc[e];b=book.loc[x];side=int(r.side);ep=a.ask if side>0 else a.bid;xp=b.bid if side>0 else b.ask;net=side*(xp/ep-1)*1e4-cost;rows.append({**r.to_dict(),'entry_sec':e,'exit_sec':x,'net_bps':net});free=x
    return pd.DataFrame(rows)

def trim(v,n):return float(np.sort(v)[:-n].mean()) if len(v)>n else math.nan

def stat(x,dates):
    if x.empty:return dict(n=0)
    v=x.net_bps.to_numpy(float);day=pd.to_datetime(x.decision_sec,unit='s',utc=True).dt.strftime('%Y-%m-%d');d=x.assign(day=day).groupby('day').net_bps.sum().reindex(dates,fill_value=0.)
    return dict(n=len(v),mean=float(v.mean()),top5=trim(v,5),top10=trim(v,10),bps_day=float(v.sum()/len(dates)),positive_days=float((d>0).mean()))

def main():
    dates,paths=download(10);x=panel(paths);x.to_parquet(OUT/'PANEL_1S.parquet',index=False);ev=events(x);ev.to_csv(OUT/'EVENTS.csv',index=False);by={v:g.set_index('sec').sort_index() for v,g in x.groupby('exchange')};c=candidates(ev,by);c.to_csv(OUT/'CANDIDATES.csv',index=False);n=len(dates);a=max(1,n//2);b=max(a+1,int(n*.75));parts={'dev':dates[:a],'val':dates[a:b],'conf':dates[b:]};rows=[];trades={}
    for pid,g in c.groupby('policy_id'):
        for cost in (8.,12.,16.):
            z=execute(g,by,cost);rec=dict(policy_id=pid,cost=cost,family=g.family.iloc[0])
            for name,ds in parts.items():
                lo=int(pd.Timestamp(ds[0],tz='UTC').timestamp());hi=int((pd.Timestamp(ds[-1],tz='UTC')+pd.Timedelta(days=1)).timestamp());rec.update({name+'_'+k:v for k,v in stat(z[(z.decision_sec>=lo)&(z.decision_sec<hi)],ds).items()})
            rows.append(rec)
            if cost==12:trades[pid]=z
    grid=pd.DataFrame(rows);base=grid[grid.cost==12];stress=grid[grid.cost==16][['policy_id','dev_mean','val_mean','dev_top5','val_top5']].rename(columns={k:'stress_'+k for k in ['dev_mean','val_mean','dev_top5','val_top5']});q=base.merge(stress,on='policy_id',how='left');rob=q[(q.dev_n>=20)&(q.val_n>=10)&(q.dev_mean>0)&(q.val_mean>0)&(q.dev_top5>0)&(q.val_top5>0)&(q.stress_dev_mean>0)&(q.stress_val_mean>0)].copy();rob['score']=rob[['dev_mean','val_mean','dev_top5','val_top5','stress_dev_mean','stress_val_mean']].min(axis=1);rob=rob.sort_values('score',ascending=False);grid.to_csv(OUT/'GRID.csv',index=False);rob.to_csv(OUT/'ROBUST.csv',index=False)
    selected=None
    if len(rob):selected=rob.iloc[0].to_dict();trades[selected['policy_id']].to_csv(OUT/'SELECTED_TRADES_12BP.csv',index=False)
    summary=dict(dates=dates,rows_1s=len(x),events=len(ev),candidate_rows=len(c),policies=int(grid.policy_id.nunique()) if len(grid) else 0,robust_count=len(rob),status='CANDIDATE' if len(rob) else 'CASH',selected=selected,split=parts,timestamp='exchange time only; research grade',causality='current burst, fixed delay, next-second touch, no future cluster maximum');(OUT/'SUMMARY.json').write_text(json.dumps(summary,indent=2,default=str)+'\n');print(json.dumps({k:summary[k] for k in ['events','policies','robust_count','status']},indent=2))
if __name__=='__main__':main()
