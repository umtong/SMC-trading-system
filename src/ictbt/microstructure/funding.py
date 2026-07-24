from __future__ import annotations

import re

import numpy as np
import pandas as pd


FUNDING_COLUMNS = (
    "symbol",
    "funding_time",
    "funding_interval_hours",
    "funding_rate",
    "mark_price",
)


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


_ALIASES = {
    "symbol": "symbol",
    "calctime": "funding_time",
    "fundingtime": "funding_time",
    "time": "funding_time",
    "fundingintervalhours": "funding_interval_hours",
    "intervalhours": "funding_interval_hours",
    "lastfundingrate": "funding_rate",
    "fundingrate": "funding_rate",
    "markprice": "mark_price",
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


def _source_frame(source: pd.DataFrame) -> pd.DataFrame:
    frame = source.copy()
    if frame.empty:
        raise ValueError("funding source cannot be empty")
    mapped = {column: _ALIASES.get(_key(column)) for column in frame.columns}
    recognized = {value for value in mapped.values() if value is not None}
    if {"funding_time", "funding_rate"}.issubset(recognized):
        return frame.rename(
            columns={column: value for column, value in mapped.items() if value is not None}
        )

    # Binance public funding archives have historically used
    # calc_time/funding_interval_hours/last_funding_rate. REST exports can add
    # symbol and mark_price. Positional parsing is restricted to these known
    # shapes and still removes an optional textual header explicitly.
    shapes = {
        3: ("funding_time", "funding_interval_hours", "funding_rate"),
        4: ("symbol", "funding_time", "funding_rate", "mark_price"),
        5: (
            "symbol",
            "funding_time",
            "funding_interval_hours",
            "funding_rate",
            "mark_price",
        ),
    }
    columns = shapes.get(frame.shape[1])
    if columns is None:
        raise ValueError("unsupported headerless funding data shape")
    frame.columns = columns
    numeric_time = pd.to_numeric(frame["funding_time"], errors="coerce")
    if pd.isna(numeric_time.iloc[0]):
        frame = frame.iloc[1:].copy()
    if frame.empty:
        raise ValueError("funding source contains only a header")
    return frame


def normalize_funding_rates(
    source: pd.DataFrame,
    *,
    symbol: str,
) -> pd.DataFrame:
    """Normalize archive or REST funding observations without fabricating rows."""

    expected_symbol = str(symbol).strip().upper()
    if not expected_symbol:
        raise ValueError("symbol is required")
    frame = _source_frame(source)

    frame["funding_time"] = pd.to_numeric(
        frame["funding_time"], errors="raise"
    ).astype("int64")
    frame["funding_rate"] = pd.to_numeric(
        frame["funding_rate"], errors="raise"
    ).astype(float)
    if "funding_interval_hours" in frame:
        frame["funding_interval_hours"] = pd.to_numeric(
            frame["funding_interval_hours"], errors="raise"
        ).astype(float)
    else:
        frame["funding_interval_hours"] = np.nan
    if "mark_price" in frame:
        frame["mark_price"] = pd.to_numeric(
            frame["mark_price"], errors="raise"
        ).astype(float)
    else:
        frame["mark_price"] = np.nan

    if "symbol" in frame:
        observed = frame["symbol"].astype(str).str.upper().str.strip()
        if bool((observed != expected_symbol).any()):
            raise ValueError("funding source contains an unexpected symbol")
    frame["symbol"] = expected_symbol

    if not np.isfinite(frame["funding_rate"].to_numpy()).all():
        raise ValueError("funding rates must be finite")
    known_intervals = frame["funding_interval_hours"].dropna()
    if bool((known_intervals <= 0).any()):
        raise ValueError("funding intervals must be positive when present")
    known_marks = frame["mark_price"].dropna()
    if bool((known_marks <= 0).any()) or not np.isfinite(known_marks.to_numpy()).all():
        raise ValueError("mark prices must be finite and positive when present")

    unit = _timestamp_unit(frame["funding_time"])
    timestamps = pd.to_datetime(frame["funding_time"], unit=unit, utc=True)
    if timestamps.isna().any():
        raise ValueError("funding timestamps must be valid")
    if pd.Index(timestamps).duplicated().any():
        raise ValueError("funding timestamps must be unique per symbol")
    if not pd.Index(timestamps).is_monotonic_increasing:
        raise ValueError("funding timestamps must be chronological")

    frame["funding_time"] = timestamps
    frame.index = pd.DatetimeIndex(timestamps, name="funding_time")
    return frame.loc[:, FUNDING_COLUMNS]


__all__ = ["FUNDING_COLUMNS", "normalize_funding_rates"]
