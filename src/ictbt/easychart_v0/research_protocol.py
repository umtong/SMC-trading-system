from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import math
import random
import statistics
from typing import Iterable, Mapping, Sequence


DEFAULT_YEARS = (2022, 2023, 2024, 2025, 2026)
DEFAULT_WINDOW_DAYS = 28
DEFAULT_WARMUP_DAYS = 45
USER_RISK_FRACTION = 0.03
USER_MINIMUM_EQUITY_MULTIPLE = 5.0


@dataclass(frozen=True, slots=True)
class AnnualWindow:
    """One leak-free operating window and its read-only feature warm-up."""

    sample_id: int
    year: int
    start: date
    end: date
    warmup_start: date

    def __post_init__(self) -> None:
        if self.sample_id < 0:
            raise ValueError("sample_id must be non-negative")
        if self.start.year != self.year:
            raise ValueError("window start must belong to its declared year")
        if self.end <= self.start:
            raise ValueError("window end must follow start")
        if self.warmup_start > self.start:
            raise ValueError("warmup cannot begin after the operating window")

    @property
    def operating_days(self) -> int:
        return (self.end - self.start).days

    @property
    def fingerprint(self) -> str:
        return f"{self.year}:{self.start.isoformat()}:{self.end.isoformat()}"

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "year": self.year,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "warmup_start": self.warmup_start.isoformat(),
            "operating_days": self.operating_days,
        }


@dataclass(frozen=True, slots=True)
class AnnualSample:
    sample_id: int
    seed: int
    windows: tuple[AnnualWindow, ...]

    def __post_init__(self) -> None:
        if self.sample_id < 0:
            raise ValueError("sample_id must be non-negative")
        if not self.windows:
            raise ValueError("a sample requires at least one annual window")
        if any(window.sample_id != self.sample_id for window in self.windows):
            raise ValueError("every window must belong to the sample")
        years = tuple(window.year for window in self.windows)
        if len(set(years)) != len(years):
            raise ValueError("a sample can contain only one window per year")
        if years != tuple(sorted(years)):
            raise ValueError("annual windows must be sorted by year")

    @property
    def operating_days(self) -> int:
        return sum(window.operating_days for window in self.windows)

    @property
    def fingerprint(self) -> str:
        return "|".join(window.fingerprint for window in self.windows)

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "seed": self.seed,
            "operating_days": self.operating_days,
            "fingerprint": self.fingerprint,
            "windows": [window.to_dict() for window in self.windows],
        }


def _available_end_exclusive(year: int, available_through: date) -> date:
    natural_end = date(year + 1, 1, 1)
    return min(natural_end, available_through + timedelta(days=1))


