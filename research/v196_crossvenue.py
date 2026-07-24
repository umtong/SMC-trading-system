from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

BYBIT_ROOT = "https://public.bybit.com/trading"
BINANCE_ROOT = "https://data.binance.vision/data/futures/um/daily/aggTrades"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
BIN_MS = 100
HORIZONS_MS = (100, 200, 500, 1_000, 2_000, 5_000)
COSTS_BPS = (4.0, 6.0, 8.0, 12.0, 18.0)
VERSION = "V1.97_CAUSAL_100MS_CROSS_VENUE"


def fetch(url: str, attempts: int = 6, timeout: int = 300) -> bytes:
    err: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "smc-v197-crossvenue/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            err = exc
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, 20))
    raise RuntimeError(f"fetch failed {url}: {err!r}")


def _bin_trades(time_ms: np.ndarray, price: np.ndarray, quote: np.ndarray, signed: np.ndarray, start_ms: int, n: int, prefix: str) -> pd.DataFrame:
    ix = ((time_ms - start_ms) // BIN_MS).astype(np.int64)
    use = (ix >= 0) & (ix < n) & np.isfinite(price) & np.isfinite(quote) & np.isfinite(signed)
    d = pd.DataFrame({"i": ix[use], "price": price[use], "quote": quote[use], "signed": signed[use]})
    g = d.groupby("i", sort=True).agg(price=("price", "last"), quote=("quote", "sum"), signed=("signed", "sum"), count=("price", "size"))
    g.columns = [prefix + c for c in g.columns]
    return g


def parse_binance(symbol: str, day: str) -> tuple[pd.DataFrame, dict, np.ndarray, np.ndarray]:
    name = f"{symbol}-aggTrades-{day}.zip"
    url = f"{BINANCE_ROOT}/{symbol}/{name}"
    checksum = fetch(url + ".CHECKSUM").decode("utf-8-sig").strip().split()[0].lower()
    payload = fetch(url)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != checksum:
        raise ValueError("Binance checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [n for n in archive.namelist() if n.endswith(".csv")]
        if len(members) != 1:
            raise ValueError("unexpected Binance archive")
        raw = pd.read_csv(archive.open(members[0]), header=None, low_memory=False)
    if pd.isna(pd.to_numeric(raw.iloc[0, 0], errors="coerce")):
        raw = raw.iloc[1:].copy()
    raw = raw.iloc[:, :7]
    raw.columns = ["agg_id", "price", "quantity", "first_id", "last_id", "time", "buyer_maker"]
    for c in ("price", "quantity", "time"):
        raw[c] = pd.to_numeric(raw[c], errors="raise")
    t = raw.time.to_numpy(np.int64)
    if int(np.nanmax(np.abs(t))) >= 10**14:
        t = t // 1_000
    price = raw.price.to_numpy(float)
    quote = price * raw.quantity.to_numpy(float)
    maker = raw.buyer_maker.astype(str).str.lower().isin(["true", "1"]).to_numpy(bool)
    signed = np.where(maker, -quote, quote)
    order = np.argsort(t, kind="stable")
    t, price, quote, signed = t[order], price[order], quote[order], signed[order]
    meta = {"url": url, "sha256": actual, "archive_bytes": len(payload), "raw_rows": len(raw)}
    return pd.DataFrame({"time_ms": t, "price": price, "quote": quote, "signed": signed}), meta, t, price


def parse_bybit(symbol: str, day: str) -> tuple[pd.DataFrame, dict]:
    name = f"{symbol}{day}.csv.gz"
    url = f"{BYBIT_ROOT}/{symbol}/{name}"
    payload = fetch(url)
    actual = hashlib.sha256(payload).hexdigest()
    raw = pd.read_csv(io.BytesIO(gzip.decompress(payload)), low_memory=False)
    required = {"timestamp", "side", "size", "price"}
    if not required.issubset(raw.columns):
        raise ValueError(f"Bybit columns {raw.columns}")
    for c in ("timestamp", "size", "price"):
        raw[c] = pd.to_numeric(raw[c], errors="raise")
    t = np.rint(raw.timestamp.to_numpy(float) * 1_000.0).astype(np.int64)
    price = raw.price.to_numpy(float)
    quote = raw["foreignNotional"].to_numpy(float) if "foreignNotional" in raw else price * raw["size"].to_numpy(float)
    signed = np.where(raw.side.astype(str).str.lower().eq("buy").to_numpy(), quote, -quote)
    order = np.argsort(t, kind="stable")
    d = pd.DataFrame({"time_ms": t[order], "price": price[order], "quote": quote[order], "signed": signed[order]})
    return d, {"url": url, "sha256": actual, "archive_bytes": len(payload), "raw_rows": len(raw)}


def causal_z(s: pd.Series, window: int, minp: int | None = None) -> pd.Series:
    minp = window if minp is None else minp
    mean = s.rolling(window, min_periods=minp).mean().shift(1)
    std = s.rolling(window, min_periods=minp).std(ddof=0).shift(1).replace(0.0, np.nan)
    return (s - mean) / std


def build_day(symbol: str, day: str, out: Path) -> None:
    braw, bm, bt, bp = parse_binance(symbol, day)
    yraw, ym = parse_bybit(symbol, day)
    start = pd.Timestamp(day, tz="UTC")
    start_ms = int(start.timestamp() * 1_000)
    n = 86_400_000 // BIN_MS
    frame = pd.DataFrame(index=pd.RangeIndex(n, name="bin"))
    frame = frame.join(_bin_trades(braw.time_ms.to_numpy(), braw.price.to_numpy(), braw.quote.to_numpy(), braw.signed.to_numpy(), start_ms, n, "binance_"))
    frame = frame.join(_bin_trades(yraw.time_ms.to_numpy(), yraw.price.to_numpy(), yraw.quote.to_numpy(), yraw.signed.to_numpy(), start_ms, n, "bybit_"))
    for venue in ("binance", "bybit"):
        frame[f"{venue}_price"] = frame[f"{venue}_price"].ffill()
        for c in ("quote", "signed", "count"):
            frame[f"{venue}_{c}"] = frame[f"{venue}_{c}"].fillna(0.0)
    frame = frame.dropna(subset=["binance_price", "bybit_price"]).copy()
    frame["bybit_imb"] = frame.bybit_signed / frame.bybit_quote.replace(0.0, np.nan)
    frame["binance_imb"] = frame.binance_signed / frame.binance_quote.replace(0.0, np.nan)
    frame[["bybit_imb", "binance_imb"]] = frame[["bybit_imb", "binance_imb"]].fillna(0.0)
    frame["flow_gap"] = frame.bybit_imb - frame.binance_imb
    frame["price_gap_bps"] = np.log(frame.bybit_price / frame.binance_price) * 1e4
    for width, label in ((2, "200"), (5, "500"), (10, "1000")):
        frame[f"bybit_ret_{label}_bps"] = np.log(frame.bybit_price / frame.bybit_price.shift(width)) * 1e4
        frame[f"binance_ret_{label}_bps"] = np.log(frame.binance_price / frame.binance_price.shift(width)) * 1e4
        frame[f"lead_ret_{label}_bps"] = frame[f"bybit_ret_{label}_bps"] - frame[f"binance_ret_{label}_bps"]
    frame["bybit_flow_z"] = causal_z(frame.bybit_imb, 600, 300)
    frame["flow_gap_z"] = causal_z(frame.flow_gap, 600, 300)
    frame["price_gap_z"] = causal_z(frame.price_gap_bps, 600, 300)
    frame["bybit_ret_z"] = causal_z(frame.bybit_ret_500_bps, 600, 300)
    frame["lead_ret_z"] = causal_z(frame.lead_ret_500_bps, 600, 300)
    frame["bybit_volume_z"] = causal_z(np.log1p(frame.bybit_quote), 600, 300)
    zcols = ["bybit_flow_z", "flow_gap_z", "price_gap_z", "bybit_ret_z", "lead_ret_z"]
    raw_mask = frame[zcols].abs().max(axis=1).ge(1.25) & np.isfinite(frame[zcols]).all(axis=1)
    raw_idx = frame.index.to_numpy(np.int64)[raw_mask.to_numpy()]
    keep: list[int] = []
    last = -10_000
    last_sign = 0
    strength = frame.loc[raw_idx, zcols].abs().max(axis=1).to_numpy()
    lead_sign = np.sign(frame.loc[raw_idx, "lead_ret_z"].to_numpy())
    for k, sgn, st in zip(raw_idx, lead_sign, strength):
        if k - last >= 5 or int(sgn) != last_sign or st >= 3.0:
            keep.append(int(k)); last = int(k); last_sign = int(sgn)
    cand = frame.loc[keep].copy()
    signal_end_ms = start_ms + (cand.index.to_numpy(np.int64) + 1) * BIN_MS
    entry_ix = np.searchsorted(bt, signal_end_ms, side="left")
    valid = entry_ix < len(bt)
    cand = cand.iloc[np.flatnonzero(valid)].copy()
    signal_end_ms = signal_end_ms[valid]
    entry_ix = entry_ix[valid]
    entry_time_ms = bt[entry_ix]
    entry_price = bp[entry_ix]
    cand["signal_end_ms"] = signal_end_ms
    cand["entry_time_ms"] = entry_time_ms
    cand["entry_price"] = entry_price
    for h in HORIZONS_MS:
        ex_ix = np.searchsorted(bt, entry_time_ms + h, side="left")
        ok = ex_ix < len(bt)
        fwd = np.full(len(cand), np.nan)
        fwd[ok] = np.log(bp[ex_ix[ok]] / entry_price[ok])
        cand[f"fwd_{h}ms"] = fwd
    keep_cols = [
        "signal_end_ms", "entry_time_ms", "entry_price", "bybit_imb", "binance_imb", "flow_gap",
        "price_gap_bps", "bybit_ret_200_bps", "binance_ret_200_bps", "lead_ret_200_bps",
        "bybit_ret_500_bps", "binance_ret_500_bps", "lead_ret_500_bps",
        "bybit_ret_1000_bps", "binance_ret_1000_bps", "lead_ret_1000_bps",
        "bybit_flow_z", "flow_gap_z", "price_gap_z", "bybit_ret_z", "lead_ret_z", "bybit_volume_z",
    ] + [f"fwd_{h}ms" for h in HORIZONS_MS]
    cand = cand[keep_cols].dropna().copy()
    cand["symbol"] = symbol
    cand["day"] = day
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{symbol}_{day}_100ms.csv.gz"
    cand.to_csv(path, index=False, compression={"method": "gzip", "compresslevel": 6, "mtime": 0})
    manifest = {
        "version": VERSION, "symbol": symbol, "day": day, "rows": len(cand),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "binance": bm, "bybit": ym,
        "decision_bin_ms": BIN_MS, "entry": "first Binance aggregate trade at or after completed signal bin end",
        "future_returns_used_in_signal": False,
    }
    (out / f"{symbol}_{day}_100ms.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest))


def greedy(entry_ms: np.ndarray, exit_ms: np.ndarray) -> np.ndarray:
    out: list[int] = []
    free = -10**30
    for i, (entry, exit_) in enumerate(zip(entry_ms, exit_ms)):
        if entry >= free:
            out.append(i); free = exit_
    return np.asarray(out, dtype=int)


def trim_top_bps(values: np.ndarray, n: int) -> float:
    if len(values) <= n:
        return float("nan")
    return float(np.sort(values)[:-n].mean() * 1e4)


def metric(rets: np.ndarray, days: int) -> dict:
    if len(rets) == 0:
        return {"trades": 0, "net_return": 0.0, "gday": 0.0, "max_drawdown": 0.0, "profit_factor": 0.0, "avg_net_bps": 0.0, "top10_bps": np.nan, "top20_bps": np.nan}
    eq = np.cumprod(1 + np.maximum(rets, -0.999))
    curve = np.r_[1.0, eq]; peak = np.maximum.accumulate(curve); dd = 1 - curve / peak
    pos = rets[rets > 0].sum(); neg = -rets[rets < 0].sum()
    return {
        "trades": int(len(rets)), "net_return": float(eq[-1] - 1),
        "gday": float(np.exp(np.log(eq[-1]) / days) - 1), "max_drawdown": float(dd.max()),
        "profit_factor": float(pos / neg) if neg > 0 else 999.0, "avg_net_bps": float(rets.mean() * 1e4),
        "top10_bps": trim_top_bps(rets, 10), "top20_bps": trim_top_bps(rets, 20),
    }


def aggregate(input_dir: Path, output_dir: Path) -> None:
    files = sorted(input_dir.rglob("*_100ms.csv.gz"))
    frames = [pd.read_csv(p) for p in files]
    panel = pd.concat(frames, ignore_index=True).sort_values(["entry_time_ms", "symbol"], kind="mergesort")
    panel["year"] = pd.to_datetime(panel.entry_time_ms, unit="ms", utc=True).dt.year
    rules: list[dict] = []
    families = ("bybit_flow", "flow_gap", "price_gap_cont", "price_gap_conv", "bybit_impulse", "lead_impulse")
    for family in families:
        if family == "bybit_flow": raw = panel.bybit_flow_z.to_numpy(); base_dir = np.sign(raw)
        elif family == "flow_gap": raw = panel.flow_gap_z.to_numpy(); base_dir = np.sign(raw)
        elif family == "price_gap_cont": raw = panel.price_gap_z.to_numpy(); base_dir = np.sign(raw)
        elif family == "price_gap_conv": raw = panel.price_gap_z.to_numpy(); base_dir = -np.sign(raw)
        elif family == "bybit_impulse": raw = panel.bybit_ret_z.to_numpy(); base_dir = np.sign(raw)
        else: raw = panel.lead_ret_z.to_numpy(); base_dir = np.sign(raw)
        for z in (1.5, 2.0, 2.5, 3.0):
            threshold = np.abs(raw) >= z
            for confirm in ("none", "price_same", "binance_lag", "flow_same"):
                mask = threshold & (base_dir != 0)
                if confirm == "price_same": mask &= np.sign(panel.bybit_ret_500_bps.to_numpy()) == base_dir
                elif confirm == "binance_lag":
                    br = panel.bybit_ret_500_bps.to_numpy(); nr = panel.binance_ret_500_bps.to_numpy()
                    mask &= (np.sign(br) == base_dir) & (np.abs(nr) <= 0.5 * np.abs(br))
                elif confirm == "flow_same": mask &= np.sign(panel.bybit_imb.to_numpy()) == base_dir
                idx = np.flatnonzero(mask)
                if not len(idx): continue
                score = np.abs(raw[idx]) + np.maximum(base_dir[idx] * panel.bybit_ret_z.to_numpy()[idx], 0.0)
                order = np.lexsort((panel.symbol.to_numpy()[idx], -score, panel.entry_time_ms.to_numpy()[idx]))
                idx = idx[order]
                for h in HORIZONS_MS:
                    chosen = greedy(panel.entry_time_ms.to_numpy(np.int64)[idx], panel.entry_time_ms.to_numpy(np.int64)[idx] + h)
                    use_idx = idx[chosen]
                    gross = base_dir[use_idx] * panel[f"fwd_{h}ms"].to_numpy()[use_idx]
                    years = panel.year.to_numpy()[use_idx]
                    for cost in COSTS_BPS:
                        net = gross - cost / 1e4
                        rec = {"config": f"{family}_z{z}_{confirm}_h{h}_c{int(cost)}", "family": family, "z": z, "confirm": confirm, "horizon_ms": h, "cost_bps": cost}
                        for year, days in ((2022, 6), (2023, 6)):
                            m = years == year
                            rec.update({f"{k}_{year}": v for k, v in metric(net[m], days).items()})
                        rules.append(rec)
    r = pd.DataFrame(rules)
    r["min_gday"] = r[["gday_2022", "gday_2023"]].min(axis=1)
    r["min_trades"] = r[["trades_2022", "trades_2023"]].min(axis=1)
    r["max_dd"] = r[["max_drawdown_2022", "max_drawdown_2023"]].max(axis=1)
    r["min_top20_bps"] = r[["top20_bps_2022", "top20_bps_2023"]].min(axis=1)
    r = r.sort_values(["min_gday", "min_top20_bps", "max_dd"], ascending=[False, False, True])
    output_dir.mkdir(parents=True, exist_ok=True)
    r.to_csv(output_dir / "screen_100ms.csv", index=False)
    eligible = r[(r.cost_bps >= 12) & (r.min_trades >= 100) & (r.net_return_2022 > 0) & (r.net_return_2023 > 0) & (r.profit_factor_2022 >= 1.1) & (r.profit_factor_2023 >= 1.1) & (r.top20_bps_2022 > 0) & (r.top20_bps_2023 > 0) & (r.max_dd < 0.30)]
    eligible.to_csv(output_dir / "eligible_100ms.csv", index=False)
    target = eligible[(eligible.gday_2022 >= 0.01) & (eligible.gday_2023 >= 0.01)]
    summary = {
        "version": VERSION, "files": len(files), "rows": len(panel), "screened": len(r), "eligible": len(eligible),
        "target_1pct_both_years": len(target), "best": r.head(30).replace([np.inf, -np.inf, np.nan], None).to_dict("records"),
        "orders_submitted": False, "paper_or_live_started": False,
    }
    (output_dir / "summary_100ms.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("day"); d.add_argument("--symbol", required=True); d.add_argument("--day", required=True); d.add_argument("--out", type=Path, required=True)
    a = sub.add_parser("aggregate"); a.add_argument("--input", type=Path, required=True); a.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == "day": build_day(args.symbol, args.day, args.out)
    else: aggregate(args.input, args.out)


if __name__ == "__main__":
    main()
