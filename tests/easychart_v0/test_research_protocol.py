from __future__ import annotations

import pandas as pd
import pytest

from ictbt.easychart_v0.research_protocol import (
    GrowthGate,
    TrialPerformance,
    YearCoverage,
    bootstrap_path_stress,
    evaluate_growth_gate,
    sample_trials,
)


def test_trials_are_deterministic_unique_and_use_exactly_140_scored_days() -> None:
    coverages = tuple(
        YearCoverage(
            year,
            pd.Timestamp(f"{year}-01-01", tz="UTC"),
            pd.Timestamp(f"{year + 1}-01-01", tz="UTC"),
        )
        for year in range(2022, 2027)
    )

    first = sample_trials(coverages, trial_count=12, seed=73)
    second = sample_trials(coverages, trial_count=12, seed=73)

    assert first == second
    assert len({trial.fingerprint for trial in first}) == 12
    assert {trial.score_days for trial in first} == {140}
    for trial in first:
        assert [window.year for window in trial.windows] == list(range(2022, 2027))
        assert all(window.score_days == 28 for window in trial.windows)
        assert all(
            window.data_start == window.score_start - pd.Timedelta(days=35)
            for window in trial.windows
        )
        assert all(
            window.data_end == window.score_end + pd.Timedelta(days=7)
            for window in trial.windows
        )


def test_partial_2026_coverage_never_samples_beyond_available_data() -> None:
    coverages = (
        YearCoverage(
            2026,
            pd.Timestamp("2026-01-01", tz="UTC"),
            pd.Timestamp("2026-07-20", tz="UTC"),
        ),
    )

    trials = sample_trials(coverages, trial_count=30, seed=11)

    assert max(trial.windows[0].data_end for trial in trials) <= pd.Timestamp(
        "2026-07-20", tz="UTC"
    )


def performance(
    index: int,
    *,
    final_equity: float,
    trades: int = 141,
    operating_days: int = 140,
) -> TrialPerformance:
    return TrialPerformance(
        f"trial-{index}",
        10_000,
        final_equity,
        0.18,
        trades,
        min(trades, 85),
        55.0,
        operating_days=operating_days,
    )


def test_growth_gate_requires_repeated_five_x_not_one_lucky_trial() -> None:
    performances = tuple(
        performance(index, final_equity=51_000 if index else 49_000)
        for index in range(20)
    )

    result = evaluate_growth_gate(performances)

    assert not result.passed
    assert "target_multiple_not_repeated" in result.reasons
    assert "completed_trades_not_above_operating_days" not in result.reasons
    assert result.summary.target_hit_rate == pytest.approx(0.95)


def test_growth_gate_requires_completed_trades_to_exceed_operating_days() -> None:
    exactly_one_per_day = tuple(
        performance(index, final_equity=51_000, trades=140)
        for index in range(20)
    )
    failed = evaluate_growth_gate(exactly_one_per_day)

    assert not failed.passed
    assert "completed_trades_not_above_operating_days" in failed.reasons
    assert failed.summary.minimum_trades_per_operating_day == 1.0
    assert failed.summary.minimum_trade_surplus_over_operating_days == 0

    above_one_per_day = tuple(
        performance(index, final_equity=51_000, trades=141)
        for index in range(20)
    )
    passed = evaluate_growth_gate(above_one_per_day)

    assert passed.passed
    assert passed.summary.minimum_trades_per_operating_day == pytest.approx(
        141 / 140
    )
    assert passed.summary.minimum_trade_surplus_over_operating_days == 1


def test_growth_gate_can_pass_strict_contract() -> None:
    performances = tuple(
        performance(
            index,
            final_equity=50_000 + index * 100,
            trades=145,
        )
        for index in range(20)
    )

    result = evaluate_growth_gate(
        performances,
        gate=GrowthGate(maximum_worst_drawdown_fraction=0.25),
    )

    assert result.passed
    assert result.reasons == ()
    assert result.summary.worst_equity_multiple == 5.0
    assert result.summary.minimum_trade_surplus_over_operating_days == 5


def test_bootstrap_path_stress_is_deterministic_and_cost_r_based() -> None:
    first = bootstrap_path_stress(
        (-1.0, 0.8, 1.2, 0.4),
        simulations=500,
        trades_per_path=141,
        seed=17,
    )
    second = bootstrap_path_stress(
        (-1.0, 0.8, 1.2, 0.4),
        simulations=500,
        trades_per_path=141,
        seed=17,
    )

    assert first == second
    assert first.risk_fraction == 0.03
    assert first.ruin_rate == 0.0
    assert 0.0 <= first.median_max_drawdown_fraction <= 1.0
