from __future__ import annotations

from dataclasses import dataclass, replace

from .domain import LiquidityDeliveryAuthority, Side
from .execution import CostConfig
from .pipeline import FeatureBook
from .target_ownership import PivotOwnershipPolicy, owned_pivot_targets
from .v04 import _target_touched
from .v08_intraday import (
    V08IntradayBuildDiagnostics,
    V08IntradayPolicy,
    build_v08_intraday_liquidity_delivery_result,
)


@dataclass(frozen=True, slots=True)
class V08IntegratedIntradayDiagnostics:
    base: V08IntradayBuildDiagnostics
    base_authorities: int
    retained_owned_target: int
    retargeted_to_owned_liquidity: int
    rejected_without_owned_target: int
    rejected_target_used_before_delivery: int
    rejected_target_space: int
    authorities: int


@dataclass(frozen=True, slots=True)
class V08IntegratedIntradayResult:
    authorities: tuple[LiquidityDeliveryAuthority, ...]
    diagnostics: V08IntegratedIntradayDiagnostics


def _entry_price(authority: LiquidityDeliveryAuthority) -> float:
    return authority.zone.high if authority.side is Side.LONG else authority.zone.low


def _target_r(
    authority: LiquidityDeliveryAuthority,
    *,
    target_price: float,
) -> float:
    entry = _entry_price(authority)
    risk = abs(entry - authority.initial_stop)
    if risk <= 0:
        return 0.0
    direction = 1.0 if authority.side is Side.LONG else -1.0
    return direction * (target_price - entry) / risk


def build_v08_integrated_intraday_result(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08IntradayPolicy = V08IntradayPolicy(),
    ownership_policy: PivotOwnershipPolicy = PivotOwnershipPolicy(),
) -> V08IntegratedIntradayResult:
    """Retarget the intraday family only to independently owned liquidity.

    H1/H4 pivots qualify as external liquidity. A lone M15 pivot does not; M15
    is retained only when another active pre-event pivot forms an equal-level
    pool. The first still-unused candidate with enough geometry is selected.
    """

    base = build_v08_intraday_liquidity_delivery_result(
        book,
        costs=costs,
        policy=policy,
    )
    output: list[LiquidityDeliveryAuthority] = []
    retained = 0
    retargeted = 0
    no_target = 0
    target_used = 0
    target_space = 0

    for authority in base.authorities:
        entry = _entry_price(authority)
        candidates = owned_pivot_targets(
            book,
            side=authority.side,
            entry_reference=entry,
            as_of=authority.known_at,
            preexisting_before=authority.liquidity_event.event_time,
            excluded_source_ids=frozenset(
                {
                    authority.liquidity_event.node_id,
                    authority.displacement_pivot.pivot_id,
                }
            ),
            policy=ownership_policy,
        )
        selected = None
        saw_used = False
        saw_small = False
        for owned in candidates:
            target = owned.candidate
            if _target_touched(
                book,
                target,
                after=authority.liquidity_event.known_at,
                through=authority.known_at,
            ):
                saw_used = True
                continue
            if (
                _target_r(authority, target_price=target.order_price) + 1e-12
                < policy.minimum_target_r
            ):
                saw_small = True
                continue
            selected = target
            break
        if selected is None:
            if saw_used:
                target_used += 1
            elif saw_small:
                target_space += 1
            else:
                no_target += 1
            continue

        if selected.source_id == authority.destination.source_id:
            retained += 1
            output.append(authority)
        else:
            retargeted += 1
            output.append(
                replace(
                    authority,
                    authority_id=(
                        f"{authority.authority_id}|owned-target={selected.source_id}"
                    ),
                    destination=selected,
                )
            )

    authorities = tuple(
        sorted(output, key=lambda item: (item.known_at, item.authority_id))
    )
    return V08IntegratedIntradayResult(
        authorities=authorities,
        diagnostics=V08IntegratedIntradayDiagnostics(
            base=base.diagnostics,
            base_authorities=len(base.authorities),
            retained_owned_target=retained,
            retargeted_to_owned_liquidity=retargeted,
            rejected_without_owned_target=no_target,
            rejected_target_used_before_delivery=target_used,
            rejected_target_space=target_space,
            authorities=len(authorities),
        ),
    )


__all__ = [
    "V08IntegratedIntradayDiagnostics",
    "V08IntegratedIntradayResult",
    "build_v08_integrated_intraday_result",
]
