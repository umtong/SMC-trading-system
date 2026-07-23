from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from huggingface_hub import snapshot_download

REPO_ID = "Mindbyte-89/btcusdt-microbar-v2"
STREAMS = ("trades", "book_ticks", "depth", "liquidations", "mark_price")


@dataclass(frozen=True)
class Policy:
    family: str
    liq_z: float
    confirm_s: int
    depth_ratio: float
    flow_z: float
    reclaim_bps: float
    horizon_s: int


def trim(x: np.ndarray, n: int) -> float:
    x = x[np.isfinite(x)]
    return float(np.sort(x)[:-n].mean()) if x.size > n else math.nan


def prior_z(x: pd.Series, window: int = 1800, minp: int = 300) -> pd.Series:
    p = x.shift(1)
    return (x - p.rolling(window, min_periods=minp).mean()) / p.rolling(window, min_periods=minp).std(ddof=0).replace(0, np.nan)


def read_many(paths: Iterable[Path], columns: list[str] | None = None) -> pd.DataFrame:
    frames = []
    for path in sorted(paths):
        try:
            frames.append(pd.read_parquet(path, columns=columns))
        except Exception:
            frames.append(pd.read_parquet(path))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def dates_under(root: Path, stream: str) -> set[str]:
    base = root / stream
    if not base.exists():
        return set()
    return {p.name for p in base.iterdir() if p.is_dir() and len(p.name) == 10}


