from __future__ import annotations

import math
from dataclasses import replace

import pandas as pd
import pytest

from ictbt.easychart_smc_v1 import (
    AuthorityRuntime,
    AuthorityStatus,
    DeliveryEvidence,
    EntryArray,
    EventKind,
    ExecutionIntent,
    ExecutionModel,
    HigherTimeframeContext,
    LiquidityEventEvidence,
    LiquidityObjective,
    ManagementPlan,
    Narrative,
    PolicyConfig,
    PriceZone,
    Side,
    TargetKind,
    choose_global_trade,
    evaluate_intent,
    size_exact_risk,
)

UTC = "UTC"
T0 = pd.Timestamp("2026-01-05 00:00", tz=UTC)


def target(
    *,
    side: Side = Side.LONG,
    price: float = 101.0,
    kind: TargetKind = TargetKind.CONFIRMED_PIVOT,
    external: bool = False,
    paired: str | None = None,
) -> LiquidityObjective:
    return LiquidityObjective(
        objective_id=f"target:{kind.value}:{price}",
        symbol="BTCUSDT",
        side=side,
        kind=kind,
        zone=PriceZone(price, price),
        known_at=T0,
        external=external,
        paired_liquidity_id=paired,
    )


def reversal_intent(
    *,
    intent_id: str = "rev",
    target_items: tuple[LiquidityObjective, ...] | None = None,
    displacement: float = 0.8,
    broke_swing: bool = True,
    reclaimed: bool = True,
    external_event: bool = True,
    return_number: int = 1,
    stop: float = 97.8,
) -> ExecutionIntent:
    side = Side.LONG
    draw = target(side=side, price=101.0, kind=TargetKind.EXTERNAL_LIQUIDITY, external=True)
    context = HigherTimeframeContext(
        context_id="ctx",
        symbol="BTCUSDT",
        side=side,
        known_at=T0,
        draw_on_liquidity=draw,
        location_valid=True,
        h1_aligned=False,
        h4_direct_conflict=False,
        at_external_liquidity=True,
    )
    event = LiquidityEventEvidence(
        event_id="sweep",
        symbol="BTCUSDT",
        side=side,
        kind=EventKind.SWEEP_RECLAIM,
        known_at=T0 + pd.Timedelta(minutes=15),
        level=98.5,
        extreme=98.0,
        external_liquidity=external_event,
        reclaimed=reclaimed,
    )
    delivery = DeliveryEvidence(
        delivery_id="delivery",
        symbol="BTCUSDT",
        side=side,
        known_at=T0 + pd.Timedelta(minutes=20),
        source_event_id=event.event_id,
        entry_array=EntryArray.OB_FVG_OVERLAP,
        zone=PriceZone(99.8, 100.2),
        displacement_body_atr=displacement,
        broke_preexisting_swing=broke_swing,
        fresh=True,
        return_number=return_number,
        invalidation_extreme=event.extreme,
    )
    return ExecutionIntent(
        intent_id=intent_id,
        symbol="BTCUSDT",
        side=side,
        model=ExecutionModel.SWEEP_MSS_RETURN,
        narrative=Narrative.REVERSAL,
        known_at=delivery.known_at,
        entry_time=delivery.known_at + pd.Timedelta(minutes=10),
        entry=100.0,
        stop=stop,
        context=context,
        event=event,
        delivery=delivery,
        targets=target_items or (draw,),
    )


