from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .domain import (
    FairValueGap,
    FormationBar,
    ObKind,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    Timeframe,
)


OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
TIMEFRAME_DELTA = {
    Timeframe.M5: pd.Timedelta(minutes=5),
    Timeframe.M15: pd.Timedelta(minutes=15),
    Timeframe.H1: pd.Timedelta(hours=1),
    Timeframe.H4: pd.Timedelta(hours=4),
}
_RESAMPLE_RULE = {
    Timeframe.M15: ("15min", 3),
    Timeframe.H1: ("1h", 12),
    Timeframe.H4: ("4h", 48),
}


def _timeframe(value: Timeframe | str) -> Timeframe:
    if isinstance(value, Timeframe):
        return value
    normalized = str(value).strip().lower()
    aliases = {"5min": "5m", "15min": "15m", "60m": "1h", "240m": "4h"}
    return Timeframe(aliases.get(normalized, normalized))


def _tick(value: float) -> float:
    tick = float(value)
    if not math.isfinite(tick) or tick <= 0:
        raise ValueError("tick_size must be finite and positive")
    return tick


def validate_ohlcv(
    candles: pd.DataFrame,
    *,
    expected_timeframe: Timeframe | str | None = None,
    require_contiguous: bool = True,
) -> pd.DataFrame:
    missing = [column for column in OHLCV_COLUMNS if column not in candles.columns]
    if missing:
        raise ValueError(f"missing candle columns: {missing}")
    if not isinstance(candles.index, pd.DatetimeIndex):
        raise TypeError("candle index must be a DatetimeIndex")
    if candles.index.tz is None:
        raise ValueError("candle index must be timezone-aware")

    frame = candles.loc[:, OHLCV_COLUMNS].copy()
    frame.index = frame.index.tz_convert("UTC")
    frame = frame.sort_index()
    if frame.index.has_duplicates:
        raise ValueError("duplicate candle timestamps")
    for column in OHLCV_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("candles contain NaN or infinite values")
    if (frame.loc[:, ["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("OHLC prices must be positive")
    if (frame["volume"] < 0).any():
        raise ValueError("volume cannot be negative")
    if (frame["high"] < frame.loc[:, ["open", "low", "close"]].max(axis=1)).any():
        raise ValueError("candle high is below another OHLC value")
    if (frame["low"] > frame.loc[:, ["open", "high", "close"]].min(axis=1)).any():
        raise ValueError("candle low is above another OHLC value")

    if expected_timeframe is not None and len(frame):
        timeframe = _timeframe(expected_timeframe)
        interval = TIMEFRAME_DELTA[timeframe]
        if (frame.index.as_unit("ns").asi8 % interval.value != 0).any():
            raise ValueError(f"candles are not aligned to the UTC {timeframe.value} grid")
        if require_contiguous and len(frame) > 1:
            deltas = frame.index.to_series().diff().dropna()
            if not (deltas == interval).all():
                raise ValueError("candles are not contiguous")
    return frame


def resample_completed(
    candles_5m: pd.DataFrame, timeframe: Timeframe | str
) -> pd.DataFrame:
    """Aggregate only complete UTC-aligned bars from a contiguous 5m frame."""

    target = _timeframe(timeframe)
    frame = validate_ohlcv(candles_5m, expected_timeframe=Timeframe.M5)
    if target is Timeframe.M5:
        return frame
    if target not in _RESAMPLE_RULE:
        raise ValueError(f"unsupported target timeframe: {target.value}")
    rule, expected_count = _RESAMPLE_RULE[target]
    grouped = frame.resample(rule, origin="epoch", label="left", closed="left")
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    counts = grouped["close"].count()
    complete = result.loc[counts == expected_count]
    return validate_ohlcv(complete, expected_timeframe=target)


def resample_all_completed(candles_5m: pd.DataFrame) -> dict[Timeframe, pd.DataFrame]:
    frame = validate_ohlcv(candles_5m, expected_timeframe=Timeframe.M5)
    return {
        Timeframe.M5: frame,
        Timeframe.M15: resample_completed(frame, Timeframe.M15),
        Timeframe.H1: resample_completed(frame, Timeframe.H1),
        Timeframe.H4: resample_completed(frame, Timeframe.H4),
    }


def _bar(open_time: pd.Timestamp, candle: pd.Series, timeframe: Timeframe) -> FormationBar:
    return FormationBar(
        open_time=open_time,
        close_time=open_time + TIMEFRAME_DELTA[timeframe],
        open=float(candle["open"]),
        high=float(candle["high"]),
        low=float(candle["low"]),
        close=float(candle["close"]),
        volume=float(candle["volume"]),
    )


def _engulfs(outer: FormationBar, inner: FormationBar) -> bool:
    return outer.body_low <= inner.body_low and outer.body_high >= inner.body_high


def _levels(
    *, side: Side, bars: Sequence[FormationBar], tick_size: float
) -> tuple[float, float, float]:
    tick = _tick(tick_size)
    if side is Side.LONG:
        stop_extreme = min(bar.low for bar in bars)
        initial_stop = stop_extreme - tick
        impulse_extreme = max(bar.high for bar in bars)
    else:
        stop_extreme = max(bar.high for bar in bars)
        initial_stop = stop_extreme + tick
        impulse_extreme = min(bar.low for bar in bars)
    if initial_stop <= 0:
        raise ValueError("tick_size puts the initial stop at a non-positive price")
    return stop_extreme, initial_stop, impulse_extreme


def _order_block(
    *,
    symbol: str,
    timeframe: Timeframe,
    kind: ObKind,
    side: Side,
    bars: tuple[FormationBar, ...],
    zone_bar: FormationBar,
    tick_size: float,
) -> OrderBlock:
    stop_extreme, initial_stop, impulse_extreme = _levels(
        side=side, bars=bars, tick_size=tick_size
    )
    opened = ":".join(bar.open_time.isoformat() for bar in bars)
    return OrderBlock(
        ob_id=f"{symbol}:{timeframe.value}:ob:{kind.value}:{side.value}:{opened}",
        symbol=symbol,
        timeframe=timeframe,
        kind=kind,
        side=side,
        formation_bars=bars,
        zone=PriceZone(zone_bar.body_low, zone_bar.body_high),
        known_at=bars[-1].close_time,
        stop_extreme=stop_extreme,
        initial_stop=initial_stop,
        impulse_extreme=impulse_extreme,
    )


def detect_simple_order_blocks(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: Timeframe | str,
    tick_size: float,
) -> tuple[OrderBlock, ...]:
    if not symbol:
        raise ValueError("symbol is required")
    native = _timeframe(timeframe)
    frame = validate_ohlcv(candles, expected_timeframe=native)
    _tick(tick_size)
    output: list[OrderBlock] = []
    for index in range(1, len(frame)):
        previous = _bar(frame.index[index - 1], frame.iloc[index - 1], native)
        engulfing = _bar(frame.index[index], frame.iloc[index], native)
        side: Side | None = None
        if previous.bearish and engulfing.bullish and _engulfs(engulfing, previous):
            side = Side.LONG
        elif previous.bullish and engulfing.bearish and _engulfs(engulfing, previous):
            side = Side.SHORT
        if side is not None:
            output.append(
                _order_block(
                    symbol=symbol,
                    timeframe=native,
                    kind=ObKind.SIMPLE_2C,
                    side=side,
                    bars=(previous, engulfing),
                    zone_bar=previous,
                    tick_size=tick_size,
                )
            )
    return tuple(output)


def detect_double_order_blocks(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: Timeframe | str,
    tick_size: float,
) -> tuple[OrderBlock, ...]:
    if not symbol:
        raise ValueError("symbol is required")
    native = _timeframe(timeframe)
    frame = validate_ohlcv(candles, expected_timeframe=native)
    _tick(tick_size)
    output: list[OrderBlock] = []
    for index in range(2, len(frame)):
        c1 = _bar(frame.index[index - 2], frame.iloc[index - 2], native)
        c2 = _bar(frame.index[index - 1], frame.iloc[index - 1], native)
        c3 = _bar(frame.index[index], frame.iloc[index], native)
        if c1.doji or c2.doji or c3.doji:
            continue
        side: Side | None = None
        if (
            c1.bullish
            and c2.bearish
            and c3.bullish
            and _engulfs(c2, c1)
            and _engulfs(c3, c2)
        ):
            side = Side.LONG
        elif (
            c1.bearish
            and c2.bullish
            and c3.bearish
            and _engulfs(c2, c1)
            and _engulfs(c3, c2)
        ):
            side = Side.SHORT
        if side is not None:
            output.append(
                _order_block(
                    symbol=symbol,
                    timeframe=native,
                    kind=ObKind.DOUBLE_3C,
                    side=side,
                    bars=(c1, c2, c3),
                    zone_bar=c2,
                    tick_size=tick_size,
                )
            )
    return tuple(output)


def detect_order_blocks(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: Timeframe | str,
    tick_size: float,
) -> tuple[OrderBlock, ...]:
    formations = [
        *detect_simple_order_blocks(
            candles, symbol=symbol, timeframe=timeframe, tick_size=tick_size
        ),
        *detect_double_order_blocks(
            candles, symbol=symbol, timeframe=timeframe, tick_size=tick_size
        ),
    ]
    return tuple(
        sorted(
            formations,
            key=lambda item: (item.known_at, item.kind.value, item.side.value, item.ob_id),
        )
    )


def detect_strict_pivots(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: Timeframe | str,
) -> tuple[StrictPivot, ...]:
    if not symbol:
        raise ValueError("symbol is required")
    native = _timeframe(timeframe)
    frame = validate_ohlcv(candles, expected_timeframe=native)
    interval = TIMEFRAME_DELTA[native]
    output: list[StrictPivot] = []
    for index in range(2, len(frame) - 2):
        high = float(frame.iloc[index]["high"])
        low = float(frame.iloc[index]["low"])
        other_highs = [float(frame.iloc[position]["high"]) for position in range(index - 2, index + 3) if position != index]
        other_lows = [float(frame.iloc[position]["low"]) for position in range(index - 2, index + 3) if position != index]
        pivot_time = frame.index[index]
        known_at = frame.index[index + 2] + interval
        if all(high > value for value in other_highs):
            output.append(
                StrictPivot(
                    pivot_id=f"{symbol}:{native.value}:pivot:high:{pivot_time.isoformat()}:{high:.12g}",
                    symbol=symbol,
                    timeframe=native,
                    kind="high",
                    price=high,
                    pivot_time=pivot_time,
                    known_at=known_at,
                )
            )
        if all(low < value for value in other_lows):
            output.append(
                StrictPivot(
                    pivot_id=f"{symbol}:{native.value}:pivot:low:{pivot_time.isoformat()}:{low:.12g}",
                    symbol=symbol,
                    timeframe=native,
                    kind="low",
                    price=low,
                    pivot_time=pivot_time,
                    known_at=known_at,
                )
            )
    return tuple(sorted(output, key=lambda item: (item.known_at, item.kind, item.price)))


def detect_fvgs(
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: Timeframe | str,
    tick_size: float,
) -> tuple[FairValueGap, ...]:
    if not symbol:
        raise ValueError("symbol is required")
    native = _timeframe(timeframe)
    frame = validate_ohlcv(candles, expected_timeframe=native)
    tick = _tick(tick_size)
    output: list[FairValueGap] = []
    for index in range(2, len(frame)):
        a = _bar(frame.index[index - 2], frame.iloc[index - 2], native)
        b = _bar(frame.index[index - 1], frame.iloc[index - 1], native)
        c = _bar(frame.index[index], frame.iloc[index], native)
        side: Side | None = None
        zone: PriceZone | None = None
        if c.low >= a.high + tick:
            side = Side.LONG
            zone = PriceZone(a.high, c.low)
        elif c.high <= a.low - tick:
            side = Side.SHORT
            zone = PriceZone(c.high, a.low)
        if side is not None and zone is not None:
            output.append(
                FairValueGap(
                    fvg_id=(
                        f"{symbol}:{native.value}:fvg:{side.value}:"
                        f"{a.open_time.isoformat()}:{b.open_time.isoformat()}:{c.open_time.isoformat()}"
                    ),
                    symbol=symbol,
                    timeframe=native,
                    side=side,
                    formation_bars=(a, b, c),
                    zone=zone,
                    known_at=c.close_time,
                )
            )
    return tuple(output)


def intersect_zones(
    zones: Sequence[PriceZone], *, minimum_width: float = 0.0
) -> PriceZone | None:
    if not zones:
        raise ValueError("at least one zone is required")
    width = float(minimum_width)
    if not math.isfinite(width) or width < 0:
        raise ValueError("minimum_width must be finite and non-negative")
    low = max(zone.low for zone in zones)
    high = min(zone.high for zone in zones)
    if high - low + 1e-12 < width:
        return None
    return PriceZone(low, high)


def merge_zones(
    zones: Iterable[PriceZone], *, maximum_gap: float = 0.0
) -> tuple[PriceZone, ...]:
    gap = float(maximum_gap)
    if not math.isfinite(gap) or gap < 0:
        raise ValueError("maximum_gap must be finite and non-negative")
    ordered = sorted(zones, key=lambda zone: (zone.low, zone.high))
    if not ordered:
        return ()
    merged = [ordered[0]]
    for zone in ordered[1:]:
        previous = merged[-1]
        if zone.low <= previous.high + gap + 1e-12:
            merged[-1] = PriceZone(previous.low, max(previous.high, zone.high))
        else:
            merged.append(zone)
    return tuple(merged)


def _bars_after(
    candles: pd.DataFrame, *, timeframe: Timeframe, after: pd.Timestamp
) -> pd.DataFrame:
    frame = validate_ohlcv(candles, expected_timeframe=timeframe)
    boundary = pd.Timestamp(after)
    if boundary.tz is None:
        raise ValueError("after must be timezone-aware")
    boundary = boundary.tz_convert("UTC")
    close_times = frame.index + TIMEFRAME_DELTA[timeframe]
    return frame.loc[close_times > boundary]


def pivot_is_consumed(
    pivot: StrictPivot, candles: pd.DataFrame, *, tick_size: float
) -> bool:
    tick = _tick(tick_size)
    later = _bars_after(candles, timeframe=pivot.timeframe, after=pivot.known_at)
    if pivot.kind == "high":
        return bool((later["high"] >= pivot.price + tick).any())
    return bool((later["low"] <= pivot.price - tick).any())


def impulse_is_consumed(
    order_block: OrderBlock, candles: pd.DataFrame, *, tick_size: float
) -> bool:
    tick = _tick(tick_size)
    later = _bars_after(
        candles, timeframe=order_block.timeframe, after=order_block.known_at
    )
    if order_block.side is Side.LONG:
        return bool((later["high"] >= order_block.impulse_extreme + tick).any())
    return bool((later["low"] <= order_block.impulse_extreme - tick).any())


def zone_is_consumed(
    zone: PriceZone,
    candles: pd.DataFrame,
    *,
    travel_side: Side,
    timeframe: Timeframe | str,
    tick_size: float,
    after: pd.Timestamp | None = None,
) -> bool:
    """Return whether price closed completely through a target zone."""

    native = _timeframe(timeframe)
    tick = _tick(tick_size)
    frame = validate_ohlcv(candles, expected_timeframe=native)
    if after is not None:
        frame = _bars_after(frame, timeframe=native, after=after)
    if travel_side is Side.LONG:
        return bool((frame["close"] >= zone.high + tick).any())
    return bool((frame["close"] <= zone.low - tick).any())
