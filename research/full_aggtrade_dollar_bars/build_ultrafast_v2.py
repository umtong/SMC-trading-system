from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um/monthly"
CONFIGS = ((72, 600), (144, 600), (288, 600), (288, 300))
EXIT_HORIZONS_SECONDS = (1800, 3600, 7200, 14400, 28800)
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "num_trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
AGG_COLUMNS = [
    "agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
    "transact_time", "is_buyer_maker",
]
CHUNK_ROWS = 2_000_000


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def infer_unit(values: pd.Series) -> str:
    x = pd.to_numeric(values, errors="raise")
    med = float(x.abs().median())
    if med > 1e17:
        return "ns"
    if med > 1e14:
        return "us"
    if med > 1e11:
        return "ms"
    return "s"


def download_verified(session: requests.Session, url: str, cache: Path, attempts: int = 6) -> tuple[Path, dict]:
    cache.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    out = cache / name
    chk = cache / f"{name}.CHECKSUM"
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            if not chk.exists():
                r = session.get(url + ".CHECKSUM", timeout=(30, 180))
                r.raise_for_status()
                chk.write_bytes(r.content)
            expected = chk.read_text(encoding="utf-8-sig").strip().split()[0].lower()
            if not out.exists() or sha256_file(out) != expected:
                tmp = out.with_suffix(out.suffix + ".part")
                tmp.unlink(missing_ok=True)
                with session.get(url, stream=True, timeout=(30, 900)) as r:
                    r.raise_for_status()
                    with tmp.open("wb") as f:
                        for block in r.iter_content(8 << 20):
                            if block:
                                f.write(block)
                tmp.replace(out)
            actual = sha256_file(out)
            if actual != expected:
                raise RuntimeError(f"checksum mismatch {url}: {actual} != {expected}")
            return out, {"url": url, "bytes": out.stat().st_size, "sha256": actual}
        except Exception as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"download failed {url}: {error!r}")


