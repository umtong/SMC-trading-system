from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .domain import (
    B1Subtype,
    FairValueGap,
    LiquidityEvent,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from .features import TIMEFRAME_DELTA
from .pipeline import FeatureBook
from .strategy import select_initial_target
from .v07 import (
    SrFlipFvgAuthority,
    V07BuildDiagnostics,
    V07BuildResult,
    _accepted,
    _boundary_is_in_fvg,
    _boundary_preference,
    _directional_break,
)


@dataclass(frozen=True, slots=True)
class V07LifecycleIndex:
    """First causal invalidation/consumption close for frozen V0.7 objects.

    The index may be built from the complete research frame because a decision
    only asks whether the first endpoint is at or before its as-of timestamp.
    This is exactly equivalent to scanning the completed prefix and does not
    expose the endpoint value as a feature.
    """

    pivot_consumed_at: dict[str, pd.Timestamp | None]
    block_invalidated_at: dict[str, pd.Timestamp | None]
    fvg_consumed_at: dict[str, pd.Timestamp | None]


def _first_close(
    frame: pd.DataFrame,
    *,
    timeframe: Timeframe,
    after: pd.Timestamp,
    condition: np.ndarray,
) -> pd.Timestamp | None:
    closes = frame.index + TIMEFRAME_DELTA[timeframe]
    start = int(closes.searchsorted(after, side="right"))
    hits = np.flatnonzero(condition[start:])
    if len(hits) == 0:
        return None
    return closes[start + int(hits[0])]


def _pivot_consumed_at(
    book: FeatureBook,
    pivot: StrictPivot,
) -> pd.Timestamp | None:
    frame = book.frames[pivot.timeframe]
    condition = (
        frame["high"].to_numpy(dtype=float, copy=False)
        >= pivot.price + book.tick_size
        if pivot.kind == "high"
        else frame["low"].to_numpy(dtype=float, copy=False)
        <= pivot.price - book.tick_size
    )
    return _first_close(
        frame,
        timeframe=pivot.timeframe,
        after=pivot.known_at,
        condition=condition,
    )


def _block_invalidated_at(
    book: FeatureBook,
    block: OrderBlock,
) -> pd.Timestamp | None:
    frame = book.frames[block.timeframe]
    condition = (
        frame["low"].to_numpy(dtype=float, copy=False) <= block.initial_stop
        if block.side is Side.LONG
        else frame["high"].to_numpy(dtype=float, copy=False) >= block.initial_stop
    )
    return _first_close(
        frame,
        timeframe=block.timeframe,
        after=block.known_at,
        condition=condition,
    )


def _fvg_consumed_at(
    book: FeatureBook,
    gap: FairValueGap,
) -> pd.Timestamp | None:
    frame = book.frames[gap.timeframe]
    # V0.7 uses each FVG only as a target for travel opposite to the FVG side.
    condition = (
        frame["close"].to_numpy(dtype=float, copy=False)
        <= gap.zone.low - book.tick_size
        if gap.side is Side.LONG
        else frame["close"].to_numpy(dtype=float, copy=False)
        >= gap.zone.high + book.tick_size
    )
    return _first_close(
        frame,
        timeframe=gap.timeframe,
        after=gap.known_at,
        condition=condition,
    )


def build_v07_lifecycle_index(book: FeatureBook) -> V07LifecycleIndex:
    pivots = {
        pivot.pivot_id: _pivot_consumed_at(book, pivot)
        for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4)
        for pivot in book.pivots[timeframe]
    }
    blocks = {
        block.ob_id: _block_invalidated_at(book, block)
        for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4)
        for block in book.order_blocks[timeframe]
    }
    gaps = {
        gap.fvg_id: _fvg_consumed_at(book, gap)
        for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4)
        for gap in book.fvgs[timeframe]
    }
    return V07LifecycleIndex(
        pivot_consumed_at=pivots,
        block_invalidated_at=blocks,
        fvg_consumed_at=gaps,
    )


