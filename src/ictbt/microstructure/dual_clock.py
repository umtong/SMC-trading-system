from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

import numpy as np
import pandas as pd

from ictbt.easychart_v0.domain import Side


REFERENCE_WINDOWS = 120
LOWER_QUANTILE = 0.20
MEDIAN_QUANTILE = 0.50
UPPER_QUANTILE = 0.80
_REQUIRED_FLOW_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "quote_volume",
    "signed_quote_volume",
)


class DualClockSceneKind(str, Enum):
    """Economic meaning of the liquidity event and its later confirmation."""

    SWEEP_REVERSAL = "sweep_reversal"
    BREAK_CONTINUATION = "break_continuation"


@dataclass(frozen=True, slots=True)
class FrozenDualClockScene:
    scene_id: str
    symbol: str
    side: Side
    kind: DualClockSceneKind
    node_price: float
    event_started_at: pd.Timestamp
    event_known_at: pd.Timestamp
    confirmation_started_at: pd.Timestamp
    confirmation_known_at: pd.Timestamp
    initial_stop: float
    initial_target: float
    tick_size: float

    def __post_init__(self) -> None:
        if not self.scene_id:
            raise ValueError("scene_id is required")
        symbol = str(self.symbol).strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        event_started = _minute(self.event_started_at, name="event_started_at")
        event_known = _minute(self.event_known_at, name="event_known_at")
        confirmation_started = _minute(
            self.confirmation_started_at,
            name="confirmation_started_at",
        )
        confirmation_known = _minute(
            self.confirmation_known_at,
            name="confirmation_known_at",
        )
        if event_known <= event_started:
            raise ValueError("event_known_at must follow event_started_at")
        if confirmation_started < event_known:
            raise ValueError("confirmation cannot begin before the event is known")
        if confirmation_known <= confirmation_started:
            raise ValueError(
                "confirmation_known_at must follow confirmation_started_at"
            )
        for name in ("node_price", "initial_stop", "initial_target", "tick_size"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        valid = (
            self.initial_stop < self.node_price < self.initial_target
            if self.side is Side.LONG
            else self.initial_target < self.node_price < self.initial_stop
        )
        if not valid:
            raise ValueError("scene stop/node/target geometry is invalid")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "kind", DualClockSceneKind(self.kind))
        object.__setattr__(self, "event_started_at", event_started)
        object.__setattr__(self, "event_known_at", event_known)
        object.__setattr__(self, "confirmation_started_at", confirmation_started)
        object.__setattr__(self, "confirmation_known_at", confirmation_known)

    @property
    def entry_time(self) -> pd.Timestamp:
        """First one-minute open available after the completed confirmation."""

        return self.confirmation_known_at

    @property
    def event_minutes(self) -> int:
        return _whole_minutes(self.event_known_at - self.event_started_at)

    @property
    def confirmation_minutes(self) -> int:
        return _whole_minutes(
            self.confirmation_known_at - self.confirmation_started_at
        )


@dataclass(frozen=True, slots=True)
class IntervalFlowStats:
    started_at: pd.Timestamp
    known_at: pd.Timestamp
    duration_minutes: int
    reference_started_at: pd.Timestamp
    reference_windows: int
    lower_delta_fraction: float
    median_delta_fraction: float
    upper_delta_fraction: float
    delta_fraction: float
    signed_quote_volume: float
    quote_volume: float
    open: float
    high: float
    low: float
    close: float
    directional_return_bps: float
    node_penetration_bps: float
    node_acceptance_bps: float
    price_efficiency_bps_per_million: float


@dataclass(frozen=True, slots=True)
class DualClockFlowFeatures:
    flow_started_at: pd.Timestamp
    flow_ended_at: pd.Timestamp
    event: IntervalFlowStats
    confirmation: IntervalFlowStats


@dataclass(frozen=True, slots=True)
class DualClockFlowDecision:
    scene: FrozenDualClockScene
    features: DualClockFlowFeatures
    accepted: bool
    reason: str
    entry_time: pd.Timestamp
    entry_price: float