def parse_kline(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError(f"expected one CSV, got {names}")
        with zf.open(names[0]) as raw:
            first = raw.readline().decode("utf-8", "replace").split(",")[0].strip().lower()
            raw.seek(0)
            header = first in {"open_time", "open time"}
            df = pd.read_csv(raw, header=0 if header else None, names=None if header else KLINE_COLUMNS)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    aliases = {
        "quote_asset_volume": "quote_volume",
        "number_of_trades": "num_trades",
        "taker_buy_base_asset_volume": "taker_buy_base",
        "taker_buy_quote_asset_volume": "taker_buy_quote",
    }
    df = df.rename(columns={c: aliases.get(c, c) for c in df.columns})
    if "open_time" not in df.columns:
        df.columns = KLINE_COLUMNS[: len(df.columns)]
    unit = infer_unit(df["open_time"])
    ts = pd.to_datetime(pd.to_numeric(df["open_time"], errors="raise"), unit=unit, utc=True)
    cols = ["open", "high", "low", "close", "quote_volume", "num_trades", "taker_buy_quote"]
    out = pd.DataFrame({"timestamp": ts})
    for c in cols:
        out[c] = pd.to_numeric(df[c], errors="raise")
    return out


def parse_funding(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as zf:
        names=[n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names)!=1: raise RuntimeError(f"expected one funding CSV, got {names}")
        with zf.open(names[0]) as raw:
            first=raw.readline().decode("utf-8","replace"); raw.seek(0)
            first_col=first.split(",")[0].strip().lower()
            header=not first_col.replace("-","").isdigit()
            d=pd.read_csv(raw,header=0 if header else None)
    if not header:
        names0=["calc_time","funding_interval_hours","last_funding_rate","mark_price"]
        d=d.iloc[:,:min(len(names0),d.shape[1])]; d.columns=names0[:d.shape[1]]
    d.columns=[str(c).strip().lower().replace(" ","_") for c in d.columns]
    tc=next((c for c in d.columns if "time" in c and "interval" not in c),d.columns[0])
    rc=next((c for c in d.columns if "funding" in c and "time" not in c and "interval" not in c),None)
    if rc is None: rc=d.columns[-1]
    unit=infer_unit(d[tc])
    return pd.DataFrame({"timestamp":pd.to_datetime(pd.to_numeric(d[tc],errors="raise"),unit=unit,utc=True),"funding_rate":pd.to_numeric(d[rc],errors="raise")}).dropna().sort_values("timestamp").drop_duplicates("timestamp")


def agg_second_chunks(path: Path) -> Iterable[pd.DataFrame]:
    """Yield exact aggregate-trade seconds with chunk-boundary identity preserved."""
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError(f"expected one CSV, got {names}")
        with zf.open(names[0]) as stream:
            first = stream.readline().decode("utf-8", "replace")
            stream.seek(0)
            first_col = first.split(",")[0].strip().lower()
            header = first_col in {"agg_trade_id", "aggregate_trade_id", "a"}
            reader = pd.read_csv(
                stream,
                header=0 if header else None,
                names=None if header else AGG_COLUMNS,
                chunksize=CHUNK_ROWS,
                low_memory=False,
            )
            carry: pd.DataFrame | None = None
            for df in reader:
                df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
                aliases = {
                    "a": "agg_trade_id", "p": "price", "q": "quantity", "f": "first_trade_id",
                    "l": "last_trade_id", "t": "transact_time", "m": "is_buyer_maker",
                    "aggregate_trade_id": "agg_trade_id", "transact_time_ms": "transact_time",
                }
                df = df.rename(columns={c: aliases.get(c, c) for c in df.columns})
                if "transact_time" not in df.columns:
                    df.columns = AGG_COLUMNS[: len(df.columns)]
                tnum = pd.to_numeric(df["transact_time"], errors="raise")
                unit = infer_unit(tnum)
                ns = pd.to_datetime(tnum, unit=unit, utc=True).astype("datetime64[ns, UTC]").astype("int64").to_numpy()
                sec = ns // 1_000_000_000
                price = pd.to_numeric(df["price"], errors="raise").to_numpy(np.float64)
                qty = pd.to_numeric(df["quantity"], errors="raise").to_numpy(np.float64)
                qv = price * qty
                maker = df["is_buyer_maker"].astype(str).str.lower().isin(["true", "1", "t"]).to_numpy()
                first_id = pd.to_numeric(df["first_trade_id"], errors="coerce").to_numpy(np.float64)
                last_id = pd.to_numeric(df["last_trade_id"], errors="coerce").to_numpy(np.float64)
                raw_count = np.where(np.isfinite(first_id) & np.isfinite(last_id), last_id - first_id + 1.0, 1.0)
                z = pd.DataFrame({
                    "sec": sec,
                    "price": price,
                    "qv": qv,
                    "signed": np.where(maker, -qv, qv),
                    "raw_count": raw_count,
                })
                if carry is not None:
                    z = pd.concat([carry, z], ignore_index=True)
                last_sec = int(z.sec.iloc[-1])
                ready = z[z.sec != last_sec]
                carry = z[z.sec == last_sec].copy()
                if len(ready):
                    yield ready.groupby("sec", sort=True).agg(
                        open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last"),
                        quote_volume=("qv", "sum"), signed_quote=("signed", "sum"), agg_count=("price", "size"), raw_count=("raw_count", "sum"),
                    ).reset_index()
            if carry is not None and len(carry):
                yield carry.groupby("sec", sort=True).agg(
                    open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last"),
                    quote_volume=("qv", "sum"), signed_quote=("signed", "sum"), agg_count=("price", "size"), raw_count=("raw_count", "sum"),
                ).reset_index()


def _bar_boundaries(sec: np.ndarray, quote: np.ndarray, threshold: float, cap_seconds: int, day_end: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(sec)
    if n == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.int8)
    cs = np.cumsum(quote, dtype=np.float64)
    starts: list[int] = []
    ends: list[int] = []
    decisions: list[int] = []
    reasons: list[int] = []
    i = 0
    while i < n:
        start_sec = int(sec[i])
        cap_pos = int(np.searchsorted(sec, start_sec + cap_seconds, side="left"))
        base = float(cs[i - 1]) if i else 0.0
        vol_pos = int(np.searchsorted(cs, base + threshold, side="left")) if math.isfinite(threshold) and threshold > 0 else n
        if vol_pos < cap_pos and vol_pos < n:
            end = vol_pos + 1
            decision = int(sec[vol_pos]) + 1
            reason = 1
        elif cap_pos < n:
            end = cap_pos
            decision = start_sec + cap_seconds
            reason = 2
        else:
            end = n
            decision = day_end
            reason = 0
        if end <= i:
            raise RuntimeError((i, end, start_sec, cap_pos, vol_pos))
        starts.append(i); ends.append(end); decisions.append(decision); reasons.append(reason)
        i = end
    return np.asarray(starts, np.int64), np.asarray(ends, np.int64), np.asarray(decisions, np.int64), np.asarray(reasons, np.int8)


def build_day_bars(day_seconds: pd.DataFrame, threshold: float, bars_per_day: int, cap_seconds: int) -> pd.DataFrame:
    if day_seconds.empty:
        return pd.DataFrame()
    d = day_seconds.sort_values("sec", kind="mergesort").drop_duplicates("sec", keep="last").reset_index(drop=True)
    sec = d.sec.to_numpy(np.int64)
    day_start = int(sec[0] // 86400 * 86400)
    day_end = day_start + 86400
    starts, ends, decisions, reasons = _bar_boundaries(sec, d.quote_volume.to_numpy(np.float64), threshold, cap_seconds, day_end)
    if not len(starts):
        return pd.DataFrame()
    op = d.open.to_numpy(np.float64); hi = d.high.to_numpy(np.float64); lo = d.low.to_numpy(np.float64); cl = d.close.to_numpy(np.float64)
    qv = d.quote_volume.to_numpy(np.float64); signed = d.signed_quote.to_numpy(np.float64)
    ac = d.agg_count.to_numpy(np.float64); rc = d.raw_count.to_numpy(np.float64)
    qcs = np.r_[0.0, np.cumsum(qv)]; scs = np.r_[0.0, np.cumsum(signed)]
    acs = np.r_[0.0, np.cumsum(ac)]; rcs = np.r_[0.0, np.cumsum(rc)]
    out = pd.DataFrame({
        "start_time": pd.to_datetime(sec[starts], unit="s", utc=True),
        "decision_time": pd.to_datetime(decisions, unit="s", utc=True),
        "open": op[starts],
        "high": np.maximum.reduceat(hi, starts),
        "low": np.minimum.reduceat(lo, starts),
        "close": cl[ends - 1],
        "quote_volume": qcs[ends] - qcs[starts],
        "signed_quote": scs[ends] - scs[starts],
        "agg_count": acs[ends] - acs[starts],
        "raw_count": rcs[ends] - rcs[starts],
        "duration_seconds": decisions - sec[starts],
        "close_reason": reasons,
        "bars_per_day_target": np.int16(bars_per_day),
        "clock_cap_seconds": np.int16(cap_seconds),
    })
    out["imbalance"] = out.signed_quote / out.quote_volume.replace(0.0, np.nan)
    return out


def resolve_first_prices(rows: pd.DataFrame, sec: np.ndarray, price: np.ndarray) -> pd.DataFrame:
    out = rows.copy()
    decision = out.decision_time.astype("int64").to_numpy() // 1_000_000_000
    pos = np.searchsorted(sec, decision, side="left")
    valid = pos < len(sec)
    entry_sec = np.full(len(out), -1, np.int64); entry_px = np.full(len(out), np.nan)
    entry_sec[valid] = sec[pos[valid]]; entry_px[valid] = price[pos[valid]]
    out["entry_time"] = pd.to_datetime(entry_sec, unit="s", utc=True, errors="coerce")
    out["entry_price"] = entry_px
    out["entry_delay_seconds"] = np.where(valid, entry_sec - decision, np.nan)
    for horizon in EXIT_HORIZONS_SECONDS:
        target = entry_sec + horizon
        xp = np.searchsorted(sec, target, side="left")
        ok = valid & (xp < len(sec))
        xt = np.full(len(out), -1, np.int64); px = np.full(len(out), np.nan)
        xt[ok] = sec[xp[ok]]; px[ok] = price[xp[ok]]
        out[f"exit_time_{horizon}s"] = pd.to_datetime(xt, unit="s", utc=True, errors="coerce")
        out[f"exit_price_{horizon}s"] = px
        out[f"exit_delay_seconds_{horizon}s"] = np.where(ok, xt - target, np.nan)
    return out


def run(symbol: str, year: int, output: Path, cache: Path) -> int:
    last_month = 12
    output.mkdir(parents=True, exist_ok=True); cache.mkdir(parents=True, exist_ok=True)
    session = requests.Session(); session.headers["User-Agent"] = "SMC-DV-AII-ultrafast/1.0"
    periods = [pd.Period(f"{year-1}-12", freq="M")] + list(pd.period_range(f"{year}-01", f"{year}-{last_month:02d}", freq="M"))
    kline_records=[]; daily_parts=[]; minute_parts=[]
    for period in periods:
        stamp=str(period); name=f"{symbol}-1m-{stamp}.zip"; url=f"{BASE}/klines/{symbol}/1m/{name}"
        path,rec=download_verified(session,url,cache/symbol/"klines"); rec["period"]=stamp; kline_records.append(rec)
        k=parse_kline(path); daily_parts.append(k.set_index("timestamp").quote_volume.resample("1D").sum(min_count=1))
        if period.year == year: minute_parts.append(k)
        print("KLINE",symbol,stamp,rec["bytes"],flush=True)
    daily=pd.concat(daily_parts).groupby(level=0).sum().sort_index()
    trailing=daily.shift(1).rolling(20,min_periods=10).median()
    threshold_maps={bpd:trailing/bpd for bpd,_ in CONFIGS}
    minute=pd.concat(minute_parts,ignore_index=True).sort_values("timestamp",kind="mergesort").drop_duplicates("timestamp")
    minute=minute[(minute.timestamp>=pd.Timestamp(f"{year}-01-01",tz="UTC"))&(minute.timestamp<pd.Timestamp(f"{year+1}-01-01",tz="UTC"))]
    minute_path=output/f"{symbol}_{year}_1m.parquet"; minute.to_parquet(minute_path,index=False,compression="zstd")
    funding_records=[]; funding_parts=[]
    for month in range(1,last_month+1):
        stamp=f"{year}-{month:02d}"; name=f"{symbol}-fundingRate-{stamp}.zip"; url=f"{BASE}/fundingRate/{symbol}/{name}"
        path,rec=download_verified(session,url,cache/symbol/"fundingRate"); rec["period"]=stamp; funding_records.append(rec); funding_parts.append(parse_funding(path))
    funding=pd.concat(funding_parts,ignore_index=True).sort_values("timestamp").drop_duplicates("timestamp") if funding_parts else pd.DataFrame(columns=["timestamp","funding_rate"])
    funding=funding[(funding.timestamp>=pd.Timestamp(f"{year}-01-01",tz="UTC"))&(funding.timestamp<pd.Timestamp(f"{year+1}-01-01",tz="UTC"))]
    funding_path=output/f"{symbol}_{year}_funding.parquet"; funding.to_parquet(funding_path,index=False,compression="zstd")

    bars={cfg:[] for cfg in CONFIGS}; agg_records=[]; all_sec=[]; all_open=[]
    for month in range(1,last_month+1):
        stamp=f"{year}-{month:02d}"; name=f"{symbol}-aggTrades-{stamp}.zip"; url=f"{BASE}/aggTrades/{symbol}/{name}"
        path,rec=download_verified(session,url,cache/symbol/"aggTrades"); rec["period"]=stamp; agg_records.append(rec)
        parts=list(agg_second_chunks(path)); seconds=pd.concat(parts,ignore_index=True).sort_values("sec",kind="mergesort").reset_index(drop=True)
        if seconds.duplicated("sec").any():
            seconds=seconds.groupby("sec",sort=True).agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),quote_volume=("quote_volume","sum"),signed_quote=("signed_quote","sum"),agg_count=("agg_count","sum"),raw_count=("raw_count","sum")).reset_index()
        all_sec.append(seconds.sec.to_numpy(np.int64)); all_open.append(seconds.open.to_numpy(np.float64))
        days=(seconds.sec.to_numpy(np.int64)//86400)
        for day_id in np.unique(days):
            mask=days==day_id; dd=seconds.loc[mask]
            day=pd.Timestamp(int(day_id*86400),unit="s",tz="UTC")
            for cfg in CONFIGS:
                bpd,cap=cfg; threshold=float(threshold_maps[bpd].get(day,np.nan))
                x=build_day_bars(dd,threshold,bpd,cap)
                if len(x): bars[cfg].append(x)
        print("AGG",symbol,stamp,rec["bytes"],"seconds",len(seconds),flush=True)
        del parts,seconds
        path.unlink(missing_ok=True); (path.parent/f"{path.name}.CHECKSUM").unlink(missing_ok=True)
    sec=np.concatenate(all_sec); px=np.concatenate(all_open); order=np.argsort(sec,kind="stable"); sec=sec[order]; px=px[order]
    keep=np.r_[sec[1:]!=sec[:-1],True]; sec=sec[keep]; px=px[keep]
    outputs=[]
    start=pd.Timestamp(f"{year}-01-01",tz="UTC"); end=pd.Timestamp(f"{year+1}-01-01",tz="UTC")
    for cfg in CONFIGS:
        bpd,cap=cfg; df=pd.concat(bars[cfg],ignore_index=True).sort_values("start_time",kind="mergesort")
        df=df[(df.start_time>=start)&(df.start_time<end)].reset_index(drop=True)
        df=resolve_first_prices(df,sec,px); df["symbol"]=symbol
        path=output/f"{symbol}_{year}_bpd{bpd}_cap{cap}s.parquet"; df.to_parquet(path,index=False,compression="zstd")
        outputs.append({"bars_per_day":bpd,"cap_seconds":cap,"rows":len(df),"threshold_rows":int((df.close_reason==1).sum()),"clock_rows":int((df.close_reason==2).sum()),"residual_rows":int((df.close_reason==0).sum()),"entry_missing":int(df.entry_price.isna().sum()),"entry_delay_gt2s":int((df.entry_delay_seconds>2).sum()),"path":str(path),"bytes":path.stat().st_size,"sha256":sha256_file(path)})
    manifest={"schema_version":2,"study_data":"dollar_volume_aggressor_imbalance","symbol":symbol,"year":year,"end_exclusive":end.isoformat(),"threshold_rule":"20 completed UTC-day median quote volume divided by target bars/day","decision_rule":"complete threshold-crossing aggregate-trade second or causal clock cap","entry_rule":"first actual aggregate-trade second at/after decision; delay explicitly recorded","exit_reference":"first actual aggregate-trade second at/after fixed economic horizon; delay explicitly recorded","configs":[list(x) for x in CONFIGS],"exit_horizons_seconds":list(EXIT_HORIZONS_SECONDS),"kline_archives":kline_records,"aggtrade_archives":agg_records,"funding_archives":funding_records,"minute":{"path":str(minute_path),"rows":len(minute),"sha256":sha256_file(minute_path)},"funding":{"path":str(funding_path),"rows":len(funding),"sha256":sha256_file(funding_path)},"outputs":outputs,"candidate_pnl_observed":False,"orders_submitted":False,"production_enabled":False}
    mp=output/f"{symbol}_{year}_manifest.json"; mp.write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"symbol":symbol,"year":year,"outputs":outputs},indent=2),flush=True)
    return 0


