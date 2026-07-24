from __future__ import annotations

import math
import re
from typing import Iterable

import numpy as np
import pandas as pd


AGGTRADE_COLUMNS = (
    "symbol",
    "agg_trade_id",
    "price",
    "quantity",
    "quote_quantity",
    "normal_quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
    "signed_quote_quantity",
)

FLOW_COLUMNS = (
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "base_volume",
    "quote_volume",
    "taker_buy_quote_volume",
    "taker_sell_quote_volume",
    "signed_quote_volume",
    "aggregate_trade_count",
    "underlying_trade_count",
    "largest_aggregate_quote",
    "vwap",
    "price_change_bps",
    "close_location_value",
)

_POSITIONAL_7 = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)
_POSITIONAL_8 = (
    "agg_trade_id",
    "price",
    "quantity",
    "normal_quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


_HEADER_ALIASES = {
    "a": "agg_trade_id",
    "aggtradeid": "agg_trade_id",
    "aggregatetradeid": "agg_trade_id",
    "price": "price",
    "p": "price",
    "quantity": "quantity",
    "qty": "quantity",
    "q": "quantity",
    "normalquantity": "normal_quantity",
    "nq": "normal_quantity",
    "firsttradeid": "first_trade_id",
    "f": "first_trade_id",
    "lasttradeid": "last_trade_id",
    "l": "last_trade_id",
    "timestamp": "transact_time",
    "time": "transact_time",
    "transacttime": "transact_time",
    "t": "transact_time",
    "wasthebuyerthemaker": "is_buyer_maker",
    "isbuyermaker": "is_buyer_maker",
    "m": "is_buyer_maker",
}


def _timestamp_unit(values: pd.Series) -> str:
    maximum = int(values.max())
    if maximum < 10**11:
        return "s"
    if maximum < 10**14:
        return "ms"
    if maximum < 10**17:
        return "us"
    return "ns"


def _bool_value(value: object) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)) and value in {0, 1}:
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "t", "1"}:
        return True
    if text in {"false", "f", "0"}:
        return False
    raise ValueError(f"invalid buyer-maker value: {value!r}")


def _as_source(source: pd.DataFrame) -> pd.DataFrame:
    frame = source.copy()
    if frame.empty:
        raise ValueError("aggregate-trade source cannot be empty")

    mapped = {
        column: _HEADER_ALIASES.get(_key(column))
        for column in frame.columns
    }
    recognized = {value for value in mapped.values() if value is not None}
    required = {
        "agg_trade_id",
        "price",
        "quantity",
        "first_trade_id",
        "last_trade_id",
        "transact_time",
        "is_buyer_maker",
    }
    if required.issubset(recognized):
        rename = {column: value for column, value in mapped.items() if value is not None}
        return frame.rename(columns=rename)

    if frame.shape[1] not in {7, 8}:
        raise ValueError(
            "headerless aggregate-trade data requires seven or eight columns"
        )
    positional = _POSITIONAL_7 if frame.shape[1] == 7 else _POSITIONAL_8
    frame.columns = positional

    # Some CSV readers are called with header=None even though the archive has a
    # header. Remove that one textual row without silently dropping malformed
    # data elsewhere.
    numeric_id = pd.to_numeric(frame["agg_trade_id"], errors="coerce")
    if pd.isna(numeric_id.iloc[0]):
        frame = frame.iloc[1:].copy()
    if frame.empty:
        raise ValueError("aggregate-trade source contains only a header")
    return frame


