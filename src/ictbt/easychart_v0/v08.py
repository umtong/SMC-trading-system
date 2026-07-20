from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import Iterable

import pandas as pd

from .domain import (
    FairValueGap,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from .execution import CostConfig
from .features import pivot_is_consumed, zone_is_consumed
from .pipeline import FeatureBook, Opportunity, OpportunityRejection, _frame_as_of
from .v07 import (
    SrFlipFvgAuthority,
    V07BuildDiagnostics,
    V07ExecutionArm,
    assemble_v07_opportunity,
    build_v07_scene_family_result,
)


class TargetOwnershipReason(str, Enum):
    HTF_EXTERNAL_PIVOT = "htf_external_pivot"
    EQUAL_LEVEL_LIQUIDITY = "equal_level_liquidity"
    OWNED_OB_AT_LIQUIDITY = "owned_ob_at_liquidity"
    FVG_AT_OWNED_LIQUIDITY = "fvg_at_owned_liquidity"


@dataclass(frozen=True, slots=True)
class V08TargetPolicy:
    """Terminal-target policy for V0.8.

    FVG and OB are delivery arrays, not automatic liquidity destinations.  A
    plain H1/H4 swing, an equal-level pool, or an OB/FVG backed by one of those
    independent liquidity objects may own the final target.  ``minimum_target_r``
    is a configurable ENGINEERING_V0 geometry floor and deliberately remains
    below 1R; the strategy is not forced to make every trade a 1R trade.
    """

    minimum_target_r: float = 0.65
    equal_level_tolerance_bps: float = 5.0
    maximum_target_age_days: int = 45

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum_target_r) or self.minimum_target_r <= 0:
            raise ValueError("minimum_target_r must be finite and positive")
        if (
            not math.isfinite(self.equal_level_tolerance_bps)
            or self.equal_level_tolerance_bps <= 0
        ):
            raise ValueError("equal_level_tolerance_bps must be finite and positive")
        if self.maximum_target_age_days <= 0:
            raise ValueError("maximum_target_age_days must be positive")


@dataclass(frozen=True, slots=True)
class OwnedTarget:
    candidate: TargetCandidate
    reason: TargetOwnershipReason
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.evidence_ids:
            raise ValueError("owned target requires independent evidence")


@dataclass(frozen=True, slots=True)
class V08BuildDiagnostics:
    base_scenes: int
    scenes_without_owned_terminal_target: int
    htf_pivot_targets: int
    equal_level_targets: int
    owned_ob_targets: int
    owned_fvg_targets: int
    authorities: int
    base_v07: V07BuildDiagnostics


@dataclass(frozen=True, slots=True)
class V08BuildResult:
    authorities: tuple[SrFlipFvgAuthority, ...]
    diagnostics: V08BuildDiagnostics
    ownership: dict[str, OwnedTarget]


def _target_kind(side: Side) -> str:
    return "high" if side is Side.LONG else "low"


def _ahead(
    *, side: Side, entry_price: float, zone: PriceZone, tick_size: float
) -> bool:
    order_price = zone.low if side is Side.LONG else zone.high
    return (
        order_price >= entry_price + tick_size - 1e-12
        if side is Side.LONG
        else order_price <= entry_price - tick_size + 1e-12
    )


def _active_pivots(
    book: FeatureBook,
    *,
    side: Side,
    as_of: pd.Timestamp,
    policy: V08TargetPolicy,
) -> tuple[StrictPivot, ...]:
    cutoff = as_of - pd.Timedelta(days=policy.maximum_target_age_days)
    kind = _target_kind(side)
    output: list[StrictPivot] = []
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, as_of)
        output.extend(
            pivot
            for pivot in book.pivots[timeframe]
            if pivot.symbol == book.symbol
            and pivot.kind == kind
            and cutoff <= pivot.known_at <= as_of
            and not pivot_is_consumed(pivot, frame, tick_size=book.tick_size)
        )
    return tuple(output)


def _equal_level_evidence(
    pivot: StrictPivot,
    pivots: Iterable[StrictPivot],
    *,
    policy: V08TargetPolicy,
    tick_size: float,
) -> tuple[str, ...]:
    tolerance = max(
        2 * tick_size,
        pivot.price * policy.equal_level_tolerance_bps / 10_000,
    )
    matches = tuple(
        other.pivot_id
        for other in pivots
        if other.pivot_id != pivot.pivot_id
        and other.kind == pivot.kind
        and abs(other.price - pivot.price) <= tolerance + 1e-12
    )
    return (pivot.pivot_id, *matches) if matches else ()


def _pivot_candidate(pivot: StrictPivot, *, side: Side) -> TargetCandidate:
    return TargetCandidate(
        candidate_id=f"v08-terminal:pivot:{pivot.pivot_id}",
        symbol=pivot.symbol,
        trade_side=side,
        kind="pivot",
        zone=PriceZone(pivot.price, pivot.price),
        known_at=pivot.known_at,
        source_id=pivot.pivot_id,
    )


