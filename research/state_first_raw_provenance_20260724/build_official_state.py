from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um/daily"
SYMBOL = "BTCUSDT"
DATE = "2023-07-03"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get(session: requests.Session, url: str) -> requests.Response:
    last: Exception | None = None
    for attempt in range(5):
        try:
            response = session.get(url, timeout=(30, 240))
            response.raise_for_status()
            return response
        except Exception as exc:
            last = exc
            if attempt == 4:
                break
            time.sleep(min(2 ** attempt, 16))
    raise RuntimeError(f"download failed: {url}") from last


def archive_url(kind: str, interval: str | None = None) -> tuple[str, str]:
    if interval:
        name = f"{SYMBOL}-{interval}-{DATE}.zip"
        return name, f"{BASE}/{kind}/{SYMBOL}/{interval}/{name}"
    name = f"{SYMBOL}-{kind}-{DATE}.zip"
    return name, f"{BASE}/{kind}/{SYMBOL}/{name}"


def verified_csv(session: requests.Session, kind: str, interval: str | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    name, url = archive_url(kind, interval)
    checksum = get(session, url + ".CHECKSUM").text
    match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum)
    if not match:
        raise RuntimeError(f"invalid checksum response: {name}")
    expected = match.group(1).lower()
    payload = get(session, url).content
    actual = sha256_bytes(payload)
    if actual != expected:
        raise RuntimeError(f"checksum mismatch {name}: {actual} != {expected}")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(members) != 1:
            raise RuntimeError(f"unexpected members {members}")
        frame = pd.read_csv(io.BytesIO(archive.read(members[0])))
    return frame, {
        "name": name,
        "url": url,
        "bytes": len(payload),
        "sha256": actual,
        "rows": int(len(frame)),
        "columns": [str(c) for c in frame.columns],
    }


