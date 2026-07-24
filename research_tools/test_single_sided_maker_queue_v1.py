from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

MODULE = Path(__file__).with_name("single_sided_maker_queue_v1.py")
spec = importlib.util.spec_from_file_location("maker_v1", MODULE)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def candidate(side: int = 1):
    return SimpleNamespace(
        side=side,
        submit_bid=100.0,
        submit_bid_qty=2.0,
        submit_ask=100.1,
        submit_ask_qty=2.0,
    )


def panel():
    idx = np.arange(0, 40, dtype=np.int64)
    return pd.DataFrame(
        {
            "sec": idx,
            "best_bid_price": 100.0,
            "best_bid_qty": 2.0,
            "best_ask_price": 100.1,
            "best_ask_qty": 2.0,
        }
    ).set_index("sec", drop=False)


def trades(times, prices, quantities, buyer_maker):
    return {
        "time": np.asarray(times, dtype=np.int64),
        "price": np.asarray(prices, dtype=float),
        "qty": np.asarray(quantities, dtype=float),
        "buyer_maker": np.asarray(buyer_maker, dtype=bool),
    }


def test_entry_requires_displayed_queue_consumption():
    c = candidate(1)
    tr = trades([1250, 1500], [100.0, 100.0], [0.9, 1.0], [True, True])
    assert m.entry_fill(c, c, tr, 1, 1.0, panel()) is None
    tr = trades([1250, 1500, 1750], [100.0, 100.0, 100.0], [0.9, 1.0, 0.2], [True, True, True])
    fill = m.entry_fill(c, c, tr, 1, 1.0, panel())
    assert fill is not None and fill["fill_ms"] == 1750 and fill["entry_price"] == 100.0


def test_wrong_aggressor_does_not_fill():
    c = candidate(1)
    tr = trades([1250, 1500, 1750], [100.0, 100.0, 100.0], [5.0, 5.0, 5.0], [False, False, False])
    assert m.entry_fill(c, c, tr, 1, 1.0, panel()) is None


def test_passive_exit_after_entry_queue_fill():
    c = candidate(1)
    fill = {"fill_ms": 1500, "entry_price": 100.0, "entry_queue": 2.0}
    tr = trades([1600, 1700, 1800], [100.1, 100.1, 100.1], [0.8, 0.8, 0.5], [False, False, False])
    out = m.exit_outcome(c, fill, tr, 3, 4.0, 1.0, panel())
    assert out is not None and out["exit_kind"] == "maker_exit" and out["exit_ms"] == 1800


def test_stop_wins_same_trade_ambiguity():
    c = candidate(1)
    fill = {"fill_ms": 1500, "entry_price": 100.0, "entry_queue": 2.0}
    # Submission spread is 0.1 and stop is 0.2 below entry for 2 spreads.
    tr = trades([1600, 1700], [99.79, 100.1], [10.0, 10.0], [True, False])
    out = m.exit_outcome(c, fill, tr, 3, 2.0, 1.0, panel())
    assert out is not None and out["exit_kind"] == "taker_stop" and out["exit_ms"] == 1600


def test_horizon_uses_first_actual_trade_and_delay_bound():
    c = candidate(1)
    fill = {"fill_ms": 1500, "entry_price": 100.0, "entry_queue": 2.0}
    tr = trades([4600], [100.02], [0.1], [True])
    out = m.exit_outcome(c, fill, tr, 3, 8.0, 10.0, panel())
    assert out is not None and out["exit_kind"] == "taker_horizon" and out["exit_ms"] == 4600
    tr = trades([7001], [100.02], [0.1], [True])
    assert m.exit_outcome(c, fill, tr, 3, 8.0, 10.0, panel()) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print({"tests": len(tests), "status": "PASS"})
