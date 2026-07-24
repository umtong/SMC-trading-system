from __future__ import annotations

import argparse, hashlib, json, traceback
from pathlib import Path

import numpy as np
import pandas as pd

PERIODS = [
    ("calib_2023H2", "2023-07-01", "2024-01-01"),
    ("2024", "2024-01-01", "2025-01-01"),
    ("2025", "2025-01-01", "2026-01-01"),
    ("2026H1", "2026-01-01", "2026-06-01"),
]
TARGETS = {15: "fwd_ret_15m", 60: "fwd_ret_60m"}
BASE = ["log_ret_1m","log_ret_5m","log_ret_15m","log_ret_60m","realized_vol_30m","rsi_14","vol_5m","taker_buy_ratio_5m","trade_count_5m","avg_trade_size_5m"]
MICRO = ["depth_imbalance_1pct","vpin_50","vpin_bucket_imbalance","hawkes_buy_intensity","hawkes_sell_intensity","hawkes_net"]
STRUCT = ["oi_btc","oi_change_1h","ls_count_ratio","taker_ls_vol_ratio"]


def utc(x):
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def download(cache: Path):
    from huggingface_hub import snapshot_download
    errors=[]
    for repo in ("ibrahimdaud/binance-btcusdt","ibrahimdaud/btcusdt-futures-features"):
        try:
            root=Path(snapshot_download(repo_id=repo,repo_type="dataset",allow_patterns=["features/BTCUSDT/*.parquet","README.md"],local_dir=cache/repo.split("/")[-1],max_workers=16))
            files=sorted((root/"features"/"BTCUSDT").glob("*.parquet"))
            if len(files)<500: raise RuntimeError(f"only {len(files)} feature files")
            return root,repo,len(files)
        except Exception as e: errors.append(f"{repo}: {type(e).__name__}: {e}")
    raise RuntimeError(" | ".join(errors))


def load(root: Path):
    import pyarrow.dataset as ds
    f=ds.dataset(str(root/"features"/"BTCUSDT"),format="parquet").to_table().to_pandas()
    f["time"]=pd.to_datetime(f.bar_time_ms,unit="ms",utc=True)
    f=f.loc[(f.time>=utc("2023-01-01"))&(f.time<utc("2026-06-01"))].sort_values("time",kind="mergesort").drop_duplicates("time").reset_index(drop=True)
    exp=pd.date_range(f.time.iloc[0],f.time.iloc[-1],freq="5min",tz="UTC")
    miss=len(exp.difference(pd.DatetimeIndex(f.time)))
    if miss: raise ValueError(f"missing 5m rows: {miss}")
    return f


def zprior(s,w,minp):
    p=s.shift(1); m=p.rolling(w,min_periods=minp).mean(); d=p.rolling(w,min_periods=minp).std(ddof=0).replace(0,np.nan)
    return (s-m)/d


def features(f):
    x=pd.DataFrame(index=f.index)
    for c in BASE+MICRO+STRUCT: x[c]=pd.to_numeric(f[c],errors="coerce").astype(float)
    x["flow"]=2*x.taker_buy_ratio_5m-1
    x["vpin_bucket_c"]=2*x.vpin_bucket_imbalance-1
    x["rsi_c"]=(x.rsi_14-50)/50
    x["log_vol"]=np.log1p(x.vol_5m.clip(lower=0)); x["log_trades"]=np.log1p(x.trade_count_5m.clip(lower=0)); x["log_avg_trade"]=np.log1p(x.avg_trade_size_5m.clip(lower=0))
    x["log_oi"]=np.log(x.oi_btc.where(x.oi_btc>0)); x["log_ls"]=np.log(x.ls_count_ratio.where(x.ls_count_ratio>0)); x["log_tls"]=np.log(x.taker_ls_vol_ratio.where(x.taker_ls_vol_ratio>0))
    x["hawkes_total"]=np.log1p((x.hawkes_buy_intensity+x.hawkes_sell_intensity).clip(lower=0))
    src=["log_ret_5m","log_ret_15m","log_ret_60m","realized_vol_30m","flow","log_vol","log_trades","depth_imbalance_1pct","vpin_50","vpin_bucket_c","hawkes_net","hawkes_total","oi_change_1h","log_ls","log_tls"]
    for c in src:
        x[c+"_z1d"]=zprior(x[c],288,96); x[c+"_z7d"]=zprior(x[c],2016,576); x[c+"_d1"]=x[c]-x[c].shift(1); x[c+"_d12"]=x[c]-x[c].shift(12)
    x["flow_depth"]=x.flow*x.depth_imbalance_1pct; x["flow_hawkes"]=x.flow*x.hawkes_net; x["depth_hawkes"]=x.depth_imbalance_1pct*x.hawkes_net
    x["flow_price"]=x.flow*np.sign(x.log_ret_5m.fillna(0)); x["flow_eff"]=x.log_ret_5m/(x.flow.abs()+.05); x["absorption"]=x.flow.abs()/(x.log_ret_5m.abs()+1e-5)
    x["vpin_flow"]=x.vpin_50*x.flow; x["vpin_depth"]=x.vpin_50*x.depth_imbalance_1pct; x["oi_price"]=x.oi_change_1h*x.log_ret_60m; x["oi_flow"]=x.oi_change_1h*x.flow
    micro_tokens=("depth","vpin","hawkes","flow_depth","flow_hawkes")
    struct_tokens=("oi_","log_oi","log_ls","log_tls")
    base=[c for c in x if not any(t in c for t in micro_tokens+struct_tokens)]
    mic=sorted(set(base+[c for c in x if any(t in c for t in micro_tokens)]))
    return x.replace([np.inf,-np.inf],np.nan),{"base":base,"micro":mic,"full":list(x)}


