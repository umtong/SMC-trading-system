#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd


def utc_series(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, utc=True, errors="raise")


def maximum_drawdown(factors: np.ndarray) -> float:
    if len(factors) == 0:
        return 0.0
    equity = np.cumprod(factors)
    curve = np.concatenate(([1.0], equity))
    peaks = np.maximum.accumulate(curve)
    return float(np.max((peaks - curve) / peaks))


def build_quarter_blocks(
    frame: pd.DataFrame,
    *,
    entry_column: str,
    return_column: str,
    evaluation_start: pd.Timestamp | None = None,
    evaluation_end: pd.Timestamp | None = None,
) -> tuple[list[np.ndarray], pd.PeriodIndex]:
    if frame.empty:
        raise ValueError("ledger is empty")
    entries = utc_series(frame[entry_column])
    returns = pd.to_numeric(frame[return_column], errors="raise").astype(float)
    if not np.isfinite(returns).all():
        raise ValueError("returns must be finite")
    start = entries.min() if evaluation_start is None else evaluation_start
    end = entries.max() if evaluation_end is None else evaluation_end
    if end < start:
        raise ValueError("evaluation_end precedes evaluation_start")
    start_period = start.tz_convert(None).to_period("Q")
    end_period = end.tz_convert(None).to_period("Q")
    quarters = pd.period_range(start_period, end_period, freq="Q")
    entry_period = entries.dt.tz_convert(None).dt.to_period("Q")
    blocks = [returns.loc[entry_period == quarter].to_numpy(float) for quarter in quarters]
    return blocks, quarters


def bootstrap_path_metrics(
    blocks: Sequence[np.ndarray],
    *,
    risk_fraction: float,
    simulations: int,
    block_quarters: int,
    seed: int,
) -> dict[str, float]:
    if simulations <= 0 or block_quarters <= 0:
        raise ValueError("simulations and block_quarters must be positive")
    if not blocks:
        raise ValueError("quarter blocks are empty")
    rng = np.random.default_rng(seed)
    start_count = max(1, len(blocks) - block_quarters + 1)
    starts = np.arange(start_count)
    finals = np.empty(simulations, dtype=float)
    drawdowns = np.empty(simulations, dtype=float)
    ruins = 0

    for simulation in range(simulations):
        selected: list[np.ndarray] = []
        while len(selected) < len(blocks):
            start = int(rng.choice(starts))
            selected.extend(blocks[start : start + block_quarters])
        selected = selected[: len(blocks)]
        values = (
            np.concatenate([item for item in selected if len(item)])
            if any(len(item) for item in selected)
            else np.array([], dtype=float)
        )
        factors = 1.0 + risk_fraction * values
        if np.any(factors <= 0):
            finals[simulation] = 0.0
            drawdowns[simulation] = 1.0
            ruins += 1
            continue
        finals[simulation] = float(np.prod(factors))
        drawdowns[simulation] = maximum_drawdown(factors)

    return {
        "p01_final_multiple": float(np.quantile(finals, 0.01)),
        "p05_final_multiple": float(np.quantile(finals, 0.05)),
        "median_final_multiple": float(np.median(finals)),
        "p95_final_multiple": float(np.quantile(finals, 0.95)),
        "median_max_drawdown": float(np.median(drawdowns)),
        "p95_max_drawdown": float(np.quantile(drawdowns, 0.95)),
        "p99_max_drawdown": float(np.quantile(drawdowns, 0.99)),
        "ruin_probability": ruins / simulations,
    }


def observed_metrics(values: np.ndarray, risk_fraction: float) -> dict[str, float]:
    factors = 1.0 + risk_fraction * values
    if np.any(factors <= 0):
        return {"final_multiple": 0.0, "maximum_drawdown": 1.0}
    return {
        "final_multiple": float(np.prod(factors)),
        "maximum_drawdown": maximum_drawdown(factors),
    }


