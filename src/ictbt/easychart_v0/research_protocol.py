from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
import statistics
from typing import Mapping, Sequence

import pandas as pd


DEFAULT_YEARS: tuple[int, ...] = (2022, 2023, 2024, 2025, 2026)


def _utc_day(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"{name} must be a valid timestamp")
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.normalize()


def _finite(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _positive(value: float, *, name: str) -> float:
    number = _finite(value, name=name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between zero and one")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    location = probability * (len(ordered) - 1)
    lower = math.floor(location)
    upper = math.ceil(location)
    if lower == upper:
        return ordered[lower]
    weight = location - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


@dataclass(frozen=True, slots=True)
class YearCoverage:
    year: int
    available_start: pd.Timestamp
    available_end: pd.Timestamp

    def __post_init__(self) -> None:
        if self.year < 1970:
            raise ValueError("year must be at least 1970")
        start = _utc_day(self.available_start, name="available_start")
        end = _utc_day(self.available_end, name="available_end")
        year_start = pd.Timestamp(f"{self.year}-01-01", tz="UTC")
        year_end = pd.Timestamp(f"{self.year + 1}-01-01", tz="UTC")
        start = max(start, year_start)
        end = min(end, year_end)
        if end <= start:
            raise ValueError("coverage must contain at least one UTC day")
        object.__setattr__(self, "available_start", start)
        object.__setattr__(self, "available_end", end)


@dataclass(frozen=True, slots=True)
class EvaluationWindow:
    year: int
    score_start: pd.Timestamp
    score_end: pd.Timestamp
    data_start: pd.Timestamp
    data_end: pd.Timestamp

    def __post_init__(self) -> None:
        score_start = _utc_day(self.score_start, name="score_start")
        score_end = _utc_day(self.score_end, name="score_end")
        data_start = _utc_day(self.data_start, name="data_start")
        data_end = _utc_day(self.data_end, name="data_end")
        if not data_start <= score_start < score_end <= data_end:
            raise ValueError("window must satisfy data_start <= score < data_end")
        if score_start.year != self.year:
            raise ValueError("score_start must belong to the declared year")
        object.__setattr__(self, "score_start", score_start)
        object.__setattr__(self, "score_end", score_end)
        object.__setattr__(self, "data_start", data_start)
        object.__setattr__(self, "data_end", data_end)

    @property
    def score_days(self) -> int:
        return int((self.score_end - self.score_start) / pd.Timedelta(days=1))

    @property
    def fingerprint(self) -> str:
        return f"{self.year}:{self.score_start.date().isoformat()}"


@dataclass(frozen=True, slots=True)
class TrialSpec:
    seed: int
    windows: tuple[EvaluationWindow, ...]

    def __post_init__(self) -> None:
        if not self.windows:
            raise ValueError("a trial requires at least one window")
        years = tuple(window.year for window in self.windows)
        if len(set(years)) != len(years):
            raise ValueError("a trial can contain only one window per year")
        if years != tuple(sorted(years)):
            raise ValueError("trial windows must be ordered by year")

    @property
    def score_days(self) -> int:
        return sum(window.score_days for window in self.windows)

    @property
    def fingerprint(self) -> str:
        payload = "|".join(window.fingerprint for window in self.windows)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_manifest(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "fingerprint": self.fingerprint,
            "score_days": self.score_days,
            "windows": [
                {
                    "year": window.year,
                    "score_start": window.score_start.isoformat(),
                    "score_end": window.score_end.isoformat(),
                    "data_start": window.data_start.isoformat(),
                    "data_end": window.data_end.isoformat(),
                }
                for window in self.windows
            ],
        }


def default_coverages(
    *,
    years: Sequence[int] = DEFAULT_YEARS,
    latest_available_end: object | None = None,
) -> tuple[YearCoverage, ...]:
    latest = (
        None
        if latest_available_end is None
        else _utc_day(latest_available_end, name="latest_available_end")
    )
    output: list[YearCoverage] = []
    for year in years:
        start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        nominal_end = pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
        end = nominal_end if latest is None else min(nominal_end, latest)
        if end <= start:
            raise ValueError(f"no available data for {year}")
        output.append(YearCoverage(year, start, end))
    return tuple(output)


def _sample_window(
    coverage: YearCoverage,
    *,
    rng: random.Random,
    score_days: int,
    warmup_days: int,
    exit_extension_days: int,
) -> EvaluationWindow:
    first_score_start = coverage.available_start + pd.Timedelta(days=warmup_days)
    final_score_start = coverage.available_end - pd.Timedelta(
        days=score_days + exit_extension_days
    )
    if final_score_start < first_score_start:
        raise ValueError(
            f"coverage for {coverage.year} is too short for the requested window"
        )
    choices = int((final_score_start - first_score_start) / pd.Timedelta(days=1)) + 1
    offset = rng.randrange(choices)
    score_start = first_score_start + pd.Timedelta(days=offset)
    score_end = score_start + pd.Timedelta(days=score_days)
    return EvaluationWindow(
        year=coverage.year,
        score_start=score_start,
        score_end=score_end,
        data_start=score_start - pd.Timedelta(days=warmup_days),
        data_end=score_end + pd.Timedelta(days=exit_extension_days),
    )


def sample_trials(
    coverages: Sequence[YearCoverage],
    *,
    trial_count: int,
    seed: int,
    score_days: int = 28,
    warmup_days: int = 35,
    exit_extension_days: int = 7,
) -> tuple[TrialSpec, ...]:
    """Sample repeated one-four-week-per-year portfolio trials.

    BTC and ETH must consume the same trial manifest.  Warm-up bars may create
    features but trades are scored only inside ``[score_start, score_end)``.
    The exit extension prevents a position opened near the score boundary from
    being force-labelled at the last scored candle.
    """

    if trial_count <= 0:
        raise ValueError("trial_count must be positive")
    for name, value in (
        ("score_days", score_days),
        ("warmup_days", warmup_days),
        ("exit_extension_days", exit_extension_days),
    ):
        if value < 0 or (name == "score_days" and value == 0):
            raise ValueError(f"{name} is invalid")
    ordered = tuple(sorted(coverages, key=lambda item: item.year))
    if not ordered or len({item.year for item in ordered}) != len(ordered):
        raise ValueError("coverages require unique years")

    master = random.Random(int(seed))
    output: list[TrialSpec] = []
    fingerprints: set[str] = set()
    attempts = 0
    maximum_attempts = max(1_000, trial_count * 100)
    while len(output) < trial_count:
        attempts += 1
        if attempts > maximum_attempts:
            raise ValueError("unable to sample enough unique trials")
        trial_seed = master.getrandbits(63)
        trial_rng = random.Random(trial_seed)
        windows = tuple(
            _sample_window(
                coverage,
                rng=trial_rng,
                score_days=score_days,
                warmup_days=warmup_days,
                exit_extension_days=exit_extension_days,
            )
            for coverage in ordered
        )
        trial = TrialSpec(trial_seed, windows)
        if trial.fingerprint in fingerprints:
            continue
        fingerprints.add(trial.fingerprint)
        output.append(trial)
    return tuple(output)


@dataclass(frozen=True, slots=True)
class TrialPerformance:
    """One chronological BTC/ETH shared-equity trial.

    ``trades`` counts only completed trades.  Pending, cancelled, rejected and
    censored intents never satisfy the frequency requirement.
    """

    trial_fingerprint: str
    initial_equity: float
    final_equity: float
    max_drawdown_fraction: float
    trades: int
    wins: int
    net_r: float
    operating_days: int = 140
    average_net_r: float | None = None

    def __post_init__(self) -> None:
        if not self.trial_fingerprint:
            raise ValueError("trial_fingerprint is required")
        initial = _positive(self.initial_equity, name="initial_equity")
        final = _finite(self.final_equity, name="final_equity")
        drawdown = _finite(self.max_drawdown_fraction, name="max_drawdown_fraction")
        if final < 0:
            raise ValueError("final_equity cannot be negative")
        if not 0 <= drawdown <= 1:
            raise ValueError("max_drawdown_fraction must be between zero and one")
        if self.operating_days <= 0:
            raise ValueError("operating_days must be positive")
        if self.trades < 0 or not 0 <= self.wins <= self.trades:
            raise ValueError("invalid trade counts")
        net_r = _finite(self.net_r, name="net_r")
        average = (
            None
            if self.average_net_r is None
            else _finite(self.average_net_r, name="average_net_r")
        )
        if average is None and self.trades:
            average = net_r / self.trades
        object.__setattr__(self, "initial_equity", initial)
        object.__setattr__(self, "final_equity", final)
        object.__setattr__(self, "max_drawdown_fraction", drawdown)
        object.__setattr__(self, "net_r", net_r)
        object.__setattr__(self, "average_net_r", average)

    @property
    def equity_multiple(self) -> float:
        return self.final_equity / self.initial_equity

    @property
    def win_rate(self) -> float | None:
        return None if self.trades == 0 else self.wins / self.trades

    @property
    def trades_per_operating_day(self) -> float:
        return self.trades / self.operating_days

    @property
    def trade_surplus_over_operating_days(self) -> int:
        return self.trades - self.operating_days


@dataclass(frozen=True, slots=True)
class RobustnessSummary:
    trials: int
    target_multiple: float
    target_hit_rate: float
    positive_trial_rate: float
    worst_equity_multiple: float
    lower_quantile_equity_multiple: float
    median_equity_multiple: float
    best_equity_multiple: float
    median_max_drawdown_fraction: float
    worst_max_drawdown_fraction: float
    minimum_trades: int
    median_trades: float
    minimum_trades_per_operating_day: float
    median_trades_per_operating_day: float
    minimum_trade_surplus_over_operating_days: int
    median_average_net_r: float | None


def summarize_trials(
    performances: Sequence[TrialPerformance],
    *,
    target_multiple: float = 5.0,
    lower_quantile: float = 0.20,
) -> RobustnessSummary:
    if not performances:
        raise ValueError("at least one trial performance is required")
    target = _positive(target_multiple, name="target_multiple")
    multiples = [item.equity_multiple for item in performances]
    drawdowns = [item.max_drawdown_fraction for item in performances]
    trades = [item.trades for item in performances]
    trade_rates = [item.trades_per_operating_day for item in performances]
    trade_surpluses = [
        item.trade_surplus_over_operating_days for item in performances
    ]
    averages = [
        item.average_net_r
        for item in performances
        if item.average_net_r is not None
    ]
    return RobustnessSummary(
        trials=len(performances),
        target_multiple=target,
        target_hit_rate=sum(value >= target for value in multiples) / len(multiples),
        positive_trial_rate=sum(value > 1.0 for value in multiples) / len(multiples),
        worst_equity_multiple=min(multiples),
        lower_quantile_equity_multiple=_quantile(multiples, lower_quantile),
        median_equity_multiple=statistics.median(multiples),
        best_equity_multiple=max(multiples),
        median_max_drawdown_fraction=statistics.median(drawdowns),
        worst_max_drawdown_fraction=max(drawdowns),
        minimum_trades=min(trades),
        median_trades=statistics.median(trades),
        minimum_trades_per_operating_day=min(trade_rates),
        median_trades_per_operating_day=statistics.median(trade_rates),
        minimum_trade_surplus_over_operating_days=min(trade_surpluses),
        median_average_net_r=(None if not averages else statistics.median(averages)),
    )


@dataclass(frozen=True, slots=True)
class GrowthGate:
    target_multiple: float = 5.0
    required_target_hit_rate: float = 1.0
    minimum_trials: int = 20
    minimum_trade_surplus_over_operating_days: int = 1
    maximum_worst_drawdown_fraction: float = 0.35
    minimum_median_average_net_r: float = 0.0

    def __post_init__(self) -> None:
        _positive(self.target_multiple, name="target_multiple")
        if not 0 <= self.required_target_hit_rate <= 1:
            raise ValueError("required_target_hit_rate must be between zero and one")
        if self.minimum_trials <= 0:
            raise ValueError("minimum_trials must be positive")
        if self.minimum_trade_surplus_over_operating_days < 1:
            raise ValueError(
                "minimum_trade_surplus_over_operating_days must be at least one"
            )
        if not 0 <= self.maximum_worst_drawdown_fraction <= 1:
            raise ValueError("maximum_worst_drawdown_fraction must be between zero and one")
        _finite(self.minimum_median_average_net_r, name="minimum_median_average_net_r")


@dataclass(frozen=True, slots=True)
class GrowthGateResult:
    passed: bool
    reasons: tuple[str, ...]
    summary: RobustnessSummary


def evaluate_growth_gate(
    performances: Sequence[TrialPerformance],
    *,
    gate: GrowthGate = GrowthGate(),
) -> GrowthGateResult:
    summary = summarize_trials(
        performances,
        target_multiple=gate.target_multiple,
    )
    reasons: list[str] = []
    if summary.trials < gate.minimum_trials:
        reasons.append("insufficient_trials")
    if summary.target_hit_rate + 1e-12 < gate.required_target_hit_rate:
        reasons.append("target_multiple_not_repeated")
    if (
        summary.minimum_trade_surplus_over_operating_days
        < gate.minimum_trade_surplus_over_operating_days
    ):
        reasons.append("completed_trades_not_above_operating_days")
    if (
        summary.worst_max_drawdown_fraction
        > gate.maximum_worst_drawdown_fraction + 1e-12
    ):
        reasons.append("drawdown_too_large")
    if (
        summary.median_average_net_r is None
        or summary.median_average_net_r + 1e-12
        < gate.minimum_median_average_net_r
    ):
        reasons.append("non_positive_median_expectancy")
    return GrowthGateResult(not reasons, tuple(reasons), summary)


@dataclass(frozen=True, slots=True)
class PathStressSummary:
    simulations: int
    trades_per_path: int
    risk_fraction: float
    ruin_rate: float
    probability_of_50pct_drawdown: float
    median_final_multiple: float
    lower_5pct_final_multiple: float
    median_max_drawdown_fraction: float
    worst_max_drawdown_fraction: float


def bootstrap_path_stress(
    net_rs: Sequence[float],
    *,
    risk_fraction: float = 0.03,
    simulations: int = 10_000,
    trades_per_path: int | None = None,
    seed: int = 0,
) -> PathStressSummary:
    """Bootstrap cost-inclusive net-R outcomes under fixed-fraction compounding."""

    if not net_rs:
        raise ValueError("net_rs cannot be empty")
    outcomes = tuple(_finite(item, name="net_r") for item in net_rs)
    risk = _positive(risk_fraction, name="risk_fraction")
    if risk >= 1:
        raise ValueError("risk_fraction must be below one")
    if simulations <= 0:
        raise ValueError("simulations must be positive")
    path_length = len(outcomes) if trades_per_path is None else trades_per_path
    if path_length <= 0:
        raise ValueError("trades_per_path must be positive")

    rng = random.Random(int(seed))
    final_multiples: list[float] = []
    drawdowns: list[float] = []
    ruins = 0
    severe_drawdowns = 0
    for _ in range(simulations):
        equity = 1.0
        peak = 1.0
        worst = 0.0
        ruined = False
        for _trade in range(path_length):
            factor = 1.0 + risk * outcomes[rng.randrange(len(outcomes))]
            if factor <= 0:
                equity = 0.0
                worst = 1.0
                ruined = True
                break
            equity *= factor
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak)
        ruins += int(ruined)
        severe_drawdowns += int(worst >= 0.50)
        final_multiples.append(equity)
        drawdowns.append(worst)

    return PathStressSummary(
        simulations=simulations,
        trades_per_path=path_length,
        risk_fraction=risk,
        ruin_rate=ruins / simulations,
        probability_of_50pct_drawdown=severe_drawdowns / simulations,
        median_final_multiple=statistics.median(final_multiples),
        lower_5pct_final_multiple=_quantile(final_multiples, 0.05),
        median_max_drawdown_fraction=statistics.median(drawdowns),
        worst_max_drawdown_fraction=max(drawdowns),
    )


def manifests_by_fingerprint(
    trials: Sequence[TrialSpec],
) -> Mapping[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    for trial in trials:
        if trial.fingerprint in output:
            raise ValueError("duplicate trial fingerprint")
        output[trial.fingerprint] = trial.to_manifest()
    return output


__all__ = [
    "DEFAULT_YEARS",
    "EvaluationWindow",
    "GrowthGate",
    "GrowthGateResult",
    "PathStressSummary",
    "RobustnessSummary",
    "TrialPerformance",
    "TrialSpec",
    "YearCoverage",
    "bootstrap_path_stress",
    "default_coverages",
    "evaluate_growth_gate",
    "manifests_by_fingerprint",
    "sample_trials",
    "summarize_trials",
]
