from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import pandas as pd

from .domain import PriceZone, Side, StrictPivot, TargetCandidate, Timeframe
from .features import pivot_is_consumed, zone_is_consumed
from .pipeline import FeatureBook, _frame_as_of, _order_block_is_active


DestinationRejection = Literal[
    "no_preexisting_pivot_liquidity",
    "intervening_structure",
]


@dataclass(frozen=True, slots=True)
class PivotDestinationDecision:
    """Admission result for a pivot-owned terminal destination.

    The terminal objective must be an already-known, still-active pivot. OBs and
    FVGs remain meaningful reaction areas, but a nearer active structure blocks a
    farther pivot rather than being silently skipped to manufacture a larger R.
    Structures whose first-touch price is within one tick of the pivot are treated
    as location confluence rather than as a separate earlier obstacle.
    """

    target: TargetCandidate | None
    blocker: TargetCandidate | None
    reason: DestinationRejection | None

    def __post_init__(self) -> None:
        if self.reason is None:
            if self.target is None or self.blocker is not None:
                raise ValueError("an accepted destination requires one target and no blocker")
            return
        if self.reason == "no_preexisting_pivot_liquidity":
            if self.target is not None or self.blocker is not None:
                raise ValueError("a missing-pivot rejection cannot carry target data")
            return
        if self.target is None or self.blocker is None:
            raise ValueError("an intervening-structure rejection requires target and blocker")

    @property
    def accepted(self) -> bool:
        return self.reason is None