def _minute(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tz is None:
        raise ValueError(f"{name} must be timezone-aware")
    timestamp = timestamp.tz_convert("UTC")
    if timestamp.second or timestamp.microsecond or timestamp.nanosecond:
        raise ValueError(f"{name} must align to an exact minute boundary")
    return timestamp


def _whole_minutes(value: pd.Timedelta) -> int:
    minute = pd.Timedelta(minutes=1)
    if value <= pd.Timedelta(0) or value % minute != pd.Timedelta(0):
        raise ValueError("flow intervals must contain a positive whole number of minutes")
    return int(value / minute)


def _validated_flow(flow_1m: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    missing = [column for column in _REQUIRED_FLOW_COLUMNS if column not in flow_1m]
    if missing:
        raise ValueError(f"1m flow is missing columns: {missing}")
    if not isinstance(flow_1m.index, pd.DatetimeIndex) or flow_1m.index.tz is None:
        raise ValueError("1m flow requires a timezone-aware DatetimeIndex")
    frame = flow_1m.loc[:, _REQUIRED_FLOW_COLUMNS].copy()
    frame.index = frame.index.tz_convert("UTC")
    frame = frame.sort_index(kind="mergesort")
    if frame.index.has_duplicates:
        raise ValueError("1m flow timestamps must be unique")
    for column in _REQUIRED_FLOW_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
    if not np.isfinite(frame.to_numpy()).all():
        raise ValueError("1m flow values must be finite")
    if bool(
        (frame[["open", "high", "low", "close", "quote_volume"]] <= 0)
        .any()
        .any()
    ):
        raise ValueError("flow prices and quote volume must be positive")
    if bool(
        (frame["signed_quote_volume"].abs() > frame["quote_volume"] + 1e-8).any()
    ):
        raise ValueError(
            "absolute signed quote volume cannot exceed total quote volume"
        )
    bad = (
        frame["high"] < frame[["open", "close", "low"]].max(axis=1)
    ) | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
    if bool(bad.any()):
        raise ValueError("1m flow contains invalid OHLC ordering")
    if "symbol" in flow_1m:
        observed = (
            flow_1m.loc[frame.index, "symbol"].astype(str).str.upper().str.strip()
        )
        if bool((observed != symbol).any()):
            raise ValueError("1m flow contains an unexpected symbol")
    return frame


def _reference_start(start: pd.Timestamp, duration_minutes: int) -> pd.Timestamp:
    # 120 one-minute-stepped, duration-matched rolling observations. The final
    # reference interval ends exactly when the event/confirmation begins.
    return start - pd.Timedelta(
        minutes=duration_minutes + REFERENCE_WINDOWS - 1
    )


def required_dual_clock_flow_interval(
    scene: FrozenDualClockScene,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = min(
        _reference_start(scene.event_started_at, scene.event_minutes),
        _reference_start(
            scene.confirmation_started_at,
            scene.confirmation_minutes,
        ),
    )
    return start, scene.entry_time + pd.Timedelta(minutes=1)


def _require_contiguous(
    frame: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> None:
    expected = pd.date_range(start, end, freq="1min", inclusive="left")
    actual = frame.loc[(frame.index >= start) & (frame.index < end)].index
    missing = expected.difference(actual)
    if len(missing):
        preview = ", ".join(item.isoformat() for item in missing[:5])
        raise ValueError(f"missing causal 1m flow bars: {preview}")
    if len(actual) != len(expected):
        raise ValueError("causal 1m flow clock contains unexpected rows")


def _reference_fractions(
    frame: pd.DataFrame,
    *,
    interval_start: pd.Timestamp,
    duration_minutes: int,
) -> tuple[pd.Series, pd.Timestamp]:
    reference_start = _reference_start(interval_start, duration_minutes)
    values: list[float] = []
    for offset in range(REFERENCE_WINDOWS - 1, -1, -1):
        end = interval_start - pd.Timedelta(minutes=offset)
        start = end - pd.Timedelta(minutes=duration_minutes)
        window = frame.loc[(frame.index >= start) & (frame.index < end)]
        if len(window) != duration_minutes:
            raise AssertionError("duration-matched reference window is incomplete")
        quote = float(window["quote_volume"].sum())
        signed = float(window["signed_quote_volume"].sum())
        values.append(signed / quote)
    return pd.Series(values, dtype=float), reference_start


def _interval_stats(
    scene: FrozenDualClockScene,
    frame: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> IntervalFlowStats:
    duration = _whole_minutes(end - start)
    interval = frame.loc[(frame.index >= start) & (frame.index < end)]
    if len(interval) != duration:
        raise AssertionError("frozen event/confirmation interval is incomplete")
    reference, reference_start = _reference_fractions(
        frame,
        interval_start=start,
        duration_minutes=duration,
    )
    quote = float(interval["quote_volume"].sum())
    signed = float(interval["signed_quote_volume"].sum())
    delta = signed / quote
    open_price = float(interval["open"].iloc[0])
    close = float(interval["close"].iloc[-1])
    high = float(interval["high"].max())
    low = float(interval["low"].min())
    direction = 1.0 if scene.side is Side.LONG else -1.0
    directional_return = direction * (close - open_price) / open_price * 10_000.0
    penetration = (
        max(0.0, (scene.node_price - low) / scene.node_price * 10_000.0)
        if scene.side is Side.LONG
        else max(0.0, (high - scene.node_price) / scene.node_price * 10_000.0)
    )
    acceptance = direction * (close - scene.node_price) / scene.node_price * 10_000.0
    signed_millions = abs(signed) / 1_000_000.0
    efficiency = (
        directional_return / signed_millions if signed_millions > 0 else 0.0
    )
    return IntervalFlowStats(
        started_at=start,
        known_at=end,
        duration_minutes=duration,
        reference_started_at=reference_start,
        reference_windows=REFERENCE_WINDOWS,
        lower_delta_fraction=float(
            reference.quantile(LOWER_QUANTILE, interpolation="linear")
        ),
        median_delta_fraction=float(
            reference.quantile(MEDIAN_QUANTILE, interpolation="linear")
        ),
        upper_delta_fraction=float(
            reference.quantile(UPPER_QUANTILE, interpolation="linear")
        ),
        delta_fraction=delta,
        signed_quote_volume=signed,
        quote_volume=quote,
        open=open_price,
        high=high,
        low=low,
        close=close,
        directional_return_bps=directional_return,
        node_penetration_bps=penetration,
        node_acceptance_bps=acceptance,
        price_efficiency_bps_per_million=efficiency,
    )


def _location_valid(
    scene: FrozenDualClockScene,
    event: IntervalFlowStats,
    confirmation: IntervalFlowStats,
) -> bool:
    tick_bps = scene.tick_size / scene.node_price * 10_000.0
    if scene.kind is DualClockSceneKind.SWEEP_REVERSAL:
        return (
            event.node_penetration_bps + 1e-12 >= tick_bps
            and event.node_acceptance_bps + 1e-12 >= tick_bps
            and confirmation.node_acceptance_bps + 1e-12 >= tick_bps
            and confirmation.directional_return_bps > 0
        )
    return (
        event.node_acceptance_bps + 1e-12 >= tick_bps
        and confirmation.node_acceptance_bps + 1e-12 >= tick_bps
        and confirmation.directional_return_bps >= -1e-12
    )


def _flow_valid(
    scene: FrozenDualClockScene,
    event: IntervalFlowStats,
    confirmation: IntervalFlowStats,
) -> bool:
    if scene.kind is DualClockSceneKind.SWEEP_REVERSAL:
        if scene.side is Side.LONG:
            return (
                event.delta_fraction <= event.lower_delta_fraction + 1e-12
                and confirmation.delta_fraction
                >= confirmation.upper_delta_fraction - 1e-12
            )
        return (
            event.delta_fraction >= event.upper_delta_fraction - 1e-12
            and confirmation.delta_fraction
            <= confirmation.lower_delta_fraction + 1e-12
        )
    if scene.side is Side.LONG:
        return (
            event.delta_fraction >= event.upper_delta_fraction - 1e-12
            and confirmation.delta_fraction
            >= confirmation.median_delta_fraction - 1e-12
        )
    return (
        event.delta_fraction <= event.lower_delta_fraction + 1e-12
        and confirmation.delta_fraction
        <= confirmation.median_delta_fraction + 1e-12
    )


def evaluate_fixed_dual_clock_flow(
    scene: FrozenDualClockScene,
    flow_1m: pd.DataFrame,
) -> DualClockFlowDecision:
    """Evaluate duration-matched event and confirmation flow without lookahead.

    Sweep scenes require extreme opposing taker flow during the actual liquidity
    event and an extreme same-direction reversal during the later delivery
    confirmation. Break scenes require extreme same-direction flow during the
    break and at least median same-direction flow during acceptance. Every
    reference distribution ends before the interval it evaluates.
    """

    frame = _validated_flow(flow_1m, symbol=scene.symbol)
    required_start, required_end = required_dual_clock_flow_interval(scene)
    _require_contiguous(frame, required_start, required_end)
    event = _interval_stats(
        scene,
        frame,
        start=scene.event_started_at,
        end=scene.event_known_at,
    )
    confirmation = _interval_stats(
        scene,
        frame,
        start=scene.confirmation_started_at,
        end=scene.confirmation_known_at,
    )
    entry_price = float(frame.loc[scene.entry_time, "open"])
    features = DualClockFlowFeatures(
        flow_started_at=required_start,
        flow_ended_at=required_end,
        event=event,
        confirmation=confirmation,
    )
    location_ok = _location_valid(scene, event, confirmation)
    flow_ok = _flow_valid(scene, event, confirmation)
    geometry_ok = (
        scene.initial_stop < entry_price < scene.initial_target
        if scene.side is Side.LONG
        else scene.initial_target < entry_price < scene.initial_stop
    )
    if not location_ok:
        accepted, reason = False, "dual_clock_location_not_confirmed"
    elif not flow_ok:
        accepted, reason = False, "dual_clock_flow_semantics_not_confirmed"
    elif not geometry_ok:
        accepted, reason = False, "entry_open_outside_frozen_trade_geometry"
    else:
        accepted, reason = True, "fixed_dual_clock_flow_confirmed"
    return DualClockFlowDecision(
        scene=scene,
        features=features,
        accepted=accepted,
        reason=reason,
        entry_time=scene.entry_time,
        entry_price=entry_price,
    )


__all__ = [
    "DualClockFlowDecision",
    "DualClockFlowFeatures",
    "DualClockSceneKind",
    "FrozenDualClockScene",
    "IntervalFlowStats",
    "LOWER_QUANTILE",
    "MEDIAN_QUANTILE",
    "REFERENCE_WINDOWS",
    "UPPER_QUANTILE",
    "evaluate_fixed_dual_clock_flow",
    "required_dual_clock_flow_interval",
]
