from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from ictbt.easychart_v0.domain import (
    B1Subtype,
    SceneFamily,
    Side,
    TargetCandidate,
)

from .dual_clock import DualClockSceneKind, FrozenDualClockScene


class _Authority(Protocol):
    authority_id: str
    symbol: str
    side: Side
    scene_family: SceneFamily
    known_at: object
    initial_stop: float
    destination: TargetCandidate | None


@dataclass(frozen=True, slots=True)
class AdaptedDualClockScene:
    scene: FrozenDualClockScene
    source_authority_id: str
    source_scene_family: SceneFamily
    source_target_id: str
    source_event_id: str
    source_confirmation_id: str


def _family(authority: object) -> SceneFamily:
    value = getattr(authority, "scene_family", None)
    return value if isinstance(value, SceneFamily) else SceneFamily(value)


def _side(authority: object) -> Side:
    value = getattr(authority, "side")
    return value if isinstance(value, Side) else Side(value)


def _target(
    authority: object,
    *,
    destination: TargetCandidate | None,
) -> TargetCandidate:
    selected = destination or getattr(authority, "destination", None)
    if not isinstance(selected, TargetCandidate):
        raise ValueError(
            "a frozen TargetCandidate is required before dual-clock adaptation"
        )
    return selected


def _event_from_book(book: object, confirmation: object) -> object:
    event_id = str(getattr(confirmation, "liquidity_event_id"))
    timeframe = getattr(confirmation, "liquidity_event_timeframe")
    events = getattr(book, "liquidity_events")[timeframe]
    matches = [item for item in events if str(getattr(item, "event_id")) == event_id]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one causal liquidity event {event_id!r}, got {len(matches)}"
        )
    return matches[0]


def _kind(subtype: object) -> DualClockSceneKind:
    selected = subtype if isinstance(subtype, B1Subtype) else B1Subtype(subtype)
    return (
        DualClockSceneKind.SWEEP_REVERSAL
        if selected is B1Subtype.SWEEP_RECLAIM
        else DualClockSceneKind.BREAK_CONTINUATION
    )


def _bar_interval(owner: object) -> tuple[pd.Timestamp, pd.Timestamp]:
    bars = tuple(getattr(owner, "formation_bars"))
    if not bars:
        raise ValueError("confirmation owner requires formation bars")
    return (
        min(pd.Timestamp(getattr(bar, "open_time")) for bar in bars),
        max(pd.Timestamp(getattr(bar, "close_time")) for bar in bars),
    )


def adapt_authority_to_dual_clock_scene(
    authority: _Authority | object,
    *,
    tick_size: float,
    book: object | None = None,
    destination: TargetCandidate | None = None,
) -> AdaptedDualClockScene:
    """Freeze separate liquidity-event and delivery-confirmation clocks.

    No target is selected here. The authority must already own a causal target,
    or the caller must supply the target frozen by the existing point-in-time
    selector. Event and confirmation windows are derived only from source bars
    that are already part of the authority or its FeatureBook liquidity event.
    """

    family = _family(authority)
    side = _side(authority)
    selected_target = _target(authority, destination=destination)

    if family is SceneFamily.A1_B1_CONFLUENCE:
        if book is None:
            raise ValueError("A1/B1 dual-clock adaptation requires its FeatureBook")
        confirmation = getattr(authority, "confirmation")
        event = _event_from_book(book, confirmation)
        owner = tuple(getattr(confirmation, "order_blocks"))[0]
        owner_start, _owner_end = _bar_interval(owner)
        event_start = pd.Timestamp(getattr(event, "event_time"))
        event_known = pd.Timestamp(getattr(event, "known_at"))
        confirmation_start = max(event_known, owner_start)
        confirmation_known = pd.Timestamp(getattr(confirmation, "known_at"))
        node_price = float(getattr(event, "node_price"))
        kind = _kind(getattr(event, "subtype"))
        event_id = str(getattr(event, "event_id"))
        confirmation_id = str(getattr(confirmation, "authority_id"))
    elif family is SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST:
        event = getattr(authority, "liquidity_event")
        owners = tuple(
            item
            for item in (
                getattr(authority, "delivery_ob", None),
                getattr(authority, "delivery_fvg", None),
            )
            if item is not None
        )
        if not owners:
            raise ValueError("liquidity delivery requires an OB or FVG owner")
        owner_starts = [_bar_interval(owner)[0] for owner in owners]
        event_start = pd.Timestamp(getattr(event, "event_time"))
        event_known = pd.Timestamp(getattr(event, "known_at"))
        confirmation_start = max(event_known, min(owner_starts))
        confirmation_known = pd.Timestamp(getattr(authority, "known_at"))
        node_price = float(getattr(event, "node_price"))
        kind = _kind(getattr(event, "subtype"))
        event_id = str(getattr(event, "event_id"))
        confirmation_id = str(getattr(authority, "delivery_root_id"))
    elif family is SceneFamily.SR_FLIP_FVG:
        break_bar = getattr(authority, "break_bar")
        acceptance_bar = getattr(authority, "acceptance_bar")
        boundary = getattr(authority, "boundary_pivot")
        event_start = pd.Timestamp(getattr(break_bar, "open_time"))
        event_known = pd.Timestamp(getattr(break_bar, "close_time"))
        confirmation_start = pd.Timestamp(getattr(acceptance_bar, "open_time"))
        confirmation_known = pd.Timestamp(getattr(acceptance_bar, "close_time"))
        node_price = float(getattr(boundary, "price"))
        kind = DualClockSceneKind.BREAK_CONTINUATION
        event_id = str(getattr(authority, "liquidity_event").event_id)
        confirmation_id = str(getattr(authority, "fvg").fvg_id)
    else:
        raise ValueError(
            f"scene family {family.value} is not registered for V0.9.1"
        )

    authority_id = str(getattr(authority, "authority_id"))
    scene = FrozenDualClockScene(
        scene_id=authority_id,
        symbol=str(getattr(authority, "symbol")),
        side=side,
        kind=kind,
        node_price=node_price,
        event_started_at=event_start,
        event_known_at=event_known,
        confirmation_started_at=confirmation_start,
        confirmation_known_at=confirmation_known,
        initial_stop=float(getattr(authority, "initial_stop")),
        initial_target=float(selected_target.order_price),
        tick_size=float(tick_size),
    )
    return AdaptedDualClockScene(
        scene=scene,
        source_authority_id=authority_id,
        source_scene_family=family,
        source_target_id=selected_target.source_id,
        source_event_id=event_id,
        source_confirmation_id=confirmation_id,
    )


__all__ = [
    "AdaptedDualClockScene",
    "adapt_authority_to_dual_clock_scene",
]
