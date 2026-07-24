from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable

from .domain import PriceZone, Side, StrictPivot, TargetCandidate, Timeframe
from .features import pivot_is_consumed, zone_is_consumed
from .pipeline import (
    FeatureBook,
    StructureState,
    _frame_as_of,
    _order_block_is_active,
    structure_snapshot,
)
from .strategy import select_initial_target
from .v07 import (
    SrFlipFvgAuthority,
    V07BuildResult,
    build_v07_scene_family_result,
)


class V08TargetPolicy(str, Enum):
    """Pre-registered target-ownership policies for the V0.7 scene."""

    BASELINE_NEAREST_ANY = "baseline_nearest_any"
    PIVOT_ONLY = "pivot_only"
    PIVOT_ZONE_CONFLUENT = "pivot_zone_confluent"


class V08ContextPolicy(str, Enum):
    """Causal H1/H4 guards evaluated at the scene-completion clock."""

    NONE = "none"
    NOT_OPPOSED = "not_opposed"
    STRICT_DELIVERY = "strict_delivery"


@dataclass(frozen=True, slots=True)
class V08BuildDiagnostics:
    baseline_authorities: int
    context_rejections: int
    target_missing: int
    destination_changed: int
    authorities: int
    destination_pivots: int


@dataclass(frozen=True, slots=True)
class V08BuildResult:
    authorities: tuple[SrFlipFvgAuthority, ...]
    diagnostics: V08BuildDiagnostics


_TIMEFRAME_RANK = {
    Timeframe.M15: 0,
    Timeframe.H1: 1,
    Timeframe.H4: 2,
}


def _context_allows(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
    policy: V08ContextPolicy,
) -> bool:
    if policy is V08ContextPolicy.NONE:
        return True
    snapshot = structure_snapshot(book, as_of=authority.known_at)
    if policy is V08ContextPolicy.NOT_OPPOSED:
        if authority.side is Side.LONG:
            return (
                snapshot.h1 is not StructureState.DOWN
                and snapshot.h4 is not StructureState.DOWN
            )
        return (
            snapshot.h1 is not StructureState.UP
            and snapshot.h4 is not StructureState.UP
        )
    if authority.side is Side.LONG:
        return (
            snapshot.h1 is StructureState.UP
            and snapshot.h4 is not StructureState.DOWN
        )
    return (
        snapshot.h1 is StructureState.DOWN
        and snapshot.h4 is not StructureState.UP
    )


def _active_target_pivots(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
) -> tuple[StrictPivot, ...]:
    wanted_kind = "high" if authority.side is Side.LONG else "low"
    output: list[StrictPivot] = []
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, authority.known_at)
        output.extend(
            pivot
            for pivot in book.pivots[timeframe]
            if pivot.symbol == book.symbol
            and pivot.kind == wanted_kind
            and pivot.known_at <= authority.known_at
            and pivot.pivot_id != authority.boundary_pivot.pivot_id
            and not pivot_is_consumed(
                pivot,
                frame,
                tick_size=book.tick_size,
            )
        )
    return tuple(
        sorted(output, key=lambda item: (item.known_at, item.pivot_id))
    )


def _active_opposing_zones(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
) -> tuple[tuple[Timeframe, PriceZone, str], ...]:
    zones: list[tuple[Timeframe, PriceZone, str]] = []
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, authority.known_at)
        for block in book.order_blocks[timeframe]:
            if (
                block.symbol == book.symbol
                and block.side is not authority.side
                and block.known_at <= authority.known_at
                and _order_block_is_active(book, block, as_of=authority.known_at)
            ):
                zones.append((timeframe, block.zone, block.ob_id))
        for gap in book.fvgs[timeframe]:
            if (
                gap.symbol == book.symbol
                and gap.side is not authority.side
                and gap.known_at <= authority.known_at
                and not zone_is_consumed(
                    gap.zone,
                    frame,
                    travel_side=authority.side,
                    timeframe=timeframe,
                    tick_size=book.tick_size,
                    after=gap.known_at,
                )
            ):
                zones.append((timeframe, gap.zone, gap.fvg_id))
    return tuple(zones)


