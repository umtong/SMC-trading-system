from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

from ictbt.easychart_v0.domain import (
    B1Subtype,
    ConfluenceAuthority,
    LiquidityDeliveryAuthority,
    OwnedM15OverlapAuthority,
)
from ictbt.easychart_v0.execution import RiskConfig
from ictbt.easychart_v0.pipeline import build_feature_book
from ictbt.easychart_v0.v04 import (
    build_baseline_event_authorities,
    run_v04_historical_replay,
)
from ictbt.easychart_v0.v05 import build_m15_m5_liquidity_delivery_result
from ictbt.easychart_v0.v06 import build_owned_m15_overlap_result

from analyze_easychart_v0_2_winrate import COSTS, WINDOWS


ARMS = (
    "A_LEADER_V03_BREAK_RETEST_PLUS_V05",
    "B_BPLUS_M15_OB_FORMATION_STOP",
    "C_BPLUS_PROTECTED_M15_SWING_STOP",
    "D_LEADER_PLUS_BPLUS_FORMATION_STOP",
    "E_LEADER_PLUS_BPLUS_PROTECTED_STOP",
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/s4_continuous_2021_2026"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/easychart_v06_owned_m15_overlap_stops"),
    )
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.03)
    parser.add_argument("--window-index", type=int)
    return parser.parse_args()


def _load_source(path: Path) -> pd.DataFrame:
    source = pd.read_csv(path)
    timestamp_column = next(
        name
        for name in ("open_time", "timestamp", "time", "datetime")
        if name in source.columns
    )
    source.index = pd.DatetimeIndex(
        pd.to_datetime(source.pop(timestamp_column), utc=True),
        name="open_time",
    )
    return source


def _window(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    begin = pd.Timestamp(start, tz="UTC")
    finish = pd.Timestamp(end, tz="UTC")
    return frame.loc[(frame.index >= begin) & (frame.index < finish)].copy()


def _protected_variant(
    authority: OwnedM15OverlapAuthority,
    *,
    tick_size: float,
) -> OwnedM15OverlapAuthority:
    extreme = authority.protected_pivot.price
    stop = (
        extreme - tick_size
        if authority.side.value == "long"
        else extreme + tick_size
    )
    return replace(
        authority,
        stop_owner="protected_m15_swing",
        stop_extreme=extreme,
        initial_stop=stop,
    )


def _mfe_r(candles: pd.DataFrame, trade) -> float:
    risk_distance = abs(trade.entry_price - trade.initial_stop)
    if risk_distance <= 0:
        return 0.0
    bars = candles.loc[
        (candles.index >= trade.entry_time)
        & (candles.index <= trade.closed_at)
    ]
    if bars.empty:
        return 0.0
    favorable = (
        float(bars["high"].max()) - trade.entry_price
        if trade.side.value == "long"
        else trade.entry_price - float(bars["low"].min())
    )
    return max(0.0, favorable / risk_distance)


def _source(authority) -> str:
    if isinstance(authority, OwnedM15OverlapAuthority):
        return "bplus"
    if isinstance(authority, LiquidityDeliveryAuthority):
        return "v05"
    return "v03_break_retest"


def _trade_row(
    *,
    arm: str,
    window_index: int,
    symbol: str,
    environment: str,
    candles: pd.DataFrame,
    attempt,
    authority,
) -> dict[str, object]:
    trade = attempt.result.trade
    assert trade is not None
    bplus = (
        authority if isinstance(authority, OwnedM15OverlapAuthority) else None
    )
    return {
        "arm": arm,
        "window_index": window_index,
        "symbol": symbol,
        "environment": environment,
        "source_strategy": _source(authority),
        "scene_family": trade.scene_family.value,
        "authority_id": attempt.authority_id,
        "scene_root_id": None if bplus is None else bplus.scene_root_id,
        "anchor_ob_id": None if bplus is None else bplus.anchor_ob.ob_id,
        "partner_ob_id": None if bplus is None else bplus.partner_ob.ob_id,
        "pair_type": None if bplus is None else bplus.pair_type,
        "partner_timing": None if bplus is None else bplus.partner_timing,
        "break_pivot_id": None if bplus is None else bplus.break_pivot.pivot_id,
        "break_pivot_price": None if bplus is None else bplus.break_pivot.price,
        "protected_pivot_id": (
            None if bplus is None else bplus.protected_pivot.pivot_id
        ),
        "protected_pivot_price": (
            None if bplus is None else bplus.protected_pivot.price
        ),
        "stop_owner": None if bplus is None else bplus.stop_owner,
        "side": trade.side.value,
        "created_at": attempt.intent.created_at.isoformat(),
        "planned_entry": attempt.intent.entry_reference,
        "entry_time": trade.entry_time.isoformat(),
        "actual_entry": trade.entry_price,
        "closed_at": trade.closed_at.isoformat(),
        "initial_stop": trade.initial_stop,
        "initial_target": trade.initial_target,
        "stop_distance": abs(trade.entry_price - trade.initial_stop),
        "stop_distance_pct": (
            abs(trade.entry_price - trade.initial_stop) / trade.entry_price
        ),
        "target_distance": abs(trade.initial_target - trade.entry_price),
        "target_r": trade.target_r,
        "partial_enabled": trade.target_r >= 1.4,
        "partial_1r": any(
            leg.reason == "partial_1r" for leg in trade.exit_legs
        ),
        "mfe_r": _mfe_r(candles, trade),
        "final_reason": trade.final_reason,
        "risk_budget": attempt.intent.risk_budget,
        "quantity": attempt.intent.quantity,
        "position_notional": attempt.intent.quantity * trade.entry_price,
        "notional_to_equity": (
            attempt.intent.quantity * trade.entry_price / attempt.equity_before
        ),
        "net_r": trade.net_pnl / attempt.intent.risk_budget,
        "net_pnl": trade.net_pnl,
        "equity_before": attempt.equity_before,
        "equity_after": attempt.equity_after,
    }


def _max_drawdown_fraction(
    rows: list[dict[str, object]],
    initial_equity: float,
) -> float:
    worst = 0.0
    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["window_index"])].append(row)
    for trades in grouped.values():
        peak = initial_equity
        for row in sorted(trades, key=lambda item: str(item["closed_at"])):
            equity = float(row["equity_after"])
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak)
    return worst


