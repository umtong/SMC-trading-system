from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .application import (
    HistoricalReplayRun,
    SnapshotPlan,
    load_5m_csv,
    plan_snapshot,
    run_historical_replay,
)
from .domain import EntryMode
from .execution import (
    DEFAULT_DAILY_LOSS_LIMIT_ENABLED,
    DEFAULT_DAILY_LOSS_LIMIT_FRACTION,
    DEFAULT_DAILY_RESET_TIMEZONE,
    DEFAULT_RISK_FRACTION,
    CostConfig,
    RiskConfig,
)


STRATEGY_VERSION = "easychart_ob_v0_3_m15_event_m5_delivery"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ictbt.easychart_v0",
        description="Independent EasyChart OB V0 scanner and sequential replay",
    )
    parser.add_argument("--input", required=True, help="UTC-aligned 5m OHLCV CSV")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--tick-size", required=True, type=float)
    parser.add_argument("--as-of", help="timezone-aware decision time; default: last 5m close")
    parser.add_argument(
        "--replay",
        action="store_true",
        help="run the confluence strategy sequentially through the full CSV",
    )
    parser.add_argument(
        "--event-created-entry-mode",
        choices=tuple(mode.value for mode in EntryMode),
        default=EntryMode.NEXT_BAR_OPEN.value,
        help="entry arm for OBs created by their liquidity event",
    )
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--risk-fraction", type=float, default=DEFAULT_RISK_FRACTION)
    parser.add_argument(
        "--daily-loss-limit",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_DAILY_LOSS_LIMIT_ENABLED,
        help="stop proposing new orders after the configured realized daily net loss",
    )
    parser.add_argument(
        "--daily-loss-fraction",
        type=float,
        default=DEFAULT_DAILY_LOSS_LIMIT_FRACTION,
    )
    parser.add_argument(
        "--daily-reset-timezone",
        default=DEFAULT_DAILY_RESET_TIMEZONE,
        help="IANA timezone used to reset realized daily PnL",
    )
    parser.add_argument("--quantity-step", type=float, default=0.001)
    parser.add_argument("--minimum-quantity", type=float, default=0.0)
    parser.add_argument("--minimum-notional", type=float, default=0.0)
    parser.add_argument("--entry-fee-rate", type=float, default=0.0)
    parser.add_argument("--stop-fee-rate", type=float, default=0.0)
    parser.add_argument("--target-fee-rate", type=float, default=0.0)
    parser.add_argument("--volume-exit-fee-rate", type=float, default=0.0)
    parser.add_argument("--stop-slippage-bps", type=float, default=0.0)
    parser.add_argument("--volume-exit-slippage-bps", type=float, default=0.0)
    parser.add_argument("--output", help="optional JSON output path")
    return parser


def _risk_settings(risk: RiskConfig) -> dict[str, Any]:
    return {
        "risk_fraction": risk.risk_fraction,
        "risk_percent": risk.risk_fraction * 100,
        "daily_loss_limit_enabled": risk.daily_loss_limit_enabled,
        "daily_loss_limit_fraction": risk.daily_loss_limit_fraction,
        "daily_loss_limit_percent": risk.daily_loss_limit_fraction * 100,
        "daily_reset_timezone": risk.daily_reset_timezone,
        "daily_loss_basis": "day_start_equity_realized_net_pnl",
        "daily_limit_action": "block_new_orders_only",
    }


def _report(
    plan: SnapshotPlan,
    *,
    source: str,
    risk: RiskConfig,
) -> dict[str, Any]:
    opportunities = {item.opportunity_id: item for item in plan.opportunities}
    orders = []
    for intent in plan.candidate_intents:
        opportunity = opportunities[intent.order_id.removeprefix("order:")]
        orders.append(
            {
                "order_id": intent.order_id,
                "source_id": intent.source_id,
                "scene": intent.scene_family.value,
                "side": intent.side.value,
                "entry_mode": intent.entry_mode.value,
                "created_at": intent.created_at.isoformat(),
                "entry": intent.entry_reference,
                "initial_stop": intent.initial_stop,
                "initial_target": intent.initial_target,
                "target_kind": opportunity.target.kind,
                "quantity": intent.quantity,
                "risk_budget": intent.risk_budget,
                "unit_stop_risk": intent.unit_stop_risk,
            }
        )
    feature_counts = {
        timeframe.value: {
            "candles": len(plan.book.frames[timeframe]),
            "order_blocks": len(plan.book.order_blocks[timeframe]),
            "pivots": len(plan.book.pivots[timeframe]),
            "fvgs": len(plan.book.fvgs[timeframe]),
            "liquidity_events": len(plan.book.liquidity_events.get(timeframe, ())),
        }
        for timeframe in plan.book.frames
    }
    return {
        "engine": "easychart_ob_v0",
        "strategy_version": STRATEGY_VERSION,
        "mode": "snapshot",
        "input": source,
        "symbol": plan.book.symbol,
        "as_of": plan.as_of.isoformat(),
        "risk_settings": _risk_settings(risk),
        "strategy_settings": {
            "event_created_entry_mode": plan.event_created_entry_mode.value,
        },
        "structure": {
            "h1": plan.structure.h1.value,
            "h4": plan.structure.h4.value,
            "delivery": plan.structure.delivery.value,
        },
        "features": feature_counts,
        "summary": {
            "opportunities": len(plan.opportunities),
            "candidate_orders": len(orders),
            "opportunity_rejections": len(plan.opportunity_rejections),
            "sizing_rejections": len(plan.sizing_rejections),
        },
        "candidate_orders": orders,
        "opportunity_rejections": [
            {
                "authority_id": item.authority_id,
                "scene": item.scene_family.value,
                "side": item.side.value,
                "reason": item.reason,
            }
            for item in plan.opportunity_rejections
        ],
        "sizing_rejections": [
            {
                "opportunity_id": item.opportunity_id,
                "reason": item.reason,
            }
            for item in plan.sizing_rejections
        ],
    }


