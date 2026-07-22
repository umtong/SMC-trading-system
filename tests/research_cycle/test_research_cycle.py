from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from smc_ict_research_cycle import (  # noqa: E402
    CASH,
    Candidate,
    HistoricalRevisionError,
    State,
    evaluate,
    load_ledger,
    run,
    verify_history,
    write_outputs,
)


def ledger(path: Path, rows: list[tuple[str, str, float]]) -> None:
    pd.DataFrame(rows, columns=["entry_time", "exit_time", "gross_return"]).to_csv(
        path, index=False
    )


def config(**overrides):
    payload = {
        "first_oos_start": "2026-01-01T00:00:00Z",
        "evaluation_end_exclusive": "2026-06-01T00:00:00Z",
        "cycle_months": 1,
        "base_cost_scenario": "base",
        "stress_cost_scenario": "stress",
        "cost_scenarios": [
            {"name": "base", "round_trip_bps": 0.0},
            {"name": "stress", "round_trip_bps": 0.0},
        ],
        "gates": {
            "minimum_completed_cycles": 1,
            "minimum_trades": 1,
            "base_profit_factor": 1.0,
            "stress_profit_factor": 1.0,
            "maximum_drawdown": 0.50,
            "minimum_positive_cycle_fraction": 0.50,
            "maximum_top_five_profit_share": 1.0,
            "require_leave_best_five_positive": False,
            "minimum_base_total_return": -1.0,
            "minimum_stress_total_return": -1.0,
            "consecutive_passes_for_promotion": 1,
            "consecutive_failures_for_demotion": 1,
        },
        "operational_gates": {
            "causality_required": True,
            "data_quality_required": True,
            "execution_model_required": True,
            "minimum_shadow_days": 0,
            "minimum_shadow_trades": 0,
            "maximum_reconciliation_errors": 0,
            "maximum_realized_slippage_bps_p95": 99.0,
        },
        "allow_cash": True,
    }
    payload.update(overrides)
    return payload


def attestation(execution=True):
    return {
        "causality_passed": True,
        "data_quality_passed": True,
        "execution_model_passed": execution,
        "shadow_days": 0,
        "shadow_trades": 0,
        "reconciliation_errors": 0,
        "realized_slippage_bps_p95": 0.0,
    }


def test_unmatured_trade_result_is_excluded(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    ledger(
        path,
        [
            ("2026-01-10T00:00:00Z", "2026-01-10T01:00:00Z", 0.02),
            ("2026-01-31T23:00:00Z", "2026-02-02T00:00:00Z", 0.50),
        ],
    )
    candidate = Candidate("TEST", path.name)
    frame = load_ledger(candidate, tmp_path)
    result = evaluate(candidate, frame, pd.Timestamp("2026-02-01T00:00:00Z"), config())
    assert result["metrics_by_scenario"]["base"]["trades"] == 1
    assert result["metrics_by_scenario"]["base"]["total_return"] == pytest.approx(0.02)


def test_future_append_preserves_prior_evidence(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    ledger(path, [("2026-01-10T00:00:00Z", "2026-01-10T01:00:00Z", 0.02)])
    candidate = Candidate("TEST", path.name)
    frame = load_ledger(candidate, tmp_path)
    state = run(config(), (candidate,), {"TEST": frame}, State(), attestation(), pd.Timestamp("2026-02-01T00:00:00Z"))
    ledger(
        path,
        [
            ("2026-01-10T00:00:00Z", "2026-01-10T01:00:00Z", 0.02),
            ("2026-03-10T00:00:00Z", "2026-03-10T01:00:00Z", 0.03),
        ],
    )
    verify_history(state, {"TEST": load_ledger(candidate, tmp_path)})


def test_historical_revision_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    ledger(path, [("2026-01-10T00:00:00Z", "2026-01-10T01:00:00Z", 0.02)])
    candidate = Candidate("TEST", path.name)
    frame = load_ledger(candidate, tmp_path)
    state = run(config(), (candidate,), {"TEST": frame}, State(), attestation(), pd.Timestamp("2026-02-01T00:00:00Z"))
    ledger(path, [("2026-01-10T00:00:00Z", "2026-01-10T01:00:00Z", 0.03)])
    with pytest.raises(HistoricalRevisionError):
        verify_history(state, {"TEST": load_ledger(candidate, tmp_path)})


def test_operational_gate_blocks_promotion(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    ledger(
        path,
        [
            ("2026-01-05T00:00:00Z", "2026-01-05T01:00:00Z", 0.03),
            ("2026-01-10T00:00:00Z", "2026-01-10T01:00:00Z", -0.01),
            ("2026-01-15T00:00:00Z", "2026-01-15T01:00:00Z", 0.03),
        ],
    )
    candidate = Candidate("TEST", path.name)
    frame = load_ledger(candidate, tmp_path)
    state = run(config(), (candidate,), {"TEST": frame}, State(), attestation(False), pd.Timestamp("2026-02-01T00:00:00Z"))
    assert state.champion == CASH
    assert state.decisions[-1]["challenger"] == "TEST"
    assert "live promotion blocked" in state.decisions[-1]["reason"]


def test_negative_candidate_keeps_cash(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    ledger(
        path,
        [
            ("2026-01-05T00:00:00Z", "2026-01-05T01:00:00Z", -0.02),
            ("2026-01-10T00:00:00Z", "2026-01-10T01:00:00Z", -0.01),
        ],
    )
    candidate = Candidate("TEST", path.name)
    frame = load_ledger(candidate, tmp_path)
    state = run(config(), (candidate,), {"TEST": frame}, State(), attestation(), pd.Timestamp("2026-02-01T00:00:00Z"))
    assert state.champion == CASH
    assert state.decisions[-1]["challenger"] == CASH


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    ledger(path, [("2026-01-05T00:00:00Z", "2026-01-05T01:00:00Z", 0.02)])
    candidate = Candidate("TEST", path.name)
    frame = load_ledger(candidate, tmp_path)
    state = run(config(), (candidate,), {"TEST": frame}, State(), attestation(), pd.Timestamp("2026-03-01T00:00:00Z"))
    output = tmp_path / "out"
    write_outputs(output, state)
    before = (output / "research_state.json").read_bytes()
    same = run(config(), (candidate,), {"TEST": frame}, state, attestation(), pd.Timestamp("2026-03-01T00:00:00Z"))
    write_outputs(output, same)
    assert (output / "research_state.json").read_bytes() == before
    json.loads(before)
