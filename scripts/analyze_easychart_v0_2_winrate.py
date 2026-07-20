from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from ictbt.easychart_v0.application import load_5m_csv, run_historical_replay
from ictbt.easychart_v0.domain import OrderBlock, Side, Timeframe
from ictbt.easychart_v0.execution import (
    CostConfig,
    RiskConfig,
    build_confluence_first_revisit_intent,
)
from ictbt.easychart_v0.pipeline import (
    Opportunity,
    assemble_opportunity,
    build_confluence_authorities,
    structure_state,
)
from ictbt.easychart_v0.replay import replay_intent
from ictbt.easychart_v0.strategy import SimpleExecutionCosts


WINDOWS = (
    ("ETHUSDT", "eth_transition", "2024-12-30", "2025-01-13", 0.01),
    ("ETHUSDT", "eth_up", "2025-07-08", "2025-07-22", 0.01),
    ("BTCUSDT", "btc_down_high_vol", "2026-01-23", "2026-02-06", 0.1),
    ("ETHUSDT", "eth_down_high_vol", "2026-01-23", "2026-02-06", 0.01),
    ("BTCUSDT", "btc_up", "2026-04-04", "2026-04-18", 0.1),
    ("BTCUSDT", "btc_range", "2026-06-08", "2026-06-22", 0.1),
)


COSTS = CostConfig(
    entry_fee_rate=0.0002,
    stop_fee_rate=0.0006,
    target_fee_rate=0.0002,
    volume_exit_fee_rate=0.0006,
    stop_slippage_bps=2.0,
    volume_exit_slippage_bps=2.0,
)
RISK = RiskConfig(risk_fraction=0.01, quantity_step=0.001)
TARGET_COSTS = SimpleExecutionCosts(entry_fee_rate=0.0002, exit_fee_rate=0.0002)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/s4_continuous_2021_2026"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/easychart_v0_2_winrate_deep_dive"),
    )
    return parser.parse_args()


