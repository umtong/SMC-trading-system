#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, io, json, math, shutil, time, urllib.request, zipfile
from pathlib import Path
import numpy as np
import pandas as pd

ROOT="https://data.binance.vision/data/futures/um/daily"
HORIZONS=(3,10,30,60)
FEATURES=(
"spread_rel","l1_imb","micro_dev","quote_age_ms","depth_z","spread_rel_z",
"ofi_1","ofi_5","ofi_30","replenish_imb","depletion_imb",
"flow_imb_1","flow_imb_5","flow_imb_30","flow_depth_pressure_z",
"volume_z","count_z","updates_z","buy_vwap_dev","sell_vwap_dev",
"ret_1","ret_5","ret_30","rv_30","flow_price_eff","is_eth",
)
DIRECTIONAL={
"l1_imb","micro_dev","ofi_1","ofi_5","ofi_30","replenish_imb","depletion_imb",
"flow_imb_1","flow_imb_5","flow_imb_30","flow_depth_pressure_z",
"buy_vwap_dev","sell_vwap_dev","ret_1","ret_5","ret_30","flow_price_eff",
}
def sha256_bytes(x:bytes)->str:return hashlib.sha256(x).hexdigest()
def get(url:str,attempts:int=6)->bytes:
    last=None
    for k in range(attempts):
        try:
            q=urllib.request.Request(url,headers={"User-Agent":"smc-state-first-l1-v2/1.0"})
            with urllib.request.urlopen(q,timeout=600) as r:return r.read()
        except Exception as e:
            last=e
            if k+1<attempts:time.sleep(min(20,2**k))
    raise RuntimeError(f"{url}: {last!r}")
def verified(symbol:str,dtype:str,day:str,cache:Path)->tuple[Path,dict]:
    name=f"{symbol}-{dtype}-{day}.zip";url=f"{ROOT}/{dtype}/{symbol}/{name}"
    cache.mkdir(parents=True,exist_ok=True);p=cache/name
    check=get(url+".CHECKSUM").decode("utf-8-sig").strip();expected=check.split()[0].lower()
    if not p.exists():p.write_bytes(get(url))
    actual=hashlib.sha256(p.read_bytes()).hexdigest()
    if actual!=expected:raise ValueError(f"checksum {name}: {actual} != {expected}")
    return p,{"url":url,"sha256":actual,"bytes":p.stat().st_size}
def prior_z(s:pd.Series,w:int=600,minp:int=300)->pd.Series:
    r=s.rolling(w,min_periods=minp)
    return (s-r.mean().shift(1))/r.std(ddof=0).shift(1).replace(0,np.nan)
