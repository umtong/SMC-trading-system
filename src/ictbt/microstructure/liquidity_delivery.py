from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

import numpy as np
import pandas as pd

from ictbt.easychart_v0.domain import Side


HISTORY_BARS = 120
EVENT_BARS = 5
EXHAUSTION_QUANTILE = 0.20
REVERSAL_QUANTILE = 0.80
_REQUIRED_FLOW_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "quote_volume",
    "signed_quote_volume",
)


class FlowSceneKind(str, Enum):
    SWEEP_RECLAIM = "sweep_reclaim"
    BREAK_ACCEPTANCE = "break_acceptance"


@dataclass(frozen=True, slots=True)
class FrozenFlowScene:
    scene_id: str
    symbol: str
    side: Side
    kind: FlowSceneKind
    node_price: float
    known_at: pd.Timestamp
    initial_stop: float
    initial_target: float
    tick_size: float

    def __post_init__(self) -> None:
        if not self.scene_id:
            raise ValueError("scene_id is required")
        symbol = str(self.symbol).strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        known = _utc(self.known_at, name="known_at")
        if known.second or known.microsecond or known.nanosecond:
            raise ValueError("known_at must align to an exact minute boundary")
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
        object.__setattr__(self, "kind", FlowSceneKind(self.kind))
        object.__setattr__(self, "known_at", known)


@dataclass(frozen=True, slots=True)
class FlowReversalFeatures:
    history_start: pd.Timestamp
    history_end: pd.Timestamp
    event_start: pd.Timestamp
    event_end: pd.Timestamp
    lower_delta_fraction: float
    upper_delta_fraction: float
    pre_reclaim_delta_fraction: float
    reclaim_delta_fraction: float
    pre_reclaim_signed_quote: float
    reclaim_signed_quote: float
    event_quote_volume: float
    event_low: float
    event_high: float
    reclaim_close: float
    sweep_distance_bps: float
    reclaim_distance_bps: float
    opposing_flow_per_sweep_bps: float
    reclaim_price_efficiency_bps_per_million: float


@dataclass(frozen=True, slots=True)
class FlowReversalDecision:
    scene: FrozenFlowScene
    features: FlowReversalFeatures
    accepted: bool
    reason: str
    entry_time: pd.Timestamp
    entry_price: float


