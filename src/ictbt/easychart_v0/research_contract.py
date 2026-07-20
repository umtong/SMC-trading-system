from __future__ import annotations

import math
import random
from typing import Sequence

import pandas as pd

from .research_protocol import EvaluationWindow, TrialSpec, YearCoverage


USER_RISK_FRACTION = 0.03
USER_SCORE_DAYS_PER_YEAR = 28
USER_YEARS = (2022, 2023, 2024, 2025, 2026)
USER_SYMBOLS = ("BTCUSDT", "ETHUSDT")


def assert_user_research_contract(
    *,
    risk_fraction: float,
    score_days_per_year: int,
    years: Sequence[int] = USER_YEARS,
    symbols: Sequence[str] = USER_SYMBOLS,
) -> None:
    """Reject research runs that silently change the user's hard invariants."""

    if not math.isclose(
        float(risk_fraction),
        USER_RISK_FRACTION,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("risk_fraction must remain fixed at 0.03")
    if int(score_days_per_year) != USER_SCORE_DAYS_PER_YEAR:
        raise ValueError("score_days_per_year must remain fixed at 28")
    if tuple(int(year) for year in years) != USER_YEARS:
        raise ValueError("research years must remain exactly 2022 through 2026")
    if tuple(str(symbol).upper() for symbol in symbols) != USER_SYMBOLS:
        raise ValueError("research symbols must remain exactly BTCUSDT and ETHUSDT")


def _candidate_starts(
    coverage: YearCoverage,
    *,
    score_days: int,
    warmup_days: int,
    exit_extension_days: int,
) -> list[pd.Timestamp]:
    first = coverage.available_start + pd.Timedelta(days=warmup_days)
    last = coverage.available_end - pd.Timedelta(
        days=score_days + exit_extension_days
    )
    if last < first:
        raise ValueError(
            f"coverage for {coverage.year} is too short for the requested window"
        )
    return list(pd.date_range(first, last, freq="1D"))


def sample_trials_without_year_reuse(
    coverages: Sequence[YearCoverage],
    *,
    trial_count: int,
    seed: int,
    score_days: int = USER_SCORE_DAYS_PER_YEAR,
    warmup_days: int = 35,
    exit_extension_days: int = 7,
) -> tuple[TrialSpec, ...]:
    """Sample one window per year without reusing an exact year/start pair.

    Different windows may overlap because market regimes do not align to calendar
    boundaries, but a start date from a given year is never silently recycled in
    the same experiment batch. BTC and ETH consume the same returned manifest.
    """

    if trial_count <= 0:
        raise ValueError("trial_count must be positive")
    if score_days <= 0:
        raise ValueError("score_days must be positive")
    if warmup_days < 0 or exit_extension_days < 0:
        raise ValueError("warmup and exit extension cannot be negative")

    ordered = tuple(sorted(coverages, key=lambda item: item.year))
    if not ordered or len({item.year for item in ordered}) != len(ordered):
        raise ValueError("coverages require unique years")

    master = random.Random(int(seed))
    starts_by_year: dict[int, list[pd.Timestamp]] = {}
    for coverage in ordered:
        starts = _candidate_starts(
            coverage,
            score_days=score_days,
            warmup_days=warmup_days,
            exit_extension_days=exit_extension_days,
        )
        if trial_count > len(starts):
            raise ValueError(
                f"trial_count={trial_count} exceeds {len(starts)} unique starts "
                f"available in {coverage.year}"
            )
        master.shuffle(starts)
        starts_by_year[coverage.year] = starts[:trial_count]

    trials: list[TrialSpec] = []
    for index in range(trial_count):
        windows = tuple(
            EvaluationWindow(
                year=coverage.year,
                score_start=starts_by_year[coverage.year][index],
                score_end=(
                    starts_by_year[coverage.year][index]
                    + pd.Timedelta(days=score_days)
                ),
                data_start=(
                    starts_by_year[coverage.year][index]
                    - pd.Timedelta(days=warmup_days)
                ),
                data_end=(
                    starts_by_year[coverage.year][index]
                    + pd.Timedelta(days=score_days + exit_extension_days)
                ),
            )
            for coverage in ordered
        )
        trials.append(TrialSpec(seed=master.getrandbits(63), windows=windows))

    if len({trial.fingerprint for trial in trials}) != len(trials):
        raise RuntimeError("non-reuse sampler produced duplicate trial manifests")
    return tuple(trials)


__all__ = [
    "USER_RISK_FRACTION",
    "USER_SCORE_DAYS_PER_YEAR",
    "USER_SYMBOLS",
    "USER_YEARS",
    "assert_user_research_contract",
    "sample_trials_without_year_reuse",
]
