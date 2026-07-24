#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, math, time, urllib.request
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd

ROOT_URL='https://data.binance.vision/data/futures/um/daily'
SYMBOLS=('BTCUSDT','ETHUSDT')
TRAIN_DAY='2023-06-27';SELECTION_DAY='2023-08-30';VALIDATION_DAY='2023-10-25';TEST_DAY='2023-12-28'
FAMILIES=('ABSORPTION','PULLBACK_CONTINUATION','FLOW_FLIP','MICROPRICE_CONTINUATION')
QUANTILES=(0.99,0.995,0.999);TTLS=(3,5,10);HORIZONS=(10,30,60);COSTS=(9.0,13.0,17.0);QUEUE_MULTS=(2.0,3.0)
LATENCY_MS=100;MAX_QUOTE_AGE_MS=500;CAPACITY_FRACTION=0.10;ORDER_NOTIONAL=1000.0

def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()

def get(url,attempts=6):
 err=None
 for i in range(attempts):
  try:
   req=urllib.request.Request(url,headers={'User-Agent':'smc-queue-aware-passive-l1-v1/1.0'})
   with urllib.request.urlopen(req,timeout=600) as r:return r.read()
  except Exception as exc:
   err=exc
   if i+1<attempts:time.sleep(min(30,2**i))
 raise RuntimeError(f'{url}: {err!r}')

def source(cache,symbol,dtype,day):
 cache.mkdir(parents=True,exist_ok=True);name=f'{symbol}-{dtype}-{day}.zip';url=f'{ROOT_URL}/{dtype}/{symbol}/{name}'
 p=cache/name;c=cache/(name+'.CHECKSUM')
 if not p.exists():p.write_bytes(get(url))
 if not c.exists():c.write_bytes(get(url+'.CHECKSUM'))
 expected=c.read_text(encoding='utf-8-sig').strip().split()[0].lower();actual=sha(p)
 if actual!=expected:raise ValueError(f'checksum mismatch {name}: {actual} != {expected}')
 return p,{'url':url,'sha256':actual,'bytes':p.stat().st_size}

