from datetime import date
import math

import pytest

from ictbt.easychart_v0.research_protocol import (
    AcceptancePolicy,
    RunMetrics,
    evaluate_batch,
    evaluate_run,
    generate_annual_samples,
    required_constant_net_r_per_trade,
)


def _passing_run(sample_id: int = 0, *, trades: int = 141) -> RunMetrics:
    return RunMetrics(
        sample_id=sample_id,
        initial_equity=10_000.0,
        final_equity=50_000.0,
        trades=trades,
        operating_days=140,
        net_r=70.5,
        max_drawdown_fraction=0.20,
        risk_fraction=0.03,
        one_total_slot=True,
        fees_included=True,
        liquidation_model_included=True,
    )


def test_random_annual_samples_are_reproducible_unique_and_140_days() -> None:
    kwargs = dict(
        years=(2022, 2023, 2024, 2025, 2026),
        sample_count=12,
        seed=739_201,
        available_through=date(2026, 6, 30),
    )
    first = generate_annual_samples(**kwargs)
    second = generate_annual_samples(**kwargs)

    assert first == second
    assert len({sample.fingerprint for sample in first}) == len(first)
    assert all(sample.operating_days == 140 for sample in first)
    for sample in first:
        assert tuple(window.year for window in sample.windows) == (
            2022,
            2023,
            2024,
            2025,
            2026,
        )
        assert all(window.operating_days == 28 for window in sample.windows)
        assert sample.windows[-1].end <= date(2026, 7, 1)


def test_sampler_rejects_an_incomplete_year() -> None:
    with pytest.raises(ValueError, match="fewer than 28"):
        generate_annual_samples(
            years=(2026,),
            sample_count=1,
            seed=1,
            available_through=date(2026, 1, 20),
        )


def test_trade_frequency_gate_is_strictly_greater_than_operating_days() -> None:
    equal_days = _passing_run(trades=140)
    above_days = _passing_run(trades=141)

    assert "completed_trades_must_exceed_operating_days" in evaluate_run(
        equal_days
    ).violations
    assert evaluate_run(above_days).accepted


def test_batch_does_not_promote_one_lucky_sample() -> None:
    policy = AcceptancePolicy(minimum_resamples=3)
    lucky = _passing_run(0)
    weak = RunMetrics(
        sample_id=1,
        initial_equity=10_000.0,
        final_equity=11_000.0,
        trades=160,
        operating_days=140,
        net_r=3.0,
        max_drawdown_fraction=0.12,
        risk_fraction=0.03,
        one_total_slot=True,
        fees_included=True,
        liquidation_model_included=True,
    )
    third = _passing_run(2)

    decision = evaluate_batch((lucky, weak, third), policy=policy)

    assert not decision.promoted
    assert "not_every_resample_passed_user_and_risk_gates" in decision.violations
    assert decision.summary["accepted_resamples"] == 2


def test_batch_requires_independent_resample_count() -> None:
    policy = AcceptancePolicy(minimum_resamples=2)
    decision = evaluate_batch((_passing_run(0),), policy=policy)

    assert not decision.promoted
    assert "insufficient_independent_resamples" in decision.violations


def test_required_constant_net_r_is_geometric_hurdle() -> None:
    hurdle = required_constant_net_r_per_trade(
        target_multiple=5.0,
        trades=140,
        risk_fraction=0.03,
    )

    assert hurdle == pytest.approx(0.38541, abs=1e-5)
    assert math.prod([1 + 0.03 * hurdle] * 140) == pytest.approx(5.0)
