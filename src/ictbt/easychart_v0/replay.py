from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal, Mapping

import pandas as pd

from .domain import EntryMode, FormationBar, Side, Timeframe
from .execution import (
    CostConfig,
    OpenPosition,
    OrderIntent,
    SingleSlotRouter,
    TradeRecord,
)


ReplayStatus = Literal[
    "CLOSED",
    "OPEN_CENSORED",
    "ENTRY_CENSORED",
    "ENTRY_REJECTED",
]
ReplayEventKind = Literal[
    "entry_filled",
    "entry_rejected",
    "partial_1r",
    "initial_target",
    "initial_stop",
    "breakeven_stop",
    "volume_exit_scheduled",
    "volume_exit_cancelled",
    "volume_spike",
    "open_censored",
    "entry_censored",
]

_OHLCV = ("open", "high", "low", "close", "volume")
_TIMEFRAME_INTERVAL = {
    Timeframe.M5: pd.Timedelta(minutes=5),
    Timeframe.M15: pd.Timedelta(minutes=15),
}


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    kind: ReplayEventKind
    occurred_at: pd.Timestamp
    price: float | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    status: ReplayStatus
    intent: OrderIntent
    events: tuple[ReplayEvent, ...]
    trade: TradeRecord | None = None
    open_position: OpenPosition | None = None
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ReplayBar:
    open_time: pd.Timestamp
    close_time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class _VolumeBar:
    timeframe: Timeframe
    bar: FormationBar
    previous_20_volumes: tuple[float, ...]


def _interval(value: object, *, name: str) -> pd.Timedelta:
    interval = pd.Timedelta(value)
    if pd.isna(interval) or interval <= pd.Timedelta(0):
        raise ValueError(f"{name} must be a positive interval")
    return interval


def _frame(candles: pd.DataFrame, *, interval: pd.Timedelta) -> pd.DataFrame:
    missing = [column for column in _OHLCV if column not in candles.columns]
    if missing:
        raise ValueError(f"missing OHLCV columns: {missing}")
    if not isinstance(candles.index, pd.DatetimeIndex) or candles.index.tz is None:
        raise ValueError("OHLCV index must be a timezone-aware DatetimeIndex")
    frame = candles.loc[:, _OHLCV].copy()
    frame.index = frame.index.tz_convert("UTC")
    frame = frame.sort_index()
    if frame.index.has_duplicates:
        raise ValueError("OHLCV timestamps must be unique")
    for column in _OHLCV:
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
    for opened, row in frame.iterrows():
        values = tuple(float(row[column]) for column in _OHLCV)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("OHLCV values must be finite")
        open_price, high, low, close, volume = values
        if min(open_price, high, low, close) <= 0 or volume < 0:
            raise ValueError("OHLC prices must be positive and volume non-negative")
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise ValueError(f"invalid OHLC ordering at {opened.isoformat()}")
    return frame


def _bars(frame: pd.DataFrame, interval: pd.Timedelta) -> tuple[_ReplayBar, ...]:
    return tuple(
        _ReplayBar(
            open_time=opened,
            close_time=opened + interval,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        for opened, row in frame.iterrows()
    )


def _formation_bar(
    opened: pd.Timestamp, row: pd.Series, timeframe: Timeframe
) -> FormationBar:
    interval = _TIMEFRAME_INTERVAL[timeframe]
    return FormationBar(
        open_time=opened,
        close_time=opened + interval,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


def _volume_clock(
    volume_bars: Mapping[Timeframe, pd.DataFrame],
) -> tuple[_VolumeBar, ...]:
    output: list[_VolumeBar] = []
    for timeframe in (Timeframe.M5, Timeframe.M15):
        source = volume_bars.get(timeframe)
        if source is None:
            continue
        interval = _TIMEFRAME_INTERVAL[timeframe]
        frame = _frame(source, interval=interval)
        volumes = frame["volume"].astype(float).tolist()
        for index in range(20, len(frame)):
            opened = frame.index[index]
            output.append(
                _VolumeBar(
                    timeframe=timeframe,
                    bar=_formation_bar(opened, frame.iloc[index], timeframe),
                    previous_20_volumes=tuple(volumes[index - 20 : index]),
                )
            )
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item.bar.close_time,
                0 if item.timeframe is Timeframe.M5 else 1,
            ),
        )
    )