def generate_annual_samples(
    *,
    years: Sequence[int] = DEFAULT_YEARS,
    sample_count: int,
    seed: int,
    available_through: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
    warmup_days: int = DEFAULT_WARMUP_DAYS,
) -> tuple[AnnualSample, ...]:
    """Generate reproducible, non-identical 4-week windows for every year.

    Exact start dates are sampled without replacement independently for each
    year.  Different samples may overlap, but the same year/window pair is not
    silently reused.  ``available_through`` must be the last fully complete UTC
    data date; this prevents a 2026 sample from reaching into unfinished data.
    """

    normalized_years = tuple(sorted({int(year) for year in years}))
    if not normalized_years:
        raise ValueError("at least one year is required")
    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if warmup_days < 0:
        raise ValueError("warmup_days cannot be negative")

    rng = random.Random(int(seed))
    starts_by_year: dict[int, list[date]] = {}
    for year in normalized_years:
        first = date(year, 1, 1)
        end_exclusive = _available_end_exclusive(year, available_through)
        latest_start = end_exclusive - timedelta(days=window_days)
        if latest_start < first:
            raise ValueError(
                f"year {year} has fewer than {window_days} complete available days"
            )
        starts = [
            first + timedelta(days=offset)
            for offset in range((latest_start - first).days + 1)
        ]
        if sample_count > len(starts):
            raise ValueError(
                f"sample_count={sample_count} exceeds {len(starts)} unique "
                f"{window_days}-day starts in {year}"
            )
        rng.shuffle(starts)
        starts_by_year[year] = starts[:sample_count]

    samples: list[AnnualSample] = []
    for sample_id in range(sample_count):
        sample_seed = rng.getrandbits(63)
        windows = tuple(
            AnnualWindow(
                sample_id=sample_id,
                year=year,
                start=starts_by_year[year][sample_id],
                end=starts_by_year[year][sample_id]
                + timedelta(days=window_days),
                warmup_start=starts_by_year[year][sample_id]
                - timedelta(days=warmup_days),
            )
            for year in normalized_years
        )
        samples.append(
            AnnualSample(sample_id=sample_id, seed=sample_seed, windows=windows)
        )

    fingerprints = {sample.fingerprint for sample in samples}
    if len(fingerprints) != len(samples):
        raise RuntimeError("random sampler produced duplicate annual portfolios")
    return tuple(samples)


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Economic facts from one chronological, shared-equity replay."""

    sample_id: int
    initial_equity: float
    final_equity: float
    trades: int
    operating_days: int
    net_r: float
    max_drawdown_fraction: float
    risk_fraction: float
    one_total_slot: bool
    fees_included: bool
    liquidation_model_included: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("initial_equity", self.initial_equity),
            ("final_equity", self.final_equity),
        ):
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.trades < 0:
            raise ValueError("trades cannot be negative")
        if self.operating_days <= 0:
            raise ValueError("operating_days must be positive")
        if not math.isfinite(self.net_r):
            raise ValueError("net_r must be finite")
        if not 0 <= self.max_drawdown_fraction < 1:
            raise ValueError("max_drawdown_fraction must be in [0, 1)")
        if not 0 < self.risk_fraction < 1:
            raise ValueError("risk_fraction must be in (0, 1)")

    @property
    def equity_multiple(self) -> float:
        return self.final_equity / self.initial_equity

    @property
    def trades_per_day(self) -> float:
        return self.trades / self.operating_days

    @property
    def average_net_r(self) -> float | None:
        return None if self.trades == 0 else self.net_r / self.trades

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "RunMetrics":
        return cls(
            sample_id=int(value["sample_id"]),
            initial_equity=float(value["initial_equity"]),
            final_equity=float(value["final_equity"]),
            trades=int(value["trades"]),
            operating_days=int(value["operating_days"]),
            net_r=float(value["net_r"]),
            max_drawdown_fraction=float(value["max_drawdown_fraction"]),
            risk_fraction=float(value["risk_fraction"]),
            one_total_slot=bool(value["one_total_slot"]),
            fees_included=bool(value["fees_included"]),
            liquidation_model_included=bool(value["liquidation_model_included"]),
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload.update(
            {
                "equity_multiple": self.equity_multiple,
                "trades_per_day": self.trades_per_day,
                "average_net_r": self.average_net_r,
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    """Explicit user gates plus conservative robustness gates.

    ``minimum_equity_multiple`` and strict ``trades > operating_days`` are user
    invariants.  Drawdown and resample-count thresholds are configurable
    ENGINEERING_V0 promotion controls, not claims that EasyChart stated them.
    """

    required_risk_fraction: float = USER_RISK_FRACTION
    minimum_equity_multiple: float = USER_MINIMUM_EQUITY_MULTIPLE
    maximum_drawdown_fraction: float = 0.30
    minimum_resamples: int = 20
    require_every_resample_to_pass: bool = True

    def __post_init__(self) -> None:
        if not 0 < self.required_risk_fraction < 1:
            raise ValueError("required_risk_fraction must be in (0, 1)")
        if self.minimum_equity_multiple <= 1:
            raise ValueError("minimum_equity_multiple must exceed one")
        if not 0 < self.maximum_drawdown_fraction < 1:
            raise ValueError("maximum_drawdown_fraction must be in (0, 1)")
        if self.minimum_resamples <= 0:
            raise ValueError("minimum_resamples must be positive")


@dataclass(frozen=True, slots=True)
class RunDecision:
    sample_id: int
    accepted: bool
    violations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BatchDecision:
    promoted: bool
    violations: tuple[str, ...]
    run_decisions: tuple[RunDecision, ...]
    summary: Mapping[str, float | int | None]

    def to_dict(self) -> dict[str, object]:
        return {
            "promoted": self.promoted,
            "violations": list(self.violations),
            "run_decisions": [decision.to_dict() for decision in self.run_decisions],
            "summary": dict(self.summary),
        }


def evaluate_run(
    metrics: RunMetrics,
    *,
    policy: AcceptancePolicy = AcceptancePolicy(),
) -> RunDecision:
    violations: list[str] = []
    if not math.isclose(
        metrics.risk_fraction,
        policy.required_risk_fraction,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        violations.append("risk_fraction_must_equal_0_03")
    if not metrics.one_total_slot:
        violations.append("btc_eth_must_share_one_total_slot")
    if not metrics.fees_included:
        violations.append("fees_must_be_included")
    if not metrics.liquidation_model_included:
        violations.append("liquidation_rules_must_be_included")
    if metrics.trades <= metrics.operating_days:
        violations.append("completed_trades_must_exceed_operating_days")
    if metrics.equity_multiple + 1e-12 < policy.minimum_equity_multiple:
        violations.append("equity_multiple_below_5x")
    if metrics.average_net_r is None or metrics.average_net_r <= 0:
        violations.append("average_net_r_must_be_positive")
    if metrics.max_drawdown_fraction > policy.maximum_drawdown_fraction + 1e-12:
        violations.append("maximum_drawdown_exceeds_policy")
    return RunDecision(
        sample_id=metrics.sample_id,
        accepted=not violations,
        violations=tuple(violations),
    )


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires data")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be in [0, 1]")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def evaluate_batch(
    runs: Iterable[RunMetrics],
    *,
    policy: AcceptancePolicy = AcceptancePolicy(),
) -> BatchDecision:
    ordered = tuple(sorted(runs, key=lambda item: item.sample_id))
    violations: list[str] = []
    if len(ordered) < policy.minimum_resamples:
        violations.append("insufficient_independent_resamples")
    ids = [run.sample_id for run in ordered]
    if len(set(ids)) != len(ids):
        violations.append("duplicate_sample_ids")

    decisions = tuple(evaluate_run(run, policy=policy) for run in ordered)
    failed = sum(not decision.accepted for decision in decisions)
    if policy.require_every_resample_to_pass and failed:
        violations.append("not_every_resample_passed_user_and_risk_gates")

    multiples = [run.equity_multiple for run in ordered]
    trade_rates = [run.trades_per_day for run in ordered]
    average_rs = [
        run.average_net_r for run in ordered if run.average_net_r is not None
    ]
    drawdowns = [run.max_drawdown_fraction for run in ordered]
    summary: dict[str, float | int | None] = {
        "resamples": len(ordered),
        "accepted_resamples": len(ordered) - failed,
        "minimum_equity_multiple": min(multiples) if multiples else None,
        "median_equity_multiple": statistics.median(multiples) if multiples else None,
        "p10_equity_multiple": _quantile(multiples, 0.10) if multiples else None,
        "minimum_trades_per_day": min(trade_rates) if trade_rates else None,
        "median_trades_per_day": statistics.median(trade_rates) if trade_rates else None,
        "minimum_average_net_r": min(average_rs) if average_rs else None,
        "median_average_net_r": statistics.median(average_rs) if average_rs else None,
        "worst_max_drawdown_fraction": max(drawdowns) if drawdowns else None,
    }
    return BatchDecision(
        promoted=not violations,
        violations=tuple(violations),
        run_decisions=decisions,
        summary=summary,
    )


def required_constant_net_r_per_trade(
    *,
    target_multiple: float,
    trades: int,
    risk_fraction: float = USER_RISK_FRACTION,
) -> float:
    """Constant net R needed per trade for the requested compounded multiple.

    This is a geometric hurdle, not a forecast.  It assumes each trade returns
    exactly the same net R and therefore understates the effect of variance,
    drawdown, skipped trades, liquidation constraints, and path dependence.
    """

    if target_multiple <= 1:
        raise ValueError("target_multiple must exceed one")
    if trades <= 0:
        raise ValueError("trades must be positive")
    if not 0 < risk_fraction < 1:
        raise ValueError("risk_fraction must be in (0, 1)")
    per_trade_growth = math.exp(math.log(target_multiple) / trades) - 1
    return per_trade_growth / risk_fraction
