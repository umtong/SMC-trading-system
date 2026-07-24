#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

DAYS = ('2023-01-03','2023-04-20','2023-08-30','2023-12-28')
SYMBOLS = ('BTCUSDT','ETHUSDT')
HORIZONS = (3,10,30,60)
QUANTILES = (0.95,0.975,0.99,0.995,0.999)
COSTS = (12.0,18.0,24.0)
LATENCY_MS = 250
MAX_BBO_DELAY_MS = 2_000
ROOT_URL = 'https://data.binance.vision/data/futures/um/daily'
FEATURES = [
    'is_eth','spread_rel','l1_imb','micro_dev','log_depth','depth_z','spread_z',
    'quote_updates_z','quote_age_ms','trade_age_ms','flow_imb_1','flow_imb_5',
    'flow_imb_30','flow_accel','volume_z','count_z','buy_vwap_dev','sell_vwap_dev',
    'ret_1','ret_5','ret_30','rv_30','depth_change_1','imb_change_1',
    'flow_depth_interaction','flow_price_eff',
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):
            h.update(b)
    return h.hexdigest()


def get(url: str, attempts: int = 6) -> bytes:
    error: Exception | None = None
    for i in range(attempts):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'smc-state-first-l1-clean-v2/1.0'})
            with urllib.request.urlopen(req,timeout=600) as r:
                return r.read()
        except Exception as exc:
            error=exc
            if i+1<attempts:
                time.sleep(min(20,2**i))
    raise RuntimeError(f'{url}: {error!r}')


def ensure_source(data_dir: Path, symbol: str, dtype: str, day: str) -> tuple[Path,dict]:
    data_dir.mkdir(parents=True,exist_ok=True)
    name=f'{symbol}-{dtype}-{day}.zip'
    path=data_dir/name
    check=data_dir/(name+'.CHECKSUM')
    url=f'{ROOT_URL}/{dtype}/{symbol}/{name}'
    if not path.exists():
        path.write_bytes(get(url))
    if not check.exists():
        check.write_bytes(get(url+'.CHECKSUM'))
    text=check.read_text(encoding='utf-8-sig').strip()
    expected=text.split()[0].lower()
    observed=sha256(path)
    if expected!=observed:
        raise ValueError(f'checksum mismatch {name}: {observed} != {expected}')
    return path,{'url':url,'sha256':observed,'bytes':path.stat().st_size}