def book_seconds(path:Path):
    parts=[];prev=None;ts=[];bids=[];asks=[]
    use=["best_bid_price","best_bid_qty","best_ask_price","best_ask_qty","event_time"]
    for c in pd.read_csv(path,compression="zip",usecols=use,chunksize=750_000):
        for col in use[:-1]:c[col]=pd.to_numeric(c[col],errors="raise")
        c["event_time"]=pd.to_numeric(c.event_time,errors="raise").astype("int64")
        c=c.sort_values("event_time",kind="mergesort").reset_index(drop=True)
        ts.append(c.event_time.to_numpy(np.int64));bids.append(c.best_bid_price.to_numpy(float));asks.append(c.best_ask_price.to_numpy(float))
        c2=pd.concat([prev,c],ignore_index=True) if prev is not None else c
        off=1 if prev is not None else 0
        bid=c2.best_bid_price.to_numpy(float);bq=c2.best_bid_qty.to_numpy(float)
        ask=c2.best_ask_price.to_numpy(float);aq=c2.best_ask_qty.to_numpy(float);t=c2.event_time.to_numpy(np.int64)
        pbid=np.r_[np.nan,bid[:-1]];pbq=np.r_[np.nan,bq[:-1]];pask=np.r_[np.nan,ask[:-1]];paq=np.r_[np.nan,aq[:-1]]
        valid=np.arange(len(c2))>=max(off,1)
        ofi=(bid>=pbid)*bq-(bid<=pbid)*pbq-(ask<=pask)*aq+(ask>=pask)*paq
        sb=bid==pbid;sa=ask==pask
        br=np.where(sb,np.maximum(bq-pbq,0),np.where(bid>pbid,bq,0));bd=np.where(sb,np.maximum(pbq-bq,0),np.where(bid<pbid,pbq,0))
        ar=np.where(sa,np.maximum(aq-paq,0),np.where(ask<pask,aq,0));ad=np.where(sa,np.maximum(paq-aq,0),np.where(ask>pask,paq,0))
        sec=t//1000
        a=pd.DataFrame({"sec":sec[valid],"ofi":ofi[valid],"bid_repl":br[valid],"bid_depl":bd[valid],
            "ask_repl":ar[valid],"ask_depl":ad[valid],"updates":1}).groupby("sec",sort=False).sum()
        last=pd.DataFrame({"sec":sec[valid],"bid":bid[valid],"bq":bq[valid],"ask":ask[valid],"aq":aq[valid],
            "last_quote_ms":t[valid]}).groupby("sec",sort=False).tail(1).set_index("sec")
        parts.append(a.join(last,how="outer"));prev=c.tail(1)
    x=pd.concat(parts).groupby(level=0).agg({"ofi":"sum","bid_repl":"sum","bid_depl":"sum","ask_repl":"sum","ask_depl":"sum",
        "updates":"sum","bid":"last","bq":"last","ask":"last","aq":"last","last_quote_ms":"last"}).sort_index()
    t=np.concatenate(ts);bid=np.concatenate(bids);ask=np.concatenate(asks);o=np.argsort(t,kind="stable")
    return x,t[o],bid[o],ask[o]