def _metrics(
    rows: list[dict[str, object]],
    *,
    initial_equity: float,
    instrument_days: int,
) -> dict[str, object]:
    pnls = [float(row["net_pnl"]) for row in rows]
    rs = [float(row["net_r"]) for row in rows]
    positive = [value for value in rs if value > 0]
    negative = [value for value in rs if value < 0]
    gross_win = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    return {
        "trades": len(rows),
        "trades_per_instrument_day": len(rows) / instrument_days,
        "wins": len(positive),
        "losses": len(negative),
        "win_rate": None if not rows else len(positive) / len(rows),
        "net_pnl": sum(pnls),
        "net_r": sum(rs),
        "net_r_per_instrument_day": sum(rs) / instrument_days,
        "average_net_r": None if not rows else statistics.fmean(rs),
        "profit_factor": (
            None if gross_loss == 0 else gross_win / gross_loss
        ),
        "median_stop_distance_pct": (
            None
            if not rows
            else statistics.median(
                float(row["stop_distance_pct"]) for row in rows
            )
        ),
        "median_target_r": (
            None
            if not rows
            else statistics.median(float(row["target_r"]) for row in rows)
        ),
        "partial_enabled": sum(bool(row["partial_enabled"]) for row in rows),
        "partial_1r_trades": sum(bool(row["partial_1r"]) for row in rows),
        "volume_exits": sum(row["final_reason"] == "volume_spike" for row in rows),
        "max_drawdown_fraction": _max_drawdown_fraction(rows, initial_equity),
    }