def model(kind):
    if kind=="ridge":
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        return make_pipeline(SimpleImputer(strategy="median",add_indicator=True),StandardScaler(),Ridge(alpha=25))
    from lightgbm import LGBMRegressor
    return LGBMRegressor(objective="huber",n_estimators=220,learning_rate=.035,num_leaves=15,max_depth=5,min_child_samples=200,subsample=.8,colsample_bytree=.8,reg_alpha=1,reg_lambda=8,random_state=20260724,n_jobs=4,verbosity=-1)


def metrics(r,start,end):
    if not len(r): return dict(trades=0,final_multiple=1.,geo_daily=0.,max_drawdown=0.,top10_share=np.nan,win_rate=np.nan)
    w=np.cumprod(1+r); path=np.r_[1.,w]; peak=np.maximum.accumulate(path); dd=path/peak-1; days=max(1,(end.normalize()-start.normalize()).days)
    pos=r[r>0]; top=float(np.sort(pos)[-10:].sum()/pos.sum()) if pos.sum()>0 else np.nan
    return dict(trades=int(len(r)),final_multiple=float(w[-1]),geo_daily=float(w[-1]**(1/days)-1) if w[-1]>0 else -1.,max_drawdown=float(dd.min()),top10_share=top,win_rate=float(np.mean(r>0)),mean_trade=float(np.mean(r)))