def norm_ms(v):
 x=np.asarray(v,dtype=np.int64);return np.where(np.abs(x)>=10**15,x//1000,x).astype(np.int64)

def read_book(path):
 cols=['best_bid_price','best_bid_qty','best_ask_price','best_ask_qty','event_time'];chunks=[]
 for c in pd.read_csv(path,compression='zip',usecols=cols,chunksize=1_000_000):
  for z in cols:c[z]=pd.to_numeric(c[z],errors='raise')
  c['event_time']=norm_ms(c.event_time.to_numpy(np.int64));chunks.append(c)
 b=pd.concat(chunks,ignore_index=True).sort_values('event_time',kind='mergesort').drop_duplicates('event_time',keep='last').reset_index(drop=True)
 if not ((b.best_bid_price>0)&(b.best_ask_price>b.best_bid_price)&(b.best_bid_qty>=0)&(b.best_ask_qty>=0)).all():raise ValueError('invalid BBO')
 return b.event_time.to_numpy(np.int64),b.best_bid_price.to_numpy(float),b.best_bid_qty.to_numpy(float),b.best_ask_price.to_numpy(float),b.best_ask_qty.to_numpy(float)

def read_trades(path):
 cols=['price','quantity','transact_time','is_buyer_maker'];parts=[]
 for c in pd.read_csv(path,compression='zip',usecols=cols,chunksize=1_000_000):
  p=pd.to_numeric(c.price,errors='raise').to_numpy(float);q=pd.to_numeric(c.quantity,errors='raise').to_numpy(float);t=norm_ms(pd.to_numeric(c.transact_time,errors='raise').to_numpy(np.int64))
  maker=c.is_buyer_maker.astype(str).str.lower().isin(['true','1']).to_numpy();parts.append(pd.DataFrame({'time_ms':t,'price':p,'qty':q,'buyer_maker':maker}))
 x=pd.concat(parts,ignore_index=True).sort_values('time_ms',kind='mergesort').reset_index(drop=True)
 return x.time_ms.to_numpy(np.int64),x.price.to_numpy(float),x.qty.to_numpy(float),x.buyer_maker.to_numpy(bool)

def trailing_z(x,w=600,minp=300):
 r=x.rolling(w,min_periods=minp);return (x-r.mean().shift(1))/r.std(ddof=0).shift(1).replace(0,np.nan)

@dataclass
class DayData:
 symbol:str;day:str;sec:np.ndarray;features:pd.DataFrame;bt:np.ndarray;bid:np.ndarray;bq:np.ndarray;ask:np.ndarray;aq:np.ndarray;tt:np.ndarray;tp:np.ndarray;tq:np.ndarray;tm:np.ndarray;sources:list

def build_day(cache,symbol,day):
 bp,bm=source(cache,symbol,'bookTicker',day);trp,trm=source(cache,symbol,'aggTrades',day);bt,bid,bq,ask,aq=read_book(bp);tt,px,qty,maker=read_trades(trp)
 sec0=int(pd.Timestamp(day,tz='UTC').timestamp());sec=np.arange(sec0,sec0+86400,dtype=np.int64);pos=np.searchsorted(bt,(sec+1)*1000,side='left')-1;valid=pos>=0
 bbid=np.full(len(sec),np.nan);bbq=np.full(len(sec),np.nan);bask=np.full(len(sec),np.nan);baq=np.full(len(sec),np.nan);age=np.full(len(sec),np.nan)
 bbid[valid]=bid[pos[valid]];bbq[valid]=bq[pos[valid]];bask[valid]=ask[pos[valid]];baq[valid]=aq[pos[valid]];age[valid]=(sec[valid]+1)*1000-bt[pos[valid]]
 stale=age>2000;bbid[stale]=bbq[stale]=bask[stale]=baq[stale]=np.nan
 tsec=tt//1000;qv=px*qty;sgn=np.where(maker,-qv,qv)
 f=pd.DataFrame({'sec':tsec,'quote':qv,'signed':sgn,'count':1,'buypxq':np.where(~maker,px*qv,0.),'buyq':np.where(~maker,qv,0.),'sellpxq':np.where(maker,px*qv,0.),'sellq':np.where(maker,qv,0.)})
 agg=f.groupby('sec',sort=True).agg({'quote':'sum','signed':'sum','count':'sum','buypxq':'sum','buyq':'sum','sellpxq':'sum','sellq':'sum'})
 x=pd.DataFrame(index=sec).join(agg).fillna({'quote':0.,'signed':0.,'count':0.,'buypxq':0.,'buyq':0.,'sellpxq':0.,'sellq':0.})
 mid=(bbid+bask)/2;depth=bbq+baq;spread=(bask-bbid)/mid;feat=pd.DataFrame(index=sec)
 feat['spread_bps']=spread*1e4;feat['l1_imb']=(bbq-baq)/np.where(depth>0,depth,np.nan);feat['micro_dev']=((bask*bbq+bbid*baq)/np.where(depth>0,depth,np.nan)-mid)/mid;feat['quote_age_ms']=age
 feat['flow_imb_1']=x.signed/x.quote.replace(0,np.nan)
 for w in (5,30):feat[f'flow_imb_{w}']=x.signed.rolling(w,min_periods=max(2,w//2)).sum()/x.quote.rolling(w,min_periods=max(2,w//2)).sum().replace(0,np.nan)
 feat['ret_1']=np.log(mid/pd.Series(mid,index=sec).shift(1));feat['ret_5']=np.log(mid/pd.Series(mid,index=sec).shift(5));feat['ret_30']=np.log(mid/pd.Series(mid,index=sec).shift(30));feat['bid']=bbid;feat['bid_qty']=bbq;feat['ask']=bask;feat['ask_qty']=baq
 return DayData(symbol,day,sec,feat,bt,bid,bq,ask,aq,tt,px,qty,maker,[{'type':'bookTicker',**bm},{'type':'aggTrades',**trm}])

def family_score(f,family):
 flow1=f.flow_imb_1.to_numpy(float);flow5=f.flow_imb_5.to_numpy(float);flow30=f.flow_imb_30.to_numpy(float);imb=f.l1_imb.to_numpy(float);micro=f.micro_dev.to_numpy(float)*1e4;r1=f.ret_1.to_numpy(float)*1e4;r5=f.ret_5.to_numpy(float)*1e4;r30=f.ret_30.to_numpy(float)*1e4;spread=f.spread_bps.to_numpy(float);age=f.quote_age_ms.to_numpy(float)
 if family=='ABSORPTION':
  side=np.sign(imb+.25*micro);valid=(side!=0)&(side*flow5<0)&(side*r5<=0)&(side*imb>0)&(side*micro>=0);score=np.abs(flow5)+np.maximum(side*imb,0)+np.maximum(side*micro,0)+np.maximum(-side*r5,0)/5
 elif family=='PULLBACK_CONTINUATION':
  side=np.sign(flow30);valid=(side!=0)&(side*imb>0)&(side*micro>0)&(side*flow1<0)&(side*r30>0);score=np.abs(flow30)+np.maximum(side*imb,0)+np.maximum(side*micro,0)+np.abs(flow1)
 elif family=='FLOW_FLIP':
  side=np.sign(flow1-flow30);valid=(side!=0)&(side*flow1>0)&(side*flow30<0)&(side*imb>0)&(side*micro>=0);score=np.abs(flow1-flow30)+np.maximum(side*imb,0)+np.maximum(side*micro,0)
 else:
  side=np.sign(micro);valid=(side!=0)&(side*imb>0)&(side*flow5>0);score=np.abs(micro)+np.abs(imb)+np.abs(flow5)
 score=score/(1+np.maximum(spread,0));valid&=np.isfinite(score)&np.isfinite(age)&(age<=MAX_QUOTE_AGE_MS)&(spread>0)&(spread<=5)
 return side.astype(np.int8),np.where(valid,score,np.nan)

def last_quote(dd,target):
 p=int(np.searchsorted(dd.bt,target,side='right')-1)
 return None if p<0 or target-int(dd.bt[p])>MAX_QUOTE_AGE_MS else p

def attempt(dd,row,side,score,ttl,horizon,qmult):
 known=int((dd.sec[row]+1)*1000);arrival=known+LATENCY_MS;p=last_quote(dd,arrival)
 if p is None:return None
 price=float(dd.bid[p] if side>0 else dd.ask[p]);shown=float(dd.bq[p] if side>0 else dd.aq[p])
 if price<=0 or shown<=0:return None
 own=min(ORDER_NOTIONAL/price,shown*.01);need=qmult*shown+own;end=arrival+ttl*1000;i=int(np.searchsorted(dd.tt,arrival,side='left'));j=int(np.searchsorted(dd.tt,end,side='right'));cum=0.;fill=-1
 for k in range(i,j):
  opposing=bool(dd.tm[k]) if side>0 else not bool(dd.tm[k]);through=dd.tp[k]<=price*(1+1e-12) if side>0 else dd.tp[k]>=price*(1-1e-12)
  if opposing and through:
   cum+=float(dd.tq[k])
   if cum>=need:fill=int(dd.tt[k]);break
 if fill<0:return {'signal_time_ms':known,'free_time_ms':end,'filled':False,'symbol':dd.symbol,'side':side,'score':score,'gross_log':math.nan}
 exit_arrival=fill+horizon*1000+LATENCY_MS;xp=last_quote(dd,exit_arrival)
 if xp is None:raise RuntimeError(f'missing fresh exit BBO {dd.symbol} {dd.day}')
 exitp=float(dd.bid[xp] if side>0 else dd.ask[xp]);exitqty=float(dd.bq[xp] if side>0 else dd.aq[xp])
 if exitp<=0 or own>CAPACITY_FRACTION*exitqty:return None
 return {'signal_time_ms':known,'free_time_ms':exit_arrival,'filled':True,'symbol':dd.symbol,'side':side,'score':score,'gross_log':side*math.log(exitp/price),'fill_time_ms':fill,'entry_price':price,'exit_price':exitp}

def symbol_attempts(dd,family,threshold,ttl,horizon,qmult):
 side,score=family_score(dd.features,family);idx=np.flatnonzero(np.isfinite(score)&(score>=threshold));rows=[]
 for r in idx:
  z=attempt(dd,int(r),int(side[r]),float(score[r]),ttl,horizon,qmult)
  if z is not None:rows.append(z)
 return pd.DataFrame(rows)

def route(parts):
 valid=[z for z in parts if not z.empty]
 if not valid:return pd.DataFrame(),pd.DataFrame()
 x=pd.concat(valid,ignore_index=True).sort_values(['signal_time_ms','score','symbol'],ascending=[True,False,True],kind='mergesort');selected=[];fills=[];free=-1
 for t,g in x.groupby('signal_time_ms',sort=True):
  t=int(t)
  if t<free:continue
  r=g.iloc[0];selected.append(r.to_dict());free=int(r.free_time_ms)
  if bool(r.filled):fills.append(r.to_dict())
 return pd.DataFrame(selected),pd.DataFrame(fills)

def metrics(selected,fills,cost):
 attempts=len(selected);n=len(fills)
 if not n:return {'attempts':attempts,'fills':0,'fill_rate':0.,'log_growth':0.,'pf':0.,'top10_share':1.,'after_top10':-1.,'mdd':0.}
 log=fills.gross_log.to_numpy(float)-cost/10000.;simple=np.expm1(log);curve=np.exp(np.r_[0.,np.cumsum(log)]);dd=1-curve/np.maximum.accumulate(curve);pos=simple[simple>0];neg=-simple[simple<0];top=np.sort(pos)[-10:].sum()/pos.sum() if pos.sum()>0 else 1.;order=np.argsort(simple)[::-1];keep=np.ones(n,bool);keep[order[:min(10,n)]]=False
 return {'attempts':attempts,'fills':n,'fill_rate':n/max(attempts,1),'log_growth':float(log.sum()),'pf':float(pos.sum()/neg.sum()) if neg.sum()>0 else (999. if pos.sum()>0 else 0.),'top10_share':float(top),'after_top10':float(log[keep].sum()),'mdd':float(dd.max())}

def load_day(cache,day):return [build_day(cache/s,s,day) for s in SYMBOLS]

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--cache',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();a.out.mkdir(parents=True,exist_ok=True)
 train=load_day(a.cache,TRAIN_DAY);thresholds={}
 for fam in FAMILIES:
  v=np.concatenate([family_score(dd.features,fam)[1][np.isfinite(family_score(dd.features,fam)[1])] for dd in train])
  for q in QUANTILES:thresholds[(fam,q)]=float(np.quantile(v,q))
 periods={SELECTION_DAY:load_day(a.cache,SELECTION_DAY),VALIDATION_DAY:load_day(a.cache,VALIDATION_DAY)};rows=[]
 for fam in FAMILIES:
  for q in QUANTILES:
   th=thresholds[(fam,q)]
   for ttl in TTLS:
    for h in HORIZONS:
     rec={'family':fam,'quantile':q,'threshold':th,'ttl_s':ttl,'horizon_s':h,'candidate_id':f'{fam}|q{q}|ttl{ttl}|h{h}'}
     for day,tag in ((SELECTION_DAY,'selection'),(VALIDATION_DAY,'validation')):
      for qm in QUEUE_MULTS:
       selected,fills=route([symbol_attempts(dd,fam,th,ttl,h,qm) for dd in periods[day]])
       for cost in COSTS:
        for k,v in metrics(selected,fills,cost).items():rec[f'{tag}_q{int(qm)}_{int(cost)}_{k}']=v
     rows.append(rec)
 screen=pd.DataFrame(rows)
 def gate(r):
  for tag in ('selection','validation'):
   if r[f'{tag}_q2_9_attempts']<100 or r[f'{tag}_q2_9_fills']<50:return False
   if r[f'{tag}_q2_9_log_growth']<=0 or r[f'{tag}_q2_13_log_growth']<=0:return False
   if r[f'{tag}_q2_9_pf']<1.10 or r[f'{tag}_q2_9_top10_share']>.40 or r[f'{tag}_q2_9_after_top10']<=0 or r[f'{tag}_q2_9_mdd']>.10:return False
   if r[f'{tag}_q3_13_fills']<30 or r[f'{tag}_q3_13_log_growth']<=0:return False
  return True
 screen['eligible_pretest']=screen.apply(gate,axis=1);screen['robust_score']=np.where(screen.eligible_pretest,screen[['selection_q2_13_log_growth','validation_q2_13_log_growth','selection_q3_13_log_growth','validation_q3_13_log_growth']].min(axis=1),-1e9);screen=screen.sort_values(['robust_score','validation_q2_13_log_growth','candidate_id'],ascending=[False,False,True],kind='mergesort');screen.to_csv(a.out/'pretest_screen.csv',index=False)
 chosen=None
 if len(screen) and bool(screen.iloc[0].eligible_pretest):
  r=screen.iloc[0];chosen={'candidate_id':r.candidate_id,'family':r.family,'quantile':float(r['quantile']),'threshold':float(r.threshold),'ttl_s':int(r.ttl_s),'horizon_s':int(r.horizon_s)}
 summary={'study_id':'queue_aware_passive_l1_v1','screened':len(screen),'eligible_pretest':int(screen.eligible_pretest.sum()),'chosen':chosen,'test_opened':False,'orders_submitted':False,'paper_live_enabled':False};(a.out/'pretest_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
 if chosen:
  dd=load_day(a.cache,TEST_DAY);result={}
  for qm in QUEUE_MULTS:
   selected,fills=route([symbol_attempts(z,chosen['family'],chosen['threshold'],chosen['ttl_s'],chosen['horizon_s'],qm) for z in dd])
   for cost in COSTS:result[f'q{int(qm)}_{int(cost)}']=metrics(selected,fills,cost)
   if qm==2: selected.to_csv(a.out/'test_attempts.csv',index=False);fills.to_csv(a.out/'test_fills.csv',index=False)
  passed=bool(result['q2_9']['fills']>=50 and result['q2_13']['log_growth']>0 and result['q2_9']['after_top10']>0 and result['q2_9']['top10_share']<=.40 and result['q3_13']['log_growth']>0);(a.out/'test_summary.json').write_text(json.dumps({'test_opened':True,'chosen':chosen,'test':result,'test_gate_passed':passed,'production_enabled':False},indent=2),encoding='utf-8')
 print((a.out/'pretest_summary.json').read_text())
if __name__=='__main__':main()
