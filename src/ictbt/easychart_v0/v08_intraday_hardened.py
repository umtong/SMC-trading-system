from __future__ import annotations

from dataclasses import dataclass

from .domain import LiquidityDeliveryAuthority, Side, Timeframe
from .execution import CostConfig
from .execution_economics import cost_inclusive_target_r
from .liquidity_destination import find_intervening_structure
from .pipeline import FeatureBook
from .v08_intraday import (
    V08IntradayBuildDiagnostics,
    V08IntradayPolicy,
    build_v08_intraday_liquidity_delivery_result,
)


@dataclass(frozen=True, slots=True)
class V08IntradayHardenedBuildDiagnostics:
    m15_locations: int
    internal_m5_pivot_pairs: int
    internal_sweep_events: int
    context_rejections: int
    episodes_without_prompt_delivery: int
    weak_displacement_rejections: int
    external_liquidity_missing: int
    target_used_before_delivery: int
    target_space_rejections: int
    exposure_rejections: int
    duplicate_scenes_suppressed: int
    base_authorities: int
    intervening_structure_rejections: int
    net_target_space_rejections: int
    authorities: int


@dataclass(frozen=True, slots=True)
class V08IntradayHardenedBuildResult:
    authorities: tuple[LiquidityDeliveryAuthority, ...]
    diagnostics: V08IntradayHardenedBuildDiagnostics


def _entry_price(authority: LiquidityDeliveryAuthority) -> float:
    return authority.zone.high if authority.side is Side.LONG else authority.zone.low


def _execution_source_ids(
    authority: LiquidityDeliveryAuthority,
) -> frozenset[str]:
    output = {
        authority.destination.source_id,
        authority.location_ob.ob_id,
        authority.displacement_pivot.pivot_id,
    }
    if authority.delivery_ob is not None:
        output.add(authority.delivery_ob.ob_id)
    if authority.delivery_fvg is not None:
        output.add(authority.delivery_fvg.fvg_id)
    return frozenset(output)


def _flatten_diagnostics(
    base: V08IntradayBuildDiagnostics,
    *,
    base_authorities: int,
    intervening_structure_rejections: int,
    net_target_space_rejections: int,
    authorities: int,
) -> V08IntradayHardenedBuildDiagnostics:
    return V08IntradayHardenedBuildDiagnostics(
        m15_locations=base.m15_locations,
        internal_m5_pivot_pairs=base.internal_m5_pivot_pairs,
        internal_sweep_events=base.internal_sweep_events,
        context_rejections=base.context_rejections,
        episodes_without_prompt_delivery=base.episodes_without_prompt_delivery,
        weak_displacement_rejections=base.weak_displacement_rejections,
        external_liquidity_missing=base.external_liquidity_missing,
        target_used_before_delivery=base.target_used_before_delivery,
        target_space_rejections=base.target_space_rejections,
        exposure_rejections=base.exposure_rejections,
        duplicate_scenes_suppressed=base.duplicate_scenes_suppressed,
        base_authorities=base_authorities,
        intervening_structure_rejections=intervening_structure_rejections,
        net_target_space_rejections=net_target_space_rejections,
        authorities=authorities,
    )


def build_v08_intraday_hardened_result(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08IntradayPolicy = V08IntradayPolicy(),
) -> V08IntradayHardenedBuildResult:
    """Validate first-obstacle ownership and cost-inclusive target room."""

    base = build_v08_intraday_liquidity_delivery_result(
        book,
        costs=costs,
        policy=policy,
    )
    output: list[LiquidityDeliveryAuthority] = []
    blocked = 0
    insufficient_net_room = 0
    for authority in base.authorities:
        blocker = find_intervening_structure(
            book,
            side=authority.side,
            entry_price=_entry_price(authority),
            target=authority.destination,
            decision_at=authority.known_at,
            obstacle_timeframes=(Timeframe.M15, Timeframe.H1, Timeframe.H4),
            excluded_source_ids=_execution_source_ids(authority),
        )
        if blocker is not None:
            blocked += 1
            continue

        net_target_r = cost_inclusive_target_r(
            side=authority.side,
            entry_price=_entry_price(authority),
            stop_price=authority.initial_stop,
            target_price=authority.destination.order_price,
            costs=costs,
        )
        if net_target_r + 1e-12 < policy.minimum_target_r:
            insufficient_net_room += 1
            continue
        output.append(authority)

    authorities = tuple(output)
    return V08IntradayHardenedBuildResult(
        authorities=authorities,
        diagnostics=_flatten_diagnostics(
            base.diagnostics,
            base_authorities=len(base.authorities),
            intervening_structure_rejections=blocked,
            net_target_space_rejections=insufficient_net_room,
            authorities=len(authorities),
        ),
    )


def build_v08_intraday_hardened_authorities(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08IntradayPolicy = V08IntradayPolicy(),
) -> tuple[LiquidityDeliveryAuthority, ...]:
    return build_v08_intraday_hardened_result(
        book,
        costs=costs,
        policy=policy,
    ).authorities


__all__ = [
    "V08IntradayHardenedBuildDiagnostics",
    "V08IntradayHardenedBuildResult",
    "build_v08_intraday_hardened_authorities",
    "build_v08_intraday_hardened_result",
]
