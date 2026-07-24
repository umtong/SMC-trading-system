from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ictbt.easychart_v0.domain import B1Subtype, SceneFamily, Side, TargetCandidate

from .liquidity_delivery import FlowSceneKind, FrozenFlowScene


class _Authority(Protocol):
    authority_id: str
    symbol: str
    side: Side
    scene_family: SceneFamily
    known_at: object
    initial_stop: float
    destination: TargetCandidate | None


@dataclass(frozen=True, slots=True)
class AdaptedFlowScene:
    scene: FrozenFlowScene
    source_authority_id: str
    source_scene_family: SceneFamily
    source_target_id: str


def _family(authority: object) -> SceneFamily:
    value = getattr(authority, "scene_family", None)
    return value if isinstance(value, SceneFamily) else SceneFamily(value)


def _target(
    authority: object,
    *,
    destination: TargetCandidate | None,
) -> TargetCandidate:
    selected = destination or getattr(authority, "destination", None)
    if not isinstance(selected, TargetCandidate):
        raise ValueError(
            "a frozen TargetCandidate is required before microstructure adaptation"
        )
    return selected


def adapt_authority_to_flow_scene(
    authority: _Authority | object,
    *,
    tick_size: float,
    destination: TargetCandidate | None = None,
) -> AdaptedFlowScene:
    """Convert supported EasyChart authorities without changing their clocks.

    The adapter never selects a new target. V0.3 authorities whose target is
    dynamic must first be assembled by the existing causal target selector and
    pass that frozen ``TargetCandidate`` explicitly.
    """

    family = _family(authority)
    side = getattr(authority, "side")
    if not isinstance(side, Side):
        side = Side(side)
    selected_target = _target(authority, destination=destination)

    if family is SceneFamily.A1_B1_CONFLUENCE:
        confirmation = getattr(authority, "confirmation")
        node_price = float(getattr(confirmation, "liquidity_node_price"))
        subtype = getattr(confirmation, "subtype")
        subtype = subtype if isinstance(subtype, B1Subtype) else B1Subtype(subtype)
        kind = (
            FlowSceneKind.SWEEP_RECLAIM
            if subtype is B1Subtype.SWEEP_RECLAIM
            else FlowSceneKind.BREAK_ACCEPTANCE
        )
    elif family is SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST:
        event = getattr(authority, "liquidity_event")
        node_price = float(getattr(event, "node_price"))
        kind = FlowSceneKind.SWEEP_RECLAIM
    elif family is SceneFamily.SR_FLIP_FVG:
        boundary = getattr(authority, "boundary_pivot")
        node_price = float(getattr(boundary, "price"))
        kind = FlowSceneKind.BREAK_ACCEPTANCE
    else:
        raise ValueError(
            f"scene family {family.value} is not registered for V0.9 flow confirmation"
        )

    authority_id = str(getattr(authority, "authority_id"))
    scene = FrozenFlowScene(
        scene_id=authority_id,
        symbol=str(getattr(authority, "symbol")),
        side=side,
        kind=kind,
        node_price=node_price,
        known_at=getattr(authority, "known_at"),
        initial_stop=float(getattr(authority, "initial_stop")),
        initial_target=float(selected_target.order_price),
        tick_size=float(tick_size),
    )
    return AdaptedFlowScene(
        scene=scene,
        source_authority_id=authority_id,
        source_scene_family=family,
        source_target_id=selected_target.source_id,
    )


__all__ = ["AdaptedFlowScene", "adapt_authority_to_flow_scene"]
