from __future__ import annotations
import json, math
from pathlib import Path
import numpy as np
import pandas as pd
from datasets import load_dataset
from lightgbm import LGBMRegressor

OUT=Path('research/phase31_l1_state_first/results');OUT.mkdir(parents=True,exist_ok=True)
COSTS=(8.,12.,18.)

def prior_z(s,w,minp=None):
    minp=minp or w//2;p=s.shift(1);return (s-p.rolling(w,min_periods=minp).mean())/p.rolling(w,min_periods=minp).std(ddof=0).replace(0,np.nan)
def trim(x,n):
    x=np.asarray(x,float);return float(np.sort(x)[:-n].mean()) if len(x)>n else math.nan
def one_slot(t,pred,y,h,thr,cost):
    keep=np.flatnonzero(np.isfinite(pred)&(np.abs(pred)>=thr));rows=[];free=pd.Timestamp.min.tz_localize('UTC')
    for i in keep:
        if t.iloc[i]<free:continue
        side=1 if pred[i]>0 else -1;rows.append((t.iloc[i],side*y[i]-cost,pred[i]));free=t.iloc[i]+pd.Timedelta(minutes=h)
    return pd.DataFrame(rows,columns=['time','net_bps','pred'])
def stat(x):
    if x.empty:return {'n':0}
    v=x.net_bps.to_numpy(float);m=x.assign(month=x.time.dt.strftime('%Y-%m')).groupby('month').net_bps.sum()
    return {'n':len(v),'mean':float(v.mean()),'median':float(np.median(v)),'top5':trim(v,5),'top10':trim(v,10),'win':float((v>0).mean()),'positive_month_fraction':float((m>0).mean())}

