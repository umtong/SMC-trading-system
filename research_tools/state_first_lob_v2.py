#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,gzip,hashlib,io,json,shutil,time,urllib.request,zipfile
from pathlib import Path
import numpy as np,pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from sklearn.ensemble import HistGradientBoostingRegressor,ExtraTreesRegressor
ROOT='https://data.binance.vision/data/futures/um/daily'
DAYS=('2023-05-16','2023-06-10','2023-08-18','2023-11-09')
H=(60,300,900); COST=(12.,18.,24.); QS=(.99,.995,.999)
F=['spread_rel','l1_imb','micro_dev','quote_age_ms','quote_updates','log_depth','depth_z','spread_z','flow_imb_1','flow_imb_5','flow_imb_30','flow_accel','volume_z','count_z','buy_vwap_dev','sell_vwap_dev','ret_1','ret_5','ret_30','rv_30','flow_price_eff','flow_depth_interaction']
def sha(p):
 h=hashlib.sha256();
 with Path(p).open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()
def get(url,p,attempts=6):
 e=None
 for k in range(attempts):
  try:
   q=urllib.request.Request(url,headers={'User-Agent':'smc-state-first-lob-v2/1.0'})
   with urllib.request.urlopen(q,timeout=600) as r,Path(p).open('wb') as o:shutil.copyfileobj(r,o,1<<20)
   return
  except Exception as x:
   e=x;Path(p).unlink(missing_ok=True)
   if k+1<attempts:time.sleep(min(20,2**k))
 raise RuntimeError(f'{url}: {e!r}')
def verified(sym,typ,day,cache):
 n=f'{sym}-{typ}-{day}.zip';u=f'{ROOT}/{typ}/{sym}/{n}';p=cache/n;c=cache/(n+'.CHECKSUM');cache.mkdir(parents=True,exist_ok=True)
 if not p.exists():get(u,p)
 if not c.exists():get(u+'.CHECKSUM',c)
 exp=c.read_text(encoding='utf-8-sig').split()[0].lower();act=sha(p)
 if exp!=act:raise ValueError(f'checksum {n}: {act} != {exp}')
 return p,{'url':u,'sha256':act,'bytes':p.stat().st_size}
def prior_z(s,w=600,minp=300):
 r=s.rolling(w,min_periods=minp);return (s-r.mean().shift(1))/r.std(ddof=0).shift(1).replace(0,np.nan)
