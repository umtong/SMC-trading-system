from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / "run_research.py"
SPEC = importlib.util.spec_from_file_location("orderflow_research", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
r = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = r
SPEC.loader.exec_module(r)


def make_bars(values: list[tuple[float, float, float, float]], start="2021-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(values), freq="5min", tz="UTC")
    rows = []
    for ts, (op, hi, lo, cl) in zip(idx, values):
        rows.append({
            "open_time": ts, "close_time": ts + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1),
            "symbol": "BTCUSDT", "open": op, "high": hi, "low": lo, "close": cl,
            "volume": 1000.0, "quote_volume": 100_000_000.0, "trade_count": 10000,
            "taker_buy_volume": 500.0, "taker_buy_quote_volume": 50_000_000.0,
        })
    return pd.DataFrame(rows)


def registration() -> dict:
    payload = json.loads((SCRIPT.parent / "registration.json").read_text())
    payload["risk"]["max_quote_volume_participation"] = 1.0
    return payload


def event(entry_index=1, side=1, atr=1.0, hold=2, target_rr=np.nan) -> pd.DataFrame:
    return pd.DataFrame([{
        "symbol": "BTCUSDT", "signal_time": pd.Timestamp("2021-01-01 00:05", tz="UTC"),
        "entry_index": entry_index, "side": side, "score": 3.0, "atr": atr,
        "stop_atr": 1.0, "hold_15m": hold, "target_rr": target_rr,
        "family": "test", "spec_id": "test",
    }])


def test_shifted_z_does_not_use_current_observation():
    x = pd.Series(([1.0, 2.0] * 15) + [100.0])
    z = r.shifted_z(x, 20, min_fraction=0.5)
    mutated = x.copy(); mutated.iloc[-1] = 200.0
    z2 = r.shifted_z(mutated, 20, min_fraction=0.5)
    hist = x.shift(1).rolling(20, min_periods=10).mean()
    hist2 = mutated.shift(1).rolling(20, min_periods=10).mean()
    assert hist.iloc[-1] == hist2.iloc[-1]
    assert z2.iloc[-1] > z.iloc[-1]


def test_same_bar_stop_has_priority_over_target():
    bars = make_bars([
        (100, 101, 99, 100),
        (100, 102, 98, 100),
        (100, 100, 100, 100), (100, 100, 100, 100),
        (100, 100, 100, 100), (100, 100, 100, 100),
        (100, 100, 100, 100), (100, 100, 100, 100),
    ])
    empty_funding = pd.DataFrame(columns=["calc_time", "symbol", "funding_interval_hours", "last_funding_rate"])
    costs = r.CostConfig(0, 0, 0, 0, 0, 0)
    trades, _, _ = r.simulate(event(target_rr=1.0), {"BTCUSDT": bars}, {"BTCUSDT": empty_funding}, registration(), costs,
                              pd.Timestamp("2021-01-01", tz="UTC"), pd.Timestamp("2021-01-02", tz="UTC"))
    assert trades.iloc[0].reason == "stop"


def test_alpha_decay_executes_before_last_bar_intrabar_path():
    bars = make_bars([
        (100, 100, 100, 100),
        (100, 100.2, 99.8, 100), (100, 100.2, 99.8, 100),
        (100, 100.2, 99.8, 100), (100, 100.2, 99.8, 100),
        (100, 100.2, 99.8, 100), (100, 100.2, 99.8, 100),
        (100, 100.2, 99.8, 100), (100, 200, 1, 100),
    ])
    empty_funding = pd.DataFrame(columns=["calc_time", "symbol", "funding_interval_hours", "last_funding_rate"])
    costs = r.CostConfig(0, 0, 0, 0, 0, 0)
    trades, _, _ = r.simulate(event(hold=2, atr=10.0, target_rr=5.0), {"BTCUSDT": bars}, {"BTCUSDT": empty_funding}, registration(), costs,
                              pd.Timestamp("2021-01-01", tz="UTC"), pd.Timestamp("2021-01-02", tz="UTC"))
    assert trades.iloc[0].reason == "alpha_decay"


def test_overlapping_signal_is_ignored_by_global_slot():
    bars = make_bars([(100, 100.2, 99.8, 100)] * 30)
    events = pd.concat([event(entry_index=1, hold=2), event(entry_index=2, hold=2)], ignore_index=True)
    events.loc[1, "signal_time"] = pd.Timestamp("2021-01-01 00:10", tz="UTC")
    empty_funding = pd.DataFrame(columns=["calc_time", "symbol", "funding_interval_hours", "last_funding_rate"])
    costs = r.CostConfig(0, 0, 0, 0, 0, 0)
    trades, _, diag = r.simulate(events, {"BTCUSDT": bars}, {"BTCUSDT": empty_funding}, registration(), costs,
                                 pd.Timestamp("2021-01-01", tz="UTC"), pd.Timestamp("2021-01-02", tz="UTC"))
    assert len(trades) == 1
    assert diag["ignored_signals"] == 1
