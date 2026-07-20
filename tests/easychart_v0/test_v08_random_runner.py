from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

from ictbt.easychart_v0.domain import PriceZone, Side


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "run_easychart_v08_random_trials_under_test",
    PROJECT_ROOT / "scripts" / "run_easychart_v08_random_trials.py",
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def frame(start: str, end: str) -> pd.DataFrame:
    index = pd.date_range(
        pd.Timestamp(start, tz="UTC"),
        pd.Timestamp(end, tz="UTC"),
        freq="5min",
        inclusive="left",
    )
    return pd.DataFrame(
        {
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
            "volume": 1.0,
        },
        index=index,
    )


def authority(authority_id: str, *, prefix: str = "leader"):
    return SimpleNamespace(
        authority_id=f"{prefix}-{authority_id}",
        known_at=pd.Timestamp("2025-01-01", tz="UTC"),
        side=Side.LONG,
        zone=PriceZone(100.0, 100.5),
        has_literal_body_overlap=False,
    )


def test_coverage_uses_shared_btc_eth_data_intersection() -> None:
    frames = {
        "BTCUSDT": frame("2021-12-01", "2027-01-01"),
        "ETHUSDT": frame("2022-01-15", "2026-07-20"),
    }

    coverages = runner._coverage_intersection(frames)

    assert coverages[0].available_start == pd.Timestamp("2022-01-15", tz="UTC")
    assert coverages[0].available_end == pd.Timestamp("2023-01-01", tz="UTC")
    assert coverages[-1].available_end == pd.Timestamp("2026-07-20", tz="UTC")


def test_semantic_dedup_prefers_v08_authority() -> None:
    leader = authority("same")
    upgraded = authority("same", prefix="v08")
    monkey_key = ("same-scene",)
    original = runner._semantic_key
    try:
        runner._semantic_key = lambda _item: monkey_key
        selected = runner._deduplicate_authorities((leader, upgraded))
    finally:
        runner._semantic_key = original

    assert len(selected) == 1
    assert selected[0].authority_id.startswith("v08-")
