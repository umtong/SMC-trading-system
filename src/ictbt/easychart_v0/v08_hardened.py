from __future__ import annotations

from dataclasses import dataclass, replace
from typing import NamedTuple

from .domain import Side, Timeframe
from .execution import CostConfig
from .execution_economics import cost_inclusive_target_r
from .liquidity_destination import select_pivot_owned_destination
from .pipeline import FeatureBook, Opportunity, OpportunityRejection
from .v07 import SrFlipFvgAuthority, build_v07_scene_family_result
from .v08 import (
    V08ContextMode,
    V08Policy,
    _context_mode,
    _displacement_is_material,
    _required_notional_to_equity,
    assemble_v08_opportunity,
)
from .v08_boundary_candidates import build_v08_boundary_candidates


@dataclass(frozen=True, slots=True)
class V08HardenedBuildDiagnostics:
    legacy_v07_scenes: int
    boundary_candidates: int
    boundary_candidate_scene_roots: int
    htf_context_rejections: int
    displacement_rejections: int
    external_liquidity_missing: int
    intervening_structure_rejections: int
    net_target_space_rejections: int
    exposure_rejections: int
    duplicate_scenes_suppressed: int
    trend_continuation_authorities: int
    h1_range_expansion_authorities: int
    authorities: int


@dataclass(frozen=True, slots=True)
class V08HardenedBuildResult:
    authorities: tuple[SrFlipFvgAuthority, ...]
    diagnostics: V08HardenedBuildDiagnostics


class _Qualified(NamedTuple):
    authority: SrFlipFvgAuthority
    mode: V08ContextMode
    net_target_r: float


def _entry_price(authority: SrFlipFvgAuthority) -> float:
    return authority.zone.high if authority.side is Side.LONG else authority.zone.low


def _scene_priority(item: _Qualified) -> tuple[object, ...]:
    """Resolve one authority only after every linked boundary has qualified."""

    authority = item.authority
    return (
        0 if authority.boundary_pivot.timeframe is Timeframe.H1 else 1,
        -item.net_target_r,
        -authority.boundary_pivot.pivot_time.value,
        authority.authority_id,
    )


def _net_target_r(
    authority: SrFlipFvgAuthority,
    *,
    costs: CostConfig,
) -> float:
    return cost_inclusive_target_r(
        side=authority.side,
        entry_price=_entry_price(authority),
        stop_price=authority.initial_stop,
        target_price=authority.destination.order_price,
        costs=costs,
    )


def build_v08_hardened_scene_family_result(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08Policy = V08Policy(),
) -> V08HardenedBuildResult:
    """Build V0.8 without legacy boundary preselection or path-skipping targets.

    ``policy.minimum_target_r`` is interpreted here as cost-inclusive net R,
    measured against the adverse stop fill plus entry/stop fees.
    """

    legacy = build_v07_scene_family_result(book)
    candidates = build_v08_boundary_candidates(book)
    qualified: list[_Qualified] = []
    htf_rejections = 0
    displacement_rejections = 0
    target_missing = 0
    intervening_rejections = 0
    net_target_space_rejections = 0
    exposure_rejections = 0

    for authority in candidates:
        mode = _context_mode(book, authority, policy=policy)
        if mode is None:
            htf_rejections += 1
            continue
        if not _displacement_is_material(book, authority, policy=policy):
            displacement_rejections += 1
            continue

        destination = select_pivot_owned_destination(
            book,
            side=authority.side,
            entry_price=_entry_price(authority),
            target_known_by=authority.break_bar.open_time,
            decision_at=authority.known_at,
            target_timeframes=(Timeframe.H1, Timeframe.H4),
            obstacle_timeframes=(Timeframe.M15, Timeframe.H1, Timeframe.H4),
            excluded_source_ids=frozenset(
                {
                    authority.boundary_pivot.pivot_id,
                    authority.fvg.fvg_id,
                }
            ),
        )
        if destination.reason == "no_preexisting_pivot_liquidity":
            target_missing += 1
            continue
        if destination.reason == "intervening_structure":
            intervening_rejections += 1
            continue
        target = destination.target
        assert target is not None

        retargeted = replace(authority, destination=target)
        net_target_r = _net_target_r(retargeted, costs=costs)
        if net_target_r + 1e-12 < policy.minimum_target_r:
            net_target_space_rejections += 1
            continue
        required_exposure = _required_notional_to_equity(
            retargeted,
            costs=costs,
            risk_fraction=policy.risk_fraction,
        )
        if required_exposure > policy.maximum_notional_to_equity + 1e-12:
            exposure_rejections += 1
            continue

        qualified.append(
            _Qualified(
                replace(
                    retargeted,
                    authority_id=(
                        f"v08-hardened-htf-liquidity-delivery:"
                        f"{authority.authority_id}|target={target.source_id}"
                    ),
                    scene_root_id=f"v08-hardened:{authority.scene_root_id}",
                ),
                mode,
                net_target_r,
            )
        )

    grouped: dict[str, list[_Qualified]] = {}
    for item in qualified:
        grouped.setdefault(item.authority.scene_root_id, []).append(item)
    selected = [min(items, key=_scene_priority) for items in grouped.values()]
    selected.sort(
        key=lambda item: (item.authority.known_at, item.authority.authority_id)
    )
    authorities = tuple(item.authority for item in selected)
    trend_count = sum(item.mode == "trend_continuation" for item in selected)
    range_count = sum(item.mode == "h1_range_expansion" for item in selected)

    return V08HardenedBuildResult(
        authorities=authorities,
        diagnostics=V08HardenedBuildDiagnostics(
            legacy_v07_scenes=len(legacy.authorities),
            boundary_candidates=len(candidates),
            boundary_candidate_scene_roots=len(
                {authority.scene_root_id for authority in candidates}
            ),
            htf_context_rejections=htf_rejections,
            displacement_rejections=displacement_rejections,
            external_liquidity_missing=target_missing,
            intervening_structure_rejections=intervening_rejections,
            net_target_space_rejections=net_target_space_rejections,
            exposure_rejections=exposure_rejections,
            duplicate_scenes_suppressed=len(qualified) - len(authorities),
            trend_continuation_authorities=trend_count,
            h1_range_expansion_authorities=range_count,
            authorities=len(authorities),
        ),
    )


def build_v08_hardened_scene_family_authorities(
    book: FeatureBook,
    *,
    costs: CostConfig,
    policy: V08Policy = V08Policy(),
) -> tuple[SrFlipFvgAuthority, ...]:
    return build_v08_hardened_scene_family_result(
        book,
        costs=costs,
        policy=policy,
    ).authorities


def assemble_v08_hardened_opportunity(
    book: FeatureBook,
    authority: SrFlipFvgAuthority,
    *,
    costs: CostConfig,
) -> Opportunity | OpportunityRejection:
    return assemble_v08_opportunity(book, authority, costs=costs)


__all__ = [
    "V08HardenedBuildDiagnostics",
    "V08HardenedBuildResult",
    "assemble_v08_hardened_opportunity",
    "build_v08_hardened_scene_family_authorities",
    "build_v08_hardened_scene_family_result",
]