def book(path):
 z=[]
 with zipfile.ZipFile(path) as a:
  n=[x for x in a.namelist() if x.endswith('.csv')][0]
  for c in pd.read_csv(a.open(n),usecols=['best_bid_price','best_bid_qty','best_ask_price','best_ask_qty','event_time'],chunksize=1_000_000):
   for q in ['best_bid_price','best_bid_qty','best_ask_price','best_ask_qty','event_time']:c[q]=pd.to_numeric(c[q],errors='raise')
   t=c.event_time.to_numpy(np.int64);t=np.where(t>=10**15,t//1000,t);c['event_time']=t;c['sec']=t//1000;g=c.groupby('sec',sort=False);last=g.tail(1).copy();last['quote_updates']=g.size().reindex(last.sec).to_numpy();z.append(last)
 b=pd.concat(z,ignore_index=True).sort_values(['sec','event_time'],kind='mergesort').groupby('sec',sort=True).tail(1).sort_values('sec').reset_index(drop=True);return b
def trades(path):
 z=[]
 with zipfile.ZipFile(path) as a:
  n=[x for x in a.namelist() if x.endswith('.csv')][0]
  for c in pd.read_csv(a.open(n),usecols=['price','quantity','transact_time','is_buyer_maker'],chunksize=1_000_000):
   p=pd.to_numeric(c.price,errors='raise').to_numpy(float);q=pd.to_numeric(c.quantity,errors='raise').to_numpy(float);t=pd.to_numeric(c.transact_time,errors='raise').to_numpy(np.int64);t=np.where(t>=10**15,t//1000,t);v=p*q;mk=c.is_buyer_maker.astype(str).str.lower().isin(['true','1']).to_numpy();buy=~mk
   x=pd.DataFrame({'sec':t//1000,'quote':v,'signed':np.where(buy,v,-v),'buyq':np.where(buy,v,0.),'sellq':np.where(buy,0.,v),'buypxq':np.where(buy,p*v,0.),'sellpxq':np.where(buy,0.,p*v),'count':1,'last_trade_ms':t});z.append(x.groupby('sec',sort=False).agg({'quote':'sum','signed':'sum','buyq':'sum','sellq':'sum','buypxq':'sum','sellpxq':'sum','count':'sum','last_trade_ms':'max'}))
 return pd.concat(z).groupby(level=0).sum().sort_index()
def one(sym,day,bp,tp):
 b=book(bp);t=trades(tp);lo=max(int(b.sec.min()),int(t.index.min()));hi=min(int(b.sec.max()),int(t.index.max()));x=pd.DataFrame(index=np.arange(lo,hi+1,dtype=np.int64)).join(b.set_index('sec')[['best_bid_price','best_bid_qty','best_ask_price','best_ask_qty','event_time','quote_updates']]).join(t)
 for c in ['best_bid_price','best_bid_qty','best_ask_price','best_ask_qty','event_time']:x[c]=x[c].ffill()
 x.quote_updates=x.quote_updates.fillna(0)
 for c in ['quote','signed','buyq','sellq','buypxq','sellpxq','count','last_trade_ms']:x[c]=x[c].fillna(0)
 x=x.dropna(subset=['best_bid_price','best_ask_price']).copy();bid=x.best_bid_price;ask=x.best_ask_price;bq=x.best_bid_qty;aq=x.best_ask_qty;mid=(bid+ask)/2;dep=bq+aq;x['spread_rel']=(ask-bid)/mid;x['l1_imb']=(bq-aq)/dep.replace(0,np.nan);x['micro_dev']=((ask*bq+bid*aq)/dep.replace(0,np.nan)-mid)/mid;x['quote_age_ms']=(x.index.to_numpy(np.int64)+1)*1000-x.event_time.to_numpy(np.int64);x['log_depth']=np.log1p(dep);x['depth_z']=prior_z(x.log_depth);x['spread_z']=prior_z(np.log(x.spread_rel.where(x.spread_rel>0)));x['flow_imb_1']=x.signed/x.quote.replace(0,np.nan);x['flow_imb_5']=x.signed.rolling(5,min_periods=2).sum()/x.quote.rolling(5,min_periods=2).sum().replace(0,np.nan);x['flow_imb_30']=x.signed.rolling(30,min_periods=15).sum()/x.quote.rolling(30,min_periods=15).sum().replace(0,np.nan);x['flow_accel']=x.flow_imb_5-x.flow_imb_30;x['volume_z']=prior_z(np.log1p(x.quote));x['count_z']=prior_z(np.log1p(x['count']));bv=x.buypxq/x.buyq.replace(0,np.nan);sv=x.sellpxq/x.sellq.replace(0,np.nan);x['buy_vwap_dev']=(bv-mid)/mid;x['sell_vwap_dev']=(sv-mid)/mid;x['ret_1']=np.log(mid/mid.shift(1));x['ret_5']=np.log(mid/mid.shift(5));x['ret_30']=np.log(mid/mid.shift(30));x['rv_30']=x.ret_1.rolling(30,min_periods=15).std(ddof=0).shift(1);x['flow_price_eff']=x.ret_5/(x.signed.rolling(5,min_periods=2).sum().abs()/x.quote.rolling(5,min_periods=2).sum().replace(0,np.nan)+1e-6);x['flow_depth_interaction']=x.flow_imb_5*x.depth_z
 bt=b.event_time.to_numpy(np.int64);bbid=b.best_bid_price.to_numpy(float);bask=b.best_ask_price.to_numpy(float);dec=(x.index.to_numpy(np.int64)+1)*1000;ei=np.searchsorted(bt,dec+250,side='left');ok=ei<len(bt);x=x.iloc[np.flatnonzero(ok)].copy();ei=ei[ok];x['entry_time_ms']=bt[ei];x['entry_bid']=bbid[ei];x['entry_ask']=bask[ei]
 for h in H:
  j=np.searchsorted(bt,x.entry_time_ms.to_numpy(np.int64)+h*1000,side='left');v=j<len(bt);eb=np.full(len(x),np.nan);ea=np.full(len(x),np.nan);eb[v]=bbid[j[v]];ea[v]=bask[j[v]];x[f'long_{h}']=np.log(eb/x.entry_ask);x[f'short_{h}']=np.log(x.entry_bid/ea);x[f'mid_{h}']=np.log((eb+ea)/(x.entry_bid+x.entry_ask))
 event=(x.index.to_numpy()%10==0)|(x.flow_imb_5.abs()>.5)|(x.l1_imb.abs()>.5)|(x.volume_z>1.5)|(x.spread_z>1.5)|(x.depth_z<-1.5)|(x.ret_5.abs()>1.5*x.rv_30);x=x[event].copy();x.insert(0,'sec',x.index.to_numpy());x['symbol']=sym;x['day']=day;return x[['sec','symbol','day','entry_time_ms','entry_bid','entry_ask']+F+[f'{a}_{h}' for h in H for a in ('long','short','mid')]]
def panel(sym,cache,out):
 out.mkdir(parents=True,exist_ok=True);rec=[]
 for d in DAYS:
  bp,bm=verified(sym,'bookTicker',d,cache/sym);tp,tm=verified(sym,'aggTrades',d,cache/sym);x=one(sym,d,bp,tp);p=out/f'{sym}_{d}.csv.gz';x.to_csv(p,index=False,compression={'method':'gzip','compresslevel':6,'mtime':0});rec.append({'day':d,'rows':len(x),'output_sha256':sha(p),'book':bm,'trades':tm});print(sym,d,len(x),flush=True)
 (out/f'{sym}_manifest.json').write_text(json.dumps({'version':'STATE_FIRST_LOB_V2','symbol':sym,'records':rec,'features':F,'entry':'first BBO after completed second +250ms','orders_submitted':False},indent=2))
def route(d,p,h,th,c):
 use=np.isfinite(p)&(np.abs(p)>=th);q=d.loc[use,['entry_time_ms','symbol',f'long_{h}',f'short_{h}']].copy();q['pred']=p[use];q=q.sort_values(['entry_time_ms','pred','symbol'],ascending=[True,False,True],key=lambda s:-s.abs() if s.name=='pred' else s,kind='mergesort');z=[];free=-1
 for t,g in q.groupby('entry_time_ms',sort=True):
  if t<free:continue
  r=g.iloc[g.pred.abs().argmax()];side=1 if r.pred>0 else -1;v=r[f'long_{h}'] if side>0 else r[f'short_{h}'];z.append((t,r.symbol,side,float(v)-c/1e4));free=t+h*1000
 return pd.DataFrame(z,columns=['time_ms','symbol','side','ret'])
def met(z):
 if z.empty:return {'n':0,'log':0.,'pf':0.,'top5':1.,'mean_bps':None}
 r=np.clip(z.ret.to_numpy(float),-.999,None);pos=r[r>0].sum();neg=-r[r<0].sum();pr=np.maximum(r,0);return {'n':len(r),'log':float(np.log1p(r).sum()),'pf':float(pos/neg) if neg else 999.,'top5':float(np.sort(pr)[-5:].sum()/pr.sum()) if pr.sum() else 1.,'mean_bps':float(r.mean()*1e4)}
def evaluate(inp,out):
 out.mkdir(parents=True,exist_ok=True);fs=sorted(inp.rglob('*.csv.gz'));d=pd.concat([pd.read_csv(p) for p in fs],ignore_index=True).sort_values(['entry_time_ms','symbol'],kind='mergesort').reset_index(drop=True);X=d[F].replace([np.inf,-np.inf],np.nan).astype(np.float32);train=d.day==DAYS[0];sel=d.day==DAYS[1];val=d.day==DAYS[2];test=d.day==DAYS[3];models={'ridge':make_pipeline(SimpleImputer(strategy='median'),StandardScaler(),Ridge(alpha=100.)),'hist':make_pipeline(SimpleImputer(strategy='median'),HistGradientBoostingRegressor(max_iter=180,max_leaf_nodes=15,learning_rate=.04,l2_regularization=20,random_state=240)),'extra':make_pipeline(SimpleImputer(strategy='median'),ExtraTreesRegressor(n_estimators=300,max_depth=12,min_samples_leaf=80,max_features=.7,n_jobs=-1,random_state=240))};pred={};rows=[]
 for h in H:
  y=d[f'mid_{h}'].to_numpy(float);ok=train&np.isfinite(y)
  for n,m in models.items():m.fit(X.loc[ok],y[ok]);p=m.predict(X);pred[(n,h)]=p
  for n in models:
   p=pred[(n,h)];ab=np.abs(p[sel])
   for q in QS:
    th=float(np.quantile(ab,q));r={'model':n,'h':h,'q':q,'threshold':th}
    for c in COST:
     for tag,mask in [('selection',sel),('validation',val)]:
      mm=met(route(d[mask].reset_index(drop=True),p[mask],h,th,c));r.update({f'{tag}_{int(c)}_{k}':v for k,v in mm.items()})
    rows.append(r)
 s=pd.DataFrame(rows);s['eligible']=(s.selection_18_n>=10)&(s.validation_18_n>=10)&(s.selection_18_log>0)&(s.validation_18_log>0)&(s.selection_18_pf>=1.1)&(s.validation_18_pf>=1.1)&(s.selection_18_top5<=.7)&(s.validation_18_top5<=.7);s['score']=np.where(s.eligible,np.minimum(s.selection_18_log,s.validation_18_log),-1e9);s=s.sort_values('score',ascending=False);s.to_csv(out/'screen.csv',index=False);opened=False;tm={};led=pd.DataFrame()
 if len(s) and bool(s.iloc[0].eligible):
  b=s.iloc[0];p=pred[(b.model,int(b.h))];led=route(d[test].reset_index(drop=True),p[test],int(b.h),float(b.threshold),18.);led.to_csv(out/'test_ledger.csv',index=False);tm=met(led);opened=True
 (out/'summary.json').write_text(json.dumps({'status':'COMPLETE','rows':len(d),'screened':len(s),'eligible':int(s.eligible.sum()),'test_opened':opened,'test':tm,'best':s.head(20).replace([np.nan,np.inf,-np.inf],None).to_dict('records'),'orders_submitted':False},indent=2));print((out/'summary.json').read_text())
def main():
 a=argparse.ArgumentParser();sub=a.add_subparsers(dest='cmd',required=True);p=sub.add_parser('panel');p.add_argument('--symbol',required=True);p.add_argument('--cache',type=Path,required=True);p.add_argument('--out',type=Path,required=True);e=sub.add_parser('evaluate');e.add_argument('--input',type=Path,required=True);e.add_argument('--out',type=Path,required=True);z=a.parse_args();panel(z.symbol,z.cache,z.out) if z.cmd=='panel' else evaluate(z.input,z.out)
if __name__=='__main__':main()
