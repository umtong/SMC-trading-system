from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / "run_research.py"
SPEC = importlib.util.spec_from_file_location("orderflow_research_nan", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
r = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = r
SPEC.loader.exec_module(r)


def test_event_generation_tolerates_feature_warmup_nans() -> None:
    idx = pd.date_range("2021-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame(index=idx)
    frame["signal_time"] = idx + pd.Timedelta(minutes=15)
    frame["entry_index"] = np.arange(8) * 3 + 3
    frame["atr"] = 10.0
    frame["volume_z_30"] = [np.nan] * 7 + [1.0]
    frame["imb_z_1_7"] = [np.nan] * 7 + [3.0]
    frame["ret_1"] = [np.nan] * 7 + [0.01]
    frame["break_high_96"] = False
    frame["break_low_96"] = False
    spec = r.Spec("flow_continuation", {
        "flow_window": 1,
        "norm_days": 7,
        "threshold": 2.5,
        "min_volume_z": 0.0,
        "confirm": "same_sign",
        "hold_15m": 8,
        "stop_atr": 2.0,
        "target_rr": None,
    })
    events = r.events_for_spec(
        {"BTCUSDT": frame},
        spec,
        pd.Timestamp("2021-01-01", tz="UTC"),
        pd.Timestamp("2021-01-02", tz="UTC"),
    )
    assert len(events) == 1
    assert int(events.iloc[0].side) == 1