def trade_seconds(path:Path):
    parts=[]
    use=["price","quantity","transact_time","is_buyer_maker"]
    for c in pd.read_csv(path,compression="zip",usecols=use,chunksize=1_000_000):
        p=pd.to_numeric(c.price,errors="raise").to_numpy(float);q=pd.to_numeric(c.quantity,errors="raise").to_numpy(float)
        t=pd.to_numeric(c.transact_time,errors="raise").to_numpy(np.int64);t=np.where(np.abs(t)>=10**15,t//1000,t)
        maker=c.is_buyer_maker.astype(str).str.lower().isin(["true","1"]).to_numpy();quote=p*q;buy=~maker;sec=t//1000
        d=pd.DataFrame({"sec":sec,"quote":quote,"signed":np.where(buy,quote,-quote),"count":1,
            "buyq":np.where(buy,quote,0.),"sellq":np.where(buy,0.,quote),
            "buypxq":np.where(buy,p*quote,0.),"sellpxq":np.where(buy,0.,p*quote)})
        parts.append(d.groupby("sec",sort=False).sum())
    return pd.concat(parts).groupby(level=0).sum().sort_index()
def build_day(symbol:str,day:str,cache:Path,out:Path):
    bp,bm=verified(symbol,"bookTicker",day,cache);tp,tm=verified(symbol,"aggTrades",day,cache)
    book,bt,bb,ba=book_seconds(bp);trade=trade_seconds(tp)
    lo=max(int(book.index.min()),int(trade.index.min()));hi=min(int(book.index.max()),int(trade.index.max()))
    idx=np.arange(lo,hi+1,dtype=np.int64);x=pd.DataFrame(index=idx).join(book).join(trade)
    for c in ["bid","bq","ask","aq","last_quote_ms"]:x[c]=x[c].ffill()
    for c in ["ofi","bid_repl","bid_depl","ask_repl","ask_depl","updates","quote","signed","count","buyq","sellq","buypxq","sellpxq"]:x[c]=x[c].fillna(0.)
    x["mid"]=(x.bid+x.ask)/2;x["depth"]=x.bq+x.aq;x["spread_rel"]=(x.ask-x.bid)/x.mid
    x["l1_imb"]=(x.bq-x.aq)/x.depth.replace(0,np.nan);x["micro_dev"]=((x.ask*x.bq+x.bid*x.aq)/x.depth.replace(0,np.nan)-x.mid)/x.mid
    x["replenish_imb"]=(x.bid_repl-x.ask_repl)/(x.bid_repl+x.ask_repl+1e-12)
    x["depletion_imb"]=(x.ask_depl-x.bid_depl)/(x.ask_depl+x.bid_depl+1e-12)
    x["quote_age_ms"]=(x.index.to_numpy()*1000+999-x.last_quote_ms).clip(lower=0)
    x["flow_imb_1"]=x.signed/x.quote.replace(0,np.nan);x["ret_1"]=np.log(x.mid/x.mid.shift(1))
    for w in (1,5,30):
        x[f"ofi_{w}"]=x.ofi.rolling(w,min_periods=max(1,w//2)).sum()/x.depth.rolling(w,min_periods=max(1,w//2)).mean().replace(0,np.nan)
        x[f"flow_imb_{w}"]=x.signed.rolling(w,min_periods=max(1,w//2)).sum()/x.quote.rolling(w,min_periods=max(1,w//2)).sum().replace(0,np.nan)
        x[f"ret_{w}"]=np.log(x.mid/x.mid.shift(w))
    x["flow_depth_pressure"]=x.signed/(x.mid*x.depth).replace(0,np.nan)
    x["flow_depth_pressure_z"]=prior_z(x.flow_depth_pressure)
    x["depth_z"]=prior_z(np.log1p(x.depth));x["spread_rel_z"]=prior_z(x.spread_rel)
    x["volume_z"]=prior_z(np.log1p(x.quote));x["count_z"]=prior_z(np.log1p(x["count"]));x["updates_z"]=prior_z(np.log1p(x.updates))
    x["buy_vwap_dev"]=(x.buypxq/x.buyq.replace(0,np.nan)-x.mid)/x.mid
    x["sell_vwap_dev"]=(x.sellpxq/x.sellq.replace(0,np.nan)-x.mid)/x.mid
    x["rv_30"]=x.ret_1.rolling(30,min_periods=15).std(ddof=0).shift(1)
    x["flow_price_eff"]=x.ret_5/(x.signed.rolling(5,min_periods=2).sum().abs()/x.quote.rolling(5,min_periods=2).sum().replace(0,np.nan)+1e-9)
    x["is_eth"]=1. if symbol=="ETHUSDT" else 0.
    signal_end=(x.index.to_numpy(np.int64)+1)*1000;ei=np.searchsorted(bt,signal_end,side="right");valid=ei<len(bt)
    x=x.iloc[np.flatnonzero(valid)].copy();signal_end=signal_end[valid];ei=ei[valid]
    et=bt[ei];eb=bb[ei];ea=ba[ei];x["signal_end_ms"]=signal_end;x["entry_time_ms"]=et;x["entry_bid"]=eb;x["entry_ask"]=ea
    for h in HORIZONS:
        xi=np.searchsorted(bt,et+h*1000,side="left");ok=xi<len(bt)
        xt=np.full(len(x),np.nan);xb=np.full(len(x),np.nan);xa=np.full(len(x),np.nan)
        xt[ok]=bt[xi[ok]];xb[ok]=bb[xi[ok]];xa[ok]=ba[xi[ok]]
        x[f"exit_time_{h}"]=xt;x[f"long_gross_{h}"]=xb/ea-1;x[f"short_gross_{h}"]=eb/xa-1
    keep=["signal_end_ms","entry_time_ms","entry_bid","entry_ask"]+list(FEATURES)+[f"exit_time_{h}" for h in HORIZONS]+[f"{s}_gross_{h}" for h in HORIZONS for s in ("long","short")]
    y=x[keep].replace([np.inf,-np.inf],np.nan).dropna().copy();y["symbol"]=symbol;y["day"]=day
    out.mkdir(parents=True,exist_ok=True);p=out/f"{symbol}_{day}_l1.csv.gz"
    y.to_csv(p,index=False,compression={"method":"gzip","compresslevel":6,"mtime":0})
    manifest={"version":"STATE_FIRST_L1_V2","symbol":symbol,"day":day,"rows":len(y),"output_sha256":hashlib.sha256(p.read_bytes()).hexdigest(),
        "bookTicker":bm,"aggTrades":tm,"decision":"completed one-second state","entry":"first BBO strictly after second end",
        "orders_submitted":False}
    (out/f"{symbol}_{day}_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print(json.dumps(manifest,indent=2))
def route(df:pd.DataFrame,side:np.ndarray,score:np.ndarray,h:int,cost_bps:float)->pd.DataFrame:
    idx=np.flatnonzero((side!=0)&np.isfinite(score))
    if len(idx)==0:return pd.DataFrame(columns=["entry_time_ms","exit_time_ms","symbol","side","net","score"])
    q=pd.DataFrame({"i":idx,"entry":df.entry_time_ms.to_numpy(np.int64)[idx],"exit":df[f"exit_time_{h}"].to_numpy(np.int64)[idx],
        "symbol":df.symbol.to_numpy()[idx],"score":score[idx]}).sort_values(["entry","score","symbol"],ascending=[True,False,True],kind="mergesort")
    q=q.groupby("entry",sort=True).head(1).sort_values("entry",kind="mergesort");use=[];free=-10**30
    for r in q.itertuples(index=False):
        if int(r.entry)>=free:use.append(int(r.i));free=int(r.exit)
    use=np.asarray(use,dtype=int);s=side[use]
    gross=np.where(s>0,df[f"long_gross_{h}"].to_numpy()[use],df[f"short_gross_{h}"].to_numpy()[use])
    return pd.DataFrame({"entry_time_ms":df.entry_time_ms.to_numpy(np.int64)[use],"exit_time_ms":df[f"exit_time_{h}"].to_numpy(np.int64)[use],
        "symbol":df.symbol.to_numpy()[use],"side":s,"net":gross-cost_bps/1e4,"score":score[use]})
def metric(z:pd.DataFrame)->dict:
    if z.empty:return {"n":0,"mean_bps":-999.,"pf":0.,"log_growth":-999.,"mdd":1.,"top5_share":1.,"positive_day_fraction":0.,"btc_n":0,"eth_n":0}
    v=z.net.to_numpy(float);log=np.log1p(np.maximum(v,-.999));eq=np.exp(np.cumsum(log));curve=np.r_[1.,eq];dd=1-curve/np.maximum.accumulate(curve)
    pos=v[v>0];neg=-v[v<0];gp=pos.sum();top5=1. if gp<=0 else np.sort(pos)[::-1][:5].sum()/gp
    day=pd.to_datetime(z.entry_time_ms,unit="ms",utc=True).dt.floor("D");daily=pd.DataFrame({"d":day.to_numpy(),"v":v}).groupby("d").v.sum()
    return {"n":int(len(v)),"mean_bps":float(v.mean()*1e4),"pf":float(pos.sum()/neg.sum()) if neg.sum()>0 else 999.,
        "log_growth":float(log.sum()),"mdd":float(dd.max()),"top5_share":float(top5),"positive_day_fraction":float((daily>0).mean()),
        "btc_n":int((z.symbol=="BTCUSDT").sum()),"eth_n":int((z.symbol=="ETHUSDT").sum())}
def aggregate(inp:Path,out:Path):
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import HistGradientBoostingRegressor
    files=sorted(inp.rglob("*_l1.csv.gz"))
    if len(files)!=24:raise ValueError(f"expected 24 symbol-day files, found {len(files)}")
    d=pd.concat([pd.read_csv(p) for p in files],ignore_index=True)
    d=d.sort_values(["entry_time_ms","symbol"],kind="mergesort").reset_index(drop=True)
    dt=pd.to_datetime(d.entry_time_ms,unit="ms",utc=True);d["date"]=dt.dt.strftime("%Y-%m-%d")
    train=d.date.isin(["2023-06-15","2023-07-15","2023-08-15"])
    calibration=d.date.isin(["2023-09-15","2023-10-15","2023-11-15"])
    validation=d.date.isin(["2024-06-15","2024-07-15","2024-08-15","2024-09-15","2024-10-15","2024-11-15"])
    if not (train.any() and calibration.any() and validation.any()):raise ValueError("frozen split is incomplete")
    X=d[list(FEATURES)].replace([np.inf,-np.inf],np.nan)
    imp=SimpleImputer(strategy="median");A=imp.fit_transform(X[train]);B=imp.transform(X)
    rows=[];survivor_ledgers=[]
    def add_periods(rec,l):
        for label,mask in (("train",train),("calibration",calibration),("validation",validation)):
            times=set(d.loc[mask,"entry_time_ms"].astype("int64"))
            z=l[l.entry_time_ms.isin(times)] if not l.empty else l
            rec.update({f"{label}_{k}":v for k,v in metric(z).items()})
        return rec
    def maybe_keep(rec,l):
        if (rec["cost_bps"]>=12 and rec["calibration_n"]>=150 and rec["validation_n"]>=300 and
            rec["calibration_log_growth"]>0 and rec["validation_log_growth"]>0 and
            rec["calibration_pf"]>=1.1 and rec["validation_pf"]>=1.1 and
            rec["calibration_top5_share"]<=.25 and rec["validation_top5_share"]<=.25 and
            rec["calibration_positive_day_fraction"]>=.5 and rec["validation_positive_day_fraction"]>=.5 and
            rec["calibration_btc_n"]>=25 and rec["calibration_eth_n"]>=25 and
            rec["validation_btc_n"]>=50 and rec["validation_eth_n"]>=50):
            z=l.copy();z["config"]=rec["config"];z["cost_bps"]=rec["cost_bps"];survivor_ledgers.append(z)
    for h in HORIZONS:
        target=(d[f"long_gross_{h}"].to_numpy()-d[f"short_gross_{h}"].to_numpy())/2
        models={
            "ridge":make_pipeline(StandardScaler(),Ridge(alpha=100.)).fit(A,target[train]),
            "hgb":HistGradientBoostingRegressor(max_iter=180,max_depth=5,learning_rate=.05,l2_regularization=10,random_state=2026).fit(A,target[train]),
        }
        for name,m in models.items():
            pred=m.predict(B)
            for q in (.90,.95,.975,.99,.995):
                th=float(np.quantile(np.abs(pred[train]),q));side=np.where(np.abs(pred)>=th,np.sign(pred),0).astype(int);score=np.abs(pred)
                for cost in (8.,12.,18.):
                    l=route(d,side,score,h,cost)
                    rec={"config":f"{name}_h{h}_q{q}_c{int(cost)}","family":name,"horizon":h,"quantile":q,"cost_bps":cost}
                    add_periods(rec,l);rows.append(rec);maybe_keep(rec,l)
        arrays={c:d[c].fillna(0).to_numpy() for c in FEATURES};rule_defs=[]
        for z in (.5,.7,.9):
            side=np.sign(arrays["flow_imb_5"]).astype(int)
            mask=(np.abs(arrays["flow_imb_5"])>=z)&(np.sign(arrays["ofi_5"])==side)&(side!=0)
            rule_defs.append((f"aligned_z{z}",np.where(mask,side,0),np.abs(arrays["flow_imb_5"])+np.abs(arrays["ofi_5"])))
            aggressor=np.sign(arrays["flow_imb_5"]).astype(int);reversal=-aggressor
            mask=(np.abs(arrays["flow_imb_5"])>=z)&(np.abs(arrays["ret_5"])*1e4<=3)&(arrays["replenish_imb"]*reversal>=.15)&(reversal!=0)
            rule_defs.append((f"absorption_z{z}",np.where(mask,reversal,0),np.abs(arrays["flow_imb_5"])+np.abs(arrays["replenish_imb"])))
            side=np.sign(arrays["flow_depth_pressure_z"]).astype(int)
            mask=(np.abs(arrays["flow_depth_pressure_z"])>=z)&(np.sign(arrays["ofi_5"])==side)&((arrays["depth_z"]<=-.5)|(arrays["spread_rel_z"]>=.5))&(side!=0)
            rule_defs.append((f"fragile_z{z}",np.where(mask,side,0),np.abs(arrays["flow_depth_pressure_z"])+np.maximum(-arrays["depth_z"],0)))
        for name,side,score in rule_defs:
            for cost in (8.,12.,18.):
                l=route(d,side,score,h,cost)
                rec={"config":f"{name}_h{h}_c{int(cost)}","family":name,"horizon":h,"quantile":np.nan,"cost_bps":cost}
                add_periods(rec,l);rows.append(rec);maybe_keep(rec,l)
    r=pd.DataFrame(rows)
    r["eligible_2024"]=((r.cost_bps>=12)&(r.calibration_n>=150)&(r.validation_n>=300)&
        (r.calibration_log_growth>0)&(r.validation_log_growth>0)&(r.calibration_pf>=1.1)&(r.validation_pf>=1.1)&
        (r.calibration_top5_share<=.25)&(r.validation_top5_share<=.25)&
        (r.calibration_positive_day_fraction>=.5)&(r.validation_positive_day_fraction>=.5)&
        (r.calibration_btc_n>=25)&(r.calibration_eth_n>=25)&(r.validation_btc_n>=50)&(r.validation_eth_n>=50))
    r["score"]=np.where(r.eligible_2024,np.minimum(r.calibration_mean_bps,r.validation_mean_bps),-1e9)
    r=r.sort_values(["eligible_2024","score","validation_top5_share"],ascending=[False,False,True])
    out.mkdir(parents=True,exist_ok=True);r.to_csv(out/"screen.csv",index=False)
    e=r[r.eligible_2024]
    if survivor_ledgers:
        led=pd.concat(survivor_ledgers,ignore_index=True).drop_duplicates(["config","entry_time_ms","symbol"]).sort_values(["config","entry_time_ms"])
        led.to_csv(out/"eligible_trade_ledger.csv.gz",index=False,compression={"method":"gzip","compresslevel":6,"mtime":0})
    summary={"version":"STATE_FIRST_L1_V2","files":len(files),"rows":len(d),"screened":len(r),
        "split":{"train":["2023-06-15","2023-07-15","2023-08-15"],"calibration":["2023-09-15","2023-10-15","2023-11-15"],"validation":["2024-06-15","2024-07-15","2024-08-15","2024-09-15","2024-10-15","2024-11-15"]},
        "eligible_2024":len(e),"2025_opened":False,"best":r.head(30).replace([np.nan,np.inf,-np.inf],None).to_dict("records"),
        "orders_submitted":False,"paper_live_started":False}
    (out/"summary.json").write_text(json.dumps(summary,indent=2)+"\n");print(json.dumps(summary,indent=2))
def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("day");p.add_argument("--symbol",required=True);p.add_argument("--day",required=True);p.add_argument("--cache",type=Path,required=True);p.add_argument("--out",type=Path,required=True)
    a=sub.add_parser("aggregate");a.add_argument("--input",type=Path,required=True);a.add_argument("--out",type=Path,required=True)
    x=ap.parse_args()
    if x.cmd=="day":build_day(x.symbol,x.day,x.cache,x.out)
    else:aggregate(x.input,x.out)
if __name__=="__main__":main()
