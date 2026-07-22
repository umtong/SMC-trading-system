from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from optimize_central_account_risk import capacity_metrics, evaluate_grid  # noqa: E402


def policy() -> dict:
    return {
        "risk_optimization": {
            "candidate_risk_fractions": [0.01, 0.02],
            "venue_leverage_cap": 5.0,
            "block_bootstrap_quarters": 1,
            "maximum_p95_drawdown": 0.50,
            "minimum_p05_final_multiple": 0.0,
            "maximum_ruin_probability": 0.50,
        },
        "treasury": {
            "margin_buffer_fraction": 0.25,
            "loss_buffer_multiples": 2.0,
            "minimum_bank_fraction": 0.35,
            "provisional_target_trading_fraction": 0.60,
        },
        "frequency": {
            "recommended_completed_trades_per_complete_operating_day": 1.0,
            "hard_gate": False,
        },
    }


def test_capacity_rejects_risk_that_breaks_minimum_bank_reserve() -> None:
    one_percent = capacity_metrics(
        pd.Series([2.0]).to_numpy(float),
        risk_fraction=0.01,
        venue_leverage_cap=5.0,
        margin_buffer_fraction=0.25,
        loss_buffer_multiples=2.0,
        minimum_bank_fraction=0.35,
        provisional_target_trading_fraction=0.60,
        capacity_quantile=0.99,
    )
    two_percent = capacity_metrics(
        pd.Series([2.0]).to_numpy(float),
        risk_fraction=0.02,
        venue_leverage_cap=5.0,
        margin_buffer_fraction=0.25,
        loss_buffer_multiples=2.0,
        minimum_bank_fraction=0.35,
        provisional_target_trading_fraction=0.60,
        capacity_quantile=0.99,
    )

    assert one_percent["required_trading_fraction"] == pytest.approx(0.52)
    assert one_percent["capacity_passed"] is True
    assert two_percent["required_trading_fraction"] == pytest.approx(1.04)
    assert two_percent["capacity_passed"] is False


def test_grid_keeps_frequency_soft_and_selects_only_capacity_feasible_risk() -> None:
    ledger = pd.DataFrame(
        {
            "entry_time": [
                "2024-01-05T00:00:00Z",
                "2024-04-05T00:00:00Z",
                "2024-07-05T00:00:00Z",
                "2024-10-05T00:00:00Z",
            ],
            "exit_time": [
                "2024-01-05T01:00:00Z",
                "2024-04-05T01:00:00Z",
                "2024-07-05T01:00:00Z",
                "2024-10-05T01:00:00Z",
            ],
            "net_r": [1.0, -0.25, 1.0, -0.25],
            "leverage_at_1pct": [2.0, 2.0, 2.0, 2.0],
        }
    )

    grid, report = evaluate_grid(
        ledger,
        policy=policy(),
        return_column="net_r",
        entry_column="entry_time",
        exit_column="exit_time",
        leverage_column="leverage_at_1pct",
        simulations=500,
        seed=7,
    )

    selected = report["research_optimum"]
    assert selected is not None
    assert selected["risk_fraction"] == pytest.approx(0.01)
    assert bool(grid.loc[grid["risk_fraction"] == 0.02, "capacity_passed"].iloc[0]) is False
    assert report["frequency"]["hard_gate"] is False
    assert report["frequency"]["recommendation_met"] is False
