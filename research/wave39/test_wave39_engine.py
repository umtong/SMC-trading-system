from __future__ import annotations

import numpy as np

from research.wave39.wave39_engine import SymbolPaths, greedy_one_slot, simulate_stop_time_paths


def _paths():
    minute_time = np.arange(20, dtype=np.int64) * 60_000
    open_price = np.full(20, 100.0)
    high_price = np.full(20, 101.0)
    low_price = np.full(20, 99.0)
    boundary = np.asarray([0], dtype=np.int64)
    atr = np.asarray([1.0])
    gross, exits, stopped, entries, entry_value = simulate_stop_time_paths(
        open_price,
        high_price,
        low_price,
        boundary,
        atr,
        np.asarray([2], dtype=np.int64),
        np.asarray([3.0], dtype=np.float64),
        np.asarray([1], dtype=np.int64),
    )
    return SymbolPaths(
        symbol="TEST",
        event_time_ms=np.asarray([0], dtype=np.int64),
        boundary_minute_index=boundary,
        entry_time_ms=np.asarray([60_000], dtype=np.int64),
        gross=gross,
        exit_index=exits,
        stopped=stopped,
        entry_index=entries,
        entry_value=entry_value,
        minute_time_ms=minute_time,
        funding_time_ms=np.asarray([120_000], dtype=np.int64),
        funding_rate=np.asarray([0.001], dtype=np.float64),
        horizons=np.asarray([2], dtype=np.int64),
        stops=np.asarray([3.0], dtype=np.float64),
        latencies=np.asarray([1], dtype=np.int64),
    )


def test_next_completed_minute_entry_and_funding_sign():
    paths = _paths()
    long_net, long_entry, long_exit, _ = paths.outcome(
        side=np.asarray([1], dtype=np.int8),
        horizon_index=0,
        stop_index=0,
        latency_index=0,
        round_trip_bp=0,
        stop_extra_bp=0,
    )
    short_net, _, _, _ = paths.outcome(
        side=np.asarray([-1], dtype=np.int8),
        horizon_index=0,
        stop_index=0,
        latency_index=0,
        round_trip_bp=0,
        stop_extra_bp=0,
    )
    assert long_entry[0] == 60_000
    assert long_exit[0] == 180_000
    assert np.isclose(long_net[0], -0.001)
    assert np.isclose(short_net[0], 0.001)


def test_gap_stop_uses_adverse_open():
    open_price = np.asarray([100.0, 100.0, 95.0, 95.0, 95.0])
    high_price = np.asarray([100.0, 101.0, 96.0, 96.0, 96.0])
    low_price = np.asarray([100.0, 99.0, 94.0, 94.0, 94.0])
    gross, exits, stopped, entries, values = simulate_stop_time_paths(
        open_price,
        high_price,
        low_price,
        np.asarray([0], dtype=np.int64),
        np.asarray([1.0]),
        np.asarray([3], dtype=np.int64),
        np.asarray([3.0]),
        np.asarray([1], dtype=np.int64),
    )
    assert entries[0, 0] == 1
    assert values[0, 0] == 100.0
    assert stopped[0, 0, 0, 0, 0] == 1
    assert exits[0, 0, 0, 0, 0] == 2
    assert np.isclose(gross[0, 0, 0, 0, 0], np.log(95.0 / 100.0))


def test_global_slot_rejects_overlap_and_same_minute_reentry():
    selected = greedy_one_slot(
        np.asarray([0, 1, 2, 3], dtype=np.int64),
        np.asarray([100, 150, 200, 301], dtype=np.int64),
        np.asarray([200, 250, 300, 400], dtype=np.int64),
    )
    assert selected.tolist() == [0, 3]
