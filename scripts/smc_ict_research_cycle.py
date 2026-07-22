#!/usr/bin/env python3
"""Causal, append-only champion/challenger cycle for SMC/ICT research.

Candidate ledgers must be produced by causal strategy code. This layer admits
only trades whose exits were known at a decision cutoff, stores prefix hashes
so historical evidence cannot be silently revised, applies fixed cost and
robustness gates, and keeps CASH when no candidate qualifies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

CASH = "CASH"
SCHEMA_VERSION = 1


class HistoricalRevisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    strategy_id: str
    ledger_path: str
    return_column: str = "gross_return"
    embedded_round_trip_bps: float = 0.0
    enabled: bool = True
    notes: str = ""


@dataclass
class State:
    champion: str = CASH
    pass_streaks: dict[str, int] = field(default_factory=dict)
    champion_fail_streak: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)


def utc(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("timestamp is missing")
    if timestamp.tz is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def canonical_hash(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    start = utc(config["first_oos_start"])
    end = utc(config["evaluation_end_exclusive"])
    if start >= end or int(config.get("cycle_months", 1)) <= 0:
        raise ValueError("invalid research-cycle date contract")
    names = {item["name"] for item in config["cost_scenarios"]}
    if config["base_cost_scenario"] not in names or config["stress_cost_scenario"] not in names:
        raise ValueError("base/stress cost scenario is absent")
    if not config.get("allow_cash", True):
        raise ValueError("CASH must remain a valid champion")
    return config


def load_registry(path: Path) -> tuple[Candidate, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = tuple(
        Candidate(**item)
        for item in payload["candidates"]
        if item.get("enabled", True)
    )
    identities = [item.strategy_id for item in candidates]
    if not candidates or len(identities) != len(set(identities)) or CASH in identities:
        raise ValueError("invalid candidate registry")
    return candidates


def load_ledger(candidate: Candidate, base_dir: Path) -> pd.DataFrame:
    source = (base_dir / candidate.ledger_path).resolve()
    frame = pd.read_csv(source)
    required = {"entry_time", "exit_time", candidate.return_column}
    if not required.issubset(frame.columns):
        raise ValueError(f"{candidate.strategy_id}: missing ledger columns")
    result = frame.copy()
    result["entry_time"] = pd.to_datetime(result["entry_time"], utc=True, errors="raise")
    result["exit_time"] = pd.to_datetime(result["exit_time"], utc=True, errors="raise")
    result["raw_return"] = pd.to_numeric(result[candidate.return_column], errors="raise")
    result = result.sort_values(["entry_time", "exit_time"], kind="stable").reset_index(drop=True)
    if (result["exit_time"] < result["entry_time"]).any() or not np.isfinite(result["raw_return"]).all():
        raise ValueError(f"{candidate.strategy_id}: invalid trade rows")
    previous_exit: pd.Timestamp | None = None
    for row in result.itertuples():
        if previous_exit is not None and row.entry_time < previous_exit:
            raise ValueError(f"{candidate.strategy_id}: overlapping trades")
        previous_exit = row.exit_time
    return result


def prefix_hash(frame: pd.DataFrame, cutoff: pd.Timestamp) -> str:
    mature = frame.loc[
        frame["exit_time"] <= cutoff,
        ["entry_time", "exit_time", "raw_return"],
    ].copy()
    mature["entry_time"] = mature["entry_time"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    mature["exit_time"] = mature["exit_time"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return canonical_hash(mature.to_dict(orient="records"))


def compound(values: np.ndarray) -> float:
    return float(np.prod(1.0 + values) - 1.0) if len(values) else 0.0


def maximum_drawdown(values: np.ndarray) -> float:
    equity = np.cumprod(1.0 + values) if len(values) else np.array([1.0])
    curve = np.concatenate(([1.0], equity))
    peaks = np.maximum.accumulate(curve)
    return float(np.max((peaks - curve) / peaks))


def profit_factor(values: np.ndarray) -> float | None:
    wins = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    if losses == 0:
        return None if wins == 0 else 1e12
    return wins / losses


def metrics(frame: pd.DataFrame, values: np.ndarray) -> dict[str, Any]:
    positives = np.sort(values[values > 0])[::-1]
    gross_positive = float(positives.sum())
    remaining = np.sort(values)[::-1][min(5, len(values)) :]
    return {
        "trades": int(len(values)),
        "total_return": compound(values),
        "mean_trade": None if not len(values) else float(values.mean()),
        "win_rate": None if not len(values) else float((values > 0).mean()),
        "profit_factor": profit_factor(values),
        "maximum_drawdown": maximum_drawdown(values),
        "active_days": int(frame["exit_time"].dt.floor("D").nunique()),
        "top_five_profit_share": (
            None
            if gross_positive == 0
            else float(positives[:5].sum()) / gross_positive
        ),
        "leave_best_five_total_return": compound(remaining),
    }


def cycle_windows(config: Mapping[str, Any]) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    start = utc(config["first_oos_start"]).normalize()
    end = utc(config["evaluation_end_exclusive"]).normalize()
    months = int(config.get("cycle_months", 1))
    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + pd.DateOffset(months=months), end)
        windows.append((cursor, next_cursor))
        cursor = next_cursor
    return windows


def evaluate(
    candidate: Candidate,
    frame: pd.DataFrame,
    cutoff: pd.Timestamp,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    first_oos = utc(config["first_oos_start"])
    mature = frame.loc[
        (frame["entry_time"] >= first_oos) & (frame["exit_time"] <= cutoff)
    ].copy()
    by_scenario: dict[str, dict[str, Any]] = {}
    values_by_name: dict[str, np.ndarray] = {}
    for scenario in config["cost_scenarios"]:
        extra_bps = float(scenario["round_trip_bps"]) - float(
            candidate.embedded_round_trip_bps
        )
        if extra_bps < 0:
            raise ValueError("cost scenario is below embedded ledger costs")
        values = mature["raw_return"].to_numpy(float) - extra_bps / 10_000.0
        values_by_name[scenario["name"]] = values
        by_scenario[scenario["name"]] = metrics(mature, values)

    base_name = config["base_cost_scenario"]
    base_values = values_by_name[base_name]
    cycle_returns: list[float] = []
    for start, end in cycle_windows(config):
        if start >= cutoff:
            break
        selected = mature.loc[
            (mature["entry_time"] >= start)
            & (mature["entry_time"] < min(end, cutoff))
        ]
        indices = selected.index.to_numpy()
        if len(indices):
            positions = mature.index.get_indexer(indices)
            cycle_returns.append(compound(base_values[positions]))
        else:
            cycle_returns.append(0.0)

    positive_fraction = (
        None
        if not cycle_returns
        else sum(value > 0 for value in cycle_returns) / len(cycle_returns)
    )
    gates = config["gates"]
    base = by_scenario[base_name]
    stress = by_scenario[config["stress_cost_scenario"]]
    checks = [
        (len(cycle_returns) >= int(gates["minimum_completed_cycles"]), "completed cycles"),
        (base["trades"] >= int(gates["minimum_trades"]), "trade count"),
        (base["profit_factor"] is not None and base["profit_factor"] >= float(gates["base_profit_factor"]), "base PF"),
        (stress["profit_factor"] is not None and stress["profit_factor"] >= float(gates["stress_profit_factor"]), "stress PF"),
        (base["maximum_drawdown"] <= float(gates["maximum_drawdown"]), "drawdown"),
        (positive_fraction is not None and positive_fraction >= float(gates["minimum_positive_cycle_fraction"]), "positive cycle fraction"),
        (base["top_five_profit_share"] is not None and base["top_five_profit_share"] <= float(gates["maximum_top_five_profit_share"]), "profit concentration"),
        (not gates.get("require_leave_best_five_positive", True) or base["leave_best_five_total_return"] > 0, "leave-best-five"),
        (base["total_return"] > float(gates.get("minimum_base_total_return", 0.0)), "base total return"),
        (stress["total_return"] > float(gates.get("minimum_stress_total_return", 0.0)), "stress total return"),
    ]
    reasons = [name for passed, name in checks if not passed]
    cost_decay = max(0.0, base["total_return"] - stress["total_return"])
    worst_cycle = min(cycle_returns) if cycle_returns else 0.0
    score = (
        (float(np.median(cycle_returns)) if cycle_returns else -1e9)
        - float(config.get("score_drawdown_penalty", 0.5))
        * base["maximum_drawdown"]
        - float(config.get("score_worst_cycle_penalty", 0.25))
        * abs(min(0.0, worst_cycle))
        - float(config.get("score_cost_decay_penalty", 0.25)) * cost_decay
    )
    return {
        "strategy_id": candidate.strategy_id,
        "cutoff": cutoff.isoformat(),
        "completed_cycles": len(cycle_returns),
        "cycle_returns": cycle_returns,
        "positive_cycle_fraction": positive_fraction,
        "metrics_by_scenario": by_scenario,
        "robust_score": score,
        "gate_passed": not reasons,
        "gate_reasons": reasons,
        "evidence_prefix_hash": prefix_hash(frame, cutoff),
    }


def operational_failures(
    attestation: Mapping[str, Any], config: Mapping[str, Any]
) -> list[str]:
    gates = config["operational_gates"]
    reasons: list[str] = []
    for flag, required in (
        ("causality_passed", gates.get("causality_required", True)),
        ("data_quality_passed", gates.get("data_quality_required", True)),
        ("execution_model_passed", gates.get("execution_model_required", True)),
    ):
        if required and not attestation.get(flag, False):
            reasons.append(flag)
    if int(attestation.get("shadow_days", 0)) < int(gates["minimum_shadow_days"]):
        reasons.append("shadow_days")
    if int(attestation.get("shadow_trades", 0)) < int(gates["minimum_shadow_trades"]):
        reasons.append("shadow_trades")
    if int(attestation.get("reconciliation_errors", 0)) > int(
        gates["maximum_reconciliation_errors"]
    ):
        reasons.append("reconciliation_errors")
    slippage = attestation.get("realized_slippage_bps_p95")
    if slippage is None or float(slippage) > float(
        gates["maximum_realized_slippage_bps_p95"]
    ):
        reasons.append("realized_slippage_bps_p95")
    return reasons


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported state schema")
    return State(
        champion=payload.get("champion", CASH),
        pass_streaks={
            key: int(value) for key, value in payload.get("pass_streaks", {}).items()
        },
        champion_fail_streak=int(payload.get("champion_fail_streak", 0)),
        decisions=list(payload.get("decisions", [])),
    )


def verify_history(state: State, frames: Mapping[str, pd.DataFrame]) -> None:
    for decision in state.decisions:
        cutoff = utc(decision["decision_cutoff"])
        for strategy_id, expected in decision["evidence_hashes"].items():
            observed = prefix_hash(frames[strategy_id], cutoff)
            if observed != expected:
                raise HistoricalRevisionError(
                    f"historical evidence changed: {strategy_id} at {cutoff}"
                )


def run(
    config: Mapping[str, Any],
    candidates: Sequence[Candidate],
    frames: Mapping[str, pd.DataFrame],
    state: State,
    attestation: Mapping[str, Any],
    maximum_cutoff: pd.Timestamp | None = None,
) -> State:
    verify_history(state, frames)
    existing = {item["decision_cutoff"] for item in state.decisions}
    operational_blocks = operational_failures(attestation, config)
    for index, (cutoff, deployment_end) in enumerate(cycle_windows(config), start=1):
        if cutoff <= utc(config["first_oos_start"]):
            continue
        if maximum_cutoff is not None and cutoff > maximum_cutoff:
            break
        if cutoff.isoformat() in existing:
            continue
        evaluations = [
            evaluate(candidate, frames[candidate.strategy_id], cutoff, config)
            for candidate in candidates
        ]
        passed = [item for item in evaluations if item["gate_passed"]]
        challenger = (
            max(passed, key=lambda item: (item["robust_score"], item["strategy_id"]))[
                "strategy_id"
            ]
            if passed
            else CASH
        )
        before = state.champion
        after = before
        reasons: list[str] = []
        for item in evaluations:
            strategy_id = item["strategy_id"]
            state.pass_streaks[strategy_id] = (
                state.pass_streaks.get(strategy_id, 0) + 1
                if item["gate_passed"]
                else 0
            )
        if before != CASH:
            current = next(
                item for item in evaluations if item["strategy_id"] == before
            )
            state.champion_fail_streak = (
                0 if current["gate_passed"] else state.champion_fail_streak + 1
            )
            if state.champion_fail_streak >= int(
                config["gates"]["consecutive_failures_for_demotion"]
            ):
                after = CASH
                reasons.append("champion demoted after consecutive gate failures")
                state.champion_fail_streak = 0
        if challenger != CASH:
            streak = state.pass_streaks[challenger]
            if streak >= int(config["gates"]["consecutive_passes_for_promotion"]):
                if operational_blocks:
                    reasons.append(
                        "live promotion blocked by operational gates: "
                        + ",".join(operational_blocks)
                    )
                else:
                    after = challenger
                    reasons.append(
                        f"promoted {challenger} after {streak} consecutive passes"
                    )
            else:
                reasons.append(f"challenger {challenger} streak {streak}")
        else:
            reasons.append("no candidate passed; keep CASH/champion")
        state.champion = after
        state.decisions.append(
            {
                "cycle_index": index,
                "decision_cutoff": cutoff.isoformat(),
                "deployment_start": cutoff.isoformat(),
                "deployment_end_exclusive": deployment_end.isoformat(),
                "champion_before": before,
                "challenger": challenger,
                "champion_after": after,
                "reason": "; ".join(reasons),
                "candidate_evaluations": evaluations,
                "evidence_hashes": {
                    item["strategy_id"]: item["evidence_prefix_hash"]
                    for item in evaluations
                },
                "config_hash": canonical_hash(config),
                "registry_hash": canonical_hash(
                    [item.__dict__ for item in candidates]
                ),
            }
        )
    return state


def write_outputs(path: Path, state: State) -> None:
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "champion": state.champion,
        "pass_streaks": state.pass_streaks,
        "champion_fail_streak": state.champion_fail_streak,
        "decisions": state.decisions,
    }
    (path / "research_state.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                key: item.get(key)
                for key in (
                    "cycle_index",
                    "decision_cutoff",
                    "deployment_end_exclusive",
                    "champion_before",
                    "challenger",
                    "champion_after",
                    "reason",
                )
            }
            for item in state.decisions
        ]
    ).to_csv(path / "decision_ledger.csv", index=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--attestation")
    parser.add_argument("--state")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--maximum-cutoff")
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    registry_path = Path(args.registry).resolve()
    output = Path(args.output_dir).resolve()
    config = load_config(config_path)
    candidates = load_registry(registry_path)
    frames = {
        item.strategy_id: load_ledger(item, registry_path.parent)
        for item in candidates
    }
    state_path = (
        Path(args.state).resolve()
        if args.state
        else output / "research_state.json"
    )
    attestation = (
        json.loads(Path(args.attestation).read_text(encoding="utf-8"))
        if args.attestation
        else {}
    )
    state = run(
        config,
        candidates,
        frames,
        load_state(state_path),
        attestation,
        None if not args.maximum_cutoff else utc(args.maximum_cutoff),
    )
    write_outputs(output, state)
    print(json.dumps({"champion": state.champion, "decisions": len(state.decisions)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