def continuation_intent(
    *,
    intent_id: str = "cont",
    entry_time_offset: int = 10,
    h1_aligned: bool = True,
    h4_conflict: bool = False,
    opposing_sweep: bool = False,
    accepted: bool = True,
) -> ExecutionIntent:
    side = Side.LONG
    draw = target(side=side, price=102.0, kind=TargetKind.EXTERNAL_LIQUIDITY, external=True)
    context = HigherTimeframeContext(
        context_id=f"ctx:{intent_id}",
        symbol="BTCUSDT",
        side=side,
        known_at=T0,
        draw_on_liquidity=draw,
        location_valid=True,
        h1_aligned=h1_aligned,
        h4_direct_conflict=h4_conflict,
        at_external_liquidity=False,
    )
    event = LiquidityEventEvidence(
        event_id=f"break:{intent_id}",
        symbol="BTCUSDT",
        side=side,
        kind=EventKind.BREAK_ACCEPTANCE,
        known_at=T0 + pd.Timedelta(minutes=15),
        level=99.5,
        extreme=99.0,
        external_liquidity=False,
        boundary_broken_by_close=True,
        accepted_after_break=accepted,
        opposing_sweep_after_event=opposing_sweep,
    )
    delivery = DeliveryEvidence(
        delivery_id=f"delivery:{intent_id}",
        symbol="BTCUSDT",
        side=side,
        known_at=T0 + pd.Timedelta(minutes=20),
        source_event_id=event.event_id,
        entry_array=EntryArray.FAIR_VALUE_GAP,
        zone=PriceZone(99.8, 100.2),
        displacement_body_atr=0.9,
        broke_preexisting_swing=True,
        fresh=True,
        return_number=1,
        invalidation_extreme=99.0,
    )
    return ExecutionIntent(
        intent_id=intent_id,
        symbol="BTCUSDT",
        side=side,
        model=ExecutionModel.BREAK_ACCEPT_RETEST,
        narrative=Narrative.CONTINUATION,
        known_at=delivery.known_at,
        entry_time=delivery.known_at + pd.Timedelta(minutes=entry_time_offset),
        entry=100.0,
        stop=98.9,
        context=context,
        event=event,
        delivery=delivery,
        targets=(draw,),
    )


def test_complete_reversal_chain_is_approved() -> None:
    decision = evaluate_intent(reversal_intent(), equity=10_000.0)
    assert decision.approved
    assert decision.trade is not None
    assert decision.trade.target.kind is TargetKind.EXTERNAL_LIQUIDITY
    assert decision.trade.sizing is not None
    assert decision.trade.management is ManagementPlan.FULL_AT_FIRST_OBSTACLE


def test_ob_or_fvg_without_owned_structure_delivery_is_rejected() -> None:
    decision = evaluate_intent(reversal_intent(broke_swing=False))
    assert not decision.approved
    assert "no_meaningful_structure_break" in decision.reasons


def test_sweep_without_reclaim_is_rejected() -> None:
    decision = evaluate_intent(reversal_intent(reclaimed=False))
    assert not decision.approved
    assert "swept_liquidity_not_reclaimed" in decision.reasons


def test_reversal_requires_external_liquidity_and_first_clean_return() -> None:
    decision = evaluate_intent(
        reversal_intent(external_event=False, return_number=2)
    )
    assert not decision.approved
    assert "reversal_not_at_external_liquidity" in decision.reasons
    assert "reversal_requires_first_clean_return" in decision.reasons


def test_fvg_alone_cannot_be_terminal_target() -> None:
    fvg = target(
        price=101.0,
        kind=TargetKind.FAIR_VALUE_GAP,
        external=False,
        paired=None,
    )
    decision = evaluate_intent(reversal_intent(target_items=(fvg,)))
    assert not decision.approved
    assert "no_preexisting_structural_target" in decision.reasons


def test_nearest_obstacle_cannot_be_skipped_for_a_farther_target() -> None:
    too_close = target(price=100.2, kind=TargetKind.CONFIRMED_PIVOT)
    far = target(price=103.0, kind=TargetKind.EXTERNAL_LIQUIDITY, external=True)
    decision = evaluate_intent(reversal_intent(target_items=(far, too_close)))
    assert not decision.approved
    assert "nearest_obstacle_does_not_pay_minimum_net_r" in decision.reasons


def test_break_acceptance_requires_htf_alignment_and_no_opposing_sweep() -> None:
    decision = evaluate_intent(
        continuation_intent(
            h1_aligned=False,
            h4_conflict=True,
            opposing_sweep=True,
            accepted=False,
        )
    )
    assert not decision.approved
    assert "break_not_accepted" in decision.reasons
    assert "continuation_not_h1_aligned" in decision.reasons
    assert "continuation_h4_direct_conflict" in decision.reasons
    assert "opposing_liquidity_event_supersedes_continuation" in decision.reasons


def test_exact_risk_sizing_includes_costs_and_loses_three_percent_at_stop() -> None:
    intent = reversal_intent()
    config = PolicyConfig()
    sizing = size_exact_risk(intent, equity=10_000.0, config=config)
    assert math.isclose(sizing.risk_budget, 300.0)
    assert math.isclose(
        sizing.quantity * sizing.all_in_stop_loss_per_unit,
        300.0,
        rel_tol=1e-12,
    )


