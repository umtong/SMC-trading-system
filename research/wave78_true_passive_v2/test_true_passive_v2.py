from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
core_spec = importlib.util.spec_from_file_location("wave78_single_slot_passive", ROOT / "wave78_single_slot_passive.py")
assert core_spec and core_spec.loader
core = importlib.util.module_from_spec(core_spec)
sys.modules[core_spec.name] = core
core_spec.loader.exec_module(core)
runner_spec = importlib.util.spec_from_file_location("wave78_true_v2", ROOT / "wave78_true_spread_capture_v2.py")
assert runner_spec and runner_spec.loader
runner = importlib.util.module_from_spec(runner_spec)
sys.modules[runner_spec.name] = runner
runner_spec.loader.exec_module(runner)


def market(exit_trade_qty: float = 2.02, extra_book: bool = True):
    book = pd.DataFrame({
        "best_bid_price": [99.0, 100.0] if extra_book else [99.0],
        "best_bid_qty": [1.0, 1.0] if extra_book else [1.0],
        "best_ask_price": [101.0, 102.0] if extra_book else [101.0],
        "best_ask_qty": [1.0, 1.0] if extra_book else [1.0],
        "event_time": [1000, 31350] if extra_book else [1000],
    })
    trades = pd.DataFrame({
        "time_ms": [1200, 1400],
        "price": [99.0, 101.0],
        "qty": [2.02, exit_trade_qty],
        "buyer_maker": [True, False],
    })
    panel = pd.DataFrame({"sec": [0], "known_time_ms": [1000]})
    return core.MarketDay("BTCUSDT", "1970-01-01", panel, book, trades), panel.iloc[0]


def test_passive_entry_and_passive_exit_capture_spread():
    day, row = market(2.02)
    result = runner.simulate_true_spread_roundtrip(day, row, 1, 1.0)
    assert result is not None and result.filled
    assert result.exit_mode == "passive_passive"
    assert result.passive_exit_qty == result.fill_qty
    assert result.taker_exit_qty == 0
    assert np.isclose(result.gross_log, np.log(101 / 99))
    assert result.free_time_ms == 1400


def test_partial_passive_exit_then_taker_tracks_both_parts():
    day, row = market(2.005)
    result = runner.simulate_true_spread_roundtrip(day, row, 1, 1.0)
    assert result is not None and result.filled
    assert result.exit_mode == "passive_partial_then_taker"
    assert 0 < result.passive_exit_qty < result.fill_qty
    assert result.taker_exit_qty > 0
    expected = (
        result.passive_exit_qty * np.log(101 / 99)
        + result.taker_exit_qty * np.log(100 / 99)
    ) / result.fill_qty
    assert np.isclose(result.gross_log, expected)
    assert result.free_time_ms == 31350


def test_unfilled_entry_occupies_slot_until_ttl():
    day, row = market(2.02)
    empty = core.MarketDay(day.symbol, day.day, day.panel, day.book, day.trades.iloc[0:0].copy())
    result = runner.simulate_true_spread_roundtrip(empty, row, 1, 1.0)
    assert result is not None and not result.filled
    assert result.free_time_ms == 6100


def test_global_slot_prefers_score_and_blocks_overlaps():
    a = runner.RoundTrip(1000, 5000, "d", "BTCUSDT", 1, 2.0, False)
    b = runner.RoundTrip(1000, 2000, "d", "ETHUSDT", -1, 1.0, False)
    c = runner.RoundTrip(3000, 4000, "d", "BTCUSDT", 1, 3.0, False)
    d = runner.RoundTrip(5000, 6000, "d", "ETHUSDT", -1, 1.0, False)
    routed = core.route_single_slot([a, b, c, d])
    assert routed.symbol.tolist() == ["BTCUSDT", "ETHUSDT"]
    assert routed.signal_time_ms.tolist() == [1000, 5000]


def test_side_features_flip_only_directional_values():
    frame = pd.DataFrame({
        "is_eth": [0.0], "spread_bps": [1.0], "log_depth_notional": [5.0], "quote_age_ms": [10.0],
        "trade_notional_z": [1.0], "trade_count_z": [2.0], "rv_30s_ticks": [0.5], "flow_price_efficiency": [0.2],
        "l1_imbalance": [0.3], "microprice_ticks": [0.4], "flow_imb_1s": [0.5], "flow_imb_5s": [0.6],
        "flow_imb_30s": [0.7], "flow_acceleration": [0.8], "ret_1s_ticks": [0.9], "ret_5s_ticks": [1.0],
    })
    long = runner.side_frame(frame, 1)
    short = runner.side_frame(frame, -1)
    for feature in runner.DIRECTIONAL_FEATURES:
        assert np.isclose(long[f"side_{feature}"].iloc[0], -short[f"side_{feature}"].iloc[0])
    assert long.spread_bps.iloc[0] == short.spread_bps.iloc[0]