def capacity_metrics(
    leverage_at_one_percent: np.ndarray,
    *,
    risk_fraction: float,
    venue_leverage_cap: float,
    margin_buffer_fraction: float,
    loss_buffer_multiples: float,
    minimum_bank_fraction: float,
    provisional_target_trading_fraction: float,
    capacity_quantile: float,
) -> dict[str, float | bool]:
    if len(leverage_at_one_percent) == 0:
        raise ValueError("leverage observations are required")
    q = float(np.quantile(leverage_at_one_percent, capacity_quantile, method="higher"))
    gross_notional_fraction = q * (risk_fraction / 0.01)
    buffered_margin_fraction = (
        gross_notional_fraction
        / venue_leverage_cap
        * (1.0 + margin_buffer_fraction)
    )
    required_trading_fraction = (
        buffered_margin_fraction + loss_buffer_multiples * risk_fraction
    )
    maximum_trading_fraction = 1.0 - minimum_bank_fraction
    target_trading_fraction = min(
        maximum_trading_fraction,
        max(provisional_target_trading_fraction, required_trading_fraction),
    )
    return {
        "leverage_at_one_percent_quantile": q,
        "gross_notional_fraction": gross_notional_fraction,
        "buffered_margin_fraction": buffered_margin_fraction,
        "required_trading_fraction": required_trading_fraction,
        "maximum_trading_fraction": maximum_trading_fraction,
        "target_trading_fraction": target_trading_fraction,
        "target_bank_fraction": 1.0 - target_trading_fraction,
        "capacity_passed": required_trading_fraction <= maximum_trading_fraction + 1e-12,
    }