def aggregate_day(root: Path, day: str) -> pd.DataFrame:
    trades = read_many((root / "trades" / day).glob("*.parquet"))
    books = read_many((root / "book_ticks" / day).glob("*.parquet"))
    depth = read_many((root / "depth" / day).glob("*.parquet"))
    liquid = read_many((root / "liquidations" / day).glob("*.parquet"))
    mark = read_many((root / "mark_price" / day).glob("*.parquet"))
    if books.empty or depth.empty:
        return pd.DataFrame()

    for frame in (trades, books, depth, liquid, mark):
        if not frame.empty:
            frame["sec"] = (pd.to_numeric(frame["timestamp_ms"], errors="coerce") // 1000).astype("Int64")
            frame.dropna(subset=["sec"], inplace=True)
            frame["sec"] = frame["sec"].astype(np.int64)

    b = books.sort_values("timestamp_ms").groupby("sec", sort=True).tail(1).set_index("sec")
    b = b[["bid_price", "bid_qty", "ask_price", "ask_qty"]]

    d = depth.sort_values("timestamp_ms").groupby("sec", sort=True).tail(1).set_index("sec")
    bid_qty_cols = [f"bid_qty_{i}" for i in range(5) if f"bid_qty_{i}" in d]
    ask_qty_cols = [f"ask_qty_{i}" for i in range(5) if f"ask_qty_{i}" in d]
    bid_price_cols = [f"bid_price_{i}" for i in range(5) if f"bid_price_{i}" in d]
    ask_price_cols = [f"ask_price_{i}" for i in range(5) if f"ask_price_{i}" in d]
    d2 = pd.DataFrame(index=d.index)
    d2["bid_depth_quote"] = sum(pd.to_numeric(d[q], errors="coerce") * pd.to_numeric(d[p], errors="coerce") for q, p in zip(bid_qty_cols, bid_price_cols))
    d2["ask_depth_quote"] = sum(pd.to_numeric(d[q], errors="coerce") * pd.to_numeric(d[p], errors="coerce") for q, p in zip(ask_qty_cols, ask_price_cols))

    start = max(int(b.index.min()), int(d2.index.min()))
    end = min(int(b.index.max()), int(d2.index.max()))
    idx = pd.Index(np.arange(start, end + 1, dtype=np.int64), name="sec")
    x = pd.DataFrame(index=idx).join(b).join(d2)
    # Quotes/depth are usable only when refreshed recently. Later rows older than 2 s are invalidated.
    quote_seen = pd.Series(b.index, index=b.index).reindex(idx).ffill()
    depth_seen = pd.Series(d2.index, index=d2.index).reindex(idx).ffill()
    x = x.ffill()
    x.loc[(idx.to_numpy() - quote_seen.to_numpy()) > 2, ["bid_price", "bid_qty", "ask_price", "ask_qty"]] = np.nan
    x.loc[(idx.to_numpy() - depth_seen.to_numpy()) > 2, ["bid_depth_quote", "ask_depth_quote"]] = np.nan

    if trades.empty:
        x["buy_quote"] = 0.0
        x["sell_quote"] = 0.0
    else:
        q = pd.to_numeric(trades["price"], errors="coerce") * pd.to_numeric(trades["quantity"], errors="coerce")
        buy = q.where(~trades["is_buyer_maker"].astype(bool), 0.0).groupby(trades["sec"]).sum()
        sell = q.where(trades["is_buyer_maker"].astype(bool), 0.0).groupby(trades["sec"]).sum()
        x["buy_quote"] = buy.reindex(idx, fill_value=0.0)
        x["sell_quote"] = sell.reindex(idx, fill_value=0.0)

    x["liq_buy_quote"] = 0.0
    x["liq_sell_quote"] = 0.0
    if not liquid.empty:
        lq = pd.to_numeric(liquid["price"], errors="coerce") * pd.to_numeric(liquid["quantity"], errors="coerce")
        side = liquid["side"].astype(str).str.upper()
        x["liq_buy_quote"] = lq.where(side == "BUY", 0.0).groupby(liquid["sec"]).sum().reindex(idx, fill_value=0.0)
        x["liq_sell_quote"] = lq.where(side == "SELL", 0.0).groupby(liquid["sec"]).sum().reindex(idx, fill_value=0.0)

    if not mark.empty:
        keep = [c for c in ("mark_price", "index_price", "funding_rate") if c in mark]
        mm = mark.sort_values("timestamp_ms").groupby("sec", sort=True).tail(1).set_index("sec")[keep]
        x = x.join(mm.reindex(idx).ffill())

    x["mid"] = (x.bid_price + x.ask_price) / 2.0
    x["spread_bps"] = (x.ask_price / x.bid_price - 1.0) * 10000.0
    x["microprice"] = (x.ask_price * x.bid_qty + x.bid_price * x.ask_qty) / (x.bid_qty + x.ask_qty).replace(0, np.nan)
    x["book_imbalance"] = (x.bid_depth_quote - x.ask_depth_quote) / (x.bid_depth_quote + x.ask_depth_quote).replace(0, np.nan)
    x["trade_flow"] = (x.buy_quote - x.sell_quote) / (x.buy_quote + x.sell_quote).replace(0, np.nan)
    x["trade_flow"] = x.trade_flow.fillna(0.0)
    x["signed_liq"] = x.liq_buy_quote - x.liq_sell_quote
    x["abs_liq"] = x.liq_buy_quote + x.liq_sell_quote
    x["ret_1s_bps"] = np.log(x.mid / x.mid.shift(1)) * 10000.0
    x["ret_5s_bps"] = np.log(x.mid / x.mid.shift(5)) * 10000.0
    x["flow_5s"] = (x.buy_quote.rolling(5).sum() - x.sell_quote.rolling(5).sum()) / (x.buy_quote.rolling(5).sum() + x.sell_quote.rolling(5).sum()).replace(0, np.nan)
    x["liq_5s"] = x.abs_liq.rolling(5).sum()
    x["liq_signed_5s"] = x.signed_liq.rolling(5).sum()
    x["liq_z"] = prior_z(np.log1p(x.liq_5s), 1800, 300)
    x["flow_z"] = prior_z(x.flow_5s, 1800, 300)
    x["spread_z"] = prior_z(x.spread_bps, 1800, 300)
    x["depth_z"] = prior_z(np.log1p(x.bid_depth_quote + x.ask_depth_quote), 1800, 300)
    return x


def rising_events(x: pd.DataFrame, threshold: float, cooldown: int = 60) -> np.ndarray:
    mask = (x.liq_z >= threshold) & (x.liq_5s > 0) & np.isfinite(x.mid)
    edge = mask & ~mask.shift(1, fill_value=False)
    raw = np.flatnonzero(edge.to_numpy())
    chosen = []
    next_allowed = -1
    for i in raw:
        if i >= next_allowed:
            chosen.append(i)
            next_allowed = i + cooldown
    return np.asarray(chosen, dtype=np.int64)


def outcome(x: pd.DataFrame, event_i: int, policy: Policy, fee_bps: float) -> tuple[int, float] | None:
    d = event_i + policy.confirm_s
    e = d + 1
    z = e + policy.horizon_s
    if z >= len(x):
        return None
    liq_side = int(np.sign(x.liq_signed_5s.iloc[event_i]))
    if liq_side == 0:
        return None
    pre_bid = float(x.bid_depth_quote.iloc[max(event_i - 1, 0)])
    pre_ask = float(x.ask_depth_quote.iloc[max(event_i - 1, 0)])
    if liq_side > 0:
        vulnerable_pre = pre_ask
        vulnerable_post = float(x.ask_depth_quote.iloc[d])
    else:
        vulnerable_pre = pre_bid
        vulnerable_post = float(x.bid_depth_quote.iloc[d])
    if not np.isfinite(vulnerable_pre) or vulnerable_pre <= 0 or not np.isfinite(vulnerable_post):
        return None
    ratio = vulnerable_post / vulnerable_pre
    flow_align = liq_side * float(x.flow_z.iloc[d])
    event_mid = float(x.mid.iloc[event_i])
    decision_mid = float(x.mid.iloc[d])
    if min(event_mid, decision_mid) <= 0 or not np.isfinite(flow_align):
        return None
    event_to_decision = liq_side * math.log(decision_mid / event_mid) * 10000.0
    micro_align = liq_side * (float(x.microprice.iloc[d]) / decision_mid - 1.0) * 10000.0
    if policy.family == "CONTINUATION":
        if ratio > policy.depth_ratio or flow_align < policy.flow_z or event_to_decision < policy.reclaim_bps or micro_align < 0:
            return None
        side = liq_side
    else:
        if ratio < policy.depth_ratio or flow_align > -policy.flow_z or event_to_decision > -policy.reclaim_bps or micro_align > 0:
            return None
        side = -liq_side
    entry = float(x.ask_price.iloc[e] if side > 0 else x.bid_price.iloc[e])
    exitp = float(x.bid_price.iloc[z] if side > 0 else x.ask_price.iloc[z])
    if min(entry, exitp) <= 0 or not np.isfinite(entry + exitp):
        return None
    net = side * math.log(exitp / entry) * 10000.0 - fee_bps
    return int(x.index[event_i]), net


def summarize(events: list[tuple[int, float]], cuts: tuple[int, int]) -> dict:
    if not events:
        return {"n": 0}
    t = np.asarray([q[0] for q in events], dtype=np.int64)
    v = np.asarray([q[1] for q in events], dtype=float)
    out = {"n": int(len(v)), "mean_bps": float(v.mean()), "top5_removed_bps": trim(v, 5), "win_rate": float((v > 0).mean())}
    labels = (("dev", -10**30, cuts[0]), ("val", cuts[0], cuts[1]), ("conf", cuts[1], 10**30))
    for name, lo, hi in labels:
        z = v[(t >= lo) & (t < hi)]
        out[f"{name}_n"] = int(len(z))
        out[f"{name}_mean_bps"] = float(z.mean()) if len(z) else math.nan
        out[f"{name}_top5_removed_bps"] = trim(z, 5)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    local = Path(snapshot_download(REPO_ID, repo_type="dataset", allow_patterns=[f"{s}/**/*.parquet" for s in STREAMS]))
    common = None
    for stream in STREAMS:
        ds = dates_under(local, stream)
        common = ds if common is None else common & ds
    days = sorted(common or set())
    if not days:
        raise RuntimeError(f"no common dates under {local}")
    panels = []
    audits = []
    for day in days:
        x = aggregate_day(local, day)
        if x.empty:
            continue
        x["day"] = day
        panels.append(x)
        audits.append({"day": day, "rows": len(x), "liq_events": int((x.abs_liq > 0).sum()), "start_sec": int(x.index.min()), "end_sec": int(x.index.max())})
        print("loaded", day, len(x), audits[-1]["liq_events"], flush=True)
    if not panels:
        raise RuntimeError("no usable panels")
    all_secs = np.concatenate([p.index.to_numpy(np.int64) for p in panels])
    q1 = int(np.quantile(all_secs, 0.40))
    q2 = int(np.quantile(all_secs, 0.70))
    policies = [
        Policy(f, lz, c, dr, fz, rb, h)
        for f in ("CONTINUATION", "REVERSAL")
        for lz in (2.0, 3.0, 4.0)
        for c in (5, 10, 20)
        for dr in ((0.7, 0.9, 1.0) if f == "CONTINUATION" else (1.0, 1.2, 1.5))
        for fz in (0.0, 0.5, 1.0)
        for rb in (0.0, 1.0, 2.0)
        for h in (10, 30, 60, 120, 300)
    ]
    rows = []
    for policy in policies:
        ev = []
        for x in panels:
            for i in rising_events(x, policy.liq_z):
                r = outcome(x, int(i), policy, 10.0)
                if r is not None:
                    ev.append(r)
        sm = summarize(ev, (q1, q2))
        if sm.get("n", 0) < 10:
            continue
        rec = {**asdict(policy), **sm}
        # Stress by another 5 and 10 bps without changing selection.
        for extra, tag in ((5.0, "15bp"), (10.0, "20bp")):
            stressed = [(t, v - extra) for t, v in ev]
            ss = summarize(stressed, (q1, q2))
            for k, v in ss.items():
                rec[f"{k}_{tag}"] = v
        vals = [rec.get("dev_mean_bps_15bp", math.nan), rec.get("val_mean_bps_15bp", math.nan), rec.get("dev_top5_removed_bps_15bp", math.nan), rec.get("val_top5_removed_bps_15bp", math.nan)]
        rec["selection_score"] = min(vals) if all(np.isfinite(vals)) else -1e9
        rec["eligible"] = bool(rec.get("dev_n", 0) >= 15 and rec.get("val_n", 0) >= 10 and rec["selection_score"] > 0)
        rows.append(rec)
    grid = pd.DataFrame(rows).sort_values(["eligible", "selection_score", "conf_mean_bps"], ascending=False)
    grid.to_csv(args.out / "GRID.csv", index=False)
    robust = grid[grid.eligible].copy() if len(grid) else pd.DataFrame()
    robust.to_csv(args.out / "ROBUST.csv", index=False)
    result = {
        "dataset": REPO_ID,
        "local_path": str(local),
        "days": days,
        "audit": audits,
        "policy_count": len(policies),
        "evaluated_count": int(len(grid)),
        "eligible_count": int(len(robust)),
        "selected": robust.iloc[0].replace({np.nan: None}).to_dict() if len(robust) else None,
        "top": grid.head(50).replace({np.nan: None}).to_dict("records") if len(grid) else [],
        "selection_note": "Chronological 40/30/30 split; event rising edge only; future liquidation peak selection forbidden; exchange timestamps only, research-only.",
    }
    (args.out / "SUMMARY.json").write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({k: result[k] for k in ("days", "policy_count", "evaluated_count", "eligible_count", "selected")}, indent=2))


if __name__ == "__main__":
    main()
