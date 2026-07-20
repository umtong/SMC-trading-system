from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Literal, Sequence

import pandas as pd

from .research_protocol import TrialSpec


ResearchPhase = Literal["discovery", "holdout"]


def minimum_constant_net_r_for_multiple(
    *,
    target_multiple: float,
    risk_fraction: float,
    trades: int,
) -> float:
    """Return constant cost-inclusive net-R needed under fixed-fraction compounding."""

    multiple = float(target_multiple)
    risk = float(risk_fraction)
    if not math.isfinite(multiple) or multiple <= 0:
        raise ValueError("target_multiple must be finite and positive")
    if not math.isfinite(risk) or not 0 < risk < 1:
        raise ValueError("risk_fraction must be between zero and one")
    if trades <= 0:
        raise ValueError("trades must be positive")
    return math.expm1(math.log(multiple) / trades) / risk


@dataclass(frozen=True, slots=True)
class GrowthFeasibility:
    target_multiple: float
    risk_fraction: float
    trades: int
    required_constant_net_r: float
    required_log_growth_per_trade: float


def growth_feasibility(
    *,
    target_multiple: float,
    risk_fraction: float,
    trades: int,
) -> GrowthFeasibility:
    required = minimum_constant_net_r_for_multiple(
        target_multiple=target_multiple,
        risk_fraction=risk_fraction,
        trades=trades,
    )
    return GrowthFeasibility(
        target_multiple=float(target_multiple),
        risk_fraction=float(risk_fraction),
        trades=int(trades),
        required_constant_net_r=required,
        required_log_growth_per_trade=math.log(float(target_multiple)) / trades,
    )


@dataclass(frozen=True, slots=True)
class TrialOverlapSummary:
    trials: int
    scored_day_observations: int
    unique_scored_days: int
    unique_day_fraction: float
    pairwise_comparisons: int
    median_pairwise_shared_days: float
    maximum_pairwise_shared_days: int
    maximum_pairwise_overlap_fraction: float


def _scored_dates(trial: TrialSpec) -> frozenset[pd.Timestamp]:
    output: set[pd.Timestamp] = set()
    for window in trial.windows:
        last = window.score_end - pd.Timedelta(days=1)
        output.update(
            pd.date_range(
                window.score_start,
                last,
                freq="1D",
                tz="UTC",
            )
        )
    return frozenset(output)


def summarize_trial_overlap(trials: Sequence[TrialSpec]) -> TrialOverlapSummary:
    """Measure how much repeated random trials reuse the same scored days.

    Repeated manifests are useful sensitivity tests, but overlapping windows are
    not independent evidence. This summary exposes that dependence rather than
    allowing nominal trial count to stand in for effective sample breadth.
    """

    if not trials:
        raise ValueError("at least one trial is required")
    date_sets = [_scored_dates(trial) for trial in trials]
    observations = sum(len(items) for items in date_sets)
    unique = len(set().union(*date_sets))
    overlaps: list[int] = []
    fractions: list[float] = []
    for left_index, left in enumerate(date_sets):
        for right in date_sets[left_index + 1 :]:
            shared = len(left & right)
            denominator = min(len(left), len(right))
            overlaps.append(shared)
            fractions.append(0.0 if denominator == 0 else shared / denominator)
    return TrialOverlapSummary(
        trials=len(trials),
        scored_day_observations=observations,
        unique_scored_days=unique,
        unique_day_fraction=unique / observations,
        pairwise_comparisons=len(overlaps),
        median_pairwise_shared_days=(0.0 if not overlaps else statistics.median(overlaps)),
        maximum_pairwise_shared_days=(0 if not overlaps else max(overlaps)),
        maximum_pairwise_overlap_fraction=(0.0 if not fractions else max(fractions)),
    )


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    eligible: bool
    phase: ResearchPhase
    reasons: tuple[str, ...]
    frozen_policy_sha: str | None


def _valid_policy_sha(value: str | None) -> bool:
    if value is None:
        return False
    candidate = value.strip().lower()
    return 7 <= len(candidate) <= 64 and all(
        character in "0123456789abcdef" for character in candidate
    )


def evaluate_promotion_eligibility(
    *,
    phase: ResearchPhase,
    economic_gate_passed: bool,
    frozen_policy_sha: str | None,
    has_censored_trials: bool,
) -> PromotionDecision:
    """Separate strategy discovery from a policy-frozen holdout decision."""

    if phase not in {"discovery", "holdout"}:
        raise ValueError("phase must be discovery or holdout")
    reasons: list[str] = []
    if phase == "discovery":
        reasons.append("discovery_results_cannot_promote")
    elif not _valid_policy_sha(frozen_policy_sha):
        reasons.append("holdout_requires_frozen_policy_sha")
    if not economic_gate_passed:
        reasons.append("economic_growth_gate_failed")
    if has_censored_trials:
        reasons.append("censored_trial_present")
    return PromotionDecision(
        eligible=not reasons,
        phase=phase,
        reasons=tuple(reasons),
        frozen_policy_sha=(
            None if frozen_policy_sha is None else frozen_policy_sha.strip().lower()
        ),
    )


__all__ = [
    "GrowthFeasibility",
    "PromotionDecision",
    "ResearchPhase",
    "TrialOverlapSummary",
    "evaluate_promotion_eligibility",
    "growth_feasibility",
    "minimum_constant_net_r_for_multiple",
    "summarize_trial_overlap",
]