def main() -> int:
    args = _args()
    indices = (
        (args.window_index,)
        if args.window_index is not None
        else tuple(range(len(WINDOWS)))
    )
    if any(index < 0 or index >= len(WINDOWS) for index in indices):
        raise SystemExit("window-index out of range")
    instrument_days = sum(
        (
            pd.Timestamp(WINDOWS[index][3])
            - pd.Timestamp(WINDOWS[index][2])
        ).days
        for index in indices
    )
    risk = RiskConfig(
        risk_fraction=args.risk_fraction,
        quantity_step=0.001,
        daily_loss_limit_enabled=False,
    )
    frames = {
        symbol: _load_source(args.data_dir / f"{symbol}_5m.csv")
        for symbol in {WINDOWS[index][0] for index in indices}
    }
    ledger: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    pairing: list[dict[str, object]] = []

    for ordinal, index in enumerate(indices, start=1):
        symbol, environment, start, end, tick_size = WINDOWS[index]
        candles = _window(frames[symbol], start, end)
        print(
            f"[{ordinal}/{len(indices)}] {symbol} {environment}: "
            f"{len(candles)} bars",
            flush=True,
        )
        book = build_feature_book(candles, symbol=symbol, tick_size=tick_size)
        baseline_break_retest = tuple(
            authority
            for authority in build_baseline_event_authorities(book)
            if authority.confirmation.subtype is B1Subtype.BREAK_RETEST
        )
        v05 = build_m15_m5_liquidity_delivery_result(book)
        leader = tuple(
            sorted(
                (*baseline_break_retest, *v05.authorities),
                key=lambda item: (item.known_at, item.authority_id),
            )
        )
        formation_result = build_owned_m15_overlap_result(
            book,
            stop_owner="m15_anchor_formation",
        )
        formation = formation_result.authorities
        protected = tuple(
            _protected_variant(authority, tick_size=tick_size)
            for authority in formation
        )
        for left, right in zip(formation, protected, strict=True):
            pairing.append(
                {
                    "window_index": index,
                    "symbol": symbol,
                    "environment": environment,
                    "scene_root_id": left.scene_root_id,
                    "anchor_ob_id": left.anchor_ob.ob_id,
                    "partner_ob_id": left.partner_ob.ob_id,
                    "pair_type": left.pair_type,
                    "partner_timing": left.partner_timing,
                    "side": left.side.value,
                    "anchor_known_at": left.anchor_ob.known_at.isoformat(),
                    "partner_known_at": left.partner_ob.known_at.isoformat(),
                    "pair_known_at": left.pair_known_at.isoformat(),
                    "departure_at": left.known_at.isoformat(),
                    "zone_low": left.zone.low,
                    "zone_high": left.zone.high,
                    "planned_entry": (
                        left.zone.high
                        if left.side.value == "long"
                        else left.zone.low
                    ),
                    "target_id": left.destination.source_id,
                    "target_price": left.destination.order_price,
                    "formation_stop": left.initial_stop,
                    "protected_stop": right.initial_stop,
                }
            )

        arm_inputs = (
            (ARMS[0], leader, None),
            (ARMS[1], formation, False),
            (ARMS[2], protected, False),
            (
                ARMS[3],
                tuple(
                    sorted(
                        (*leader, *formation),
                        key=lambda item: (item.known_at, item.authority_id),
                    )
                ),
                None,
            ),
            (
                ARMS[4],
                tuple(
                    sorted(
                        (*leader, *protected),
                        key=lambda item: (item.known_at, item.authority_id),
                    )
                ),
                None,
            ),
        )
        leader_times = {item.known_at for item in leader}
        bplus_times = {item.known_at for item in formation}
        same_cutoff_collisions = len(leader_times & bplus_times)
        for arm, authorities, use_v03_targets in arm_inputs:
            run = run_v04_historical_replay(
                candles,
                symbol=symbol,
                tick_size=tick_size,
                equity=args.initial_equity,
                costs=COSTS,
                risk=risk,
                book=book,
                authorities=authorities,
                use_v03_targets=use_v03_targets,
            )
            authority_map = {
                authority.authority_id: authority for authority in run.authorities
            }
            for attempt in run.attempts:
                if attempt.result.trade is None:
                    continue
                ledger.append(
                    _trade_row(
                        arm=arm,
                        window_index=index,
                        symbol=symbol,
                        environment=environment,
                        candles=candles,
                        attempt=attempt,
                        authority=authority_map[attempt.authority_id],
                    )
                )
            row: dict[str, object] = {
                "arm": arm,
                "window_index": index,
                "symbol": symbol,
                "environment": environment,
                "authorities": len(run.authorities),
                "attempts": len(run.attempts),
                "closed_trades": len(run.closed_trades),
                "opportunity_rejections": len(run.opportunity_rejections),
                "sizing_rejections": len(run.sizing_rejections),
                "pending_cancellations": len(run.pending_cancellations),
                "slot_suppressed_authorities": run.slot_suppressed_authorities,
                "same_cutoff_cross_family_collisions": same_cutoff_collisions,
                "cancellation_reasons": json.dumps(
                    Counter(item.reason for item in run.pending_cancellations),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "final_equity": run.final_equity,
            }
            if arm in {ARMS[1], ARMS[2]}:
                row.update(
                    {
                        f"funnel_{key}": value
                        for key, value in asdict(
                            formation_result.diagnostics
                        ).items()
                    }
                )
            diagnostics.append(row)
            print(
                f"  {arm}: authorities={len(run.authorities)}, "
                f"closed={len(run.closed_trades)}, "
                f"equity={run.final_equity:.2f}",
                flush=True,
            )

    summary = {
        "contract": {
            "windows": len(indices),
            "instrument_days": instrument_days,
            "initial_equity_per_window": args.initial_equity,
            "risk_fraction": args.risk_fraction,
            "daily_loss_limit_enabled": False,
            "one_total_slot": True,
            "entry_mode": "limit_first_return_after_departure",
            "partial_policy": "target_R>=1.4: half_at_1R_then_BE; else full_target",
            "volume_exit": "M5_or_M15_RVOL20_median>=2_and_net_profitable",
            "costs": {
                "entry_fee_rate": COSTS.entry_fee_rate,
                "stop_fee_rate": COSTS.stop_fee_rate,
                "target_fee_rate": COSTS.target_fee_rate,
                "volume_exit_fee_rate": COSTS.volume_exit_fee_rate,
                "stop_slippage_bps": COSTS.stop_slippage_bps,
                "volume_exit_slippage_bps": COSTS.volume_exit_slippage_bps,
            },
        },
        "pairing": {
            "formation_scenes": len(pairing),
            "protected_scenes": len(pairing),
            "common_scenes": len(pairing),
            "formation_only_scenes": 0,
            "protected_only_scenes": 0,
            "same_zone_target_mismatches": 0,
        },
        "arms": {
            arm: _metrics(
                [row for row in ledger if row["arm"] == arm],
                initial_equity=args.initial_equity,
                instrument_days=instrument_days,
            )
            for arm in ARMS
        },
        "by_environment": {
            arm: {
                environment: _metrics(
                    [
                        row
                        for row in ledger
                        if row["arm"] == arm
                        and row["environment"] == environment
                    ],
                    initial_equity=args.initial_equity,
                    instrument_days=(
                        pd.Timestamp(end) - pd.Timestamp(start)
                    ).days,
                )
                for _, environment, start, end, _ in (
                    WINDOWS[index] for index in indices
                )
            }
            for arm in ARMS
        },
        "bplus_by_pair_type": {
            arm: {
                pair_type: _metrics(
                    [
                        row
                        for row in ledger
                        if row["arm"] == arm and row["pair_type"] == pair_type
                    ],
                    initial_equity=args.initial_equity,
                    instrument_days=instrument_days,
                )
                for pair_type in ("h1_m15", "m15_m5")
            }
            for arm in (ARMS[1], ARMS[2])
        },
        "bplus_by_partner_timing": {
            arm: {
                timing: _metrics(
                    [
                        row
                        for row in ledger
                        if row["arm"] == arm
                        and row["partner_timing"] == timing
                    ],
                    initial_equity=args.initial_equity,
                    instrument_days=instrument_days,
                )
                for timing in ("at_anchor_close", "later_fresh")
            }
            for arm in (ARMS[1], ARMS[2])
        },
        "diagnostics": diagnostics,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    pd.DataFrame.from_records(ledger).to_csv(
        args.output_dir / "trade_ledger.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame.from_records(diagnostics).to_csv(
        args.output_dir / "diagnostics.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame.from_records(pairing).to_csv(
        args.output_dir / "authority_pairing.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(json.dumps(summary["arms"], ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