def _utc(value: object, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tz is None:
        raise ValueError(f"{name} must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _direction(side: Side) -> float:
    return 1.0 if side is Side.LONG else -1.0


def _distance(side: Side, *, entry_price: float, candidate_price: float) -> float:
    return _direction(side) * (candidate_price - entry_price)


def _ahead(
    side: Side,
    *,
    entry_price: float,
    candidate_price: float,
    tick_size: float,
) -> bool:
    return _distance(
        side,
        entry_price=entry_price,
        candidate_price=candidate_price,
    ) >= tick_size - 1e-12


def _timeframe_rank(timeframe: Timeframe) -> int:
    return {
        Timeframe.H4: 0,
        Timeframe.H1: 1,
        Timeframe.M15: 2,
        Timeframe.M5: 3,
    }[timeframe]


def _pivot_candidate(pivot: StrictPivot, *, side: Side, prefix: str) -> TargetCandidate:
    return TargetCandidate(
        candidate_id=f"{prefix}:pivot:{pivot.pivot_id}",
        symbol=pivot.symbol,
        trade_side=side,
        kind="pivot",
        zone=PriceZone(pivot.price, pivot.price),
        known_at=pivot.known_at,
        source_id=pivot.pivot_id,
    )


def _target_pivots(
    book: FeatureBook,
    *,
    side: Side,
    entry_price: float,
    target_known_by: pd.Timestamp,
    decision_at: pd.Timestamp,
    target_timeframes: Sequence[Timeframe],
    excluded_source_ids: frozenset[str],
) -> tuple[tuple[TargetCandidate, Timeframe, StrictPivot], ...]:
    pivot_kind = "high" if side is Side.LONG else "low"
    output: list[tuple[TargetCandidate, Timeframe, StrictPivot]] = []
    for timeframe in target_timeframes:
        frame = _frame_as_of(book, timeframe, decision_at)
        for pivot in book.pivots[timeframe]:
            if (
                pivot.symbol != book.symbol
                or pivot.kind != pivot_kind
                or pivot.pivot_id in excluded_source_ids
                or pivot.known_at > target_known_by
                or not _ahead(
                    side,
                    entry_price=entry_price,
                    candidate_price=pivot.price,
                    tick_size=book.tick_size,
                )
                or pivot_is_consumed(
                    pivot,
                    frame,
                    tick_size=book.tick_size,
                )
            ):
                continue
            output.append(
                (
                    _pivot_candidate(
                        pivot,
                        side=side,
                        prefix="pivot-destination",
                    ),
                    timeframe,
                    pivot,
                )
            )
    return tuple(output)


def _structural_obstacles(
    book: FeatureBook,
    *,
    side: Side,
    entry_price: float,
    decision_at: pd.Timestamp,
    obstacle_timeframes: Sequence[Timeframe],
    excluded_source_ids: frozenset[str],
) -> tuple[TargetCandidate, ...]:
    pivot_kind = "high" if side is Side.LONG else "low"
    output: list[TargetCandidate] = []

    for timeframe in obstacle_timeframes:
        frame = _frame_as_of(book, timeframe, decision_at)
        for pivot in book.pivots[timeframe]:
            if (
                pivot.symbol != book.symbol
                or pivot.kind != pivot_kind
                or pivot.pivot_id in excluded_source_ids
                or pivot.known_at > decision_at
                or not _ahead(
                    side,
                    entry_price=entry_price,
                    candidate_price=pivot.price,
                    tick_size=book.tick_size,
                )
                or pivot_is_consumed(
                    pivot,
                    frame,
                    tick_size=book.tick_size,
                )
            ):
                continue
            output.append(
                _pivot_candidate(
                    pivot,
                    side=side,
                    prefix="structural-obstacle",
                )
            )

        for block in book.order_blocks[timeframe]:
            if (
                block.symbol != book.symbol
                or block.side is side
                or block.ob_id in excluded_source_ids
                or block.known_at > decision_at
                or not _order_block_is_active(book, block, as_of=decision_at)
            ):
                continue
            candidate = TargetCandidate(
                candidate_id=f"structural-obstacle:ob:{block.ob_id}",
                symbol=book.symbol,
                trade_side=side,
                kind="order_block",
                zone=block.zone,
                known_at=block.known_at,
                source_id=block.ob_id,
            )
            if _ahead(
                side,
                entry_price=entry_price,
                candidate_price=candidate.order_price,
                tick_size=book.tick_size,
            ):
                output.append(candidate)

        for gap in book.fvgs[timeframe]:
            if (
                gap.symbol != book.symbol
                or gap.side is side
                or gap.fvg_id in excluded_source_ids
                or gap.known_at > decision_at
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
            candidate = TargetCandidate(
                candidate_id=f"structural-obstacle:fvg:{gap.fvg_id}",
                symbol=book.symbol,
                trade_side=side,
                kind="fvg",
                zone=gap.zone,
                known_at=gap.known_at,
                source_id=gap.fvg_id,
            )
            if _ahead(
                side,
                entry_price=entry_price,
                candidate_price=candidate.order_price,
                tick_size=book.tick_size,
            ):
                output.append(candidate)

    deduplicated: dict[tuple[str, str], TargetCandidate] = {}
    for candidate in output:
        deduplicated[(candidate.kind, candidate.source_id)] = candidate
    return tuple(deduplicated.values())


def find_intervening_structure(
    book: FeatureBook,
    *,
    side: Side,
    entry_price: float,
    target: TargetCandidate,
    decision_at: object,
    obstacle_timeframes: Sequence[Timeframe] = (
        Timeframe.M15,
        Timeframe.H1,
        Timeframe.H4,
    ),
    excluded_source_ids: frozenset[str] = frozenset(),
) -> TargetCandidate | None:
    """Return the nearest active structure strictly before ``target``.

    A structure at the target within one tick is considered confluence with the
    target, not a separate obstacle. This keeps terminal ownership with the pivot
    while preventing a farther target from being selected through a nearer zone.
    """

    cutoff = _utc(decision_at, name="decision_at")
    if target.symbol != book.symbol or target.trade_side is not side:
        raise ValueError("target and feature book must match the requested trade")
    target_distance = _distance(
        side,
        entry_price=entry_price,
        candidate_price=target.order_price,
    )
    if target_distance < book.tick_size - 1e-12:
        raise ValueError("target must be at least one tick ahead of entry")

    obstacles = _structural_obstacles(
        book,
        side=side,
        entry_price=entry_price,
        decision_at=cutoff,
        obstacle_timeframes=obstacle_timeframes,
        excluded_source_ids=frozenset(
            {*excluded_source_ids, target.source_id}
        ),
    )
    nearer = [
        candidate
        for candidate in obstacles
        if _distance(
            side,
            entry_price=entry_price,
            candidate_price=candidate.order_price,
        )
        <= target_distance - book.tick_size + 1e-12
    ]
    if not nearer:
        return None
    return min(
        nearer,
        key=lambda candidate: (
            _distance(
                side,
                entry_price=entry_price,
                candidate_price=candidate.order_price,
            ),
            0 if candidate.kind == "pivot" else 1 if candidate.kind == "order_block" else 2,
            candidate.known_at,
            candidate.source_id,
        ),
    )


def select_pivot_owned_destination(
    book: FeatureBook,
    *,
    side: Side,
    entry_price: float,
    target_known_by: object,
    decision_at: object,
    target_timeframes: Sequence[Timeframe],
    obstacle_timeframes: Sequence[Timeframe] = (
        Timeframe.M15,
        Timeframe.H1,
        Timeframe.H4,
    ),
    excluded_source_ids: frozenset[str] = frozenset(),
) -> PivotDestinationDecision:
    """Select the nearest admissible pivot and reject path-skipping geometry."""

    known_by = _utc(target_known_by, name="target_known_by")
    cutoff = _utc(decision_at, name="decision_at")
    if known_by > cutoff:
        raise ValueError("target_known_by cannot follow decision_at")
    candidates = _target_pivots(
        book,
        side=side,
        entry_price=float(entry_price),
        target_known_by=known_by,
        decision_at=cutoff,
        target_timeframes=target_timeframes,
        excluded_source_ids=excluded_source_ids,
    )
    if not candidates:
        return PivotDestinationDecision(
            target=None,
            blocker=None,
            reason="no_preexisting_pivot_liquidity",
        )

    target, _timeframe, pivot = min(
        candidates,
        key=lambda item: (
            _distance(
                side,
                entry_price=entry_price,
                candidate_price=item[0].order_price,
            ),
            _timeframe_rank(item[1]),
            -item[2].pivot_time.value,
            item[2].pivot_id,
        ),
    )
    blocker = find_intervening_structure(
        book,
        side=side,
        entry_price=entry_price,
        target=target,
        decision_at=cutoff,
        obstacle_timeframes=obstacle_timeframes,
        excluded_source_ids=excluded_source_ids,
    )
    if blocker is not None:
        return PivotDestinationDecision(
            target=target,
            blocker=blocker,
            reason="intervening_structure",
        )
    return PivotDestinationDecision(target=target, blocker=None, reason=None)


__all__ = [
    "DestinationRejection",
    "PivotDestinationDecision",
    "find_intervening_structure",
    "select_pivot_owned_destination",
]