def normalize_aggtrades(
    source: pd.DataFrame,
    *,
    symbol: str,
) -> pd.DataFrame:
    """Normalize one official Binance aggregate-trade archive causally.

    Positive signed quote volume means buyer-initiated trading. Binance's
    ``is_buyer_maker`` flag is true when the buyer supplied liquidity, so the
    seller was the taker and the signed flow is negative.
    """

    name = str(symbol).strip().upper()
    if not name:
        raise ValueError("symbol is required")
    frame = _as_source(source)

    for column in (
        "agg_trade_id",
        "first_trade_id",
        "last_trade_id",
        "transact_time",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype("int64")
    for column in ("price", "quantity"):
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
    if "normal_quantity" in frame:
        frame["normal_quantity"] = pd.to_numeric(
            frame["normal_quantity"], errors="raise"
        ).astype(float)
    else:
        frame["normal_quantity"] = np.nan
    frame["is_buyer_maker"] = frame["is_buyer_maker"].map(_bool_value)

    if not np.isfinite(frame[["price", "quantity"]].to_numpy()).all():
        raise ValueError("aggregate-trade price and quantity must be finite")
    if bool((frame["price"] <= 0).any()) or bool((frame["quantity"] <= 0).any()):
        raise ValueError("aggregate-trade price and quantity must be positive")
    if bool((frame["first_trade_id"] > frame["last_trade_id"]).any()):
        raise ValueError("first_trade_id cannot exceed last_trade_id")
    if frame["agg_trade_id"].duplicated().any():
        raise ValueError("aggregate trade ids must be unique")
    if not frame["agg_trade_id"].is_monotonic_increasing:
        raise ValueError("aggregate trade ids must be chronological")
    if not frame["transact_time"].is_monotonic_increasing:
        raise ValueError("aggregate trade timestamps must be chronological")

    unit = _timestamp_unit(frame["transact_time"])
    timestamps = pd.to_datetime(frame["transact_time"], unit=unit, utc=True)
    if timestamps.isna().any():
        raise ValueError("aggregate trade timestamps must be valid")

    frame["quote_quantity"] = frame["price"] * frame["quantity"]
    direction = np.where(frame["is_buyer_maker"], -1.0, 1.0)
    frame["signed_quote_quantity"] = frame["quote_quantity"] * direction
    frame.insert(0, "symbol", name)
    frame["transact_time"] = timestamps
    frame.index = pd.DatetimeIndex(timestamps, name="transact_time")
    return frame.loc[:, AGGTRADE_COLUMNS]


def _first(values: pd.Series) -> float:
    return float(values.iloc[0])


def _last(values: pd.Series) -> float:
    return float(values.iloc[-1])


def aggregate_trade_flow(
    trades: pd.DataFrame,
    *,
    frequency: str = "1min",
) -> pd.DataFrame:
    """Aggregate normalized trades into sparse causal flow bars.

    Empty intervals are deliberately absent. A caller may align them to a
    separately checksum-verified kline clock; blindly filling an archive gap as
    zero flow would otherwise turn missing data into a false signal.
    """

    if not isinstance(trades.index, pd.DatetimeIndex) or trades.index.tz is None:
        raise ValueError("trades require a timezone-aware DatetimeIndex")
    missing = [column for column in AGGTRADE_COLUMNS if column not in trades.columns]
    if missing:
        raise ValueError(f"normalized aggregate trades are missing columns: {missing}")
    interval = pd.Timedelta(frequency)
    if pd.isna(interval) or interval <= pd.Timedelta(0):
        raise ValueError("frequency must be positive")

    source = trades.sort_index(kind="mergesort")
    bucket = source.index.floor(interval)
    grouped = source.groupby(bucket, sort=True)
    flow = grouped.agg(
        symbol=("symbol", "first"),
        open=("price", _first),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", _last),
        base_volume=("quantity", "sum"),
        quote_volume=("quote_quantity", "sum"),
        signed_quote_volume=("signed_quote_quantity", "sum"),
        aggregate_trade_count=("agg_trade_id", "size"),
        largest_aggregate_quote=("quote_quantity", "max"),
    )
    buy = source["quote_quantity"].where(~source["is_buyer_maker"], 0.0)
    sell = source["quote_quantity"].where(source["is_buyer_maker"], 0.0)
    underlying = source["last_trade_id"] - source["first_trade_id"] + 1
    flow["taker_buy_quote_volume"] = buy.groupby(bucket, sort=True).sum()
    flow["taker_sell_quote_volume"] = sell.groupby(bucket, sort=True).sum()
    flow["underlying_trade_count"] = underlying.groupby(bucket, sort=True).sum()
    flow["vwap"] = flow["quote_volume"] / flow["base_volume"]
    flow["price_change_bps"] = (flow["close"] / flow["open"] - 1.0) * 10_000.0
    width = flow["high"] - flow["low"]
    flow["close_location_value"] = np.where(
        width > 0,
        ((flow["close"] - flow["low"]) / width) * 2.0 - 1.0,
        0.0,
    )
    flow.index = pd.DatetimeIndex(flow.index, name="open_time")

    numeric = [column for column in FLOW_COLUMNS if column != "symbol"]
    if not np.isfinite(flow[numeric].to_numpy()).all():
        raise ValueError("aggregated trade-flow values must be finite")
    if bool((flow["quote_volume"] <= 0).any()):
        raise ValueError("every emitted flow bar must contain positive quote volume")
    return flow.loc[:, FLOW_COLUMNS]


__all__ = [
    "AGGTRADE_COLUMNS",
    "FLOW_COLUMNS",
    "aggregate_trade_flow",
    "normalize_aggtrades",
]
