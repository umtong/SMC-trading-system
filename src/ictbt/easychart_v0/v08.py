from __future__ import annotations

from dataclasses import dataclass, replace
import math
import statistics
from typing import Literal

import pandas as pd

from .domain import PriceZone, Side, StrictPivot, TargetCandidate, Timeframe
from .execution import CostConfig, RiskConfig
from .features import pivot_is_consumed
from .pipeline import (
    FeatureBook,
    Opportunity,
    OpportunityRejection,
    StructureState,
    _frame_as_of,
    build_feature_book,
    structure_snapshot,
)
from .v07 import (
    SrFlipFvgAuthority,
    V07ExecutionArm,
    assemble_v07_opportunity,
    build_v07_scene_family_result,
    run_v07_historical_replay,
)


V08ContextMode = Literal["trend_continuation", "h1_range_expansion"]


@dataclass(frozen=True, slots=True)
class V08Policy:
    """Economic and structural admission rules for the V0.8 SR-flip family.

    Order blocks and FVGs may explain delivery or refine execution, but they do
    not create a terminal objective by themselves.  The terminal draw must be
    an already-confirmed, still-unconsumed H1/H4 liquidity pivot.
    """

    displacement_lookback_bars: int = 20
    minimum_displacement_history_bars: int = 8
    minimum_displacement_range_multiple: float = 1.20
    minimum_displacement_body_fraction: float = 0.55
    minimum_close_through_fraction: float = 0.15
    minimum_acceptance_buffer_fraction: float = 0.05
    minimum_target_r: float = 0.75
    risk_fraction: float = 0.03
    maximum_notional_to_equity: float = 8.0
    allow_h1_range_expansion: bool = True

    def __post_init__(self) -> None:
        if self.displacement_lookback_bars <= 0:
            raise ValueError("displacement_lookback_bars must be positive")
        if not 1 <= self.minimum_displacement_history_bars <= self.displacement_lookback_bars:
            raise ValueError("minimum displacement history is invalid")
        for name in (
            "minimum_displacement_range_multiple",
            "minimum_displacement_body_fraction",
            "minimum_close_through_fraction",
            "minimum_acceptance_buffer_fraction",
            "minimum_target_r",
            "risk_fraction",
            "maximum_notional_to_equity",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        if self.minimum_displacement_body_fraction > 1:
            raise ValueError("minimum_displacement_body_fraction cannot exceed one")
        if self.risk_fraction > 1:
            raise ValueError("risk_fraction cannot exceed one")
        object.__setattr__(self, "allow_h1_range_expansion", bool(self.allow_h1_range_expansion))


@dataclass(frozen=True, slots=True)
class V08BuildDiagnostics:
    base_v07_scenes: int
    htf_context_rejections: int
    displacement_rejections: int
    external_liquidity_missing: int
    target_space_rejections: int
    exposure_rejections: int
    trend_continuation_authorities: int
    h1_range_expansion_authorities: int
    authorities: int


@dataclass(frozen=True, slots=True)
class V08BuildResult:
    authorities: tuple[SrFlipFvgAuthority, ...]
    diagnostics: V08BuildDiagnostics


def _direction(side: Side) -> float:
    return 1.0 if side is Side.LONG else -1.0


def _context_mode(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
    *,
    policy: V08Policy,
) -> V08ContextMode | None:
    snapshot = structure_snapshot(book, as_of=authority.known_at)
    if authority.side is Side.LONG:
        if snapshot.h1 is StructureState.UP and snapshot.h4 is not StructureState.DOWN:
            return "trend_continuation"
        if (
            policy.allow_h1_range_expansion
            and snapshot.h1 is StructureState.RANGE
            and snapshot.h4 is not StructureState.DOWN
            and authority.boundary_pivot.timeframe is Timeframe.H1
        ):
            return "h1_range_expansion"
        return None
    if snapshot.h1 is StructureState.DOWN and snapshot.h4 is not StructureState.UP:
        return "trend_continuation"
    if (
        policy.allow_h1_range_expansion
        and snapshot.h1 is StructureState.RANGE
        and snapshot.h4 is not StructureState.UP
        and authority.boundary_pivot.timeframe is Timeframe.H1
    ):
        return "h1_range_expansion"
    return None


def _displacement_is_material(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
    *,
    policy: V08Policy,
) -> bool:
    frame = book.frames[Timeframe.M15]
    prior = frame.loc[frame.index < authority.break_bar.open_time].tail(
        policy.displacement_lookback_bars
    )
    if len(prior) < policy.minimum_displacement_history_bars:
        return False
    prior_ranges = [
        float(high) - float(low)
        for high, low in zip(prior["high"], prior["low"], strict=True)
        if float(high) > float(low)
    ]
    if len(prior_ranges) < policy.minimum_displacement_history_bars:
        return False
    median_range = statistics.median(prior_ranges)
    break_range = authority.break_bar.high - authority.break_bar.low
    if median_range <= 0 or break_range <= 0:
        return False
    body = abs(authority.break_bar.close - authority.break_bar.open)
    body_fraction = body / break_range
    close_through = (
        authority.break_bar.close - authority.boundary_pivot.price
        if authority.side is Side.LONG
        else authority.boundary_pivot.price - authority.break_bar.close
    ) / break_range
    close_location = (
        (authority.break_bar.close - authority.break_bar.low) / break_range
        if authority.side is Side.LONG
        else (authority.break_bar.high - authority.break_bar.close) / break_range
    )
    acceptance_buffer = (
        authority.acceptance_bar.close - authority.boundary_pivot.price
        if authority.side is Side.LONG
        else authority.boundary_pivot.price - authority.acceptance_bar.close
    ) / break_range
    return (
        break_range + 1e-12
        >= median_range * policy.minimum_displacement_range_multiple
        and body_fraction + 1e-12 >= policy.minimum_displacement_body_fraction
        and close_through + 1e-12 >= policy.minimum_close_through_fraction
        and close_location + 1e-12 >= 0.75
        and acceptance_buffer + 1e-12 >= policy.minimum_acceptance_buffer_fraction
    )


def _ahead(side: Side, price: float, entry: float, tick_size: float) -> bool:
    return _direction(side) * (price - entry) >= tick_size - 1e-12


def _external_liquidity_target(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
) -> TargetCandidate | None:
    entry = authority.zone.high if authority.side is Side.LONG else authority.zone.low
    kind = "high" if authority.side is Side.LONG else "low"
    candidates: list[StrictPivot] = []
    for timeframe in (Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, authority.known_at)
        for pivot in book.pivots[timeframe]:
            if (
                pivot.symbol != book.symbol
                or pivot.kind != kind
                or pivot.pivot_id == authority.boundary_pivot.pivot_id
                or pivot.known_at > authority.break_bar.open_time
                or not _ahead(authority.side, pivot.price, entry, book.tick_size)
                or pivot_is_consumed(
                    pivot,
                    frame,
                    tick_size=book.tick_size,
                )
            ):
                continue
            candidates.append(pivot)
    if not candidates:
        return None
    pivot = min(
        candidates,
        key=lambda item: (
            abs(item.price - entry),
            0 if item.timeframe is Timeframe.H4 else 1,
            -item.pivot_time.value,
            item.pivot_id,
        ),
    )
    return TargetCandidate(
        candidate_id=f"v08-external-liquidity:{pivot.pivot_id}",
        symbol=book.symbol,
        trade_side=authority.side,
        kind="pivot",
        zone=PriceZone(pivot.price, pivot.price),
        known_at=pivot.known_at,
        source_id=pivot.pivot_id,
    )


def _target_r(
    authority: SrFlipFvgAuthority,
    target: TargetCandidate,
) -> float:
    entry = authority.zone.high if authority.side is Side.LONG else authority.zone.low
    risk_distance = abs(entry - authority.initial_stop)
    if risk_distance <= 0:
        return 0.0
    return _direction(authority.side) * (target.order_price - entry) / risk_distance


def _required_notional_to_equity(
    authority: SrFlipFvgAuthority,
    *,
    costs: CostConfig,
    risk_fraction: float,
) -> float:
    entry = authority.zone.high if authority.side is Side.LONG else authority.zone.low
    stop = authority.initial_stop
    slippage = costs.stop_slippage_bps / 10_000
    stop_fill = stop * (
        1.0 - slippage if authority.side is Side.LONG else 1.0 + slippage
    )
    unit_risk = (
        abs(entry - stop)
        + abs(stop - stop_fill)
        + entry * costs.entry_fee_rate
        + stop_fill * costs.stop_fee_rate
    )
    if unit_risk <= 0:
        return math.inf
    return risk_fraction * entry / unit_risk


def build_v08_scene_family_result(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08Policy = V08Policy(),
) -> V08BuildResult:
    """Qualify V0.7 geometry with HTF narrative and liquidity-owned targets."""

    base = build_v07_scene_family_result(book)
    output: list[SrFlipFvgAuthority] = []
    htf_rejections = 0
    displacement_rejections = 0
    target_missing = 0
    target_space_rejections = 0
    exposure_rejections = 0
    modes: dict[V08ContextMode, int] = {
        "trend_continuation": 0,
        "h1_range_expansion": 0,
    }

    for authority in base.authorities:
        mode = _context_mode(book, authority, policy=policy)
        if mode is None:
            htf_rejections += 1
            continue
        if not _displacement_is_material(book, authority, policy=policy):
            displacement_rejections += 1
            continue
        target = _external_liquidity_target(book, authority)
        if target is None:
            target_missing += 1
            continue
        if _target_r(authority, target) + 1e-12 < policy.minimum_target_r:
            target_space_rejections += 1
            continue
        required_exposure = _required_notional_to_equity(
            authority,
            costs=costs,
            risk_fraction=policy.risk_fraction,
        )
        if required_exposure > policy.maximum_notional_to_equity + 1e-12:
            exposure_rejections += 1
            continue
        output.append(
            replace(
                authority,
                authority_id=(
                    f"v08-htf-liquidity-delivery:{authority.authority_id}|"
                    f"target={target.source_id}"
                ),
                scene_root_id=f"v08:{authority.scene_root_id}",
                destination=target,
            )
        )
        modes[mode] += 1

    authorities = tuple(
        sorted(output, key=lambda item: (item.known_at, item.authority_id))
    )
    return V08BuildResult(
        authorities=authorities,
        diagnostics=V08BuildDiagnostics(
            base_v07_scenes=len(base.authorities),
            htf_context_rejections=htf_rejections,
            displacement_rejections=displacement_rejections,
            external_liquidity_missing=target_missing,
            target_space_rejections=target_space_rejections,
            exposure_rejections=exposure_rejections,
            trend_continuation_authorities=modes["trend_continuation"],
            h1_range_expansion_authorities=modes["h1_range_expansion"],
            authorities=len(authorities),
        ),
    )


def build_v08_scene_family_authorities(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08Policy = V08Policy(),
) -> tuple[SrFlipFvgAuthority, ...]:
    return build_v08_scene_family_result(book, costs=costs, policy=policy).authorities


def assemble_v08_opportunity(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
    *,
    costs: CostConfig,
) -> Opportunity | OpportunityRejection:
    return assemble_v07_opportunity(
        book,
        authority,
        costs=costs,
        entry_arm=V07ExecutionArm.FIRST_RETURN_LIMIT,
    )


def run_v08_historical_replay(
    candles_5m: pd.DataFrame,
    *,
    symbol: str,
    tick_size: float,
    equity: float,
    costs: CostConfig,
    risk: RiskConfig,
    book: FeatureBook | None = None,
    authorities: tuple[object, ...] | None = None,
    policy: V08Policy = V08Policy(),
    use_v03_targets: bool | None = None,
):
    feature_book = (
        book
        if book is not None
        else build_feature_book(candles_5m, symbol=symbol, tick_size=tick_size)
    )
    selected = (
        build_v08_scene_family_result(
            feature_book,
            costs=costs,
            policy=policy,
        ).authorities
        if authorities is None
        else tuple(authorities)
    )
    return run_v07_historical_replay(
        candles_5m,
        symbol=symbol,
        tick_size=tick_size,
        equity=equity,
        costs=costs,
        risk=risk,
        entry_arm=V07ExecutionArm.FIRST_RETURN_LIMIT,
        book=feature_book,
        authorities=selected,
        use_v03_targets=use_v03_targets,
    )


__all__ = [
    "V08BuildDiagnostics",
    "V08BuildResult",
    "V08ContextMode",
    "V08Policy",
    "assemble_v08_opportunity",
    "build_v08_scene_family_authorities",
    "build_v08_scene_family_result",
    "run_v08_historical_replay",
]
