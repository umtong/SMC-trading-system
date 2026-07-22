from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Sequence

import pandas as pd

from ictbt.easychart_v0.domain import B1Subtype
from ictbt.easychart_v0.execution import CostConfig, RiskConfig
from ictbt.easychart_v0.pipeline import build_feature_book
from ictbt.easychart_v0.v04 import build_baseline_event_authorities
from ictbt.easychart_v0.v05 import build_m15_m5_liquidity_delivery_result
from ictbt.easychart_v0.v07 import assemble_v07_opportunity, build_v07_scene_family_result
from ictbt.easychart_v0.v08 import (
    V08ContextPolicy,
    V08TargetPolicy,
    build_v08_scene_family_result,
)
from scripts import compare_easychart_v07_scene_families as v07_comparison
from scripts.compare_easychart_v07_scene_families import (
    BOUNDARY_ACCEPT_NEXT_OPEN,
    COSTS,
    WindowContext,
    _instrument_days,
    _load_source,
    _metrics,
    _portfolio_operating_dates,
    _run_global_arm,
    _trade_row,
    _utc,
    _window,
)
from scripts.v08_windows import DEVELOPMENT_WINDOWS, HOLDOUT_WINDOWS, WARMUP_DAYS


_ORIGINAL_REPLAY_INTENT = v07_comparison.replay_intent
_REPLAY_VOLUME_LOOKBACK = pd.Timedelta(hours=6)


def _trimmed_replay_intent(
    intent: object,
    *,
    candles: pd.DataFrame,
    candle_interval: object,
    costs: object,
    lower_native_bars: pd.DataFrame | None = None,
    lower_native_interval: object | None = None,
    volume_bars: object | None = None,
) -> object:
    """Drop causally irrelevant pre-order history before each replay.

    ``replay_intent`` ignores price bars before ``intent.created_at``. Volume
    exits need only the preceding 20 completed M5/M15 volumes; six hours is a
    fixed, non-optimized superset of both lookbacks. The wrapper therefore
    preserves the exact execution clock while avoiding repeated materialization
    of the 28-day feature warm-up for every candidate.
    """

    created_at = pd.Timestamp(getattr(intent, "created_at"))
    price = (
        candles.loc[candles.index >= created_at]
        if isinstance(candles, pd.DataFrame)
        else candles
    )
    lower = lower_native_bars
    if isinstance(lower_native_bars, pd.DataFrame):
        lower = lower_native_bars.loc[lower_native_bars.index >= created_at]

    volumes = volume_bars
    if isinstance(volume_bars, dict):
        volume_start = created_at - _REPLAY_VOLUME_LOOKBACK
        volumes = {
            timeframe: (
                frame.loc[frame.index >= volume_start]
                if isinstance(frame, pd.DataFrame)
                else frame
            )
            for timeframe, frame in volume_bars.items()
        }

    return _ORIGINAL_REPLAY_INTENT(
        intent,
        candles=price,
        candle_interval=candle_interval,
        costs=costs,
        lower_native_bars=lower,
        lower_native_interval=lower_native_interval,
        volume_bars=volumes,
    )


def _install_trimmed_replay() -> None:
    v07_comparison.replay_intent = _trimmed_replay_intent


@dataclass(frozen=True, slots=True)
class Arm:
    name: str
    source: str
    combined: bool = False
    target: V08TargetPolicy | None = None
    context: V08ContextPolicy | None = None
    primary: bool = False