def _pivot_is_zone_confluent(
    pivot: StrictPivot,
    zones: Iterable[tuple[Timeframe, PriceZone, str]],
    *,
    tick_size: float,
) -> bool:
    pivot_rank = _TIMEFRAME_RANK[pivot.timeframe]
    return any(
        _TIMEFRAME_RANK[zone_timeframe] >= pivot_rank
        and zone.low - tick_size <= pivot.price <= zone.high + tick_size
        for zone_timeframe, zone, _source_id in zones
    )


def _pivot_candidate(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
    pivot: StrictPivot,
    *,
    policy: V08TargetPolicy,
) -> TargetCandidate:
    return TargetCandidate(
        candidate_id=(
            f"v08-target:{policy.value}:pivot:{pivot.pivot_id}"
        ),
        symbol=book.symbol,
        trade_side=authority.side,
        kind="pivot",
        zone=PriceZone(pivot.price, pivot.price),
        known_at=pivot.known_at,
        source_id=pivot.pivot_id,
    )


def _select_destination(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
    policy: V08TargetPolicy,
) -> TargetCandidate | None:
    if policy is V08TargetPolicy.BASELINE_NEAREST_ANY:
        return authority.destination

    pivots = _active_target_pivots(book, authority)
    if policy is V08TargetPolicy.PIVOT_ZONE_CONFLUENT:
        zones = _active_opposing_zones(book, authority)
        pivots = tuple(
            pivot
            for pivot in pivots
            if _pivot_is_zone_confluent(
                pivot,
                zones,
                tick_size=book.tick_size,
            )
        )
    candidates = tuple(
        _pivot_candidate(book, authority, pivot, policy=policy)
        for pivot in pivots
    )
    return select_initial_target(
        candidates,
        side=authority.side,
        entry_price=authority.boundary_pivot.price,
        tick_size=book.tick_size,
    ).target


def build_v08_scene_family_result(
    book: FeatureBook,
    *,
    target_policy: V08TargetPolicy | str,
    context_policy: V08ContextPolicy | str,
    baseline: V07BuildResult | None = None,
) -> V08BuildResult:
    """Re-freeze V0.7 scenes with causal target ownership and HTF context.

    The scene clock, boundary, FVG, stop, and entry contract are unchanged.
    Only information known by the V0.7 C-bar close may affect target/context.
    A caller may supply the already-built V0.7 result to avoid recomputing the
    identical causal scene set for several pre-registered policy arms.
    """

    target = V08TargetPolicy(target_policy)
    context = V08ContextPolicy(context_policy)
    source = baseline or build_v07_scene_family_result(book)
    selected: list[SrFlipFvgAuthority] = []
    context_rejections = 0
    target_missing = 0
    destination_changed = 0

    for authority in source.authorities:
        if not _context_allows(book, authority, context):
            context_rejections += 1
            continue
        destination = _select_destination(book, authority, target)
        if destination is None:
            target_missing += 1
            continue
        if destination.source_id != authority.destination.source_id:
            destination_changed += 1
        selected.append(
            replace(
                authority,
                authority_id=(
                    f"{authority.authority_id}|v08-target={target.value}"
                    f"|v08-context={context.value}"
                ),
                destination=destination,
            )
        )

    authorities = tuple(
        sorted(selected, key=lambda item: (item.known_at, item.authority_id))
    )
    return V08BuildResult(
        authorities=authorities,
        diagnostics=V08BuildDiagnostics(
            baseline_authorities=len(source.authorities),
            context_rejections=context_rejections,
            target_missing=target_missing,
            destination_changed=destination_changed,
            authorities=len(authorities),
            destination_pivots=sum(
                item.destination.kind == "pivot" for item in authorities
            ),
        ),
    )


__all__ = [
    "V08BuildDiagnostics",
    "V08BuildResult",
    "V08ContextPolicy",
    "V08TargetPolicy",
    "build_v08_scene_family_result",
]