def simulate(t,score,target,lo,hi,mode,horizon,cost,start,end):
    side=np.zeros(len(score),np.int8)
    if mode in ("long","both"): side[score>=hi]=1
    if mode in ("short","both"): side[score<=lo]=-1
    idx=np.flatnonzero((side!=0)&np.isfinite(target)); accepted=[]; free=np.datetime64("1900-01-01")
    hold=np.timedelta64(horizon,"m")
    for i in idx:
        decision=t[i]+np.timedelta64(5,"m")
        if decision<free: continue
        accepted.append(i); free=decision+hold
    a=np.asarray(accepted,dtype=int)
    if not len(a): return metrics(np.array([]),start,end),a,np.array([])
    ret=np.expm1(target[a]*side[a])-cost/10000
    ok=ret>-.999; a=a[ok]; ret=ret[ok]
    return metrics(ret,start,end),a,side[a]


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--cache",type=Path,default=Path(".cache/v18-btc-l2")); ap.add_argument("--output",type=Path,default=Path("artifacts/v18-btc-l2")); a=ap.parse_args(); a.output.mkdir(parents=True,exist_ok=True)
    try:
        root,repo,nfiles=download(a.cache); f=load(root); X,sets=features(f)
        (a.output/"data_manifest.json").write_text(json.dumps(dict(repo=repo,files=nfiles,rows=len(f),start=f.time.min().isoformat(),end=f.time.max().isoformat(),feature_sizes={k:len(v) for k,v in sets.items()}),indent=2),encoding="utf-8")
        pred_records=[]; meta=[]
        for pname,es,ee in PERIODS:
            start,end=utc(es),utc(ee); em=(f.time>=start)&(f.time<end)
            for fs in ("base","micro","full"):
              for kind in ("ridge","lgbm"):
               for mem in (6,12):
                train_start=start-pd.DateOffset(months=mem); cal_start=start-pd.DateOffset(months=2); fit=(f.time>=train_start)&(f.time<cal_start); cal=(f.time>=cal_start)&(f.time<start)
                for h,targetcol in TARGETS.items():
                    valid=fit&f[targetcol].notna(); fi=np.flatnonzero(valid)[::max(1,h//5)]
                    if len(fi)<10000 or cal.sum()<5000: continue
                    m=model(kind); cols=sets[fs]; m.fit(X.iloc[fi][cols],f.iloc[fi][targetcol].to_numpy(float))
                    cp=np.asarray(m.predict(X.loc[cal,cols]),float); ep=np.asarray(m.predict(X.loc[em,cols]),float); mid=f"{fs}__{kind}__m{mem}__h{h}"
                    q={str(v):float(np.nanquantile(cp,v)) for v in (.005,.01,.025,.05,.10,.90,.95,.975,.99,.995)}
                    meta.append(dict(period=pname,model_id=mid,feature_set=fs,model_type=kind,memory_months=mem,horizon_min=h,fit_rows=len(fi),calib_rows=int(cal.sum()),eval_rows=int(em.sum()),quantiles=json.dumps(q,sort_keys=True)))
                    pred_records.append(pd.DataFrame(dict(time=f.loc[em,"time"].to_numpy(),bar_time_ms=f.loc[em,"bar_time_ms"].to_numpy(),model_id=mid,score=ep,target=f.loc[em,targetcol].to_numpy(float))))
                    print("trained",pname,mid,len(fi),flush=True)
        pred=pd.concat(pred_records,ignore_index=True); pd.DataFrame(meta).to_csv(a.output/"model_meta.csv",index=False); pred.to_parquet(a.output/"predictions.parquet",index=False,compression="zstd")
        md=pd.DataFrame(meta); md["qd"]=md.quantiles.map(json.loads); rows=[]; signal_rows=[]
        for pname,es,ee in PERIODS:
            start,end=utc(es),utc(ee); pg=pred[(pred.time>=start)&(pred.time<end)]
            for mid,g in pg.groupby("model_id"):
                mr=md[(md.period==pname)&(md.model_id==mid)]
                if mr.empty: continue
                qd=mr.iloc[0].qd; h=int(mr.iloc[0].horizon_min); tt=g.time.to_numpy(dtype="datetime64[ns]"); score=g.score.to_numpy(float); target=g.target.to_numpy(float)
                for tail in (.005,.01,.025,.05,.10):
                    lo,hi=float(qd[str(tail)]),float(qd[str(1-tail)])
                    for mode in ("long","short","both"):
                        cid=f"{mid}__q{tail:.3f}__{mode}"
                        for cost in (12.,20.,40.):
                            met,idx,side=simulate(tt,score,target,lo,hi,mode,h,cost,start,end); rows.append(dict(period=pname,candidate_id=cid,model_id=mid,tail=tail,mode=mode,horizon_min=h,cost_bps=cost,**met))
                            if cost==20 and len(idx):
                                z=g.iloc[idx][["bar_time_ms","time","score","target"]].copy(); z["candidate_id"]=cid; z["period"]=pname; z["side"]=side; z["horizon_min"]=h; signal_rows.append(z)
        res=pd.DataFrame(rows); res.to_csv(a.output/"candidate_metrics.csv",index=False)
        pv=res.pivot_table(index=["candidate_id","cost_bps"],columns="period",values=["final_multiple","geo_daily","max_drawdown","trades","top10_share"],aggfunc="first"); pv.columns=[f"{x}__{y}" for x,y in pv.columns]; pv=pv.reset_index(); pv.to_csv(a.output/"candidate_pivot.csv",index=False)
        s=pv[pv.cost_bps==20].copy(); req=["final_multiple__calib_2023H2","final_multiple__2024","trades__calib_2023H2","trades__2024"]
        for c in req:
            if c not in s: s[c]=np.nan
        e=s[(s.final_multiple__calib_2023H2>1)&(s.final_multiple__2024>1)&(s.trades__calib_2023H2>=20)&(s.trades__2024>=30)].copy()
        if len(e): e["selection_score"]=np.minimum(e.geo_daily__calib_2023H2,e.geo_daily__2024)-.25*np.maximum(-e.max_drawdown__calib_2023H2,-e.max_drawdown__2024)/365; e=e.sort_values("selection_score",ascending=False)
        e.to_csv(a.output/"eligible_pre2025.csv",index=False); top=e.candidate_id.head(20).tolist(); term=pv[pv.candidate_id.isin(top)].copy(); term.to_csv(a.output/"terminal_fixed.csv",index=False)
        if signal_rows:
            sig=pd.concat(signal_rows,ignore_index=True); sig=sig[sig.candidate_id.isin(top[:10])]; sig.to_parquet(a.output/"selected_signals.parquet",index=False,compression="zstd")
        ab=[]
        for fs in ("base","micro","full"):
            q=pv[(pv.cost_bps==20)&pv.candidate_id.str.startswith(fs+"__")].copy()
            if len(q): q["dmin"]=q[["geo_daily__calib_2023H2","geo_daily__2024"]].min(axis=1); ab.append(dict(feature_set=fs,**q.sort_values("dmin",ascending=False).iloc[0].to_dict()))
        pd.DataFrame(ab).to_csv(a.output/"feature_ablation.csv",index=False)
        target_met=bool(len(term) and (((term.cost_bps==20)&(term.get("geo_daily__2025",pd.Series(index=term.index,dtype=float))>=.01)&(term.get("geo_daily__2026H1",pd.Series(index=term.index,dtype=float))>=.01)).any()))
        summary=dict(candidate_count=int(res.candidate_id.nunique()),eligible_pre2025_count=len(e),top_candidate_ids=top[:10],target_met_20bp=target_met)
        (a.output/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in a.output.iterdir() if p.is_file()}; (a.output/"SHA256SUMS.json").write_text(json.dumps(hashes,indent=2,sort_keys=True),encoding="utf-8")
        print(json.dumps(summary,indent=2),flush=True); return 0
    except Exception:
        (a.output/"failure.txt").write_text(traceback.format_exc(),encoding="utf-8"); print(traceback.format_exc(),flush=True); return 1

if __name__=="__main__": raise SystemExit(main())