ARMS = (
    Arm("A_LEADER_V03_BREAK_RETEST_PLUS_V05", "leader"),
    Arm("B_V07_ANY_TARGET_NEXT_OPEN", "v07"),
    Arm(
        "C_V08_PIVOT_ONLY",
        "v08",
        target=V08TargetPolicy.PIVOT_ONLY,
        context=V08ContextPolicy.NONE,
    ),
    Arm(
        "D_V08_PIVOT_ONLY_NOT_OPPOSED_PRIMARY",
        "v08",
        target=V08TargetPolicy.PIVOT_ONLY,
        context=V08ContextPolicy.NOT_OPPOSED,
        primary=True,
    ),
    Arm(
        "E_V08_PIVOT_ONLY_STRICT_DELIVERY",
        "v08",
        target=V08TargetPolicy.PIVOT_ONLY,
        context=V08ContextPolicy.STRICT_DELIVERY,
    ),
    Arm(
        "F_V08_CONFLUENT_PIVOT_NOT_OPPOSED",
        "v08",
        target=V08TargetPolicy.PIVOT_ZONE_CONFLUENT,
        context=V08ContextPolicy.NOT_OPPOSED,
    ),
    Arm(
        "G_LEADER_PLUS_V08_PIVOT_ONLY",
        "v08",
        combined=True,
        target=V08TargetPolicy.PIVOT_ONLY,
        context=V08ContextPolicy.NONE,
    ),
    Arm(
        "H_LEADER_PLUS_V08_PIVOT_ONLY_NOT_OPPOSED_PRIMARY",
        "v08",
        combined=True,
        target=V08TargetPolicy.PIVOT_ONLY,
        context=V08ContextPolicy.NOT_OPPOSED,
        primary=True,
    ),
    Arm(
        "I_LEADER_PLUS_V08_PIVOT_ONLY_STRICT_DELIVERY",
        "v08",
        combined=True,
        target=V08TargetPolicy.PIVOT_ONLY,
        context=V08ContextPolicy.STRICT_DELIVERY,
    ),
    Arm(
        "J_LEADER_PLUS_V08_CONFLUENT_PIVOT_NOT_OPPOSED",
        "v08",
        combined=True,
        target=V08TargetPolicy.PIVOT_ZONE_CONFLUENT,
        context=V08ContextPolicy.NOT_OPPOSED,
    ),
    Arm("K_LEADER_PLUS_V07_ANY_TARGET", "v07", combined=True),
)
LEADER = ARMS[0].name
PRIMARY_STANDALONE = ARMS[3].name
PRIMARY_COMBINED = ARMS[7].name


@dataclass(frozen=True, slots=True)
class Split:
    name: str
    windows: tuple[tuple[object, ...], ...]
    contexts: tuple[WindowContext, ...]
    authority_sets: dict[str, dict[int, tuple[object, ...]]]
    diagnostics: tuple[dict[str, object], ...]


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
        default=Path("results/easychart_v08_target_ownership"),
    )
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.03)
    return parser.parse_args()


def _policy_key(
    target: V08TargetPolicy,
    context: V08ContextPolicy,
) -> str:
    return f"{target.value}|{context.value}"


def _policies() -> tuple[tuple[V08TargetPolicy, V08ContextPolicy], ...]:
    return tuple(
        sorted(
            {
                (arm.target, arm.context)
                for arm in ARMS
                if arm.source == "v08"
            },
            key=lambda item: (item[0].value, item[1].value),
        )
    )  # type: ignore[union-attr]


