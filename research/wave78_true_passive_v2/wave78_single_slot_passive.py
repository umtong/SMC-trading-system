#!/usr/bin/env python3
"""Wave78: single-slot passive spread capture with causal toxicity filtering.

Research-only core. No credentials, network calls, or order submission. The
module consumes already checksum-verified Binance Vision daily bookTicker and
aggTrades ZIP archives. Features from second s use only observations strictly
before s+1s. At most one pending order or filled position exists globally.
"""
from __future__ import annotations

import argparse
import json
import math
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

FEATURES = (
    "is_eth",
    "spread_bps",
    "l1_imbalance",
    "microprice_ticks",
    "log_depth_notional",
    "quote_age_ms",
    "flow_imb_1s",
    "flow_imb_5s",
    "flow_imb_30s",
    "flow_acceleration",
    "trade_notional_z",
    "trade_count_z",
    "ret_1s_ticks",
    "ret_5s_ticks",
    "rv_30s_ticks",
    "flow_price_efficiency",
    "side",
)


def norm_ms(values: Sequence[object]) -> np.ndarray:
    x = pd.to_numeric(pd.Series(values), errors="raise").to_numpy(np.int64)
    y = x.copy()
    ns = np.abs(x) >= 10**17
    us = (np.abs(x) >= 10**14) & ~ns
    sec = np.abs(x) < 10**11
    y[ns] //= 1_000_000
    y[us] //= 1_000
    y[sec] *= 1_000
    return y


def _bool_maker(values: pd.Series) -> np.ndarray:
    if pd.api.types.is_bool_dtype(values.dtype):
        return values.to_numpy(bool, copy=False)
    text = values.astype(str).str.strip().str.lower()
    if not bool(text.isin(("true", "false", "1", "0")).all()):
        raise ValueError("unrecognized is_buyer_maker value")
    return text.isin(("true", "1")).to_numpy(bool)