def load_policy(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported live capital policy schema")
    return payload


def evaluate_grid(
    frame: pd.DataFrame,
    *,
    policy: dict[str, Any],
    return_column: str,
    entry_column: str,
    exit_column: str,
    leverage_column: str,
    simulations: int,
    seed: int,
    evaluation_start: pd.Timestamp | None = None,
    evaluation_end: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    required = {return_column, entry_column, exit_column, leverage_column}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"ledger missing columns: {missing}")
    work = frame.copy()
    work[entry_column] = utc_series(work[entry_column])
    work[exit_column] = utc_series(work[exit_column])
    work[return_column] = pd.to_numeric(work[return_column], errors="raise").astype(float)
    work[leverage_column] = pd.to_numeric(work[leverage_column], errors="raise").astype(float)
    if (work[exit_column] < work[entry_column]).any():
        raise ValueError("exit precedes entry")
    if not np.isfinite(work[[return_column, leverage_column]].to_numpy(float)).all():
        raise ValueError("ledger contains non-finite values")
    if (work[leverage_column] <= 0).any():
        raise ValueError("leverage_at_one_percent must be positive")
    work = work.sort_values([entry_column, exit_column], kind="stable").reset_index(drop=True)
    if evaluation_start is not None:
        work = work.loc[work[entry_column] >= evaluation_start]
    if evaluation_end is not None:
        work = work.loc[work[entry_column] < evaluation_end]
    if work.empty:
        raise ValueError("no trades in evaluation range")

    blocks, quarters = build_quarter_blocks(
        work,
        entry_column=entry_column,
        return_column=return_column,
        evaluation_start=evaluation_start,
        evaluation_end=(
            evaluation_end - pd.Timedelta(nanoseconds=1)
            if evaluation_end is not None
            else None
        ),
    )
    values = work[return_column].to_numpy(float)
    leverage_values = work[leverage_column].to_numpy(float)
    risk_config = policy["risk_optimization"]
    treasury = policy["treasury"]
    rows: list[dict[str, Any]] = []

    for ordinal, risk_fraction in enumerate(risk_config["candidate_risk_fractions"]):
        risk = float(risk_fraction)
        bootstrap = bootstrap_path_metrics(
            blocks,
            risk_fraction=risk,
            simulations=simulations,
            block_quarters=int(risk_config["block_bootstrap_quarters"]),
            seed=seed + ordinal * 1009,
        )
        capacity = capacity_metrics(
            leverage_values,
            risk_fraction=risk,
            venue_leverage_cap=float(risk_config["venue_leverage_cap"]),
            margin_buffer_fraction=float(treasury["margin_buffer_fraction"]),
            loss_buffer_multiples=float(treasury["loss_buffer_multiples"]),
            minimum_bank_fraction=float(treasury["minimum_bank_fraction"]),
            provisional_target_trading_fraction=float(
                treasury["provisional_target_trading_fraction"]
            ),
            capacity_quantile=0.99,
        )
        observed = observed_metrics(values, risk)
        passed = (
            bootstrap["p95_max_drawdown"]
            <= float(risk_config["maximum_p95_drawdown"])
            and bootstrap["p05_final_multiple"]
            > float(risk_config["minimum_p05_final_multiple"])
            and bootstrap["ruin_probability"]
            <= float(risk_config["maximum_ruin_probability"])
            and bool(capacity["capacity_passed"])
        )
        rows.append(
            {
                "risk_fraction": risk,
                "risk_percent": risk * 100,
                **observed,
                **bootstrap,
                **capacity,
                "constraints_passed": passed,
            }
        )

    grid = pd.DataFrame(rows).sort_values("risk_fraction").reset_index(drop=True)
    eligible = grid.loc[grid["constraints_passed"]]
    research_optimum = None
    if not eligible.empty:
        selected = eligible.sort_values(
            ["median_final_multiple", "risk_fraction"],
            ascending=[False, True],
        ).iloc[0]
        research_optimum = selected.to_dict()

    first_day = work[entry_column].min().normalize()
    last_day = work[exit_column].max().normalize()
    operating_days = int((last_day - first_day).days) + 1
    frequency = {
        "completed_trades": int(len(work)),
        "complete_operating_days": operating_days,
        "trades_per_operating_day": len(work) / operating_days,
        "recommended_minimum": float(
            policy["frequency"]["recommended_completed_trades_per_complete_operating_day"]
        ),
        "recommendation_met": (
            len(work) / operating_days
            >= float(
                policy["frequency"][
                    "recommended_completed_trades_per_complete_operating_day"
                ]
            )
        ),
        "hard_gate": bool(policy["frequency"]["hard_gate"]),
    }
    report = {
        "schema": "smc.central_account_risk_optimization.v1",
        "trades": int(len(work)),
        "evaluation_start": work[entry_column].min().isoformat(),
        "evaluation_end": work[exit_column].max().isoformat(),
        "quarters": [str(item) for item in quarters],
        "active_quarters": int(sum(len(item) > 0 for item in blocks)),
        "simulations": simulations,
        "seed": seed,
        "frequency": frequency,
        "research_optimum": research_optimum,
        "deployment_contract": (
            "deployment risk remains zero unless the frozen strategy separately passes "
            "research, causality, execution, and shadow promotion gates"
        ),
    }
    return grid, report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--return-column", default="net_r")
    parser.add_argument("--entry-column", default="entry_time")
    parser.add_argument("--exit-column", default="exit_time")
    parser.add_argument("--leverage-column", default="leverage_at_1pct")
    parser.add_argument("--evaluation-start")
    parser.add_argument("--evaluation-end")
    parser.add_argument("--simulations", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument(
        "--strategy-eligible",
        action="store_true",
        help="set only after the frozen strategy passes every separate promotion gate",
    )
    args = parser.parse_args(argv)

    frame = pd.read_csv(args.ledger)
    policy = load_policy(args.policy)
    start = (
        None
        if args.evaluation_start is None
        else pd.Timestamp(args.evaluation_start, tz="UTC")
    )
    end = (
        None
        if args.evaluation_end is None
        else pd.Timestamp(args.evaluation_end, tz="UTC")
    )
    grid, report = evaluate_grid(
        frame,
        policy=policy,
        return_column=args.return_column,
        entry_column=args.entry_column,
        exit_column=args.exit_column,
        leverage_column=args.leverage_column,
        simulations=args.simulations,
        seed=args.seed,
        evaluation_start=start,
        evaluation_end=end,
    )
    report["strategy_eligible"] = bool(args.strategy_eligible)
    report["deployment_risk_fraction"] = (
        None
        if args.strategy_eligible and report["research_optimum"] is None
        else (
            float(report["research_optimum"]["risk_fraction"])
            if args.strategy_eligible and report["research_optimum"] is not None
            else 0.0
        )
    )
    report["deployment_risk_percent"] = report["deployment_risk_fraction"] * 100
    report["live_state"] = (
        "ELIGIBLE_RISK_SELECTED"
        if args.strategy_eligible and report["research_optimum"] is not None
        else "CASH_NO_LIVE_ORDER"
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    grid.to_csv(args.output_dir / "risk_grid.csv", index=False)
    (args.output_dir / "risk_optimization.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
