from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .domain import LiquidityDeliveryAuthority, Side, Timeframe
from .execution import CostConfig
from .execution_economics import cost_inclusive_target_r
from .features import TIMEFRAME_DELTA
from .liquidity_destination import find_intervening_structure
from .pipeline import FeatureBook, _order_block_is_active
from .target_ownership import PivotOwnershipPolicy, owned_pivot_targets
from .v04 import _m5_sweep_episode_is_valid, _target_touched
from .v05 import _candidate_key, _fvg_candidate, _ob_candidate
from .v08_intraday import (
    V08IntradayPolicy,
    _detect_internal_m5_sweeps,
    _displacement_is_material,
    _entry_price,
    _required_notional_to_equity,
)


@dataclass(frozen=True, slots=True)
class V08IntegratedIntradayDiagnostics:
    m15_locations: int
    internal_m5_pivot_pairs: int
    internal_sweep_events: int
    context_rejections: int
    episodes_without_prompt_delivery: int
    weak_displacement_rejections: int
    owned_liquidity_missing: int
    target_used_before_delivery: int
    intervening_structure_rejections: int
    net_target_space_rejections: int
    exposure_rejections: int
    duplicate_scenes_suppressed: int
    authorities: int


@dataclass(frozen=True, slots=True)
class V08IntegratedIntradayResult:
    authorities: tuple[LiquidityDeliveryAuthority, ...]
    diagnostics: V08IntegratedIntradayDiagnostics


def _execution_source_ids(
    *,
    location_id: str,
    event_node_id: str,
    candidate: object,
) -> frozenset[str]:
    output = {
        location_id,
        event_node_id,
        getattr(candidate, "pivot").pivot_id,
    }
    order_block = getattr(candidate, "order_block")
    if order_block is not None:
        output.add(order_block.ob_id)
    gap = getattr(candidate, "fvg")
    if gap is not None:
        output.add(gap.fvg_id)
    return frozenset(output)


def _authority(
    *,
    book: FeatureBook,
    item: object,
    candidate: object,
    target: object,
    target_reason: str,
) -> LiquidityDeliveryAuthority:
    event = getattr(item, "event")
    location = getattr(item, "location")
    return LiquidityDeliveryAuthority(
        authority_id=(
            f"v09-owned-internal-liquidity:{location.ob_id}|{event.event_id}|"
            f"{getattr(candidate, 'delivery_root_id')}|{getattr(candidate, 'kind')}|"
            f"target={getattr(target, 'source_id')}|ownership={target_reason}"
        ),
        symbol=book.symbol,
        side=event.side,
        location_ob=location,
        liquidity_event=event,
        delivery_kind=getattr(candidate, "kind"),
        delivery_root_id=getattr(candidate, "delivery_root_id"),
        displacement_pivot=getattr(candidate, "pivot"),
        delivery_ob=getattr(candidate, "order_block"),
        delivery_fvg=getattr(candidate, "fvg"),
        zone=getattr(candidate, "zone"),
        entry_zone_source=getattr(candidate, "entry_zone_source"),
        known_at=getattr(candidate, "known_at"),
        stop_owner=getattr(candidate, "stop_owner"),
        stop_extreme=getattr(candidate, "stop_extreme"),
        initial_stop=getattr(candidate, "initial_stop"),
        impulse_extreme=getattr(candidate, "impulse_extreme"),
        destination=target,
    )