def synthetic_test() -> None:
    rng=np.random.default_rng(2407); sec=np.arange(1000,1000+3600,dtype=np.int64); sec=sec[rng.random(len(sec))>.08]
    price=100*np.exp(np.cumsum(rng.normal(0,1e-4,len(sec)))); qv=rng.lognormal(8,1,len(sec)); signed=qv*rng.uniform(-1,1,len(sec))
    d=pd.DataFrame({"sec":sec,"open":price,"high":price*(1+rng.random(len(sec))*2e-4),"low":price*(1-rng.random(len(sec))*2e-4),"close":price,"quote_volume":qv,"signed_quote":signed,"agg_count":1.0,"raw_count":1.0})
    x=build_day_bars(d,threshold=float(np.median(qv)*20),bars_per_day=72,cap_seconds=600)
    assert len(x)>0 and x.start_time.is_monotonic_increasing and (x.quote_volume>0).all()
    assert x.close_reason.isin([0,1,2]).all()
    print(json.dumps({"synthetic_bars":len(x),"status":"PASS"}))


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--symbol"); ap.add_argument("--year",type=int); ap.add_argument("--output",type=Path); ap.add_argument("--cache",type=Path,default=Path(".cache/dv-aii")); ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test: synthetic_test(); return 0
    if not args.symbol or not args.year or not args.output: ap.error("--symbol, --year, --output required")
    return run(args.symbol.upper(),args.year,args.output,args.cache)


if __name__ == "__main__":
    raise SystemExit(main())