def numeric(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for col in columns:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def normalize_klines(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    required = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume"]
    missing = set(required).difference(frame.columns)
    if missing:
        raise RuntimeError(f"{interval} kline missing {sorted(missing)}")
    frame = numeric(frame.copy(), required)
    frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time"], unit="ms", utc=True)
    frame = frame.sort_values("open_time").drop_duplicates("open_time", keep="last").set_index("open_time")
    if interval == "1m" and len(frame) != 1440:
        raise RuntimeError(f"expected 1440 one-minute bars, got {len(frame)}")
    if interval == "5m" and len(frame) != 288:
        raise RuntimeError(f"expected 288 five-minute bars, got {len(frame)}")
    invalid = (
        (frame["high"] < frame[["open", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "close"]].min(axis=1))
        | (frame["high"] < frame["low"])
    )
    if bool(invalid.fillna(False).any()):
        raise RuntimeError(f"invalid {interval} OHLC")
    return frame


def normalize_aggtrades(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time"]
    missing = set(columns + ["is_buyer_maker"]).difference(frame.columns)
    if missing:
        raise RuntimeError(f"aggTrades missing {sorted(missing)}")
    frame = numeric(frame.copy(), columns)
    frame["time"] = pd.to_datetime(frame["transact_time"], unit="ms", utc=True)
    maker = frame["is_buyer_maker"].astype(str).str.lower().isin(["true", "1"])
    frame["quote"] = frame["price"] * frame["quantity"]
    frame["buyer_quote"] = np.where(maker, 0.0, frame["quote"])
    frame["seller_quote"] = np.where(maker, frame["quote"], 0.0)
    frame["signed_quote"] = frame["buyer_quote"] - frame["seller_quote"]
    return frame.sort_values(["time", "agg_trade_id"]).reset_index(drop=True)


def normalize_depth(frame: pd.DataFrame) -> pd.DataFrame:
    required = ["timestamp", "percentage", "depth", "notional"]
    missing = set(required).difference(frame.columns)
    if missing:
        raise RuntimeError(f"bookDepth missing {sorted(missing)}")
    frame = numeric(frame.copy(), ["percentage", "depth", "notional"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values(["timestamp", "percentage"])
    expected = set(range(-5, 0)) | set(range(1, 6))
    observed = set(frame["percentage"].dropna().astype(int).unique())
    if observed != expected:
        raise RuntimeError(f"unexpected depth percentage set {sorted(observed)}")
    pivot = frame.pivot_table(index="timestamp", columns="percentage", values="notional", aggfunc="last")
    if pivot.isna().any(axis=None):
        raise RuntimeError("incomplete bookDepth snapshot")
    pivot.columns = [f"depth_notional_{int(c):+d}pct" for c in pivot.columns]
    return pivot.sort_index()


def normalize_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    required = [
        "create_time", "sum_open_interest", "sum_open_interest_value",
        "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
        "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
    ]
    missing = set(required).difference(frame.columns)
    if missing:
        raise RuntimeError(f"metrics missing {sorted(missing)}")
    frame = numeric(frame.copy(), required[1:])
    frame["create_time"] = pd.to_datetime(frame["create_time"], utc=True)
    frame = frame.sort_values("create_time").drop_duplicates("create_time", keep="last").set_index("create_time")
    if len(frame) != 288:
        raise RuntimeError(f"expected 288 metric observations, got {len(frame)}")
    return frame


def rsi(series: pd.Series, periods: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / periods, adjust=False, min_periods=periods).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / periods, adjust=False, min_periods=periods).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def construct_features(one: pd.DataFrame, five: pd.DataFrame, trades: pd.DataFrame, depth: pd.DataFrame, metrics: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    result = five.copy()
    result.index.name = "bar_time"
    close_times = pd.DatetimeIndex(result["close_time"])

    one_close = one["close"]
    one_ret = np.log(one_close / one_close.shift(1))
    rv30 = one_ret.rolling(30, min_periods=30).std(ddof=0) * math.sqrt(30)
    result["log_ret_1m"] = one_ret.reindex(close_times.floor("min")).to_numpy()
    result["realized_vol_30m"] = rv30.reindex(close_times.floor("min")).to_numpy()
    result["log_ret_5m"] = np.log(result["close"] / result["close"].shift(1))
    result["log_ret_15m"] = np.log(result["close"] / result["close"].shift(3))
    result["log_ret_60m"] = np.log(result["close"] / result["close"].shift(12))
    result["rsi_14"] = rsi(result["close"])
    result["vol_5m"] = result["volume"]
    result["taker_buy_ratio_5m"] = result["taker_buy_quote_volume"] / result["quote_volume"].replace(0, np.nan)
    result["trade_count_5m"] = result["count"]
    result["avg_trade_size_5m"] = result["volume"] / result["count"].replace(0, np.nan)

    trades = trades.copy()
    trades["bar_time"] = trades["time"].dt.floor("5min")
    grouped = trades.groupby("bar_time", sort=True)
    flow = grouped.agg(
        agg_trade_rows=("agg_trade_id", "size"),
        agg_quote=("quote", "sum"),
        aggressive_buy_quote=("buyer_quote", "sum"),
        aggressive_sell_quote=("seller_quote", "sum"),
        signed_aggressive_quote=("signed_quote", "sum"),
        max_agg_trade_quote=("quote", "max"),
        mean_agg_trade_quote=("quote", "mean"),
    )
    top5 = grouped["quote"].apply(lambda x: float(x.nlargest(min(5, len(x))).sum() / x.sum()) if float(x.sum()) > 0 else np.nan)
    flow["top5_agg_trade_quote_share"] = top5
    result = result.join(flow, how="left")
    result["aggressive_flow_ratio"] = result["signed_aggressive_quote"] / result["agg_quote"].replace(0, np.nan)
    result["flow_toxicity_50"] = result["signed_aggressive_quote"].abs().rolling(50, min_periods=20).sum() / result["agg_quote"].rolling(50, min_periods=20).sum().replace(0, np.nan)
    alpha = 1 - math.exp(-5 / 15)
    result["buy_intensity_ewm"] = result["aggressive_buy_quote"].ewm(alpha=alpha, adjust=False).mean()
    result["sell_intensity_ewm"] = result["aggressive_sell_quote"].ewm(alpha=alpha, adjust=False).mean()
    result["intensity_net"] = (result["buy_intensity_ewm"] - result["sell_intensity_ewm"]) / (result["buy_intensity_ewm"] + result["sell_intensity_ewm"]).replace(0, np.nan)

    left = pd.DataFrame({"bar_time": result.index, "available_at": close_times}).sort_values("available_at")
    depth_reset = depth.reset_index().rename(columns={"timestamp": "depth_time"}).sort_values("depth_time")
    merged_depth = pd.merge_asof(left, depth_reset, left_on="available_at", right_on="depth_time", direction="backward", allow_exact_matches=True).set_index("bar_time")
    for col in depth.columns:
        result[col] = merged_depth[col]
    result["depth_source_time"] = pd.to_datetime(merged_depth["depth_time"], utc=True)
    result["depth_snapshot_age_seconds"] = (pd.Series(close_times, index=result.index) - result["depth_source_time"]).dt.total_seconds()
    bid1 = result["depth_notional_-1pct"]
    ask1 = result["depth_notional_+1pct"]
    result["depth_imbalance_1pct"] = (bid1 - ask1) / (bid1 + ask1).replace(0, np.nan)
    result["depth_total_1pct"] = bid1 + ask1
    bid5 = result["depth_notional_-5pct"]
    ask5 = result["depth_notional_+5pct"]
    result["depth_imbalance_5pct"] = (bid5 - ask5) / (bid5 + ask5).replace(0, np.nan)
    result["depth_near_share"] = (bid1 + ask1) / (bid5 + ask5).replace(0, np.nan)
    result["depth_bid_slope"] = np.log(bid5 / bid1.replace(0, np.nan)) / 4
    result["depth_ask_slope"] = np.log(ask5 / ask1.replace(0, np.nan)) / 4
    result["depth_change_1pct"] = np.log(result["depth_total_1pct"] / result["depth_total_1pct"].shift(1))

    delayed = metrics.shift(1).copy()
    delayed["metric_source_time"] = metrics.index.to_series().shift(1)
    result = result.join(delayed, how="left", rsuffix="_metric")
    result["oi_change_1h"] = np.log(result["sum_open_interest_value"] / result["sum_open_interest_value"].shift(12))

    quote_error = (result["agg_quote"] - result["quote_volume"]).abs() / result["quote_volume"].replace(0, np.nan)
    buy_error = (result["aggressive_buy_quote"] - result["taker_buy_quote_volume"]).abs() / result["taker_buy_quote_volume"].replace(0, np.nan)
    depth_after = result["depth_source_time"] > pd.Series(close_times, index=result.index)
    metric_available_at = pd.to_datetime(result["metric_source_time"], utc=True) + pd.Timedelta(minutes=5)
    metric_after = metric_available_at > pd.Series(close_times, index=result.index)
    audit = {
        "rows": int(len(result)),
        "max_agg_quote_relative_error": float(quote_error.dropna().max()),
        "median_agg_quote_relative_error": float(quote_error.dropna().median()),
        "max_aggressive_buy_quote_relative_error": float(buy_error.dropna().max()),
        "median_aggressive_buy_quote_relative_error": float(buy_error.dropna().median()),
        "future_depth_rows": int(depth_after.fillna(False).sum()),
        "future_metric_rows": int(metric_after.fillna(False).sum()),
        "max_depth_snapshot_age_seconds": float(result["depth_snapshot_age_seconds"].dropna().max()),
        "missing_depth_rows": int(result["depth_source_time"].isna().sum()),
        "missing_metric_rows": int(result["sum_open_interest_value"].isna().sum()),
        "strategy_executed": False,
        "candidate_pnl_observed": False,
    }
    if len(result) != 288:
        raise RuntimeError("feature grid is not 288 rows")
    if audit["future_depth_rows"] or audit["future_metric_rows"]:
        raise RuntimeError(f"future information detected: {audit}")
    if audit["max_agg_quote_relative_error"] > 2e-5 or audit["max_aggressive_buy_quote_relative_error"] > 2e-5:
        raise RuntimeError(f"aggTrade/kline reconciliation failed: {audit}")
    return result, audit


def prefix_invariance(one: pd.DataFrame, five: pd.DataFrame, trades: pd.DataFrame, depth: pd.DataFrame, metrics: pd.DataFrame, full: pd.DataFrame) -> dict[str, Any]:
    cutoff = pd.Timestamp(f"{DATE} 12:00:00", tz="UTC")
    one_cut = one.loc[one.index < cutoff]
    five_cut = five.loc[five.index < cutoff]
    trades_cut = trades.loc[trades["time"] < cutoff]
    depth_cut = depth.loc[depth.index < cutoff]
    metrics_cut = metrics.loc[metrics.index < cutoff]
    prefix, _ = construct_features(one_cut, five_cut, trades_cut, depth_cut, metrics_cut)
    common = prefix.index.intersection(full.index)
    ignore = {"depth_source_time", "metric_source_time", "close_time"}
    columns = [c for c in prefix.columns if c in full.columns and c not in ignore]
    mismatches: dict[str, float] = {}
    for col in columns:
        left, right = prefix.loc[common, col], full.loc[common, col]
        if pd.api.types.is_numeric_dtype(left):
            a, b = left.to_numpy(float), right.to_numpy(float)
            same = np.isclose(a, b, rtol=1e-12, atol=1e-12, equal_nan=True)
            if not bool(same.all()):
                mismatches[col] = float(np.nanmax(np.abs(a - b)))
        else:
            same = (left.fillna("<NA>").astype(str) == right.fillna("<NA>").astype(str))
            if not bool(same.all()):
                mismatches[col] = float((~same).sum())
    if mismatches:
        raise RuntimeError(f"prefix invariance failed: {mismatches}")
    return {"cutoff": str(cutoff), "rows_compared": int(len(common)), "columns_compared": len(columns), "mismatches": mismatches, "passed": True}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "smc-ict-official-state-builder/1.0"})

    sources: dict[str, Any] = {}
    one_raw, sources["klines_1m"] = verified_csv(session, "klines", "1m")
    five_raw, sources["klines_5m"] = verified_csv(session, "klines", "5m")
    trade_raw, sources["aggTrades"] = verified_csv(session, "aggTrades")
    depth_raw, sources["bookDepth"] = verified_csv(session, "bookDepth")
    metrics_raw, sources["metrics"] = verified_csv(session, "metrics")

    one = normalize_klines(one_raw, "1m")
    five = normalize_klines(five_raw, "5m")
    trades = normalize_aggtrades(trade_raw)
    depth = normalize_depth(depth_raw)
    metrics = normalize_metrics(metrics_raw)
    features, audit = construct_features(one, five, trades, depth, metrics)
    prefix = prefix_invariance(one, five, trades, depth, metrics, features)

    output_columns = [
        "close", "log_ret_1m", "log_ret_5m", "log_ret_15m", "log_ret_60m", "realized_vol_30m", "rsi_14",
        "vol_5m", "taker_buy_ratio_5m", "trade_count_5m", "avg_trade_size_5m",
        "agg_trade_rows", "agg_quote", "aggressive_buy_quote", "aggressive_sell_quote", "aggressive_flow_ratio",
        "max_agg_trade_quote", "mean_agg_trade_quote", "top5_agg_trade_quote_share", "flow_toxicity_50",
        "buy_intensity_ewm", "sell_intensity_ewm", "intensity_net",
        "depth_imbalance_1pct", "depth_total_1pct", "depth_imbalance_5pct", "depth_near_share",
        "depth_bid_slope", "depth_ask_slope", "depth_change_1pct", "depth_snapshot_age_seconds", "depth_source_time",
        "sum_open_interest", "sum_open_interest_value", "oi_change_1h", "count_toptrader_long_short_ratio",
        "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio", "metric_source_time",
    ]
    output = features[output_columns].copy()
    output.insert(0, "bar_time", output.index)
    feature_path = args.output / "official_state_features_2023-07-03.parquet"
    output.to_parquet(feature_path, index=False, compression="zstd")
    manifest = {
        "schema_version": 1,
        "symbol": SYMBOL,
        "date": DATE,
        "sources": sources,
        "feature_sha256": hashlib.sha256(feature_path.read_bytes()).hexdigest(),
        "feature_rows": int(len(output)),
        "feature_columns": output_columns,
        "audit": audit,
        "prefix_invariance": prefix,
        "strategy_executed": False,
        "candidate_pnl_observed": False,
        "2024_opened": False,
        "2025_opened": False,
        "2026_opened": False,
        "orders_submitted": False,
        "paper_or_live_started": False,
    }
    manifest_path = args.output / "official_state_builder_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
