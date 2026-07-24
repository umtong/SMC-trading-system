from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / "run_research.py"
SPEC = importlib.util.spec_from_file_location("orderflow_selection_merge", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
r = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = r
SPEC.loader.exec_module(r)


def test_finalist_selection_uses_development_gate_when_names_overlap() -> None:
    spec = r.Spec("flow_continuation", {"test": 1})
    development = pd.DataFrame([{
        "spec_id": spec.spec_id,
        "robustness_score": 0.1,
        "positive_fold_share": 0.75,
        "worst_fold_g": 0.0001,
        "top_5_positive_share": 0.50,
    }])
    selection = pd.DataFrame([{
        "spec_id": spec.spec_id,
        "family": spec.family,
        "geometric_daily_return": 0.0002,
        "total_return": 0.10,
        "max_drawdown": -0.05,
        "profit_factor": 1.5,
        "trades": 30,
        # Deliberately conflicting selection-fold fields. These must not
        # overwrite the pre-registered development robustness gate.
        "positive_fold_share": 0.0,
        "worst_fold_g": -0.01,
        "top_5_positive_share": 0.99,
    }])
    stress = pd.DataFrame([{
        "spec_id": spec.spec_id,
        "geometric_daily_return": 0.0001,
        "total_return": 0.05,
        "max_drawdown": -0.06,
        "profit_factor": 1.2,
    }])
    result = r.select_finalists(development, selection, stress, {spec.spec_id: spec})
    assert result == [spec]