def _window(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    begin = pd.Timestamp(start, tz="UTC")
    finish = pd.Timestamp(end, tz="UTC")
    return frame.loc[(frame.index >= begin) & (frame.index < finish)].copy()


def _tf_minutes(timeframe: Timeframe) -> int:
    return {
        Timeframe.M5: 5,
        Timeframe.M15: 15,
        Timeframe.H1: 60,
        Timeframe.H4: 240,
    }[timeframe]


def _group_name(authority) -> str:
    prefix = (
        f"{authority.location.timeframe.value}-ob"
        if isinstance(authority.location, OrderBlock)
        else f"{authority.location.timeframe.value}-pivot"
    )
    return f"{prefix}+{authority.confirmation.timeframes[0].value}"


def _directional_r(side: Side, entry: float, price: float, risk: float) -> float:
    direction = 1.0 if side is Side.LONG else -1.0
    return direction * (price - entry) / risk


def _event_bar(book, event) -> pd.Series:
    return book.frames[event.timeframe].loc[event.event_time]


def _bars_between(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return frame.loc[(frame.index >= start) & (frame.index < end)].copy()


def _event_premise_invalidated_before_exit(book, event, closed_at: pd.Timestamp) -> bool:
    frame = book.frames[event.timeframe]
    close_times = frame.index + pd.Timedelta(minutes=_tf_minutes(event.timeframe))
    closes = frame.loc[
        (close_times > event.known_at) & (close_times <= closed_at), "close"
    ]
    if event.side is Side.LONG:
        return bool((closes <= event.node_price - book.tick_size + 1e-12).any())
    return bool((closes >= event.node_price + book.tick_size - 1e-12).any())


def _counterfactual_event_stop(
    *,
    attempt,
    event_stop: float,
    candles: pd.DataFrame,
    volume_15m: pd.DataFrame,
):
    entry = attempt.intent.entry_reference
    side = attempt.intent.side
    farther = event_stop < attempt.intent.initial_stop if side is Side.LONG else event_stop > attempt.intent.initial_stop
    valid = event_stop < entry if side is Side.LONG else event_stop > entry
    if not farther or not valid or event_stop <= 0:
        return None
    intent = build_confluence_first_revisit_intent(
        order_id=f"counterfactual:{attempt.intent.order_id}",
        source_id=attempt.intent.source_id,
        symbol=attempt.intent.symbol,
        side=side,
        created_at=attempt.intent.created_at,
        initial_stop=event_stop,
        initial_target=attempt.intent.initial_target,
        equity=attempt.equity_before,
        costs=COSTS,
        risk=RISK,
        limit_price=entry,
    )
    return replay_intent(
        intent,
        candles=candles,
        candle_interval="5min",
        costs=COSTS,
        volume_bars={Timeframe.M5: candles, Timeframe.M15: volume_15m},
    )


def _record_for_attempt(*, run, attempt, environment: str) -> dict[str, object]:
    trade = attempt.result.trade
    assert trade is not None
    authorities = {
        item.authority_id: item
        for item in build_confluence_authorities(run.book, as_of=attempt.intent.created_at)
    }
    authority = authorities[attempt.authority_id]
    confirmation = authority.confirmation
    block = confirmation.order_blocks[0]
    event = next(
        item
        for item in run.book.liquidity_events[block.timeframe]
        if item.event_id == confirmation.liquidity_event_id
    )
    opportunity = assemble_opportunity(
        run.book,
        authority,
        as_of=attempt.intent.created_at,
        costs=TARGET_COSTS,
    )
    assert isinstance(opportunity, Opportunity)

    risk_distance = abs(trade.entry_price - trade.initial_stop)
    frame_5m = run.book.frames[Timeframe.M5]
    held = _bars_between(
        frame_5m,
        trade.entry_time.floor("5min"),
        trade.closed_at + pd.Timedelta(microseconds=1),
    )
    if held.empty:
        max_favorable_high_low_r = 0.0
        max_favorable_close_r = 0.0
        max_adverse_high_low_r = 0.0
    else:
        if trade.side is Side.LONG:
            max_favorable_high_low_r = (float(held.high.max()) - trade.entry_price) / risk_distance
            max_favorable_close_r = (float(held.close.max()) - trade.entry_price) / risk_distance
            max_adverse_high_low_r = (trade.entry_price - float(held.low.min())) / risk_distance
        else:
            max_favorable_high_low_r = (trade.entry_price - float(held.low.min())) / risk_distance
            max_favorable_close_r = (trade.entry_price - float(held.close.min())) / risk_distance
            max_adverse_high_low_r = (float(held.high.max()) - trade.entry_price) / risk_distance

    event_bar = _event_bar(run.book, event)
    event_extreme = float(event_bar.low if trade.side is Side.LONG else event_bar.high)
    event_stop = (
        event_extreme - run.book.tick_size
        if trade.side is Side.LONG
        else event_extreme + run.book.tick_size
    )
    event_stop_r = abs(trade.entry_price - event_stop) / risk_distance
    event_stop_is_farther = (
        event_stop < trade.initial_stop
        if trade.side is Side.LONG
        else event_stop > trade.initial_stop
    )
    event_extreme_touched_during_trade = (
        bool((held.low <= event_stop).any())
        if trade.side is Side.LONG
        else bool((held.high >= event_stop).any())
    )
    counterfactual = _counterfactual_event_stop(
        attempt=attempt,
        event_stop=event_stop,
        candles=frame_5m,
        volume_15m=run.book.frames[Timeframe.M15],
    )
    counterfactual_trade = None if counterfactual is None else counterfactual.trade

    location = authority.location
    location_zone_width = (
        location.zone.width if isinstance(location, OrderBlock) else 0.0
    )
    intersection_width = (
        min(location.zone.high, block.zone.high) - max(location.zone.low, block.zone.low)
        if isinstance(location, OrderBlock)
        else 0.0
    )
    literal_overlap = intersection_width + 1e-12 >= run.book.tick_size
    event_in_formation = event.event_time in {
        formation.open_time for formation in block.formation_bars
    }
    partial_filled = any(leg.reason == "partial_1r" for leg in trade.exit_legs)
    breakeven_stop = trade.final_reason == "initial_stop" and partial_filled
    initial_stop_exit = trade.final_reason == "initial_stop" and not partial_filled

    return {
        "symbol": trade.symbol,
        "environment": environment,
        "authority_id": attempt.authority_id,
        "side": trade.side.value,
        "path": _group_name(authority),
        "a1_type": "ob" if isinstance(location, OrderBlock) else "pivot",
        "a1_timeframe": location.timeframe.value,
        "b1_timeframe": block.timeframe.value,
        "b1_kind": block.kind.value,
        "subtype": confirmation.subtype.value,
        "h1_state": structure_state(run.book, Timeframe.H1, as_of=attempt.intent.created_at).value,
        "h4_state": structure_state(run.book, Timeframe.H4, as_of=attempt.intent.created_at).value,
        "literal_body_overlap": literal_overlap,
        "intersection_width": max(0.0, intersection_width),
        "location_zone_width": location_zone_width,
        "b1_zone_width": block.zone.width,
        "event_in_b1_formation": event_in_formation,
        "event_to_b1_minutes": (block.known_at - event.known_at).total_seconds() / 60.0,
        "event_to_b1_bars": (block.known_at - event.known_at).total_seconds() / 60.0 / _tf_minutes(block.timeframe),
        "b1_to_entry_minutes": (trade.entry_time - block.known_at).total_seconds() / 60.0,
        "b1_to_entry_5m_bars": (trade.entry_time - block.known_at).total_seconds() / 300.0,
        "holding_minutes": (trade.closed_at - trade.entry_time).total_seconds() / 60.0,
        "entry_time": trade.entry_time.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "entry_price": trade.entry_price,
        "initial_stop": trade.initial_stop,
        "initial_target": trade.initial_target,
        "risk_distance": risk_distance,
        "risk_pct_of_entry": risk_distance / trade.entry_price,
        "target_r": trade.target_r,
        "target_kind": opportunity.target.kind,
        "target_source_id": opportunity.target.source_id,
        "event_node_price": event.node_price,
        "event_extreme": event_extreme,
        "event_stop": event_stop,
        "event_stop_r_multiple": event_stop_r,
        "event_stop_is_farther": event_stop_is_farther,
        "event_extreme_touched_during_trade": event_extreme_touched_during_trade,
        "event_premise_invalidated_before_exit": _event_premise_invalidated_before_exit(
            run.book, event, trade.closed_at
        ),
        "max_favorable_high_low_r": max_favorable_high_low_r,
        "max_favorable_close_r": max_favorable_close_r,
        "max_adverse_high_low_r": max_adverse_high_low_r,
        "partial_filled": partial_filled,
        "breakeven_stop": breakeven_stop,
        "initial_stop_exit": initial_stop_exit,
        "final_reason": trade.final_reason,
        "net_pnl": trade.net_pnl,
        "winner": trade.net_pnl > 0,
        "counterfactual_event_stop_available": counterfactual is not None,
        "counterfactual_status": None if counterfactual is None else counterfactual.status,
        "counterfactual_net_pnl": None if counterfactual_trade is None else counterfactual_trade.net_pnl,
        "counterfactual_winner": None if counterfactual_trade is None else counterfactual_trade.net_pnl > 0,
        "counterfactual_final_reason": None if counterfactual_trade is None else counterfactual_trade.final_reason,
        "counterfactual_target_r": None if counterfactual_trade is None else counterfactual_trade.target_r,
    }


def _summary(records: pd.DataFrame) -> dict[str, object]:
    wins = records.loc[records.winner, "net_pnl"]
    losses = records.loc[~records.winner, "net_pnl"]

    def grouped(column: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for value, group in records.groupby(column, dropna=False):
            rows.append(
                {
                    "value": str(value),
                    "trades": int(len(group)),
                    "wins": int(group.winner.sum()),
                    "win_rate": float(group.winner.mean()),
                    "net_pnl": float(group.net_pnl.sum()),
                    "mean_pnl": float(group.net_pnl.mean()),
                }
            )
        return sorted(rows, key=lambda row: (row["net_pnl"], row["value"]))

    stopouts = records.loc[records.initial_stop_exit]
    cf = records.loc[
        records.counterfactual_event_stop_available
        & records.counterfactual_net_pnl.notna()
    ]
    return {
        "trades": int(len(records)),
        "wins": int(records.winner.sum()),
        "win_rate": float(records.winner.mean()),
        "net_pnl": float(records.net_pnl.sum()),
        "average_win": float(wins.mean()),
        "average_loss": float(losses.mean()),
        "payoff_ratio": float(wins.mean() / -losses.mean()),
        "break_even_win_rate_for_realized_payoff": float(
            -losses.mean() / (wins.mean() - losses.mean())
        ),
        "initial_stop_exits": int(records.initial_stop_exit.sum()),
        "partial_then_breakeven_stops": int(records.breakeven_stop.sum()),
        "partial_fills": int(records.partial_filled.sum()),
        "stopouts_before_event_premise_invalidated": int(
            (~stopouts.event_premise_invalidated_before_exit).sum()
        ),
        "stopouts_before_event_extreme_touched": int(
            (~stopouts.event_extreme_touched_during_trade).sum()
        ),
        "event_stop_counterfactual": {
            "eligible_trades": int(len(cf)),
            "closed_trades": int(cf.counterfactual_net_pnl.notna().sum()),
            "wins": int(cf.counterfactual_winner.sum()),
            "win_rate": float(cf.counterfactual_winner.mean()) if len(cf) else 0.0,
            "net_pnl_sum_as_independent_scenes": float(cf.counterfactual_net_pnl.sum()),
            "original_net_pnl_same_scenes": float(cf.net_pnl.sum()),
        },
        "groups": {
            column: grouped(column)
            for column in (
                "symbol",
                "environment",
                "side",
                "path",
                "a1_type",
                "b1_timeframe",
                "b1_kind",
                "subtype",
                "h1_state",
                "h4_state",
                "literal_body_overlap",
                "event_in_b1_formation",
                "target_kind",
                "partial_filled",
                "final_reason",
            )
        },
    }


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source_frames = {
        symbol: load_5m_csv(args.data_dir / f"{symbol}_5m.csv")
        for symbol in {item[0] for item in WINDOWS}
    }
    records: list[dict[str, object]] = []
    run_summaries: list[dict[str, object]] = []
    for symbol, environment, start, end, tick_size in WINDOWS:
        candles = _window(source_frames[symbol], start, end)
        run = run_historical_replay(
            candles,
            symbol=symbol,
            tick_size=tick_size,
            equity=10_000.0,
            costs=COSTS,
            risk=RISK,
        )
        closed = 0
        for attempt in run.attempts:
            if attempt.result.trade is None:
                continue
            records.append(
                _record_for_attempt(run=run, attempt=attempt, environment=environment)
            )
            closed += 1
        run_summaries.append(
            {
                "symbol": symbol,
                "environment": environment,
                "start": start,
                "end": end,
                "attempts": len(run.attempts),
                "closed": closed,
                "net_pnl": run.final_equity - run.initial_equity,
            }
        )
        print(f"completed {environment}: {closed} trades", flush=True)

    table = pd.DataFrame.from_records(records)
    table.to_csv(args.output_dir / "trades.csv", index=False)
    payload = {
        "strategy_version": "easychart_ob_v0_2_location_event_b1",
        "run_summaries": run_summaries,
        "summary": _summary(table),
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
