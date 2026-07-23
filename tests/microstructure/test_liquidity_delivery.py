from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ictbt.easychart_v0.domain import Side
from ictbt.microstructure import (
    FlowSceneKind,
    FrozenFlowScene,
    evaluate_fixed_flow_reversal,
)


START = pd.Timestamp("2025-01-01T00:00:00Z")
KNOWN = START + pd.Timedelta(minutes=125)


def scene(side: Side, *, kind: FlowSceneKind = FlowSceneKind.SWEEP_RECLAIM) -> FrozenFlowScene:
    return FrozenFlowScene(
        scene_id=f"scene-{side.value}",
        symbol="BTCUSDT",
        side=side,
        kind=kind,
        node_price=100.0,
        known_at=KNOWN,
        initial_stop=99.0 if side is Side.LONG else 101.0,
        initial_target=102.0 if side is Side.LONG else 98.0,
        tick_size=0.1,
    )


def flow(side: Side, *, reclaim_fraction: float | None = None) -> pd.DataFrame:
    index = pd.date_range(START, periods=126, freq="1min", tz="UTC")
    history_fraction = np.linspace(-0.5, 0.5, 120)
    if side is Side.LONG:
        event_fraction = np.array([-0.9, -0.85, -0.8, -0.75, 0.9])
        event_open = np.array([100.0, 99.95, 99.9, 99.85, 99.9])
        event_close = np.array([99.95, 99.9, 99.85, 99.9, 100.2])
        event_high = np.maximum(event_open, event_close) + 0.05
        event_low = np.minimum(event_open, event_close) - 0.05
        event_low[2] = 99.75
        entry_open = 100.3
    else:
        event_fraction = np.array([0.9, 0.85, 0.8, 0.75, -0.9])
        event_open = np.array([100.0, 100.05, 100.1, 100.15, 100.1])
        event_close = np.array([100.05, 100.1, 100.15, 100.1, 99.8])
        event_high = np.maximum(event_open, event_close) + 0.05
        event_low = np.minimum(event_open, event_close) - 0.05
        event_high[2] = 100.25
        entry_open = 99.7
    if reclaim_fraction is not None:
        event_fraction[-1] = reclaim_fraction

    open_prices = np.full(126, 100.0)
    close_prices = np.full(126, 100.0)
    high_prices = np.full(126, 100.05)
    low_prices = np.full(126, 99.95)
    open_prices[120:125] = event_open
    close_prices[120:125] = event_close
    high_prices[120:125] = event_high
    low_prices[120:125] = event_low
    open_prices[125] = entry_open
    close_prices[125] = entry_open
    high_prices[125] = entry_open + 0.05
    low_prices[125] = entry_open - 0.05

    quote = np.full(126, 1_000_000.0)
    fractions = np.concatenate((history_fraction, event_fraction, [0.0]))
    return pd.DataFrame(
        {
            "symbol": "BTCUSDT",
            "open": open_prices,
            "high": high_prices,
            "low": low_prices,
            "close": close_prices,
            "quote_volume": quote,
            "signed_quote_volume": fractions * quote,
        },
        index=pd.DatetimeIndex(index, name="open_time"),
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_fixed_20_80_flow_reversal_accepts_symmetric_confirmations(side: Side) -> None:
    decision = evaluate_fixed_flow_reversal(scene(side), flow(side))

    assert decision.accepted
    assert decision.reason == "fixed_flow_reversal_confirmed"
    assert decision.entry_time == KNOWN
    assert decision.entry_price == pytest.approx(100.3 if side is Side.LONG else 99.7)
    if side is Side.LONG:
        assert decision.features.pre_reclaim_delta_fraction < decision.features.lower_delta_fraction
        assert decision.features.reclaim_delta_fraction > decision.features.upper_delta_fraction
        assert decision.features.sweep_distance_bps > 0
    else:
        assert decision.features.pre_reclaim_delta_fraction > decision.features.upper_delta_fraction
        assert decision.features.reclaim_delta_fraction < decision.features.lower_delta_fraction
        assert decision.features.sweep_distance_bps > 0


def test_thresholds_never_use_event_or_future_bars() -> None:
    base = flow(Side.LONG)
    decision = evaluate_fixed_flow_reversal(scene(Side.LONG), base)
    future = pd.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "open": [100.3],
            "high": [101.0],
            "low": [99.0],
            "close": [100.8],
            "quote_volume": [10_000_000_000.0],
            "signed_quote_volume": [-10_000_000_000.0],
        },
        index=pd.DatetimeIndex([KNOWN + pd.Timedelta(minutes=1)], name="open_time"),
    )
    with_future = pd.concat((base, future))
    repeated = evaluate_fixed_flow_reversal(scene(Side.LONG), with_future)

    assert repeated.features.lower_delta_fraction == pytest.approx(
        decision.features.lower_delta_fraction
    )
    assert repeated.features.upper_delta_fraction == pytest.approx(
        decision.features.upper_delta_fraction
    )
    assert repeated.accepted == decision.accepted
    assert repeated.entry_price == decision.entry_price


def test_missing_minute_is_hard_data_error_not_zero_flow() -> None:
    missing = flow(Side.LONG).drop(START + pd.Timedelta(minutes=50))

    with pytest.raises(ValueError, match="missing causal 1m flow bars"):
        evaluate_fixed_flow_reversal(scene(Side.LONG), missing)


def test_weak_reclaim_flow_is_rejected_without_retuning_thresholds() -> None:
    decision = evaluate_fixed_flow_reversal(
        scene(Side.LONG),
        flow(Side.LONG, reclaim_fraction=0.0),
    )

    assert not decision.accepted
    assert decision.reason == "taker_flow_did_not_reverse_across_fixed_20_80_thresholds"


def test_next_open_outside_frozen_geometry_is_rejected() -> None:
    bad = flow(Side.LONG)
    bad.loc[KNOWN, ["open", "high", "low", "close"]] = [102.1, 102.2, 102.0, 102.1]
    decision = evaluate_fixed_flow_reversal(scene(Side.LONG), bad)

    assert not decision.accepted
    assert decision.reason == "next_minute_open_outside_frozen_trade_geometry"


def test_break_acceptance_requires_only_directional_node_acceptance() -> None:
    no_deep_sweep = flow(Side.LONG)
    no_deep_sweep.loc[KNOWN - pd.Timedelta(minutes=3), "low"] = 99.95
    sweep = evaluate_fixed_flow_reversal(scene(Side.LONG), no_deep_sweep)
    acceptance = evaluate_fixed_flow_reversal(
        scene(Side.LONG, kind=FlowSceneKind.BREAK_ACCEPTANCE),
        no_deep_sweep,
    )

    assert not sweep.accepted
    assert sweep.reason == "scene_location_not_confirmed_on_1m_flow_clock"
    assert acceptance.accepted