def norm_ms(values: np.ndarray) -> np.ndarray:
    v=np.asarray(values,dtype=np.int64)
    return np.where(np.abs(v)>=10**15,v//1000,v).astype(np.int64)


def read_book(path: Path) -> pd.DataFrame:
    use=['best_bid_price','best_bid_qty','best_ask_price','best_ask_qty','event_time']
    parts=[]
    for c in pd.read_csv(path,compression='zip',usecols=use,chunksize=1_000_000):
        for col in use:
            c[col]=pd.to_numeric(c[col],errors='raise')
        c['event_time']=norm_ms(c.event_time.to_numpy(np.int64))
        parts.append(c)
    b=pd.concat(parts,ignore_index=True)
    b=b.sort_values('event_time',kind='mergesort').drop_duplicates('event_time',keep='last').reset_index(drop=True)
    if not ((b.best_bid_price>0)&(b.best_ask_price>b.best_bid_price)&(b.best_bid_qty>=0)&(b.best_ask_qty>=0)).all():
        raise ValueError(f'invalid BBO in {path}')
    return b


def read_trades(path: Path) -> pd.DataFrame:
    use=['price','quantity','transact_time','is_buyer_maker']
    parts=[]
    for c in pd.read_csv(path,compression='zip',usecols=use,chunksize=1_000_000):
        p=pd.to_numeric(c.price,errors='raise').to_numpy(float)
        q=pd.to_numeric(c.quantity,errors='raise').to_numpy(float)
        t=norm_ms(pd.to_numeric(c.transact_time,errors='raise').to_numpy(np.int64))
        maker=c.is_buyer_maker.astype(str).str.lower().isin(['true','1']).to_numpy()
        quote=p*q;buy=~maker
        parts.append(pd.DataFrame({
            'sec':t//1000,'quote':quote,'signed':np.where(buy,quote,-quote),
            'buyq':np.where(buy,quote,0.0),'sellq':np.where(buy,0.0,quote),
            'buypxq':np.where(buy,p*quote,0.0),'sellpxq':np.where(buy,0.0,p*quote),
            'count':1,'last_trade_ms':t,
        }))
    x=pd.concat(parts,ignore_index=True)
    return x.groupby('sec',sort=True).agg({
        'quote':'sum','signed':'sum','buyq':'sum','sellq':'sum','buypxq':'sum',
        'sellpxq':'sum','count':'sum','last_trade_ms':'max',
    }).reset_index()


def trailing_z(x: pd.Series, window: int=600, min_periods: int=300) -> pd.Series:
    r=x.rolling(window,min_periods=min_periods)
    return (x-r.mean().shift(1))/r.std(ddof=0).shift(1).replace(0,np.nan)


def first_after(times: np.ndarray, targets: np.ndarray, max_delay_ms: int) -> np.ndarray:
    pos=np.searchsorted(times,targets,side='left')
    out=np.full(len(targets),-1,dtype=np.int64)
    ok=pos<len(times)
    oi=np.flatnonzero(ok)
    good=times[pos[ok]]-targets[ok] <= max_delay_ms
    out[oi[good]]=pos[ok][good]
    return out


def build_day(symbol: str, day: str, data_dir: Path) -> tuple[pd.DataFrame,list[dict]]:
    book_path,bmeta=ensure_source(data_dir,symbol,'bookTicker',day)
    trade_path,tmeta=ensure_source(data_dir,symbol,'aggTrades',day)
    b=read_book(book_path)
    t=read_trades(trade_path)

    b['sec']=b.event_time.to_numpy(np.int64)//1000
    last=b.groupby('sec',sort=True).tail(1).copy()
    counts=b.groupby('sec',sort=True).size().rename('quote_updates').reset_index()
    x=last[['sec','best_bid_price','best_bid_qty','best_ask_price','best_ask_qty','event_time']].merge(counts,on='sec').merge(t,on='sec',how='left').sort_values('sec').reset_index(drop=True)
    for c in ('quote','signed','buyq','sellq','buypxq','sellpxq','count'):
        x[c]=x[c].fillna(0.0)
    x['last_trade_ms']=x.last_trade_ms.fillna(-1).astype(np.int64)

    bid=x.best_bid_price;ask=x.best_ask_price;bq=x.best_bid_qty;aq=x.best_ask_qty
    mid=(bid+ask)/2.0;depth=bq+aq
    x['symbol']=symbol;x['day']=day;x['is_eth']=float(symbol.startswith('ETH'))
    x['known_time_ms']=(x.sec.to_numpy(np.int64)+1)*1000
    x['spread_rel']=(ask-bid)/mid
    x['l1_imb']=(bq-aq)/depth.replace(0,np.nan)
    x['micro_dev']=((ask*bq+bid*aq)/depth.replace(0,np.nan)-mid)/mid
    x['log_depth']=np.log1p(depth)
    x['depth_z']=trailing_z(x.log_depth)
    x['spread_z']=trailing_z(np.log(x.spread_rel.replace(0,np.nan)))
    x['quote_updates_z']=trailing_z(np.log1p(x.quote_updates))
    x['quote_age_ms']=(x.known_time_ms-x.event_time).clip(lower=0,upper=10_000)
    x['trade_age_ms']=(x.known_time_ms-x.last_trade_ms).where(x.last_trade_ms>=0,10_000).clip(lower=0,upper=10_000)
    x['flow_imb_1']=x.signed/x.quote.replace(0,np.nan)
    for w in (5,30):
        x[f'flow_imb_{w}']=x.signed.rolling(w,min_periods=max(2,w//2)).sum()/x.quote.rolling(w,min_periods=max(2,w//2)).sum().replace(0,np.nan)
    x['flow_accel']=x.flow_imb_1-x.flow_imb_5
    x['volume_z']=trailing_z(np.log1p(x.quote))
    x['count_z']=trailing_z(np.log1p(x['count']))
    buyv=x.buypxq/x.buyq.replace(0,np.nan);sellv=x.sellpxq/x.sellq.replace(0,np.nan)
    x['buy_vwap_dev']=(buyv-mid)/mid;x['sell_vwap_dev']=(sellv-mid)/mid
    x['ret_1']=np.log(mid/mid.shift(1));x['ret_5']=np.log(mid/mid.shift(5));x['ret_30']=np.log(mid/mid.shift(30))
    x['rv_30']=x.ret_1.rolling(30,min_periods=15).std(ddof=0).shift(1)
    x['depth_change_1']=np.log(depth/depth.shift(1));x['imb_change_1']=x.l1_imb-x.l1_imb.shift(1)
    x['flow_depth_interaction']=x.flow_imb_5*x.l1_imb
    rolling_abs_flow=x.signed.abs().rolling(5,min_periods=2).sum()/x.quote.rolling(5,min_periods=2).sum().replace(0,np.nan)
    x['flow_price_eff']=x.ret_5.abs()/rolling_abs_flow.replace(0,np.nan)

    bt=b.event_time.to_numpy(np.int64)
    bbid=b.best_bid_price.to_numpy(float);bask=b.best_ask_price.to_numpy(float)
    targets=x.known_time_ms.to_numpy(np.int64)+LATENCY_MS
    ep=first_after(bt,targets,MAX_BBO_DELAY_MS)
    valid=ep>=0
    entry_time=np.full(len(x),-1,np.int64);entry_bid=np.full(len(x),np.nan);entry_ask=np.full(len(x),np.nan)
    entry_time[valid]=bt[ep[valid]];entry_bid[valid]=bbid[ep[valid]];entry_ask[valid]=bask[ep[valid]]
    x['entry_time_ms']=entry_time;x['entry_bid']=entry_bid;x['entry_ask']=entry_ask
    entry_mid=(entry_bid+entry_ask)/2.0
    for h in HORIZONS:
        xp=first_after(bt,entry_time+h*1000,MAX_BBO_DELAY_MS)
        ok=valid&(xp>=0)
        exit_time=np.full(len(x),-1,np.int64);exit_bid=np.full(len(x),np.nan);exit_ask=np.full(len(x),np.nan)
        exit_time[ok]=bt[xp[ok]];exit_bid[ok]=bbid[xp[ok]];exit_ask[ok]=bask[xp[ok]]
        x[f'exit_time_ms_{h}']=exit_time
        x[f'long_gross_log_{h}']=np.log(exit_bid/entry_ask)
        x[f'short_gross_log_{h}']=np.log(entry_bid/exit_ask)
        x[f'mid_log_{h}']=np.log(((exit_bid+exit_ask)/2.0)/entry_mid)
        x.loc[~ok,[f'long_gross_log_{h}',f'short_gross_log_{h}',f'mid_log_{h}']]=np.nan

    keep=['symbol','day','known_time_ms','entry_time_ms','entry_bid','entry_ask']+FEATURES
    for h in HORIZONS:
        keep += [f'exit_time_ms_{h}',f'long_gross_log_{h}',f'short_gross_log_{h}',f'mid_log_{h}']
    panel=x[keep].replace([np.inf,-np.inf],np.nan)
    panel=panel.dropna(subset=['entry_time_ms']).copy()
    sources=[{'day':day,'type':'bookTicker',**bmeta},{'day':day,'type':'aggTrades',**tmeta}]
    return panel,sources


def panel_command(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True,exist_ok=True)
    frames=[];sources=[]
    for day in args.dates:
        print('build',args.symbol,day,flush=True)
        p,s=build_day(args.symbol,day,args.data_dir)
        frames.append(p);sources.extend(s)
    out=pd.concat(frames,ignore_index=True).sort_values(['entry_time_ms','symbol'],kind='mergesort')
    panel_path=args.output_dir/f'{args.symbol}_state_first_l1_v2.csv.gz'
    out.to_csv(panel_path,index=False,compression={'method':'gzip','compresslevel':6,'mtime':0})
    manifest={
        'contract':'STATE_FIRST_L1_TRADE_FLOW_V2','symbol':args.symbol,'dates':list(args.dates),
        'rows':int(len(out)),'features':FEATURES,'latency_ms':LATENCY_MS,'max_bbo_delay_ms':MAX_BBO_DELAY_MS,
        'sources':sources,'panel_sha256':sha256(panel_path),'orders_submitted':False,'credentials_used':False,
    }
    (args.output_dir/f'{args.symbol}_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(manifest,indent=2),flush=True)
    return 0


def rule_predictions(d: pd.DataFrame) -> dict[str,np.ndarray]:
    flow=d.flow_imb_5.fillna(0).to_numpy(float);imb=d.l1_imb.fillna(0).to_numpy(float);micro=d.micro_dev.fillna(0).to_numpy(float);eff=d.flow_price_eff.replace([np.inf,-np.inf],np.nan).fillna(d.flow_price_eff.median()).to_numpy(float)
    return {
        'flow_depth_continuation':np.sign(flow)*(np.abs(flow)+np.abs(imb))*(np.sign(flow)==np.sign(imb)),
        'flow_depth_absorption_reversal':-np.sign(flow)*(np.abs(flow)+np.abs(imb)+1/(1+np.maximum(eff,0)))*(np.sign(flow)!=np.sign(imb)),
        'microprice_continuation':np.sign(micro)*(np.abs(micro)*1e4+np.abs(imb)),
    }


def route(d: pd.DataFrame,pred: np.ndarray,h: int,threshold: float,day: str,cost_bps: float) -> pd.DataFrame:
    longv=pd.to_numeric(d[f'long_gross_log_{h}'],errors='coerce').to_numpy(float);shortv=pd.to_numeric(d[f'short_gross_log_{h}'],errors='coerce').to_numpy(float)
    use=(d.day.to_numpy(str)==day)&np.isfinite(pred)&(pred!=0)&(np.abs(pred)>=threshold)&np.isfinite(longv)&np.isfinite(shortv)
    idx=np.flatnonzero(use)
    if not len(idx):return pd.DataFrame(columns=['entry_time_ms','exit_time_ms','symbol','side','gross_log','net_log'])
    q=d.iloc[idx][['entry_time_ms',f'exit_time_ms_{h}','symbol']].copy();q['row_idx']=idx;q['score']=pred[idx]
    q=q.sort_values(['entry_time_ms','score','symbol'],ascending=[True,False,True],key=lambda s:-s.abs() if s.name=='score' else s,kind='mergesort')
    rows=[];free=-1
    for t,g in q.groupby('entry_time_ms',sort=True):
        t=int(t)
        if t<free:continue
        r=g.iloc[int(np.argmax(np.abs(g.score.to_numpy(float))))];j=int(r.row_idx);side=1 if pred[j]>0 else -1;gross=float(longv[j] if side>0 else shortv[j]);exit_time=int(r[f'exit_time_ms_{h}'])
        if exit_time<0:continue
        rows.append({'entry_time_ms':t,'exit_time_ms':exit_time,'symbol':str(r.symbol),'side':side,'score':float(pred[j]),'gross_log':gross,'net_log':gross-cost_bps/10_000.0})
        free=exit_time
    return pd.DataFrame(rows)


def metrics(t: pd.DataFrame) -> dict:
    if t.empty:return {'trades':0,'log_growth':0.0,'g_daily':0.0,'pf':0.0,'top5_share':1.0,'mdd':0.0,'win_rate':0.0}
    log=t.net_log.to_numpy(float);simple=np.expm1(log);curve=np.exp(np.r_[0.0,np.cumsum(log)]);dd=1-curve/np.maximum.accumulate(curve);pos=simple[simple>0];neg=-simple[simple<0];top=float(np.sort(pos)[-5:].sum()/pos.sum()) if pos.sum()>0 else 1.0
    return {'trades':int(len(log)),'log_growth':float(log.sum()),'g_daily':float(math.exp(log.sum())-1),'pf':float(pos.sum()/neg.sum()) if neg.sum()>0 else (999.0 if pos.sum()>0 else 0.0),'top5_share':top,'mdd':float(dd.max()),'win_rate':float((simple>0).mean())}


def evaluate_command(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True,exist_ok=True)
    files=sorted(args.input_dir.rglob('*_state_first_l1_v2.csv.gz'))
    if len(files)!=2:raise ValueError(f'expected two symbol panels, got {files}')
    d=pd.concat([pd.read_csv(p) for p in files],ignore_index=True).sort_values(['entry_time_ms','symbol'],kind='mergesort').reset_index(drop=True)
    X=d[FEATURES].replace([np.inf,-np.inf],np.nan)
    train=d.day.eq(DAYS[0])
    models={
        'ridge':lambda:make_pipeline(SimpleImputer(strategy='median'),StandardScaler(),Ridge(alpha=100.0)),
        'hist':lambda:make_pipeline(SimpleImputer(strategy='median'),HistGradientBoostingRegressor(max_iter=160,max_leaf_nodes=15,l2_regularization=30.0,learning_rate=0.04,random_state=2407)),
        'extra':lambda:make_pipeline(SimpleImputer(strategy='median'),ExtraTreesRegressor(n_estimators=240,min_samples_leaf=80,max_features=0.7,n_jobs=-1,random_state=2407)),
    }
    predictions:dict[tuple[str,int],np.ndarray]={}
    for h in HORIZONS:
        y=pd.to_numeric(d[f'mid_log_{h}'],errors='coerce')
        ok=train&y.notna()
        for name,maker in models.items():
            m=maker();m.fit(X.loc[ok],y.loc[ok]);predictions[(name,h)]=m.predict(X).astype(float)
    for name,p in rule_predictions(d).items():
        for h in HORIZONS:predictions[(name,h)]=p
    rows=[]
    sel=d.day.eq(DAYS[1]).to_numpy()
    for (family,h),pred in predictions.items():
        values=np.abs(pred[sel]);values=values[np.isfinite(values)&(values>0)]
        for q in QUANTILES:
            if len(values)<100:continue
            threshold=float(np.quantile(values,q));rec={'family':family,'horizon_s':h,'quantile':q,'threshold':threshold,'candidate_id':f'{family}|h{h}|q{q}'}
            for day_tag,day in (('selection',DAYS[1]),('validation',DAYS[2])):
                for cost in COSTS:
                    mm=metrics(route(d,pred,h,threshold,day,cost))
                    for k,v in mm.items():rec[f'{day_tag}_{int(cost)}_{k}']=v
            rows.append(rec)
    screen=pd.DataFrame(rows)
    def gate(r:pd.Series)->bool:
        for tag in ('selection','validation'):
            if r[f'{tag}_12_trades']<50 or r[f'{tag}_18_trades']<50:return False
            if r[f'{tag}_12_log_growth']<=0 or r[f'{tag}_18_log_growth']<=0:return False
            if r[f'{tag}_12_pf']<1.10 or r[f'{tag}_12_top5_share']>0.35 or r[f'{tag}_12_mdd']>0.15:return False
        return True
    screen['eligible_pretest']=screen.apply(gate,axis=1) if len(screen) else False
    screen['robust_score']=np.where(screen.eligible_pretest,screen[['selection_18_log_growth','validation_18_log_growth']].min(axis=1),-1e9) if len(screen) else []
    if len(screen):screen=screen.sort_values(['robust_score','validation_18_log_growth','candidate_id'],ascending=[False,False,True],kind='mergesort')
    opened=False;test=None
    if len(screen) and bool(screen.iloc[0].eligible_pretest):
        b=screen.iloc[0];pred=predictions[(str(b.family),int(b.horizon_s))];opened=True;test={}
        for cost in COSTS:
            led=route(d,pred,int(b.horizon_s),float(b.threshold),DAYS[3],cost);test[str(int(cost))]=metrics(led)
            if cost==12:led.to_csv(args.output_dir/'test_ledger.csv',index=False)
    screen.to_csv(args.output_dir/'screen.csv',index=False)
    summary={'status':'COMPLETE','contract':'STATE_FIRST_L1_TRADE_FLOW_V2','dates':DAYS,'screened':int(len(screen)),'eligible_pretest':int(screen.eligible_pretest.sum()) if len(screen) else 0,'test_opened':opened,'test':test,'strict_target_gate_passed':bool(opened and test and test['12']['g_daily']>=.01 and test['18']['log_growth']>0 and test['12']['trades']>=50 and test['12']['top5_share']<=.35),'promotion_allowed':False,'orders_submitted':False,'paper_or_live_started':False,'best':screen.head(30).replace([np.nan,np.inf,-np.inf],None).to_dict('records') if len(screen) else []}
    (args.output_dir/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8');print(json.dumps({k:summary[k] for k in ('screened','eligible_pretest','test_opened','test','strict_target_gate_passed')},indent=2),flush=True);return 0


def main() -> int:
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest='command',required=True)
    p=sub.add_parser('panel');p.add_argument('--symbol',choices=SYMBOLS,required=True);p.add_argument('--dates',nargs='+',default=list(DAYS));p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True)
    e=sub.add_parser('evaluate');e.add_argument('--input-dir',type=Path,required=True);e.add_argument('--output-dir',type=Path,required=True)
    args=ap.parse_args();return panel_command(args) if args.command=='panel' else evaluate_command(args)

if __name__=='__main__':raise SystemExit(main())