def test_trade_is_rejected_when_exact_risk_requires_excess_leverage() -> None:
    intent = continuation_intent()
    tight_delivery = replace(
        intent.delivery,
        zone=PriceZone(99.95, 100.05),
        invalidation_extreme=99.91,
    )
    tight = replace(intent, stop=99.90, delivery=tight_delivery)
    decision = evaluate_intent(
        tight,
        equity=10_000.0,
        config=PolicyConfig(maximum_required_leverage=10.0),
    )
    assert not decision.approved
    assert "exact_three_percent_risk_requires_unsafe_leverage" in decision.reasons


def test_global_router_refuses_opposing_same_symbol_narratives() -> None:
    long_intent = continuation_intent(intent_id="long")
    short_draw = target(
        side=Side.SHORT,
        price=98.0,
        kind=TargetKind.EXTERNAL_LIQUIDITY,
        external=True,
    )
    short_context = HigherTimeframeContext(
        context_id="short-ctx",
        symbol="BTCUSDT",
        side=Side.SHORT,
        known_at=T0,
        draw_on_liquidity=short_draw,
        location_valid=True,
        h1_aligned=True,
        h4_direct_conflict=False,
        at_external_liquidity=False,
    )
    short_event = LiquidityEventEvidence(
        event_id="short-break",
        symbol="BTCUSDT",
        side=Side.SHORT,
        kind=EventKind.BREAK_ACCEPTANCE,
        known_at=T0 + pd.Timedelta(minutes=15),
        level=100.5,
        extreme=101.0,
        external_liquidity=False,
        boundary_broken_by_close=True,
        accepted_after_break=True,
    )
    short_delivery = DeliveryEvidence(
        delivery_id="short-delivery",
        symbol="BTCUSDT",
        side=Side.SHORT,
        known_at=T0 + pd.Timedelta(minutes=20),
        source_event_id=short_event.event_id,
        entry_array=EntryArray.FAIR_VALUE_GAP,
        zone=PriceZone(99.8, 100.2),
        displacement_body_atr=0.9,
        broke_preexisting_swing=True,
        fresh=True,
        return_number=1,
        invalidation_extreme=101.0,
    )
    short_intent = ExecutionIntent(
        intent_id="short",
        symbol="BTCUSDT",
        side=Side.SHORT,
        model=ExecutionModel.BREAK_ACCEPT_RETEST,
        narrative=Narrative.CONTINUATION,
        known_at=long_intent.known_at,
        entry_time=long_intent.entry_time,
        entry=100.0,
        stop=101.0,
        context=short_context,
        event=short_event,
        delivery=short_delivery,
        targets=(short_draw,),
    )
    decision = choose_global_trade([long_intent, short_intent])
    assert not decision.approved
    assert decision.reasons == ("opposing_same_symbol_narratives",)


def test_global_router_enforces_single_slot() -> None:
    decision = choose_global_trade(
        [continuation_intent()], pending_or_open=True
    )
    assert not decision.approved
    assert decision.reasons == ("global_slot_occupied",)


def test_authority_runtime_rearms_only_after_close_departure_and_new_delivery() -> None:
    runtime = AuthorityRuntime("authority")
    runtime.arm()
    runtime.submit_entry(global_slot_occupied=False)
    runtime.fill()
    runtime.close(closed_at=T0 + pd.Timedelta(hours=1), stopped=False)
    assert runtime.status is AuthorityStatus.DEPARTURE_REQUIRED

    runtime.observe_departure(departure_r=0.9, micro_delivery_confirmed=True)
    assert runtime.status is AuthorityStatus.DEPARTURE_REQUIRED

    runtime.observe_departure(departure_r=1.1, micro_delivery_confirmed=True)
    assert runtime.status is AuthorityStatus.REARMED


def test_stop_loss_invalidates_authority_and_prevents_averaging() -> None:
    runtime = AuthorityRuntime("authority")
    runtime.arm()
    runtime.submit_entry(global_slot_occupied=False)
    runtime.fill()
    runtime.close(closed_at=T0 + pd.Timedelta(hours=1), stopped=True)
    assert runtime.status is AuthorityStatus.INVALIDATED
    with pytest.raises(RuntimeError):
        runtime.submit_entry(global_slot_occupied=False)