def _replay_report(
    run: HistoricalReplayRun,
    *,
    source: str,
    risk: RiskConfig,
) -> dict[str, Any]:
    attempts = []
    for attempt in run.attempts:
        result = attempt.result
        trade = result.trade
        attempts.append(
            {
                "opportunity_id": attempt.opportunity_id,
                "authority_id": attempt.authority_id,
                "order_id": attempt.intent.order_id,
                "scene": attempt.intent.scene_family.value,
                "side": attempt.intent.side.value,
                "entry_mode": attempt.intent.entry_mode.value,
                "created_at": attempt.intent.created_at.isoformat(),
                "entry": attempt.intent.entry_reference,
                "initial_stop": attempt.intent.initial_stop,
                "initial_target": attempt.intent.initial_target,
                "quantity": attempt.intent.quantity,
                "status": result.status,
                "equity_before": attempt.equity_before,
                "equity_after": attempt.equity_after,
                "net_pnl": None if trade is None else trade.net_pnl,
                "closed_at": None if trade is None else trade.closed_at.isoformat(),
                "final_reason": None if trade is None else trade.final_reason,
                "rejection_reason": result.rejection_reason,
                "events": [
                    {
                        "kind": event.kind,
                        "occurred_at": event.occurred_at.isoformat(),
                        "price": event.price,
                        "detail": event.detail,
                    }
                    for event in result.events
                ],
            }
        )
    statuses = [attempt.result.status for attempt in run.attempts]
    return {
        "engine": "easychart_ob_v0",
        "strategy_version": STRATEGY_VERSION,
        "mode": "historical_replay",
        "input": source,
        "symbol": run.book.symbol,
        "initial_equity": run.initial_equity,
        "final_equity": run.final_equity,
        "risk_settings": _risk_settings(risk),
        "strategy_settings": {
            "event_created_entry_mode": run.event_created_entry_mode.value,
        },
        "summary": {
            "decision_times": len(run.decision_times),
            "attempts": len(run.attempts),
            "closed_trades": len(run.closed_trades),
            "entry_rejected": statuses.count("ENTRY_REJECTED"),
            "open_censored": statuses.count("OPEN_CENSORED"),
            "entry_censored": statuses.count("ENTRY_CENSORED"),
            "opportunity_rejections": len(run.opportunity_rejections),
            "sizing_rejections": len(run.sizing_rejections),
            "pending_cancellations": len(run.pending_cancellations),
            "expired_before_submission": len(run.expired_before_submission),
            "daily_loss_blocks": len(run.daily_loss_blocks),
        },
        "attempts": attempts,
        "opportunity_rejections": [
            {
                "authority_id": item.authority_id,
                "scene": item.scene_family.value,
                "side": item.side.value,
                "reason": item.reason,
            }
            for item in run.opportunity_rejections
        ],
        "sizing_rejections": [
            {
                "opportunity_id": item.opportunity_id,
                "reason": item.reason,
            }
            for item in run.sizing_rejections
        ],
        "pending_cancellations": [
            {
                "opportunity_id": item.opportunity_id,
                "authority_id": item.authority_id,
                "order_id": item.order_id,
                "cancelled_at": item.cancelled_at.isoformat(),
                "reason": item.reason,
            }
            for item in run.pending_cancellations
        ],
        "expired_before_submission": [
            {
                "opportunity_id": item.opportunity_id,
                "authority_id": item.authority_id,
                "expired_at": item.expired_at.isoformat(),
                "reason": item.reason,
            }
            for item in run.expired_before_submission
        ],
        "daily_loss_blocks": [
            {
                "decision_at": item.decision_at.isoformat(),
                "local_date": item.local_date.isoformat(),
                "day_start_equity": item.day_start_equity,
                "realized_net_pnl": item.realized_net_pnl,
                "loss_limit_cash": item.loss_limit_cash,
            }
            for item in run.daily_loss_blocks
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    costs = CostConfig(
        entry_fee_rate=args.entry_fee_rate,
        stop_fee_rate=args.stop_fee_rate,
        target_fee_rate=args.target_fee_rate,
        volume_exit_fee_rate=args.volume_exit_fee_rate,
        stop_slippage_bps=args.stop_slippage_bps,
        volume_exit_slippage_bps=args.volume_exit_slippage_bps,
    )
    risk = RiskConfig(
        risk_fraction=args.risk_fraction,
        quantity_step=args.quantity_step,
        minimum_quantity=args.minimum_quantity,
        minimum_notional=args.minimum_notional,
        daily_loss_limit_enabled=args.daily_loss_limit,
        daily_loss_limit_fraction=args.daily_loss_fraction,
        daily_reset_timezone=args.daily_reset_timezone,
    )
    candles = load_5m_csv(args.input)
    event_created_entry_mode = EntryMode(args.event_created_entry_mode)
    if args.replay:
        run = run_historical_replay(
            candles,
            symbol=args.symbol,
            tick_size=args.tick_size,
            equity=args.equity,
            costs=costs,
            risk=risk,
            event_created_entry_mode=event_created_entry_mode,
        )
        report = _replay_report(run, source=args.input, risk=risk)
    else:
        plan = plan_snapshot(
            candles,
            symbol=args.symbol,
            tick_size=args.tick_size,
            equity=args.equity,
            costs=costs,
            risk=risk,
            as_of=args.as_of,
            event_created_entry_mode=event_created_entry_mode,
        )
        report = _report(plan, source=args.input, risk=risk)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