def _stop_open(side: Side, price: float, stop: float) -> bool:
    return price <= stop if side is Side.LONG else price >= stop


def _target_open(side: Side, price: float, target: float) -> bool:
    return price >= target if side is Side.LONG else price <= target


def _favorable_limit_open(side: Side, price: float, limit: float) -> bool:
    return price <= limit if side is Side.LONG else price >= limit


def _level_hit(side: Side, bar: _ReplayBar, level: float, *, favorable: bool) -> bool:
    if favorable:
        return bar.high >= level if side is Side.LONG else bar.low <= level
    return bar.low <= level if side is Side.LONG else bar.high >= level


def _append_exit_event(
    events: list[ReplayEvent], trade: TradeRecord, *, breakeven: bool = False
) -> None:
    leg = trade.exit_legs[-1]
    kind: ReplayEventKind
    if leg.reason == "volume_spike":
        kind = "volume_spike"
    elif leg.reason == "initial_target":
        kind = "initial_target"
    else:
        kind = "breakeven_stop" if breakeven else "initial_stop"
    events.append(ReplayEvent(kind, leg.filled_at, leg.fill_price))


def _fill_partial(
    router: SingleSlotRouter, events: list[ReplayEvent], *, filled_at: pd.Timestamp
) -> None:
    position = router.partial_fill(filled_at=filled_at)
    leg = position.exit_legs[-1]
    events.append(ReplayEvent("partial_1r", leg.filled_at, leg.fill_price))


def _manage_open_gap(
    router: SingleSlotRouter, events: list[ReplayEvent], bar: _ReplayBar
) -> TradeRecord | None:
    position = router.position
    if position is None:
        return None
    if _stop_open(position.intent.side, bar.open, position.stop_price):
        was_breakeven = position.partial_filled
        trade = router.stop_fill(filled_at=bar.open_time, actual_fill_price=bar.open)
        _append_exit_event(events, trade, breakeven=was_breakeven)
        return trade
    if _target_open(position.intent.side, bar.open, position.initial_target):
        if position.partial_price is not None and not position.partial_filled:
            _fill_partial(router, events, filled_at=bar.open_time)
        trade = router.target_fill(filled_at=bar.open_time)
        _append_exit_event(events, trade)
        return trade
    if (
        position.partial_price is not None
        and not position.partial_filled
        and _target_open(position.intent.side, bar.open, position.partial_price)
    ):
        _fill_partial(router, events, filled_at=bar.open_time)
    return None


def _manage_open_bar(
    router: SingleSlotRouter, events: list[ReplayEvent], bar: _ReplayBar
) -> TradeRecord | None:
    position = router.position
    if position is None:
        return None

    # Gaps are known at the bar open and therefore precede its later range.
    gap_trade = _manage_open_gap(router, events, bar)
    if gap_trade is not None:
        return gap_trade

    position = router.position
    if position is None:
        return None
    stop_hit = _level_hit(
        position.intent.side, bar, position.stop_price, favorable=False
    )
    target_hit = _level_hit(
        position.intent.side, bar, position.initial_target, favorable=True
    )

    if position.partial_filled or position.partial_price is None:
        # The smallest available bar cannot order two intrabar touches.
        if stop_hit:
            was_breakeven = position.partial_filled
            trade = router.stop_fill(filled_at=bar.close_time)
            _append_exit_event(events, trade, breakeven=was_breakeven)
            return trade
        if target_hit:
            trade = router.target_fill(filled_at=bar.close_time)
            _append_exit_event(events, trade)
            return trade
        return None

    partial_hit = _level_hit(
        position.intent.side, bar, position.partial_price, favorable=True
    )
    if stop_hit:
        trade = router.stop_fill(filled_at=bar.close_time)
        _append_exit_event(events, trade)
        return trade
    if not partial_hit:
        return None

    _fill_partial(router, events, filled_at=bar.close_time)
    after_partial = router.position
    if after_partial is None:
        return None
    entry_stop_hit = _level_hit(
        after_partial.intent.side, bar, after_partial.entry_price, favorable=False
    )
    # If target and the new entry stop overlap in this same smallest bar, the
    # conservative sequence is partial then entry stop.  With no entry touch,
    # a partial and target in the same bar execute in that order.
    if entry_stop_hit:
        trade = router.stop_fill(filled_at=bar.close_time)
        _append_exit_event(events, trade, breakeven=True)
        return trade
    if target_hit:
        trade = router.target_fill(filled_at=bar.close_time)
        _append_exit_event(events, trade)
        return trade
    return None