def _order_block_owns_break(
    book: FeatureBook,
    block: OrderBlock,
) -> tuple[str, ...]:
    """Return pivots directly broken by the block's final formation close."""

    break_bar = block.formation_bars[-1]
    kind = "high" if block.side is Side.LONG else "low"
    evidence: list[str] = []
    for timeframe in (block.timeframe, Timeframe.H1, Timeframe.H4):
        if timeframe not in book.pivots:
            continue
        for pivot in book.pivots[timeframe]:
            if (
                pivot.kind != kind
                or pivot.known_at > break_bar.open_time
                or pivot.symbol != block.symbol
            ):
                continue
            broken = (
                break_bar.close >= pivot.price + book.tick_size - 1e-12
                if block.side is Side.LONG
                else break_bar.close <= pivot.price - book.tick_size + 1e-12
            )
            if broken:
                evidence.append(pivot.pivot_id)
    return tuple(sorted(set(evidence)))


def _active_opposing_order_blocks(
    book: FeatureBook,
    *,
    side: Side,
    as_of: pd.Timestamp,
    policy: V08TargetPolicy,
) -> tuple[OrderBlock, ...]:
    cutoff = as_of - pd.Timedelta(days=policy.maximum_target_age_days)
    output: list[OrderBlock] = []
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, as_of)
        for block in book.order_blocks[timeframe]:
            if (
                block.symbol != book.symbol
                or block.side is side
                or not cutoff <= block.known_at <= as_of
                or zone_is_consumed(
                    block.zone,
                    frame,
                    travel_side=side,
                    timeframe=timeframe,
                    tick_size=book.tick_size,
                    after=block.known_at,
                )
            ):
                continue
            output.append(block)
    return tuple(output)


def _active_opposing_fvgs(
    book: FeatureBook,
    *,
    side: Side,
    as_of: pd.Timestamp,
    policy: V08TargetPolicy,
) -> tuple[FairValueGap, ...]:
    cutoff = as_of - pd.Timedelta(days=policy.maximum_target_age_days)
    output: list[FairValueGap] = []
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, as_of)
        for gap in book.fvgs[timeframe]:
            if (
                gap.symbol != book.symbol
                or gap.side is side
                or not cutoff <= gap.known_at <= as_of
                or zone_is_consumed(
                    gap.zone,
                    frame,
                    travel_side=side,
                    timeframe=timeframe,
                    tick_size=book.tick_size,
                    after=gap.known_at,
                )
            ):
                continue
            output.append(gap)
    return tuple(output)


def build_owned_terminal_targets(
    book: FeatureBook,
    *,
    side: Side,
    entry_price: float,
    as_of: pd.Timestamp,
    excluded_source_ids: frozenset[str] = frozenset(),
    policy: V08TargetPolicy = V08TargetPolicy(),
) -> tuple[OwnedTarget, ...]:
    """Build only destinations with independent terminal-target ownership."""

    pivots = _active_pivots(book, side=side, as_of=as_of, policy=policy)
    owned: list[OwnedTarget] = []
    liquidity_zones: list[tuple[PriceZone, tuple[str, ...]]] = []

    for pivot in pivots:
        if pivot.pivot_id in excluded_source_ids:
            continue
        zone = PriceZone(pivot.price, pivot.price)
        if not _ahead(
            side=side,
            entry_price=entry_price,
            zone=zone,
            tick_size=book.tick_size,
        ):
            continue
        equal_evidence = _equal_level_evidence(
            pivot,
            pivots,
            policy=policy,
            tick_size=book.tick_size,
        )
        if pivot.timeframe in {Timeframe.H1, Timeframe.H4}:
            reason = TargetOwnershipReason.HTF_EXTERNAL_PIVOT
            evidence = (pivot.pivot_id,)
        elif equal_evidence:
            reason = TargetOwnershipReason.EQUAL_LEVEL_LIQUIDITY
            evidence = equal_evidence
        else:
            continue
        candidate = _pivot_candidate(pivot, side=side)
        owned.append(OwnedTarget(candidate, reason, evidence))
        liquidity_zones.append((candidate.zone, evidence))

    for block in _active_opposing_order_blocks(
        book,
        side=side,
        as_of=as_of,
        policy=policy,
    ):
        if block.ob_id in excluded_source_ids or not _ahead(
            side=side,
            entry_price=entry_price,
            zone=block.zone,
            tick_size=book.tick_size,
        ):
            continue
        break_evidence = _order_block_owns_break(book, block)
        liquidity_evidence = tuple(
            evidence_id
            for zone, evidence in liquidity_zones
            if block.zone.intersects(zone, tolerance=2 * book.tick_size)
            for evidence_id in evidence
        )
        evidence = tuple(sorted(set((*break_evidence, *liquidity_evidence))))
        if not break_evidence or not liquidity_evidence:
            continue
        candidate = TargetCandidate(
            candidate_id=f"v08-terminal:ob:{block.ob_id}",
            symbol=book.symbol,
            trade_side=side,
            kind="order_block",
            zone=block.zone,
            known_at=block.known_at,
            source_id=block.ob_id,
        )
        owned.append(
            OwnedTarget(
                candidate,
                TargetOwnershipReason.OWNED_OB_AT_LIQUIDITY,
                evidence,
            )
        )
        liquidity_zones.append((candidate.zone, (block.ob_id, *evidence)))

    for gap in _active_opposing_fvgs(
        book,
        side=side,
        as_of=as_of,
        policy=policy,
    ):
        if gap.fvg_id in excluded_source_ids or not _ahead(
            side=side,
            entry_price=entry_price,
            zone=gap.zone,
            tick_size=book.tick_size,
        ):
            continue
        evidence = tuple(
            evidence_id
            for zone, zone_evidence in liquidity_zones
            if gap.zone.intersects(zone, tolerance=2 * book.tick_size)
            for evidence_id in zone_evidence
        )
        evidence = tuple(sorted(set(evidence)))
        if not evidence:
            continue
        candidate = TargetCandidate(
            candidate_id=f"v08-terminal:fvg:{gap.fvg_id}",
            symbol=book.symbol,
            trade_side=side,
            kind="fvg",
            zone=gap.zone,
            known_at=gap.known_at,
            source_id=gap.fvg_id,
        )
        owned.append(
            OwnedTarget(
                candidate,
                TargetOwnershipReason.FVG_AT_OWNED_LIQUIDITY,
                evidence,
            )
        )

    def sort_key(item: OwnedTarget) -> tuple[float, int, str]:
        price = item.candidate.order_price
        distance = price - entry_price if side is Side.LONG else entry_price - price
        reason_priority = {
            TargetOwnershipReason.HTF_EXTERNAL_PIVOT: 0,
            TargetOwnershipReason.EQUAL_LEVEL_LIQUIDITY: 1,
            TargetOwnershipReason.OWNED_OB_AT_LIQUIDITY: 2,
            TargetOwnershipReason.FVG_AT_OWNED_LIQUIDITY: 3,
        }[item.reason]
        return distance, reason_priority, item.candidate.candidate_id

    deduplicated: dict[tuple[str, str], OwnedTarget] = {}
    for item in owned:
        key = (item.candidate.kind, item.candidate.source_id)
        current = deduplicated.get(key)
        if current is None or sort_key(item) < sort_key(current):
            deduplicated[key] = item
    return tuple(sorted(deduplicated.values(), key=sort_key))