def _csv_member(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
    if len(names) != 1:
        raise ValueError(f"expected one CSV in {path}: {names}")
    return names[0]


def read_book(path: Path) -> pd.DataFrame:
    member = _csv_member(path)
    use = ["best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty", "event_time"]
    pieces: list[pd.DataFrame] = []
    with zipfile.ZipFile(path) as archive, archive.open(member) as raw:
        for chunk in pd.read_csv(raw, usecols=use, chunksize=750_000):
            for column in use[:-1]:
                chunk[column] = pd.to_numeric(chunk[column], errors="raise")
            chunk["event_time"] = norm_ms(chunk["event_time"])
            pieces.append(chunk)
    data = pd.concat(pieces, ignore_index=True)
    data = data.sort_values("event_time", kind="mergesort").drop_duplicates("event_time", keep="last")
    good = (
        (data.best_bid_price > 0)
        & (data.best_ask_price > data.best_bid_price)
        & (data.best_bid_qty >= 0)
        & (data.best_ask_qty >= 0)
    )
    if not bool(good.all()):
        raise ValueError(f"invalid BBO rows in {path}")
    return data.reset_index(drop=True)


def read_trades(path: Path) -> pd.DataFrame:
    member = _csv_member(path)
    use = ["price", "quantity", "transact_time", "is_buyer_maker"]
    pieces: list[pd.DataFrame] = []
    with zipfile.ZipFile(path) as archive, archive.open(member) as raw:
        for chunk in pd.read_csv(raw, usecols=use, chunksize=750_000):
            price = pd.to_numeric(chunk.price, errors="raise").to_numpy(float)
            quantity = pd.to_numeric(chunk.quantity, errors="raise").to_numpy(float)
            timestamp = norm_ms(chunk.transact_time)
            maker = _bool_maker(chunk.is_buyer_maker)
            pieces.append(pd.DataFrame({"time_ms": timestamp, "price": price, "qty": quantity, "buyer_maker": maker}))
    data = pd.concat(pieces, ignore_index=True).sort_values("time_ms", kind="mergesort")
    if not bool(((data.price > 0) & (data.qty >= 0)).all()):
        raise ValueError(f"invalid trade rows in {path}")
    return data.reset_index(drop=True)


def prior_z(x: pd.Series, window: int = 600, min_periods: int = 300) -> pd.Series:
    prior = x.shift(1)
    rolling = prior.rolling(window, min_periods=min_periods)
    return (x - rolling.mean()) / rolling.std(ddof=0).replace(0, np.nan)


@dataclass(frozen=True)
class MarketDay:
    symbol: str
    day: str
    panel: pd.DataFrame
    book: pd.DataFrame
    trades: pd.DataFrame


def build_second_panel(symbol: str, day: str, book: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    start = int(pd.Timestamp(day, tz="UTC").timestamp())
    seconds = np.arange(start, start + 86_400, dtype=np.int64)
    book_times = book.event_time.to_numpy(np.int64)
    position = np.searchsorted(book_times, (seconds + 1) * 1000, side="left") - 1
    valid = position >= 0
    bid = np.full(len(seconds), np.nan)
    ask = np.full(len(seconds), np.nan)
    bid_qty = np.full(len(seconds), np.nan)
    ask_qty = np.full(len(seconds), np.nan)
    age = np.full(len(seconds), np.nan)
    bid[valid] = book.best_bid_price.to_numpy(float)[position[valid]]
    ask[valid] = book.best_ask_price.to_numpy(float)[position[valid]]
    bid_qty[valid] = book.best_bid_qty.to_numpy(float)[position[valid]]
    ask_qty[valid] = book.best_ask_qty.to_numpy(float)[position[valid]]
    age[valid] = (seconds[valid] + 1) * 1000 - book_times[position[valid]]

    trade_seconds = trades.time_ms.to_numpy(np.int64) // 1000
    quote = trades.price.to_numpy(float) * trades.qty.to_numpy(float)
    signed = np.where(trades.buyer_maker.to_numpy(bool), -quote, quote)
    aggregate = (
        pd.DataFrame({"sec": trade_seconds, "quote": quote, "signed": signed, "count": 1})
        .groupby("sec", sort=True)
        .agg(quote=("quote", "sum"), signed=("signed", "sum"), count=("count", "sum"))
    )
    flow = pd.DataFrame(index=seconds).join(aggregate).fillna({"quote": 0.0, "signed": 0.0, "count": 0.0})
    mid = (bid + ask) / 2.0
    tick = np.maximum(ask - bid, np.finfo(float).eps)
    depth_notional = bid * bid_qty + ask * ask_qty
    output = pd.DataFrame(index=seconds)
    output.index.name = "sec"
    output["symbol"] = symbol
    output["day"] = day
    output["known_time_ms"] = (seconds + 1) * 1000
    output["is_eth"] = float(symbol.startswith("ETH"))
    output["bid"] = bid
    output["ask"] = ask
    output["bid_qty"] = bid_qty
    output["ask_qty"] = ask_qty
    output["quote_age_ms"] = age
    output["spread_bps"] = (ask - bid) / mid * 1e4
    output["l1_imbalance"] = (bid_qty - ask_qty) / np.where((bid_qty + ask_qty) > 0, bid_qty + ask_qty, np.nan)
    microprice = (ask * bid_qty + bid * ask_qty) / np.where((bid_qty + ask_qty) > 0, bid_qty + ask_qty, np.nan)
    output["microprice_ticks"] = (microprice - mid) / tick
    output["log_depth_notional"] = np.log1p(depth_notional)
    output["flow_imb_1s"] = flow.signed / flow.quote.replace(0, np.nan)
    for window in (5, 30):
        output[f"flow_imb_{window}s"] = (
            flow.signed.rolling(window, min_periods=max(2, window // 2)).sum()
            / flow.quote.rolling(window, min_periods=max(2, window // 2)).sum().replace(0, np.nan)
        )
    output["flow_acceleration"] = output.flow_imb_1s - output.flow_imb_5s
    output["trade_notional_z"] = prior_z(np.log1p(flow.quote))
    output["trade_count_z"] = prior_z(np.log1p(flow["count"]))
    mid_series = pd.Series(mid, index=seconds)
    tick_series = pd.Series(tick, index=seconds)
    output["ret_1s_ticks"] = (mid_series - mid_series.shift(1)) / tick_series
    output["ret_5s_ticks"] = (mid_series - mid_series.shift(5)) / tick_series
    output["rv_30s_ticks"] = output.ret_1s_ticks.rolling(30, min_periods=15).std(ddof=0).shift(1)
    absolute_flow = flow.signed.abs().rolling(5, min_periods=2).sum() / flow.quote.rolling(5, min_periods=2).sum().replace(0, np.nan)
    output["flow_price_efficiency"] = output.ret_5s_ticks.abs() / absolute_flow.replace(0, np.nan)
    return output.replace([np.inf, -np.inf], np.nan).reset_index()


@dataclass(frozen=True)
class Attempt:
    signal_time_ms: int
    free_time_ms: int
    symbol: str
    side: int
    score: float
    filled: bool
    fill_qty: float = 0.0
    fill_time_ms: int = -1
    entry_price: float = math.nan
    exit_time_ms: int = -1
    exit_price: float = math.nan
    gross_log: float = math.nan
    emergency_exit_delay_ms: int = 0


def _last_quote_before(book_times: np.ndarray, target_ms: int, max_age_ms: int) -> int | None:
    position = int(np.searchsorted(book_times, target_ms, side="right") - 1)
    if position < 0 or target_ms - int(book_times[position]) > max_age_ms:
        return None
    return position


def _first_quote_after(book_times: np.ndarray, target_ms: int, max_delay_ms: int) -> int | None:
    position = int(np.searchsorted(book_times, target_ms, side="left"))
    if position >= len(book_times) or int(book_times[position]) - target_ms > max_delay_ms:
        return None
    return position


def route_single_slot(attempts: Iterable[object]) -> pd.DataFrame:
    rows = [asdict(attempt) for attempt in attempts]
    if not rows:
        return pd.DataFrame()
    data = pd.DataFrame(rows).sort_values(
        ["signal_time_ms", "score", "symbol", "side"],
        ascending=[True, False, True, False],
        kind="mergesort",
    )
    selected: list[dict] = []
    free_time = -1
    for signal_time, group in data.groupby("signal_time_ms", sort=True):
        timestamp = int(signal_time)
        if timestamp < free_time:
            continue
        row = group.iloc[0].to_dict()
        selected.append(row)
        free_time = int(row["free_time_ms"])
    return pd.DataFrame(selected)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    book = read_book(args.book)
    trades = read_trades(args.trades)
    panel = build_second_panel(args.symbol.upper(), args.day, book, trades)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(args.output, index=False, compression={"method": "gzip", "compresslevel": 6, "mtime": 0})
    print(json.dumps({"symbol": args.symbol.upper(), "day": args.day, "rows": len(panel), "orders_submitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
