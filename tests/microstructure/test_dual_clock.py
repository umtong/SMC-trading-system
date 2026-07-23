from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from ictbt.easychart_v0.domain import Side
from ictbt.microstructure.dual_clock import (
    DualClockSceneKind,
    FrozenDualClockScene,
    REFERENCE_WINDOWS,
    evaluate_fixed_dual_clock_flow,
    required_dual_clock_flow_interval,
)


START = pd.Timestamp("2025-01-01T00:00:00Z")
EVENT_START = START + pd.Timedelta(minutes=240)
EVENT_END = EVENT_START + pd.Timedelta(minutes=15)
CONFIRMATION_START = EVENT_END + pd.Timedelta(minutes=15)
CONFIRMATION_END = CONFIRMATION_START + pd.Timedelta(minutes=5)


def scene(
    side: Side,
    *,
    kind: DualClockSceneKind = DualClockSceneKind.SWEEP_REVERSAL,
) -> FrozenDualClockScene:
    return FrozenDualClockScene(
        scene_id=f"{kind.value}-{side.value}",
        symbol="BTCUSDT",
        side=side,
        kind=kind,
        node_price=100.0,
        event_started_at=EVENT_START,
        event_known_at=EVENT_END,
        confirmation_started_at=CONFIRMATION_START,
        confirmation_known_at=CONFIRMATION_END,
        initial_stop=99.0 if side is Side.LONG else 101.0,
        initial_target=102.0 if side is Side.LONG else 98.0,
        tick_size=0.1,
    )


