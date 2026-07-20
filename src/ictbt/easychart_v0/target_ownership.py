from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Iterable

import pandas as pd

from .domain import PriceZone, Side, StrictPivot, TargetCandidate, Timeframe
from .features import pivot_is_consumed
from .pipeline import FeatureBook, _frame_as_of


class PivotOwnershipReason(str, Enum):
    HTF_EXTERNAL = "htf_external"
    M15_EQUAL_LEVEL_POOL = "m15_equal_level_pool"


@dataclass(frozen=True, slots=True)
class PivotOwnershipPolicy:
    equal_level_tolerance_bps: float = 5.0
    minimum_equal_level_ticks: int = 2
    maximum_age_days: int = 60

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.equal_level_tolerance_bps)
            or self.equal_level_tolerance_bps <= 0
        ):
            raise ValueError("equal_level_tolerance_bps must be finite and positive")
        if self.minimum_equal_level_ticks <= 0:
            raise ValueError("minimum_equal_level_ticks must be positive")
        if self.maximum_age_days <= 0:
            raise ValueError("maximum_age_days must be positive")


@dataclass(frozen=True, slots=True)
class OwnedPivotTarget:
    candidate: TargetCandidate
    reason: PivotOwnershipReason
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.evidence_ids:
            raise ValueError("owned pivot target requires evidence")


def _ahead(side: Side, price: float, reference: float, tick: float) -> bool:
    distance = price - reference if side is Side.LONG else reference - price
    return distance >= tick - 1e-12


def _kind(side: Side) -> str:
    return "high" if side is Side.LONG else "low"


def _active_pivots(
    book: FeatureBook,
    *,
    side: Side,
    as_of: pd.Timestamp,
    preexisting_before: pd.Timestamp,
    entry_reference: float,
    policy: PivotOwnershipPolicy,
    excluded_source_ids: frozenset[str],
) -> tuple[StrictPivot, ...]:
    cutoff = preexisting_before - pd.Timedelta(days=policy.maximum_age_days)
    output: list[StrictPivot] = []
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, as_of)
        for pivot in book.pivots[timeframe]:
            if (
                pivot.symbol != book.symbol
                or pivot.kind != _kind(side)
                or pivot.pivot_id in excluded_source_ids
                or not cutoff <= pivot.known_at <= preexisting_before
                or not _ahead(side, pivot.price, entry_reference, book.tick_size)
                or pivot_is_consumed(pivot, frame, tick_size=book.tick_size)
            ):
                continue
            output.append(pivot)
    return tuple(output)


def _equal_level_evidence(
    pivot: StrictPivot,
    peers: Iterable[StrictPivot],
    *,
    tick_size: float,
    policy: PivotOwnershipPolicy,
) -> tuple[str, ...]:
    tolerance = max(
        tick_size * policy.minimum_equal_level_ticks,
        pivot.price * policy.equal_level_tolerance_bps / 10_000,
    )
    matches = tuple(
        peer.pivot_id
        for peer in peers
        if peer.timeframe is Timeframe.M15
        and peer.pivot_id != pivot.pivot_id
        and abs(peer.price - pivot.price) <= tolerance + 1e-12
    )
    return (pivot.pivot_id, *matches) if matches else ()


def owned_pivot_targets(
    book: FeatureBook,
    *,
    side: Side,
    entry_reference: float,
    as_of: pd.Timestamp,
    preexisting_before: pd.Timestamp,
    policy: PivotOwnershipPolicy = PivotOwnershipPolicy(),
    excluded_source_ids: frozenset[str] = frozenset(),
) -> tuple[OwnedPivotTarget, ...]:
    active = _active_pivots(
        book,
        side=side,
        as_of=as_of,
        preexisting_before=preexisting_before,
        entry_reference=entry_reference,
        policy=policy,
        excluded_source_ids=excluded_source_ids,
    )
    output: list[OwnedPivotTarget] = []
    for pivot in active:
        if pivot.timeframe in {Timeframe.H1, Timeframe.H4}:
            reason = PivotOwnershipReason.HTF_EXTERNAL
            evidence = (pivot.pivot_id,)
        else:
            evidence = _equal_level_evidence(
                pivot,
                active,
                tick_size=book.tick_size,
                policy=policy,
            )
            if not evidence:
                continue
            reason = PivotOwnershipReason.M15_EQUAL_LEVEL_POOL
        candidate = TargetCandidate(
            candidate_id=f"owned-pivot:{pivot.pivot_id}",
            symbol=book.symbol,
            trade_side=side,
            kind="pivot",
            zone=PriceZone(pivot.price, pivot.price),
            known_at=pivot.known_at,
            source_id=pivot.pivot_id,
        )
        output.append(OwnedPivotTarget(candidate, reason, evidence))

    direction = 1.0 if side is Side.LONG else -1.0
    return tuple(
        sorted(
            output,
            key=lambda item: (
                direction * (item.candidate.order_price - entry_reference),
                0 if item.reason is PivotOwnershipReason.HTF_EXTERNAL else 1,
                item.candidate.candidate_id,
            ),
        )
    )


__all__ = [
    "OwnedPivotTarget",
    "PivotOwnershipPolicy",
    "PivotOwnershipReason",
    "owned_pivot_targets",
]