def _still_active(endpoint: pd.Timestamp | None, as_of: pd.Timestamp) -> bool:
    return endpoint is None or endpoint > as_of


def _active_boundaries_indexed(
    book: FeatureBook,
    index: V07LifecycleIndex,
    *,
    side: Side,
    break_open: pd.Timestamp,
) -> tuple[StrictPivot, ...]:
    kind = "high" if side is Side.LONG else "low"
    return tuple(
        pivot
        for timeframe in (Timeframe.M15, Timeframe.H1)
        for pivot in book.pivots[timeframe]
        if pivot.symbol == book.symbol
        and pivot.kind == kind
        and pivot.known_at <= break_open
        and _still_active(index.pivot_consumed_at[pivot.pivot_id], break_open)
    )


def _nearest_destination_indexed(
    book: FeatureBook,
    index: V07LifecycleIndex,
    *,
    side: Side,
    entry_price: float,
    as_of: pd.Timestamp,
    excluded_source_ids: frozenset[str],
) -> TargetCandidate | None:
    candidates: list[TargetCandidate] = []
    target_pivot_kind = "high" if side is Side.LONG else "low"
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        for pivot in book.pivots[timeframe]:
            if (
                pivot.symbol != book.symbol
                or pivot.kind != target_pivot_kind
                or pivot.known_at > as_of
                or pivot.pivot_id in excluded_source_ids
                or not _still_active(
                    index.pivot_consumed_at[pivot.pivot_id],
                    as_of,
                )
            ):
                continue
            candidates.append(
                TargetCandidate(
                    candidate_id=f"v07-scene-target:pivot:{pivot.pivot_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="pivot",
                    zone=PriceZone(pivot.price, pivot.price),
                    known_at=pivot.known_at,
                    source_id=pivot.pivot_id,
                )
            )

        for block in book.order_blocks[timeframe]:
            if (
                block.symbol != book.symbol
                or block.side is side
                or block.known_at > as_of
                or block.ob_id in excluded_source_ids
                or not _still_active(
                    index.block_invalidated_at[block.ob_id],
                    as_of,
                )
            ):
                continue
            candidates.append(
                TargetCandidate(
                    candidate_id=f"v07-scene-target:ob:{block.ob_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="order_block",
                    zone=block.zone,
                    known_at=block.known_at,
                    source_id=block.ob_id,
                )
            )

        for gap in book.fvgs[timeframe]:
            if (
                gap.symbol != book.symbol
                or gap.side is side
                or gap.known_at > as_of
                or gap.fvg_id in excluded_source_ids
                or not _still_active(
                    index.fvg_consumed_at[gap.fvg_id],
                    as_of,
                )
            ):
                continue
            candidates.append(
                TargetCandidate(
                    candidate_id=f"v07-scene-target:fvg:{gap.fvg_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="fvg",
                    zone=gap.zone,
                    known_at=gap.known_at,
                    source_id=gap.fvg_id,
                )
            )

    return select_initial_target(
        candidates,
        side=side,
        entry_price=entry_price,
        tick_size=book.tick_size,
    ).target