def main():
    bt=load_dataset('Mindbyte-89/btcusdt_perp_bookticker_features_1m_05_2023_to_03_2024',split='train').to_pandas()
    px=load_dataset('Torch-Trade/btcusdt_perp_1m_05_2021_to_02_2026',split='train').to_pandas()
    bt['timestamp']=pd.to_datetime(bt['timestamp'],utc=True);px['timestamp']=pd.to_datetime(px['timestamp'],utc=True)
    d=px.merge(bt,on='timestamp',how='inner',validate='one_to_one').sort_values('timestamp').reset_index(drop=True)
    q='quote_volume';tb='taker_buy_quote_volume'
    if tb not in d and 'taker_buy_quote' in d:tb='taker_buy_quote'
    flow=(2*d[tb]-d[q])/d[q].replace(0,np.nan)
    logc=np.log(d.close);r=logc.diff();vol=r.rolling(1440,min_periods=720).std(ddof=0).shift(1)
    X=pd.DataFrame(index=d.index)
    for c in ['bt_spread_bps_close','bt_spread_bps_twap','bt_bid_qty_close','bt_ask_qty_close','bt_imbalance_close','bt_imbalance_twap','bt_microprice_premium_close','bt_update_rate']:X[c]=d[c]
    X['imb_mom1']=d.bt_imbalance_close-d.bt_imbalance_close.shift(1);X['imb_mom5']=d.bt_imbalance_close-d.bt_imbalance_close.shift(5)
    X['imb_close_minus_twap']=d.bt_imbalance_close-d.bt_imbalance_twap
    X['spread_z']=prior_z(d.bt_spread_bps_twap,1440);X['update_z']=prior_z(np.log1p(d.bt_update_rate),1440)
    X['micro_persist5']=np.sign(d.bt_microprice_premium_close).rolling(5,min_periods=5).mean();X['micro_persist15']=np.sign(d.bt_microprice_premium_close).rolling(15,min_periods=15).mean()
    for L in (1,5,15,30,60):
        ret=logc-logc.shift(L);rz=ret/(vol*np.sqrt(L));fs=flow.rolling(L,min_periods=L).mean();fz=prior_z(fs,1440);X[f'retz{L}']=rz;X[f'flow{L}']=fs;X[f'flowz{L}']=fz
    hour=d.timestamp.dt.hour;dow=d.timestamp.dt.dayofweek;X['hour_sin']=np.sin(2*np.pi*hour/24);X['hour_cos']=np.cos(2*np.pi*hour/24);X['dow_sin']=np.sin(2*np.pi*dow/7);X['dow_cos']=np.cos(2*np.pi*dow/7)
    horizons=(1,5,15,30,60);labels={h:np.log(d.open.shift(-(1+h))/d.open.shift(-1))*1e4 for h in horizons}
    finite=X.notna().all(axis=1);d=d[finite].reset_index(drop=True);X=X[finite].reset_index(drop=True);labels={h:y[finite].reset_index(drop=True) for h,y in labels.items()}
    audit={'rows':len(d),'start':str(d.timestamp.min()),'end':str(d.timestamp.max()),'features':list(X.columns)};(OUT/'AUDIT.json').write_text(json.dumps(audit,indent=2)+'\n')
    rules=[]
    state={'L1_CONT':np.sign(X.flowz15)*((np.sign(X.flowz15)==np.sign(X.bt_imbalance_twap))&(np.sign(X.flowz15)==np.sign(X.bt_microprice_premium_close))),'IMB_LEAD':np.sign(X.bt_imbalance_twap)*((X.bt_imbalance_twap.abs()>=.5)&(X.retz5.abs()<=.5)),'ABSORB_REV':-np.sign(X.flowz15)*((X.flowz15.abs()>=1)&(np.sign(X.flowz15)!=np.sign(X.bt_microprice_premium_close))&(X.retz15.abs()<=1)),'IMB_FLIP_REV':np.sign(X.bt_imbalance_close)*((X.imb_close_minus_twap.abs()>=.5)&(np.sign(X.bt_imbalance_close)!=np.sign(X.bt_imbalance_twap)))}
    periods={'dev':('2023-05-16','2023-08-31 23:59'),'val':('2023-09-01','2023-11-30 23:59'),'conf':('2023-12-01','2024-03-31 23:59')}
    strength=np.maximum.reduce([X.bt_imbalance_twap.abs().to_numpy(),X.flowz15.abs().to_numpy(),(X.bt_microprice_premium_close.abs()*1e4).to_numpy()])
    for fam,side0 in state.items():
      for th in (.5,1.,1.5):
       for spread in (.05,.1,.2):
        for h in horizons:
         side=np.asarray(side0,float);mask=(np.abs(side)>0)&(X.bt_spread_bps_twap<=spread)&(strength>=th);idx=np.flatnonzero(mask);chosen=[];free=-1
         for i in idx:
          if i>=free:chosen.append(i);free=i+15
         idx=np.asarray(chosen,int)
         for cost in COSTS:
          rec={'family':fam,'strength':th,'spread_max':spread,'horizon':h,'cost':cost}
          for name,(a,b) in periods.items():
           m=(d.timestamp.iloc[idx]>=pd.Timestamp(a,tz='UTC'))&(d.timestamp.iloc[idx]<=pd.Timestamp(b,tz='UTC'));ii=idx[m];v=side[ii]*labels[h].iloc[ii].to_numpy()-cost;rec.update({f'{name}_n':len(v),f'{name}_mean':float(v.mean()) if len(v) else math.nan,f'{name}_top5':trim(v,5),f'{name}_top10':trim(v,10)})
          rules.append(rec)
    rg=pd.DataFrame(rules);rg['robust']=(rg.cost==12)&(rg.dev_n>=50)&(rg.val_n>=30)&(rg.dev_mean>0)&(rg.val_mean>0)&(rg.dev_top5>0)&(rg.val_top5>0);rg.to_csv(OUT/'RULE_GRID.csv',index=False)
    split={'train':('2023-05-16','2023-08-31 23:59'),'cal':('2023-09-01','2023-10-31 23:59'),'test':('2023-11-01','2023-12-31 23:59'),'hold':('2024-01-01','2024-03-31 23:59')}
    masks={k:(d.timestamp>=pd.Timestamp(a,tz='UTC'))&(d.timestamp<=pd.Timestamp(b,tz='UTC')) for k,(a,b) in split.items()};rankings=[];selected=[]
    for h in horizons:
      tr=np.flatnonzero(masks['train']&labels[h].notna());ca=np.flatnonzero(masks['cal']&labels[h].notna());te=np.flatnonzero(masks['test']&labels[h].notna());ho=np.flatnonzero(masks['hold']&labels[h].notna())
      model=LGBMRegressor(objective='huber',n_estimators=80,learning_rate=.04,num_leaves=7,max_depth=3,min_child_samples=200,reg_alpha=5,reg_lambda=30,verbosity=-1,random_state=31)
      model.fit(X.iloc[tr],np.clip(labels[h].iloc[tr],-200,200));pc=model.predict(X.iloc[ca]);pt=model.predict(X.iloc[te]);ph=model.predict(X.iloc[ho])
      for qtile in (.95,.975,.99,.995):
        threshold=float(np.quantile(np.abs(pc),qtile));xc=one_slot(d.timestamp.iloc[ca].reset_index(drop=True),pc,labels[h].iloc[ca].to_numpy(),h,threshold,12);sc=stat(xc);eligible=sc.get('n',0)>=30 and sc.get('mean',-1)>0 and sc.get('top5',-1)>0 and sc.get('positive_month_fraction',0)>=.5
        rankings.append({'horizon':h,'quantile':qtile,'threshold':threshold,'eligible':eligible,**{'cal_'+k:v for k,v in sc.items()}})
        if eligible:
          xt=one_slot(d.timestamp.iloc[te].reset_index(drop=True),pt,labels[h].iloc[te].to_numpy(),h,threshold,12);xh=one_slot(d.timestamp.iloc[ho].reset_index(drop=True),ph,labels[h].iloc[ho].to_numpy(),h,threshold,12);selected.append({'horizon':h,'quantile':qtile,'threshold':threshold,'cal':sc,'test':stat(xt),'hold':stat(xh)})
    pd.DataFrame(rankings).sort_values(['eligible','cal_mean'],ascending=False).to_csv(OUT/'ML_RANKINGS.csv',index=False)
    summary={'audit':audit,'rule_candidates':len(rg),'rule_robust':int(rg.robust.sum()),'ml_selected':selected,'status':'CANDIDATE' if selected else 'CASH'};(OUT/'SUMMARY.json').write_text(json.dumps(summary,indent=2,default=str)+'\n');print(json.dumps({'rule_robust':summary['rule_robust'],'ml_count':len(selected),'status':summary['status']},indent=2))
if __name__=='__main__':main()