def flow(
    side: Side,
    *,
    kind: DualClockSceneKind = DualClockSceneKind.SWEEP_REVERSAL,
) -> pd.DataFrame:
    end = CONFIRMATION_END + pd.Timedelta(minutes=1)
    index = pd.date_range(START, end, freq="1min", inclusive="left")
    count = len(index)
    baseline = np.resize(np.linspace(-0.5, 0.5, 21), count)
    quote = np.full(count, 1_000_000.0)
    open_price = np.full(count, 100.0)
    close = np.full(count, 100.0)
    high = np.full(count, 100.05)
    low = np.full(count, 99.95)

    event_mask = (index >= EVENT_START) & (index < EVENT_END)
    confirm_mask = (index >= CONFIRMATION_START) & (index < CONFIRMATION_END)
    event_indices = np.flatnonzero(event_mask)
    confirmation_indices = np.flatnonzero(confirm_mask)

    direction = 1.0 if side is Side.LONG else -1.0
    if kind is DualClockSceneKind.SWEEP_REVERSAL:
        baseline[event_indices] = -0.9 * direction
        baseline[confirmation_indices] = 0.9 * direction
        if side is Side.LONG:
            low[event_indices[4]] = 99.7
            close[event_indices[-1]] = 100.2
            high[event_indices[-1]] = 100.25
            open_price[confirmation_indices] = np.linspace(100.2, 100.4, 5)
            close[confirmation_indices] = np.linspace(100.25, 100.5, 5)
            high[confirmation_indices] = close[confirmation_indices] + 0.05
            low[confirmation_indices] = open_price[confirmation_indices] - 0.05
            entry_open = 100.55
        else:
            high[event_indices[4]] = 100.3
            close[event_indices[-1]] = 99.8
            low[event_indices[-1]] = 99.75
            open_price[confirmation_indices] = np.linspace(99.8, 99.6, 5)
            close[confirmation_indices] = np.linspace(99.75, 99.5, 5)
            high[confirmation_indices] = open_price[confirmation_indices] + 0.05
            low[confirmation_indices] = close[confirmation_indices] - 0.05
            entry_open = 99.45
    else:
        baseline[event_indices] = 0.9 * direction
        baseline[confirmation_indices] = 0.35 * direction
        if side is Side.LONG:
            open_price[event_indices] = np.linspace(99.9, 100.3, 15)
            close[event_indices] = np.linspace(100.0, 100.4, 15)
            high[event_indices] = close[event_indices] + 0.05
            low[event_indices] = open_price[event_indices] - 0.05
            open_price[confirmation_indices] = np.linspace(100.4, 100.55, 5)
            close[confirmation_indices] = np.linspace(100.45, 100.65, 5)
            high[confirmation_indices] = close[confirmation_indices] + 0.05
            low[confirmation_indices] = open_price[confirmation_indices] - 0.05
            entry_open = 100.7
        else:
            open_price[event_indices] = np.linspace(100.1, 99.7, 15)
            close[event_indices] = np.linspace(100.0, 99.6, 15)
            high[event_indices] = open_price[event_indices] + 0.05
            low[event_indices] = close[event_indices] - 0.05
            open_price[confirmation_indices] = np.linspace(99.6, 99.45, 5)
            close[confirmation_indices] = np.linspace(99.55, 99.35, 5)
            high[confirmation_indices] = open_price[confirmation_indices] + 0.05
            low[confirmation_indices] = close[confirmation_indices] - 0.05
            entry_open = 99.3

    entry_index = int(np.flatnonzero(index == CONFIRMATION_END)[0])
    open_price[entry_index] = entry_open
    close[entry_index] = entry_open
    high[entry_index] = entry_open + 0.05
    low[entry_index] = entry_open - 0.05

    # Rebuild untouched event ranges around modified closes.
    high = np.maximum(high, np.maximum(open_price, close))
    low = np.minimum(low, np.minimum(open_price, close))
    return pd.DataFrame(
        {
            "symbol": "BTCUSDT",
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "quote_volume": quote,
            "signed_quote_volume": baseline * quote,
        },
        index=pd.DatetimeIndex(index, name="open_time"),
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_sweep_reversal_uses_actual_event_and_later_confirmation(side: Side) -> None:
    decision = evaluate_fixed_dual_clock_flow(scene(side), flow(side))

    assert decision.accepted
    assert decision.reason == "fixed_dual_clock_flow_confirmed"
    assert decision.entry_time == CONFIRMATION_END
    assert decision.features.event.duration_minutes == 15
    assert decision.features.confirmation.duration_minutes == 5
    assert decision.features.event.reference_windows == REFERENCE_WINDOWS
    assert decision.features.confirmation.reference_windows == REFERENCE_WINDOWS
    if side is Side.LONG:
        assert (
            decision.features.event.delta_fraction
            < decision.features.event.lower_delta_fraction
        )
        assert (
            decision.features.confirmation.delta_fraction
            > decision.features.confirmation.upper_delta_fraction
        )
    else:
        assert (
            decision.features.event.delta_fraction
            > decision.features.event.upper_delta_fraction
        )
        assert (
            decision.features.confirmation.delta_fraction
            < decision.features.confirmation.lower_delta_fraction
        )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_break_continuation_has_separate_flow_semantics(side: Side) -> None:
    selected = scene(side, kind=DualClockSceneKind.BREAK_CONTINUATION)
    selected_flow = flow(side, kind=DualClockSceneKind.BREAK_CONTINUATION)
    continuation = evaluate_fixed_dual_clock_flow(selected, selected_flow)
    incorrectly_relabelled = evaluate_fixed_dual_clock_flow(
        replace(selected, kind=DualClockSceneKind.SWEEP_REVERSAL),
        selected_flow,
    )

    assert continuation.accepted
    assert not incorrectly_relabelled.accepted
    assert incorrectly_relabelled.reason == "dual_clock_location_not_confirmed"


def test_future_flow_cannot_change_past_decision() -> None:
    selected = scene(Side.LONG)
    base = flow(Side.LONG)
    decision = evaluate_fixed_dual_clock_flow(selected, base)
    future_time = CONFIRMATION_END + pd.Timedelta(minutes=1)
    future = pd.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "open": [100.0],
            "high": [200.0],
            "low": [50.0],
            "close": [150.0],
            "quote_volume": [10_000_000_000.0],
            "signed_quote_volume": [-10_000_000_000.0],
        },
        index=pd.DatetimeIndex([future_time], name="open_time"),
    )
    repeated = evaluate_fixed_dual_clock_flow(selected, pd.concat((base, future)))

    assert repeated == decision


def test_missing_required_minute_is_not_zero_filled() -> None:
    selected = scene(Side.LONG)
    start, _ = required_dual_clock_flow_interval(selected)
    missing = flow(Side.LONG).drop(start + pd.Timedelta(minutes=10))

    with pytest.raises(ValueError, match="missing causal 1m flow bars"):
        evaluate_fixed_dual_clock_flow(selected, missing)


def test_confirmation_cannot_begin_before_event_is_known() -> None:
    with pytest.raises(ValueError, match="before the event is known"):
        replace(
            scene(Side.LONG),
            confirmation_started_at=EVENT_END - pd.Timedelta(minutes=1),
        )