def build_v08_scene_family_result(
    book: FeatureBook,
    *,
    policy: V08TargetPolicy = V08TargetPolicy(),
) -> V08BuildResult:
    base = build_v07_scene_family_result(book)
    selected: list[SrFlipFvgAuthority] = []
    ownership: dict[str, OwnedTarget] = {}
    counts = {reason: 0 for reason in TargetOwnershipReason}
    missing = 0

    for authority in base.authorities:
        targets = build_owned_terminal_targets(
            book,
            side=authority.side,
            entry_price=authority.boundary_pivot.price,
            as_of=authority.known_at,
            excluded_source_ids=frozenset(
                {authority.boundary_pivot.pivot_id, authority.fvg.fvg_id}
            ),
            policy=policy,
        )
        if not targets:
            missing += 1
            continue
        target = targets[0]
        updated = replace(authority, destination=target.candidate)
        selected.append(updated)
        ownership[updated.authority_id] = target
        counts[target.reason] += 1

    authorities = tuple(
        sorted(selected, key=lambda item: (item.known_at, item.authority_id))
    )
    return V08BuildResult(
        authorities=authorities,
        diagnostics=V08BuildDiagnostics(
            base_scenes=len(base.authorities),
            scenes_without_owned_terminal_target=missing,
            htf_pivot_targets=counts[TargetOwnershipReason.HTF_EXTERNAL_PIVOT],
            equal_level_targets=counts[TargetOwnershipReason.EQUAL_LEVEL_LIQUIDITY],
            owned_ob_targets=counts[TargetOwnershipReason.OWNED_OB_AT_LIQUIDITY],
            owned_fvg_targets=counts[
                TargetOwnershipReason.FVG_AT_OWNED_LIQUIDITY
            ],
            authorities=len(authorities),
            base_v07=base.diagnostics,
        ),
        ownership=ownership,
    )


def assemble_v08_opportunity(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
    *,
    costs: CostConfig,
    entry_arm: V07ExecutionArm | str,
    policy: V08TargetPolicy = V08TargetPolicy(),
) -> Opportunity | OpportunityRejection:
    result = assemble_v07_opportunity(
        book,
        authority,
        costs=costs,
        entry_arm=entry_arm,
    )
    if isinstance(result, OpportunityRejection):
        return result
    stop_distance = abs(result.planned_entry.price - result.initial_stop)
    target_distance = abs(result.target.order_price - result.planned_entry.price)
    if (
        stop_distance <= 0
        or target_distance / stop_distance + 1e-12 < policy.minimum_target_r
    ):
        return OpportunityRejection(
            symbol=result.symbol,
            side=result.side,
            authority=authority,
            reason="target_space_conflict",
        )
    return result