def _utc(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tz is None:
        raise ValueError(f"{name} must be timezone-aware")
    return timestamp.tz_convert("UTC")


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
    if bool((frame[["open", "high", "low", "close", "quote_volume"]] <= 0).any().any()):
        raise ValueError("flow prices and quote volume must be positive")
    if bool((frame["signed_quote_volume"].abs() > frame["quote_volume"] + 1e-8).any()):
        raise ValueError("absolute signed quote volume cannot exceed total quote volume")
    bad = (
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
    )
    if bool(bad.any()):
        raise ValueError("1m flow contains invalid OHLC ordering")
    if "symbol" in flow_1m:
        observed = flow_1m.loc[frame.index, "symbol"].astype(str).str.upper().str.strip()
        if bool((observed != symbol).any()):
            raise ValueError("1m flow contains an unexpected symbol")
    return frame


def _require_contiguous(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> None:
    expected = pd.date_range(start, end, freq="1min", inclusive="left")
    actual = frame.loc[(frame.index >= start) & (frame.index < end)].index
    missing = expected.difference(actual)
    if len(missing):
        preview = ", ".join(item.isoformat() for item in missing[:5])
        raise ValueError(f"missing causal 1m flow bars: {preview}")
    if len(actual) != len(expected):
        raise ValueError("causal 1m flow clock contains unexpected rows")


def _location_valid(scene: FrozenFlowScene, event: pd.DataFrame) -> bool:
    reclaim = event.iloc[-1]
    node = scene.node_price
    tick = scene.tick_size
    if scene.kind is FlowSceneKind.SWEEP_RECLAIM:
        if scene.side is Side.LONG:
            return bool(event["low"].min() <= node - tick + 1e-12) and float(
                reclaim["close"]
            ) >= node + tick - 1e-12
        return bool(event["high"].max() >= node + tick - 1e-12) and float(
            reclaim["close"]
        ) <= node - tick + 1e-12
    if scene.side is Side.LONG:
        return float(reclaim["close"]) >= node + tick - 1e-12
    return float(reclaim["close"]) <= node - tick + 1e-12


def _flow_valid(
    scene: FrozenFlowScene,
    *,
    pre_reclaim: float,
    reclaim: float,
    lower: float,
    upper: float,
) -> bool:
    if scene.side is Side.LONG:
        return pre_reclaim <= lower + 1e-12 and reclaim >= upper - 1e-12
    return pre_reclaim >= upper - 1e-12 and reclaim <= lower + 1e-12


def evaluate_fixed_flow_reversal(
    scene: FrozenFlowScene,
    flow_1m: pd.DataFrame,
) -> FlowReversalDecision:
    """Evaluate the pre-registered 20/80 taker-flow reversal rule.

    Thresholds use exactly 120 completed one-minute bars ending before the
    five-bar event window. The event and the entry bar never enter their own
    reference distribution. Entry is the actual open of the next minute after
    ``known_at``.
    """

    frame = _validated_flow(flow_1m, symbol=scene.symbol)
    event_end = scene.known_at
    event_start = event_end - pd.Timedelta(minutes=EVENT_BARS)
    history_end = event_start
    history_start = history_end - pd.Timedelta(minutes=HISTORY_BARS)
    entry_time = event_end
    required_end = entry_time + pd.Timedelta(minutes=1)
    _require_contiguous(frame, history_start, required_end)

    history = frame.loc[(frame.index >= history_start) & (frame.index < history_end)]
    event = frame.loc[(frame.index >= event_start) & (frame.index < event_end)]
    entry_bar = frame.loc[entry_time]
    if len(history) != HISTORY_BARS or len(event) != EVENT_BARS:
        raise AssertionError("fixed flow windows have the wrong length")

    history_fraction = history["signed_quote_volume"] / history["quote_volume"]
    event_fraction = event["signed_quote_volume"] / event["quote_volume"]
    lower = float(history_fraction.quantile(EXHAUSTION_QUANTILE, interpolation="linear"))
    upper = float(history_fraction.quantile(REVERSAL_QUANTILE, interpolation="linear"))
    pre_fraction = float(event_fraction.iloc[:-1].mean())
    reclaim_fraction = float(event_fraction.iloc[-1])
    pre_signed = float(event["signed_quote_volume"].iloc[:-1].sum())
    reclaim_signed = float(event["signed_quote_volume"].iloc[-1])
    event_quote = float(event["quote_volume"].sum())
    event_low = float(event["low"].min())
    event_high = float(event["high"].max())
    reclaim_open = float(event["open"].iloc[-1])
    reclaim_close = float(event["close"].iloc[-1])
    entry_price = float(entry_bar["open"])

    if scene.side is Side.LONG:
        sweep_distance = max(0.0, (scene.node_price - event_low) / scene.node_price * 10_000.0)
        reclaim_distance = (reclaim_close - scene.node_price) / scene.node_price * 10_000.0
        opposing_quote = max(0.0, -pre_signed)
        reclaim_price_move = (reclaim_close - reclaim_open) / reclaim_open * 10_000.0
    else:
        sweep_distance = max(0.0, (event_high - scene.node_price) / scene.node_price * 10_000.0)
        reclaim_distance = (scene.node_price - reclaim_close) / scene.node_price * 10_000.0
        opposing_quote = max(0.0, pre_signed)
        reclaim_price_move = (reclaim_open - reclaim_close) / reclaim_open * 10_000.0
    minimum_sweep_bps = scene.tick_size / scene.node_price * 10_000.0
    opposing_per_sweep = opposing_quote / max(sweep_distance, minimum_sweep_bps)
    reclaim_millions = abs(reclaim_signed) / 1_000_000.0
    reclaim_efficiency = (
        reclaim_price_move / reclaim_millions if reclaim_millions > 0 else 0.0
    )

    features = FlowReversalFeatures(
        history_start=history_start,
        history_end=history_end,
        event_start=event_start,
        event_end=event_end,
        lower_delta_fraction=lower,
        upper_delta_fraction=upper,
        pre_reclaim_delta_fraction=pre_fraction,
        reclaim_delta_fraction=reclaim_fraction,
        pre_reclaim_signed_quote=pre_signed,
        reclaim_signed_quote=reclaim_signed,
        event_quote_volume=event_quote,
        event_low=event_low,
        event_high=event_high,
        reclaim_close=reclaim_close,
        sweep_distance_bps=sweep_distance,
        reclaim_distance_bps=reclaim_distance,
        opposing_flow_per_sweep_bps=opposing_per_sweep,
        reclaim_price_efficiency_bps_per_million=reclaim_efficiency,
    )

    location_ok = _location_valid(scene, event)
    flow_ok = _flow_valid(
        scene,
        pre_reclaim=pre_fraction,
        reclaim=reclaim_fraction,
        lower=lower,
        upper=upper,
    )
    geometry_ok = (
        scene.initial_stop < entry_price < scene.initial_target
        if scene.side is Side.LONG
        else scene.initial_target < entry_price < scene.initial_stop
    )
    if not location_ok:
        accepted, reason = False, "scene_location_not_confirmed_on_1m_flow_clock"
    elif not flow_ok:
        accepted, reason = False, "taker_flow_did_not_reverse_across_fixed_20_80_thresholds"
    elif not geometry_ok:
        accepted, reason = False, "next_minute_open_outside_frozen_trade_geometry"
    else:
        accepted, reason = True, "fixed_flow_reversal_confirmed"

    return FlowReversalDecision(
        scene=scene,
        features=features,
        accepted=accepted,
        reason=reason,
        entry_time=entry_time,
        entry_price=entry_price,
    )


__all__ = [
    "EVENT_BARS",
    "EXHAUSTION_QUANTILE",
    "FlowReversalDecision",
    "FlowReversalFeatures",
    "FlowSceneKind",
    "FrozenFlowScene",
    "HISTORY_BARS",
    "REVERSAL_QUANTILE",
    "evaluate_fixed_flow_reversal",
]