def _in_evaluation(
    authority: object,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> bool:
    return start <= authority.known_at < end  # type: ignore[attr-defined]


def _prepare(
    name: str,
    windows: Sequence[tuple[object, ...]],
    data_dir: Path,
) -> Split:
    frames = {
        str(row[0]): _load_source(data_dir / f"{row[0]}_5m.csv")
        for row in windows
    }
    contexts: list[WindowContext] = []
    sets: dict[str, dict[int, tuple[object, ...]]] = {
        "leader": {},
        "v07": {},
    }
    for target, context in _policies():
        sets[_policy_key(target, context)] = {}
    diagnostics: list[dict[str, object]] = []

    for index, (symbol, environment, start, end, tick_size) in enumerate(windows):
        evaluation_start, evaluation_end = _utc(start), _utc(end)
        warmup_start = evaluation_start - pd.Timedelta(days=WARMUP_DAYS)
        candles = _window(
            frames[str(symbol)],
            warmup_start.isoformat(),
            str(end),
        )
        if candles.empty:
            raise ValueError(
                f"{name}/{environment}: empty warm-up/evaluation window"
            )
        book = build_feature_book(
            candles,
            symbol=str(symbol),
            tick_size=float(tick_size),
        )
        baseline = tuple(
            item
            for item in build_baseline_event_authorities(book)
            if item.confirmation.subtype is B1Subtype.BREAK_RETEST
            and _in_evaluation(item, evaluation_start, evaluation_end)
        )
        v05 = tuple(
            item
            for item in build_m15_m5_liquidity_delivery_result(book).authorities
            if _in_evaluation(item, evaluation_start, evaluation_end)
        )
        leader = tuple(
            sorted(
                (*baseline, *v05),
                key=lambda item: (item.known_at, item.authority_id),
            )
        )
        v07_result = build_v07_scene_family_result(book)
        v07 = tuple(
            item
            for item in v07_result.authorities
            if _in_evaluation(item, evaluation_start, evaluation_end)
        )
        sets["leader"][index], sets["v07"][index] = leader, v07
        diagnostics.append(
            {
                "split": name,
                "window_index": index,
                "symbol": symbol,
                "environment": environment,
                "policy": "v07_baseline",
                **asdict(v07_result.diagnostics),
            }
        )

        for target, context in _policies():
            result = build_v08_scene_family_result(
                book,
                target_policy=target,
                context_policy=context,
                baseline=v07_result,
            )
            selected = tuple(
                item
                for item in result.authorities
                if _in_evaluation(item, evaluation_start, evaluation_end)
            )
            sets[_policy_key(target, context)][index] = selected
            diagnostics.append(
                {
                    "split": name,
                    "window_index": index,
                    "symbol": symbol,
                    "environment": environment,
                    "policy": _policy_key(target, context),
                    **asdict(result.diagnostics),
                    "evaluation_authorities": len(selected),
                }
            )

        contexts.append(
            WindowContext(
                index=index,
                symbol=str(symbol),
                environment=str(environment),
                start=evaluation_start,
                end=evaluation_end,
                tick_size=float(tick_size),
                candles=candles,
                book=book,
                leader=leader,
                v07=v07,
            )
        )
        print(
            f"prepared {name}/{environment}: bars={len(candles)} "
            f"leader={len(leader)} v07={len(v07)} warmup={WARMUP_DAYS}d",
            flush=True,
        )

    return Split(
        name,
        tuple(tuple(row) for row in windows),
        tuple(contexts),
        sets,
        tuple(diagnostics),
    )


def _scaled_costs(scale: float) -> CostConfig:
    return replace(
        COSTS,
        entry_fee_rate=COSTS.entry_fee_rate * scale,
        stop_fee_rate=COSTS.stop_fee_rate * scale,
        target_fee_rate=COSTS.target_fee_rate * scale,
        volume_exit_fee_rate=COSTS.volume_exit_fee_rate * scale,
        stop_slippage_bps=COSTS.stop_slippage_bps * scale,
        volume_exit_slippage_bps=COSTS.volume_exit_slippage_bps * scale,
    )


def _contexts(split: Split, arm: Arm) -> tuple[WindowContext, ...]:
    key = (
        "leader"
        if arm.source == "leader"
        else "v07"
        if arm.source == "v07"
        else _policy_key(arm.target, arm.context)  # type: ignore[arg-type]
    )
    return tuple(
        replace(
            context,
            v07=split.authority_sets[key][context.index],
        )
        for context in split.contexts
    )


def _authority_scope(arm: Arm) -> str:
    return (
        "leader"
        if arm.source == "leader"
        else "combined"
        if arm.combined
        else "v07"
    )


def _lookup(
    metrics: Sequence[dict[str, object]],
    arm: str,
    scale: float,
) -> dict[str, object]:
    return next(
        row
        for row in metrics
        if row["split"] == "holdout"
        and row["arm"] == arm
        and math.isclose(float(row["cost_scale"]), scale)
    )


def _daily(metric: dict[str, object]) -> float:
    return math.expm1(
        float(metric["net_log_growth"])
        / int(metric["portfolio_operating_days"])
    )


def _decision(
    metrics: Sequence[dict[str, object]],
) -> dict[str, object]:
    lb, ls = _lookup(metrics, LEADER, 1.0), _lookup(metrics, LEADER, 1.5)
    sb, ss = (
        _lookup(metrics, PRIMARY_STANDALONE, 1.0),
        _lookup(metrics, PRIMARY_STANDALONE, 1.5),
    )
    cb, cs = (
        _lookup(metrics, PRIMARY_COMBINED, 1.0),
        _lookup(metrics, PRIMARY_COMBINED, 1.5),
    )
    conditions = {
        "standalone_positive_base": float(sb["net_r"]) > 0,
        "standalone_positive_1_5x_cost": float(ss["net_r"]) > 0,
        "combined_beats_leader_base": float(cb["net_log_growth"])
        > float(lb["net_log_growth"]),
        "combined_beats_leader_1_5x_cost": float(cs["net_log_growth"])
        > float(ls["net_log_growth"]),
        "drawdown_increase_at_most_2pp": float(cb["max_drawdown_fraction"])
        <= float(lb["max_drawdown_fraction"]) + 0.02,
        "completed_trades_at_least_portfolio_days": int(cb["trades"])
        >= int(cb["portfolio_operating_days"]),
        "geometric_daily_return_at_least_1pct": _daily(cb) >= 0.01,
    }
    robust = all(conditions[key] for key in tuple(conditions)[:5])
    target = (
        robust
        and conditions["completed_trades_at_least_portfolio_days"]
        and conditions["geometric_daily_return_at_least_1pct"]
    )
    return {
        "status": (
            "TARGET_GATE_MET"
            if target
            else "RESEARCH_LEADER_CANDIDATE"
            if robust
            else "REJECT_PRIMARY_V08"
        ),
        "primary_standalone": PRIMARY_STANDALONE,
        "primary_combined": PRIMARY_COMBINED,
        "holdout_geometric_daily_return": _daily(cb),
        "conditions": conditions,
        "risk_optimization_allowed": False,
        "paper_live_allowed": False,
    }


def _report(
    metrics: Sequence[dict[str, object]],
    decision: dict[str, object],
) -> str:
    rows = [
        row
        for row in metrics
        if row["split"] == "holdout"
        and math.isclose(float(row["cost_scale"]), 1.0)
    ]
    lines = [
        "# EasyChart V0.8 유동성 소유 목표 비교",
        "",
        "사전 고정 주가설: `PIVOT_ONLY + NOT_OPPOSED`. 평가 전 28일은 "
        "feature warm-up일 뿐 거래·성과 분모에 포함하지 않았다.",
        "",
        "| arm | 거래 | net R | 누적수익 | MDD | 일기하수익 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['arm']} | {row['trades']} | "
            f"{float(row['net_r']):+.3f} | "
            f"{100*float(row['cumulative_return']):+.2f}% | "
            f"{100*float(row['max_drawdown_fraction']):.2f}% | "
            f"{100*_daily(row):+.4f}% |"
        )
    lines += ["", f"자동 판정: `{decision['status']}`", ""]
    lines += [
        f"- {key}: `{value}`"
        for key, value in dict(decision["conditions"]).items()
    ]
    lines += [
        "",
        "위 결과와 무관하게 위험률·레버리지·은행계좌 배분 최적화 및 "
        "paper/live 주문 권한은 동결한다.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    _install_trimmed_replay()
    args = _args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    risk = RiskConfig(
        risk_fraction=args.risk_fraction,
        quantity_step=0.001,
        daily_loss_limit_enabled=False,
    )
    splits = (
        _prepare("development", DEVELOPMENT_WINDOWS, args.data_dir),
        _prepare("holdout", HOLDOUT_WINDOWS, args.data_dir),
    )
    metrics: list[dict[str, object]] = []
    ledger: list[dict[str, object]] = []
    diagnostics = [row for split in splits for row in split.diagnostics]

    for split in splits:
        instrument_days = _instrument_days(split.windows)
        portfolio_days = len(_portfolio_operating_dates(split.windows))
        for scale in (1.0, 1.5, 2.0):
            costs = _scaled_costs(scale)
            for arm in ARMS:
                result = _run_global_arm(
                    _contexts(split, arm),
                    arm=arm.name,
                    authority_scope=_authority_scope(arm),
                    execution_arm=(
                        "leader_locked_limit"
                        if arm.source == "leader"
                        else BOUNDARY_ACCEPT_NEXT_OPEN
                    ),
                    initial_equity=args.initial_equity,
                    costs=costs,
                    risk=risk,
                    assemble_v07_opportunity=assemble_v07_opportunity,
                )
                rows: list[dict[str, object]] = []
                for closed in result.closed_attempts:
                    row = _trade_row(
                        arm=arm.name,
                        scope="global_portfolio",
                        entry_arm=(
                            "leader_locked_limit"
                            if arm.source == "leader"
                            else BOUNDARY_ACCEPT_NEXT_OPEN
                        ),
                        window_index=closed.context.index,
                        symbol=closed.context.symbol,
                        environment=closed.context.environment,
                        candles=closed.context.candles,
                        attempt=closed.attempt,
                        authority=closed.authority,
                    )
                    row.update(
                        {
                            "split": split.name,
                            "cost_scale": scale,
                            "target_policy": (
                                None
                                if arm.target is None
                                else arm.target.value
                            ),
                            "context_policy": (
                                None
                                if arm.context is None
                                else arm.context.value
                            ),
                        }
                    )
                    rows.append(row)
                    ledger.append(row)
                metric = _metrics(
                    rows,
                    initial_equity=args.initial_equity,
                    instrument_days=instrument_days,
                    portfolio_operating_days=portfolio_days,
                    equity_scope="global_portfolio",
                )
                metric.update(
                    {
                        "split": split.name,
                        "cost_scale": scale,
                        "arm": arm.name,
                        "authority_source": arm.source,
                        "combined": arm.combined,
                        "primary": arm.primary,
                        "target_policy": (
                            None if arm.target is None else arm.target.value
                        ),
                        "context_policy": (
                            None if arm.context is None else arm.context.value
                        ),
                        "final_equity": result.final_equity,
                        "opportunity_rejections": result.opportunity_rejections,
                        "sizing_rejections": result.sizing_rejections,
                        "pending_cancellations": result.pending_cancellations,
                        "entry_rejections": result.entry_rejections,
                        "open_censored": result.open_censored,
                        "entry_censored": result.entry_censored,
                        "slot_suppressed_authorities": (
                            result.slot_suppressed_authorities
                        ),
                        "final_reason_counts": dict(
                            Counter(row["final_reason"] for row in rows)
                        ),
                    }
                )
                metrics.append(metric)
                print(
                    f"{split.name} cost={scale:.1f} {arm.name}: "
                    f"trades={metric['trades']} netR={metric['net_r']:+.3f} "
                    f"return={100*float(metric['cumulative_return']):+.2f}%",
                    flush=True,
                )

    decision = _decision(metrics)
    payload = {
        "strategy_version": "easychart_v08_liquidity_owned_terminal_target",
        "design": {
            "development_windows": DEVELOPMENT_WINDOWS,
            "holdout_windows": HOLDOUT_WINDOWS,
            "feature_warmup_days": WARMUP_DAYS,
            "primary_standalone": PRIMARY_STANDALONE,
            "primary_combined": PRIMARY_COMBINED,
            "risk_fraction": args.risk_fraction,
            "cost_scales": [1.0, 1.5, 2.0],
        },
        "decision": decision,
        "metrics": metrics,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame.from_records(metrics).to_csv(
        args.output_dir / "metrics.csv",
        index=False,
    )
    pd.DataFrame.from_records(ledger).to_csv(
        args.output_dir / "trade_ledger.csv",
        index=False,
    )
    pd.DataFrame.from_records(diagnostics).to_csv(
        args.output_dir / "authority_diagnostics.csv",
        index=False,
    )
    (args.output_dir / "REPORT_KO.md").write_text(
        _report(metrics, decision),
        encoding="utf-8",
    )
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