def replay_intent(
    intent: OrderIntent,
    *,
    candles: pd.DataFrame,
    candle_interval: object,
    costs: CostConfig,
    lower_native_bars: pd.DataFrame | None = None,
    lower_native_interval: object | None = None,
    volume_bars: Mapping[Timeframe, pd.DataFrame] | None = None,
) -> ReplayResult:
    """Execute one frozen confluence entry intent on completed OHLCV.

    ``lower_native_bars`` replaces the wider candle range for price-event
    ordering when it is supplied.  Volume signals still use the explicit 5m
    and 15m frames and execute through the execution module's next-open API.
    A first-revisit limit waits for its first price return after the causal
    clock.  A next-bar-open intent fills only the first eligible bar open and
    is reserved by the intent contract for event-created OBs. An open position
    at the last bar remains open-censored; it is never closed merely because
    the input ended.
    """

    parent_interval = _interval(candle_interval, name="candle_interval")
    parent = _frame(candles, interval=parent_interval)
    if lower_native_bars is not None:
        if lower_native_interval is None:
            raise ValueError("lower_native_interval is required with lower-native bars")
        price_interval = _interval(
            lower_native_interval, name="lower_native_interval"
        )
        if price_interval >= parent_interval:
            raise ValueError("lower-native interval must be smaller than candle interval")
        price_frame = _frame(lower_native_bars, interval=price_interval)
    else:
        price_interval = parent_interval
        price_frame = parent

    configured_volume_bars: Mapping[Timeframe, pd.DataFrame]
    if volume_bars is not None:
        configured_volume_bars = volume_bars
    elif parent_interval == _TIMEFRAME_INTERVAL[Timeframe.M5]:
        configured_volume_bars = {Timeframe.M5: parent}
    elif parent_interval == _TIMEFRAME_INTERVAL[Timeframe.M15]:
        configured_volume_bars = {Timeframe.M15: parent}
    else:
        configured_volume_bars = {}

    clock = _bars(price_frame, price_interval)
    volume_clock = _volume_clock(configured_volume_bars)
    volume_cursor = 0
    events: list[ReplayEvent] = []
    router = SingleSlotRouter(costs=costs)
    router.submit(intent)
    rejection_reason: str | None = None

    def schedule_volume_through(timestamp: pd.Timestamp) -> None:
        nonlocal volume_cursor
        while (
            volume_cursor < len(volume_clock)
            and volume_clock[volume_cursor].bar.close_time <= timestamp
        ):
            candidate = volume_clock[volume_cursor]
            volume_cursor += 1
            if router.position is None:
                continue
            scheduled = router.schedule_volume_exit(
                timeframe=candidate.timeframe,
                completed_bar=candidate.bar,
                previous_20_volumes=candidate.previous_20_volumes,
            )
            if scheduled:
                reservation = router.position.volume_exit
                assert reservation is not None
                events.append(
                    ReplayEvent(
                        "volume_exit_scheduled",
                        reservation.signal_bar_close,
                        reservation.signal_close,
                        candidate.timeframe.value,
                    )
                )

    for bar in clock:
        if bar.close_time <= intent.created_at:
            schedule_volume_through(bar.close_time)
            continue
        if bar.open_time < intent.created_at:
            continue

        schedule_volume_through(bar.open_time)
        position = router.position
        if position is not None and position.volume_exit is not None:
            # Resting stop/partial/target orders at the same open execute before
            # a volume signal's scheduled market exit.
            gap_trade = _manage_open_gap(router, events, bar)
            if gap_trade is not None:
                return ReplayResult(
                    status="CLOSED",
                    intent=intent,
                    events=tuple(events),
                    trade=gap_trade,
                )
            position = router.position
            if position is None or position.volume_exit is None:
                continue
            result = router.execute_volume_exit(
                next_open_price=bar.open, opened_at=bar.open_time
            )
            if isinstance(result, TradeRecord):
                _append_exit_event(events, result)
                return ReplayResult(
                    status="CLOSED",
                    intent=intent,
                    events=tuple(events),
                    trade=result,
                )
            events.append(
                ReplayEvent(
                    "volume_exit_cancelled",
                    bar.open_time,
                    bar.open,
                    "next open no longer has positive net profit",
                )
            )

        filled_at_bar_close = False
        if router.pending is not None:
            if intent.entry_mode is EntryMode.NEXT_BAR_OPEN:
                valid_open_geometry = (
                    intent.initial_stop < bar.open < intent.initial_target
                    if intent.side is Side.LONG
                    else intent.initial_target < bar.open < intent.initial_stop
                )
                if not valid_open_geometry:
                    rejection_reason = "next_bar_open_outside_trade_geometry"
                else:
                    try:
                        router.fill_entry(
                            actual_fill_price=bar.open,
                            filled_at=bar.open_time,
                        )
                    except ValueError as exc:
                        rejection_reason = f"next_bar_open_rejected:{exc}"
                    else:
                        events.append(
                            ReplayEvent("entry_filled", bar.open_time, bar.open)
                        )
            elif _stop_open(intent.side, bar.open, intent.initial_stop):
                rejection_reason = "first_revisit_open_outside_initial_stop"
            elif _favorable_limit_open(
                intent.side, bar.open, intent.entry_reference
            ):
                router.fill_entry(actual_fill_price=bar.open, filled_at=bar.open_time)
                events.append(ReplayEvent("entry_filled", bar.open_time, bar.open))
            elif _level_hit(
                intent.side, bar, intent.entry_reference, favorable=False
            ):
                router.fill_entry(
                    actual_fill_price=intent.entry_reference,
                    filled_at=bar.close_time,
                )
                filled_at_bar_close = True
                events.append(
                    ReplayEvent("entry_filled", bar.close_time, intent.entry_reference)
                )

        if rejection_reason is not None:
            router.cancel_entry()
            events.append(
                ReplayEvent(
                    "entry_rejected", bar.open_time, bar.open, rejection_reason
                )
            )
            return ReplayResult(
                status="ENTRY_REJECTED",
                intent=intent,
                events=tuple(events),
                rejection_reason=rejection_reason,
            )

        if router.position is not None:
            if filled_at_bar_close:
                # At the smallest available bar, a limit touch is timestamped
                # at its close. Earlier high/target information from that bar
                # cannot be reused after the fill. A same-bar adverse stop is
                # the explicit conservative entry->stop fallback.
                position = router.position
                assert position is not None
                stop_hit = _level_hit(
                    position.intent.side,
                    bar,
                    position.stop_price,
                    favorable=False,
                )
                trade = None
                if stop_hit:
                    trade = router.stop_fill(filled_at=bar.close_time)
                    _append_exit_event(events, trade)
            else:
                trade = _manage_open_bar(router, events, bar)
            if trade is not None:
                return ReplayResult(
                    status="CLOSED",
                    intent=intent,
                    events=tuple(events),
                    trade=trade,
                )

        # Price management occurs before a completed-bar volume decision.
        schedule_volume_through(bar.close_time)

    if router.position is not None:
        ended_at = clock[-1].close_time if clock else intent.created_at
        events.append(
            ReplayEvent(
                "open_censored",
                ended_at,
                router.position.entry_price,
                "input ended with the position still open",
            )
        )
        return ReplayResult(
            status="OPEN_CENSORED",
            intent=intent,
            events=tuple(events),
            open_position=router.position,
        )

    ended_at = clock[-1].close_time if clock else intent.created_at
    events.append(
        ReplayEvent(
            "entry_censored",
            ended_at,
            intent.entry_reference,
            "input ended before entry",
        )
    )
    return ReplayResult(
        status="ENTRY_CENSORED",
        intent=intent,
        events=tuple(events),
    )


__all__ = ["ReplayEvent", "ReplayResult", "ReplayStatus", "replay_intent"]