def build_v07_scene_family_result_indexed(
    book: FeatureBook,
    *,
    lifecycle_index: V07LifecycleIndex | None = None,
) -> V07BuildResult:
    """Causally equivalent V0.7 builder with indexed object lifetimes."""

    index = lifecycle_index or build_v07_lifecycle_index(book)
    raw: list[SrFlipFvgAuthority] = []
    preconfirmed = 0
    directional = 0
    accepted = 0
    linked = 0
    targets_missing = 0

    for gap in book.fvgs[Timeframe.M15]:
        a_bar, break_bar, acceptance_bar = gap.formation_bars
        boundaries = _active_boundaries_indexed(
            book,
            index,
            side=gap.side,
            break_open=break_bar.open_time,
        )
        if not boundaries:
            continue
        preconfirmed += 1
        broken = tuple(
            boundary
            for boundary in boundaries
            if _directional_break(
                side=gap.side,
                a_bar=a_bar,
                break_bar=break_bar,
                boundary=boundary.price,
                tick_size=book.tick_size,
            )
        )
        if not broken:
            continue
        directional += 1
        accepted_boundaries = tuple(
            boundary
            for boundary in broken
            if _accepted(
                side=gap.side,
                acceptance_bar=acceptance_bar,
                boundary=boundary.price,
                tick_size=book.tick_size,
            )
        )
        if not accepted_boundaries:
            continue
        accepted += 1
        linked_boundaries = tuple(
            boundary
            for boundary in accepted_boundaries
            if _boundary_is_in_fvg(gap, boundary=boundary.price)
        )
        if not linked_boundaries:
            continue
        linked += 1
        boundary = min(linked_boundaries, key=_boundary_preference)

        root_id = (
            f"v07-sr-flip-fvg:{book.symbol}:{gap.side.value}:"
            f"{break_bar.open_time.isoformat()}"
        )
        event_extreme = (
            min(bar.low for bar in gap.formation_bars)
            if gap.side is Side.LONG
            else max(bar.high for bar in gap.formation_bars)
        )
        event = LiquidityEvent(
            event_id=f"{root_id}:boundary-acceptance",
            symbol=book.symbol,
            timeframe=Timeframe.M15,
            subtype=B1Subtype.BREAK_RETEST,
            side=gap.side,
            node_id=boundary.pivot_id,
            node_price=boundary.price,
            event_time=break_bar.open_time,
            known_at=acceptance_bar.close_time,
            event_extreme=event_extreme,
        )
        destination = _nearest_destination_indexed(
            book,
            index,
            side=gap.side,
            entry_price=boundary.price,
            as_of=gap.known_at,
            excluded_source_ids=frozenset({boundary.pivot_id, gap.fvg_id}),
        )
        if destination is None:
            targets_missing += 1
            continue

        stop_extreme = event_extreme
        initial_stop = (
            stop_extreme - book.tick_size
            if gap.side is Side.LONG
            else stop_extreme + book.tick_size
        )
        impulse_extreme = (
            max(bar.high for bar in gap.formation_bars)
            if gap.side is Side.LONG
            else min(bar.low for bar in gap.formation_bars)
        )
        raw.append(
            SrFlipFvgAuthority(
                authority_id=(
                    f"{root_id}|boundary={boundary.pivot_id}|fvg={gap.fvg_id}"
                ),
                scene_root_id=root_id,
                symbol=book.symbol,
                side=gap.side,
                boundary_pivot=boundary,
                liquidity_event=event,
                fvg=gap,
                break_bar=break_bar,
                acceptance_bar=acceptance_bar,
                zone=PriceZone(boundary.price, boundary.price),
                known_at=gap.known_at,
                stop_extreme=stop_extreme,
                initial_stop=initial_stop,
                impulse_extreme=impulse_extreme,
                destination=destination,
            )
        )

    grouped: dict[str, list[SrFlipFvgAuthority]] = {}
    for authority in raw:
        grouped.setdefault(authority.scene_root_id, []).append(authority)
    selected = [
        min(items, key=lambda authority: authority.authority_id)
        for items in grouped.values()
    ]
    authorities = tuple(
        sorted(
            selected,
            key=lambda authority: (authority.known_at, authority.authority_id),
        )
    )
    return V07BuildResult(
        authorities=authorities,
        diagnostics=V07BuildDiagnostics(
            m15_fvgs=len(book.fvgs[Timeframe.M15]),
            preconfirmed_boundaries=preconfirmed,
            directional_breaks=directional,
            accepted_breaks=accepted,
            boundary_linked_fvgs=linked,
            targets_missing_at_acceptance=targets_missing,
            duplicate_scenes_suppressed=len(raw) - len(authorities),
            authorities=len(authorities),
        ),
    )


__all__ = [
    "V07LifecycleIndex",
    "build_v07_lifecycle_index",
    "build_v07_scene_family_result_indexed",
]
