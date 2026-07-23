from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import PriceZone, Side


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "run_easychart_v09_random_trials_under_test",
    PROJECT_ROOT / "scripts" / "run_easychart_v09_random_trials.py",
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def authority(authority_id: str):
    return SimpleNamespace(
        authority_id=authority_id,
        known_at=pd.Timestamp("2025-01-01", tz="UTC"),
        side=Side.LONG,
        zone=PriceZone(100.0, 100.5),
        has_literal_body_overlap=False,
    )


def args(**overrides):
    values = {
        "risk_fraction": 0.03,
        "score_days_per_year": 28,
        "trials": 20,
        "maximum_notional_to_equity": 8.0,
        "output_dir": Path("results/easychart_v08_hardened_random_trials"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_v09_dedup_prefers_owned_intraday_over_older_scene(monkeypatch) -> None:
    leader = authority("leader-scene")
    hardened = authority("v08-hardened-scene")
    owned = authority("v09-owned-scene")
    monkeypatch.setattr(
        runner.hardened.base,
        "_semantic_key",
        lambda _authority: ("same-causal-scene",),
    )

    selected = runner._deduplicate_authorities((leader, hardened, owned))

    assert selected == (owned,)


def test_v09_runner_rejects_risk_or_trial_count_relaxation(monkeypatch) -> None:
    monkeypatch.setattr(
        runner,
        "_ORIGINAL_ARGS",
        lambda: args(risk_fraction=0.02),
    )
    with pytest.raises(SystemExit, match="0.03"):
        runner._strict_args()

    monkeypatch.setattr(
        runner,
        "_ORIGINAL_ARGS",
        lambda: args(trials=19),
    )
    with pytest.raises(SystemExit, match="at least 20"):
        runner._strict_args()


def test_v09_runner_moves_default_output_and_builds_margin_contract(monkeypatch) -> None:
    monkeypatch.setattr(runner, "_ORIGINAL_ARGS", lambda: args())

    parsed = runner._strict_args()

    assert parsed.output_dir == Path("results/easychart_v09_random_trials")
    assert runner._MARGIN_CONFIG.maximum_notional_to_equity == 8.0
    assert runner._MARGIN_CONFIG.minimum_stop_to_liquidation_r == 1.0
