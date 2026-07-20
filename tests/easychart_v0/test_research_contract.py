from __future__ import annotations

import pandas as pd
import pytest

from ictbt.easychart_v0.research_contract import (
    assert_user_research_contract,
    sample_trials_without_year_reuse,
)
from ictbt.easychart_v0.research_protocol import YearCoverage


def coverages() -> tuple[YearCoverage, ...]:
    return tuple(
        YearCoverage(
            year,
            pd.Timestamp(f"{year}-01-01", tz="UTC"),
            pd.Timestamp(f"{year + 1}-01-01", tz="UTC"),
        )
        for year in range(2022, 2027)
    )


def test_non_reuse_sampler_is_deterministic_and_never_recycles_year_start() -> None:
    first = sample_trials_without_year_reuse(
        coverages(),
        trial_count=20,
        seed=20260720,
    )
    second = sample_trials_without_year_reuse(
        coverages(),
        trial_count=20,
        seed=20260720,
    )

    assert first == second
    assert all(trial.score_days == 140 for trial in first)
    for position, year in enumerate(range(2022, 2027)):
        starts = [trial.windows[position].score_start for trial in first]
        assert len(set(starts)) == len(starts), year


def test_user_contract_locks_three_percent_and_four_weeks() -> None:
    assert_user_research_contract(
        risk_fraction=0.03,
        score_days_per_year=28,
    )
    with pytest.raises(ValueError, match="0.03"):
        assert_user_research_contract(
            risk_fraction=0.02,
            score_days_per_year=28,
        )
    with pytest.raises(ValueError, match="28"):
        assert_user_research_contract(
            risk_fraction=0.03,
            score_days_per_year=21,
        )
