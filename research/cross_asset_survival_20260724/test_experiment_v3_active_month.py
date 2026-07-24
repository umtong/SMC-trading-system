from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PATH = Path(__file__).parents[1] / "remote" / "cross_asset_survival_v2.py"
spec = importlib.util.spec_from_file_location("survival_experiment_v3", PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def panel_row(signal, exit_, survived, route="ETH_LONG"):
    row = {c: 0.0 for c in mod.NUMERIC}
    row.update({"signal_time": pd.Timestamp(signal), "exit_time": pd.Timestamp(exit_), "stop_survived": survived, "route": route})
    return row


def test_monthly_oof_purges_labels_crossing_test_month(monkeypatch):
    panel = pd.DataFrame([
        panel_row("2024-01-01T00:00:00Z", "2024-01-01T04:00:00Z", 0),
        panel_row("2024-01-02T00:00:00Z", "2024-01-02T04:00:00Z", 1),
        panel_row("2024-01-31T23:00:00Z", "2024-02-01T03:00:00Z", 0),
        panel_row("2024-02-05T00:00:00Z", "2024-02-05T04:00:00Z", 1),
    ])
    fitted = []

    class Dummy:
        def fit(self, X, y):
            fitted.append(list(X.index))
            return self

        def predict_proba(self, X):
            return np.column_stack([np.full(len(X), 0.4), np.full(len(X), 0.6)])

    monkeypatch.setattr(mod, "MIN_TRAIN_EVENTS", 1)
    monkeypatch.setattr(mod, "make_model", lambda: Dummy())
    pred = mod.monthly_oof(panel)
    assert np.isfinite(pred[3])
    assert fitted[-1] == [0, 1]
    assert 2 not in fitted[-1]


def test_outcome_columns_are_physically_removed():
    frame = pd.DataFrame({
        "ret3_z": [1.0], "fwd1": [2.0], "fwd_12": [3.0], "future_price": [4.0],
        "label_up": [1], "target_price": [5.0], "outcome": [1.0], "mfe_12": [2.0], "mae": [1.0],
    })
    clean, dropped = mod.drop_outcome_columns(frame)
    assert clean.columns.tolist() == ["ret3_z"]
    assert set(dropped) == set(frame.columns) - {"ret3_z"}


def test_historical_threshold_never_uses_current_or_future_probability():
    pred = np.array([np.nan, 0.1, 0.9, 0.2, 0.8])
    selected, threshold = mod.historical_quantile_selection(pred, 0.5, 2)
    assert np.isnan(threshold[2])
    assert threshold[3] == 0.5 and not selected[3]
    assert threshold[4] == 0.2 and selected[4]


def metric_template(**overrides):
    value = {
        "trades": 60, "h1_trades": 20, "h2_trades": 40, "total_return": 0.05,
        "profit_factor": 1.3, "max_drawdown": 0.05, "top5_positive_share": 0.4,
        "positive_month_fraction": 5 / 12, "active_months": 9,
        "positive_active_month_fraction": 5 / 9, "worst_month": -0.02,
        "h1_return": 0.01, "h2_return": 0.04,
    }
    value.update(overrides)
    return value


def test_development_gate_uses_active_months_not_zero_trade_months():
    metrics = {k: metric_template() for k in mod.COSTS}
    assert metrics["base"]["positive_month_fraction"] < 0.50
    assert mod.development_gate(metrics)


def test_development_gate_requires_eight_active_months():
    metrics = {k: metric_template() for k in mod.COSTS}
    metrics["base"] = metric_template(active_months=7, positive_active_month_fraction=5 / 7)
    assert not mod.development_gate(metrics)


def test_confirmation_gate_uses_same_active_month_contract():
    metrics = {k: metric_template(trades=45) for k in mod.COSTS}
    assert mod.confirmation_gate(metrics)
    metrics["base"] = metric_template(trades=45, positive_active_month_fraction=4 / 9)
    assert not mod.confirmation_gate(metrics)
