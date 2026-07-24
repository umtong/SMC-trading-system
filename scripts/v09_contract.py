from __future__ import annotations

import pandas as pd


WARMUP_DAYS = 28
SYMBOL_TICKS = {
    "BTCUSDT": 0.1,
    "ETHUSDT": 0.01,
}
RESEARCH_START = "2021-01-01"
TRAIN_END = "2023-07-01"
HOLDOUT_END = "2024-01-01"
TARGET_ENTRY_FEE_RATE = 0.0006
TARGET_EXIT_FEE_RATE = 0.0002


def utc(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("timestamp must be valid")
    if timestamp.tz is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def monthly_starts(
    start: object = RESEARCH_START,
    end: object = HOLDOUT_END,
) -> tuple[pd.Timestamp, ...]:
    begin = utc(start)
    finish = utc(end)
    if finish <= begin:
        raise ValueError("end must follow start")
    first = begin.tz_localize(None).to_period("M")
    last = (finish - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M")
    return tuple(
        pd.Timestamp(period.start_time, tz="UTC")
        for period in pd.period_range(first, last, freq="M")
    )


def month_bounds(month: object) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = utc(month).normalize().tz_localize(None).to_period("M").start_time
    begin = pd.Timestamp(start, tz="UTC")
    return begin, begin + pd.offsets.MonthBegin(1)


REGISTERED_MONTHS = monthly_starts()


__all__ = [
    "HOLDOUT_END",
    "REGISTERED_MONTHS",
    "RESEARCH_START",
    "SYMBOL_TICKS",
    "TARGET_ENTRY_FEE_RATE",
    "TARGET_EXIT_FEE_RATE",
    "TRAIN_END",
    "WARMUP_DAYS",
    "month_bounds",
    "monthly_starts",
    "utc",
]