def build_v08_integrated_intraday_result(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08IntradayPolicy = V08IntradayPolicy(),
    ownership_policy: PivotOwnershipPolicy = PivotOwnershipPolicy(),
) -> V08IntegratedIntradayResult:
    """Build internal-liquidity scenes around independently owned destinations.

    The setup is assembled from the causal event rather than post-filtering the
    legacy V0.8 output.  That prevents an incidental lone M15 pivot from deciding
    the scene before target ownership is examined.  H1/H4 pivots own external
    liquidity directly; an M15 pivot needs an already-confirmed equal-level peer.
    A candidate is rejected when that draw was used before delivery, a nearer
    active structure blocks the path, or the cost-inclusive target room is below
    policy.  Farther targets cannot be selected through a nearer obstacle merely
    to manufacture a larger R multiple.
    """

    sweeps, pair_count, context_rejections = _detect_internal_m5_sweeps(book)
    m5_blocks = book.order_blocks[Timeframe.M5]
    m5_fvgs = book.fvgs[Timeframe.M5]
    raw: list[LiquidityDeliveryAuthority] = []
    no_prompt_delivery = 0
    weak_displacement = 0
    target_missing = 0
    target_used = 0
    intervening = 0
    net_target_space = 0
    exposure_rejections = 0

    for item in sweeps:
        event = item.event
        latest_delivery_time = event.known_at + (
            TIMEFRAME_DELTA[Timeframe.M5] * policy.maximum_delivery_delay_bars
        )
        candidates: list[object] = []
        for block in m5_blocks:
            if block.known_at > latest_delivery_time:
                continue
            candidate = _ob_candidate(
                book,
                event=event,
                location=item.location,
                block=block,
            )
            if candidate is not None:
                candidates.append(candidate)
        for gap in m5_fvgs:
            if gap.known_at > latest_delivery_time:
                continue
            candidate = _fvg_candidate(
                book,
                event=event,
                location=item.location,
                gap=gap,
            )
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            no_prompt_delivery += 1
            continue

        material = sorted(
            (
                candidate
                for candidate in candidates
                if _displacement_is_material(book, candidate, policy=policy)
            ),
            key=_candidate_key,
        )
        if not material:
            weak_displacement += 1
            continue

        accepted: LiquidityDeliveryAuthority | None = None
        saw_valid_delivery = False
        saw_owned_target = False
        saw_unused_target = False
        saw_unblocked_target = False
        saw_economic_target = False

        for candidate in material:
            if not _order_block_is_active(
                book,
                item.location,
                as_of=candidate.known_at,
            ) or not _m5_sweep_episode_is_valid(
                book,
                event,
                until=candidate.known_at,
            ):
                continue
            saw_valid_delivery = True
            entry = _entry_price(event.side, candidate.zone)
            excluded = _execution_source_ids(
                location_id=item.location.ob_id,
                event_node_id=event.node_id,
                candidate=candidate,
            )
            owned_targets = owned_pivot_targets(
                book,
                side=event.side,
                entry_reference=entry,
                as_of=event.known_at,
                preexisting_before=event.event_time,
                policy=ownership_policy,
                excluded_source_ids=excluded,
            )
            if not owned_targets:
                continue
            saw_owned_target = True

            for owned in owned_targets:
                target = owned.candidate
                if _target_touched(
                    book,
                    target,
                    after=event.known_at,
                    through=candidate.known_at,
                ):
                    continue
                saw_unused_target = True
                blocker = find_intervening_structure(
                    book,
                    side=event.side,
                    entry_price=entry,
                    target=target,
                    decision_at=candidate.known_at,
                    obstacle_timeframes=(
                        Timeframe.M15,
                        Timeframe.H1,
                        Timeframe.H4,
                    ),
                    excluded_source_ids=excluded,
                )
                if blocker is not None:
                    continue
                saw_unblocked_target = True
                net_target_r = cost_inclusive_target_r(
                    side=event.side,
                    entry_price=entry,
                    stop_price=candidate.initial_stop,
                    target_price=target.order_price,
                    costs=costs,
                )
                if net_target_r + 1e-12 < policy.minimum_target_r:
                    continue
                saw_economic_target = True
                required_exposure = _required_notional_to_equity(
                    side=event.side,
                    entry=entry,
                    stop=candidate.initial_stop,
                    costs=costs,
                    risk_fraction=policy.risk_fraction,
                )
                if (
                    required_exposure
                    > policy.maximum_notional_to_equity + 1e-12
                ):
                    continue
                accepted = _authority(
                    book=book,
                    item=item,
                    candidate=candidate,
                    target=target,
                    target_reason=owned.reason.value,
                )
                break
            if accepted is not None:
                break

        if accepted is not None:
            raw.append(accepted)
        elif not saw_valid_delivery:
            no_prompt_delivery += 1
        elif not saw_owned_target:
            target_missing += 1
        elif not saw_unused_target:
            target_used += 1
        elif not saw_unblocked_target:
            intervening += 1
        elif not saw_economic_target:
            net_target_space += 1
        else:
            exposure_rejections += 1

    grouped: dict[
        tuple[pd.Timestamp, Side, str],
        list[LiquidityDeliveryAuthority],
    ] = {}
    for authority in raw:
        grouped.setdefault(
            (authority.known_at, authority.side, authority.delivery_root_id),
            [],
        ).append(authority)
    selected = [
        min(
            items,
            key=lambda authority: (
                0
                if authority.entry_zone_source
                in {"m15_m5_intersection", "ob_fvg_intersection"}
                else 1,
                authority.zone.width,
                -authority.liquidity_event.known_at.value,
                -authority.location_ob.known_at.value,
                authority.authority_id,
            ),
        )
        for items in grouped.values()
    ]
    authorities = tuple(
        sorted(selected, key=lambda item: (item.known_at, item.authority_id))
    )
    return V08IntegratedIntradayResult(
        authorities=authorities,
        diagnostics=V08IntegratedIntradayDiagnostics(
            m15_locations=len(book.order_blocks[Timeframe.M15]),
            internal_m5_pivot_pairs=pair_count,
            internal_sweep_events=len(sweeps),
            context_rejections=context_rejections,
            episodes_without_prompt_delivery=no_prompt_delivery,
            weak_displacement_rejections=weak_displacement,
            owned_liquidity_missing=target_missing,
            target_used_before_delivery=target_used,
            intervening_structure_rejections=intervening,
            net_target_space_rejections=net_target_space,
            exposure_rejections=exposure_rejections,
            duplicate_scenes_suppressed=len(raw) - len(authorities),
            authorities=len(authorities),
        ),
    )


__all__ = [
    "V08IntegratedIntradayDiagnostics",
    "V08IntegratedIntradayResult",
    "build_v08_integrated_intraday_result",
]
