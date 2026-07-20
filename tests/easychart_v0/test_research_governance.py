from __future__ import annotations

import math

import pandas as pd

from ictbt.easychart_v0.research_governance import (
    evaluate_promotion_eligibility,
    minimum_constant_net_r_for_multiple,
    summarize_trial_overlap,
)
from ictbt.easychart_v0.research_protocol import EvaluationWindow, TrialSpec


def window(year: int, start: str) -> EvaluationWindow:
    score_start = pd.Timestamp(start, tz="UTC")
    score_end = score_start + pd.Timedelta(days=28)
    return EvaluationWindow(
        year=year,
        score_start=score_start,
        score_end=score_end,
        data_start=score_start - pd.Timedelta(days=35),
        data_end=score_end + pd.Timedelta(days=7),
    )


def test_five_x_feasibility_is_explicit_at_fixed_three_percent_risk() -> None:
    required = minimum_constant_net_r_for_multiple(
        target_multiple=5.0,
        risk_fraction=0.03,
        trades=141,
    )

    assert math.isclose(required, 0.3826615574934541, rel_tol=1e-12)


def test_overlap_summary_does_not_treat_reused_days_as_independent() -> None:
    first = TrialSpec(seed=1, windows=(window(2022, "2022-01-01"),))
    second = TrialSpec(seed=2, windows=(window(2022, "2022-01-15"),))

    summary = summarize_trial_overlap((first, second))

    assert summary.scored_day_observations == 56
    assert summary.unique_scored_days == 42
    assert summary.unique_day_fraction == 0.75
    assert summary.maximum_pairwise_shared_days == 14
    assert summary.maximum_pairwise_overlap_fraction == 0.5


def test_discovery_never_promotes_and_holdout_requires_a_frozen_policy() -> None:
    discovery = evaluate_promotion_eligibility(
        phase="discovery",
        economic_gate_passed=True,
        frozen_policy_sha=None,
        has_censored_trials=False,
    )
    assert not discovery.eligible
    assert discovery.reasons == ("discovery_results_cannot_promote",)

    unfrozen = evaluate_promotion_eligibility(
        phase="holdout",
        economic_gate_passed=True,
        frozen_policy_sha=None,
        has_censored_trials=False,
    )
    assert not unfrozen.eligible
    assert unfrozen.reasons == ("holdout_requires_frozen_policy_sha",)

    frozen = evaluate_promotion_eligibility(
        phase="holdout",
        economic_gate_passed=True,
        frozen_policy_sha="a" * 40,
        has_censored_trials=False,
    )
    assert frozen.eligible
    assert frozen.reasons == ()
