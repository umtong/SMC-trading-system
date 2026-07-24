from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ictbt.easychart_v0.domain import SceneFamily, Side
from ictbt.microstructure import FlowSceneKind, FrozenFlowScene
from ictbt.microstructure.scene_adapter import AdaptedFlowScene
from ictbt.microstructure.scene_manifest import (
    build_scene_manifest,
    record_from_adapted_scene,
)
from scripts.download_binance_um_scene_microstructure import (
    _daily_agg_url,
    _validate_daily_flow,
    _validate_scene_coverage,
)


def test_daily_aggregate_trade_url_is_official_usdm_layout() -> None:
    assert _daily_agg_url("BTCUSDT", "2024-03-03") == (
        "https://data.binance.vision/data/futures/um/daily/aggTrades/"
        "BTCUSDT/BTCUSDT-aggTrades-2024-03-03.zip"
    )


def minute_flow(day: str) -> pd.DataFrame:
    start = pd.Timestamp(day, tz="UTC")
    index = pd.date_range(start, start + pd.Timedelta(days=1), freq="1min", inclusive="left")
    return pd.DataFrame(
        {
            "symbol": "BTCUSDT",
            "open": 100.0,
            "high": 100.1,
            "low": 99.9,
            "close": 100.0,
            "base_volume": 1.0,
            "quote_volume": 100.0,
            "taker_buy_quote_volume": 50.0,
            "taker_sell_quote_volume": 50.0,
            "signed_quote_volume": 0.0,
            "aggregate_trade_count": 1,
            "underlying_trade_count": 1,
            "largest_aggregate_quote": 100.0,
            "vwap": 100.0,
            "price_change_bps": 0.0,
            "close_location_value": 0.0,
        },
        index=pd.DatetimeIndex(index, name="open_time"),
    )


def test_daily_flow_requires_every_minute() -> None:
    valid = minute_flow("2023-01-01")
    _validate_daily_flow(valid, symbol="BTCUSDT", day="2023-01-01")

    missing = valid.drop(valid.index[100])
    with pytest.raises(ValueError, match="misses 1 minutes"):
        _validate_daily_flow(missing, symbol="BTCUSDT", day="2023-01-01")


def _write(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = frame.reset_index()
    output["open_time"] = output["open_time"].map(lambda value: value.isoformat())
    output.to_csv(path, index=False, compression="gzip")


def test_scene_coverage_spans_midnight_and_rejects_missing_minute(tmp_path: Path) -> None:
    scene = FrozenFlowScene(
        scene_id="scene",
        symbol="BTCUSDT",
        side=Side.LONG,
        kind=FlowSceneKind.SWEEP_RECLAIM,
        node_price=100.0,
        known_at=pd.Timestamp("2023-01-02T00:30:00Z"),
        initial_stop=99.0,
        initial_target=102.0,
        tick_size=0.1,
    )
    adapted = AdaptedFlowScene(
        scene=scene,
        source_authority_id="scene",
        source_scene_family=SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST,
        source_target_id="target",
    )
    manifest = build_scene_manifest(
        (record_from_adapted_scene(adapted),),
        research_start="2023-01-01",
        research_end="2023-02-01",
        generated_at="2023-02-02T00:00:00Z",
    )
    base = tmp_path / "normalized" / "BTCUSDT"
    first = minute_flow("2023-01-01")
    second = minute_flow("2023-01-02")
    _write(first, base / "flow_1m_2023-01-01.csv.gz")
    _write(second, base / "flow_1m_2023-01-02.csv.gz")
    _validate_scene_coverage(manifest, tmp_path)

    second = second.drop(pd.Timestamp("2023-01-02T00:10:00Z"))
    _write(second, base / "flow_1m_2023-01-02.csv.gz")
    with pytest.raises(ValueError, match="lacks 1 causal flow minutes"):
        _validate_scene_coverage(manifest, tmp_path)
