from __future__ import annotations

from pathlib import Path
import hashlib
import json

import pandas as pd
import pytest

from ictbt.easychart_v0.domain import SceneFamily, Side
from ictbt.microstructure import DualClockSceneKind, FrozenDualClockScene
from ictbt.microstructure.scene_adapter_v1 import AdaptedDualClockScene
from ictbt.microstructure.scene_manifest_v2 import (
    build_dual_clock_scene_manifest,
    record_from_dual_clock_scene,
)
from scripts.download_binance_um_scene_microstructure_v2 import (
    _daily_agg_url,
    _flow_path,
    _persist_sidecar,
    _reuse_day,
    _validate_daily_flow,
    _validate_scene_coverage,
    _write_csv_gzip,
)


def test_daily_aggregate_trade_url_is_official_usdm_layout() -> None:
    assert _daily_agg_url("BTCUSDT", "2024-03-03") == (
        "https://data.binance.vision/data/futures/um/daily/aggTrades/"
        "BTCUSDT/BTCUSDT-aggTrades-2024-03-03.zip"
    )


def minute_flow(day: str) -> pd.DataFrame:
    start = pd.Timestamp(day, tz="UTC")
    index = pd.date_range(
        start,
        start + pd.Timedelta(days=1),
        freq="1min",
        inclusive="left",
    )
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


def dual_clock_manifest():
    scene = FrozenDualClockScene(
        scene_id="scene",
        symbol="BTCUSDT",
        side=Side.LONG,
        kind=DualClockSceneKind.SWEEP_REVERSAL,
        node_price=100.0,
        event_started_at=pd.Timestamp("2023-01-02T00:02:00Z"),
        event_known_at=pd.Timestamp("2023-01-02T00:17:00Z"),
        confirmation_started_at=pd.Timestamp("2023-01-02T00:20:00Z"),
        confirmation_known_at=pd.Timestamp("2023-01-02T00:25:00Z"),
        initial_stop=99.0,
        initial_target=102.0,
        tick_size=0.1,
    )
    adapted = AdaptedDualClockScene(
        scene=scene,
        source_authority_id="authority",
        source_scene_family=SceneFamily.M15_OB_M5_LIQUIDITY_DELIVERY_FIRST_RETEST,
        source_target_id="target",
        source_event_id="event",
        source_confirmation_id="confirmation",
    )
    return build_dual_clock_scene_manifest(
        (record_from_dual_clock_scene(adapted),),
        research_start="2023-01-01",
        research_end="2023-02-01",
        generated_at="2023-02-02T00:00:00Z",
    )


def test_daily_flow_requires_every_minute() -> None:
    valid = minute_flow("2023-01-01")
    _validate_daily_flow(valid, symbol="BTCUSDT", day="2023-01-01")

    missing = valid.drop(valid.index[100])
    with pytest.raises(ValueError, match="misses 1 minutes"):
        _validate_daily_flow(missing, symbol="BTCUSDT", day="2023-01-01")


def test_dual_clock_scene_coverage_spans_midnight(tmp_path: Path) -> None:
    manifest = dual_clock_manifest()
    first = minute_flow("2023-01-01")
    second = minute_flow("2023-01-02")
    _write_csv_gzip(first, _flow_path(tmp_path, "BTCUSDT", "2023-01-01"))
    _write_csv_gzip(second, _flow_path(tmp_path, "BTCUSDT", "2023-01-02"))

    _validate_scene_coverage(manifest, tmp_path)

    second = second.drop(pd.Timestamp("2023-01-02T00:10:00Z"))
    _write_csv_gzip(second, _flow_path(tmp_path, "BTCUSDT", "2023-01-02"))
    with pytest.raises(ValueError, match="lacks 1 causal flow minutes"):
        _validate_scene_coverage(manifest, tmp_path)


def test_reuse_requires_matching_normalized_hash_and_identity(tmp_path: Path) -> None:
    day = "2023-01-01"
    path = _flow_path(tmp_path, "BTCUSDT", day)
    output = _write_csv_gzip(minute_flow(day), path)
    source = {
        "symbol": "BTCUSDT",
        "date": day,
        "url": _daily_agg_url("BTCUSDT", day),
        "checksum_url": _daily_agg_url("BTCUSDT", day) + ".CHECKSUM",
        "sha256": "a" * 64,
        "archive_bytes": 123,
    }
    _persist_sidecar(
        tmp_path,
        symbol="BTCUSDT",
        day=day,
        source=source,
        output=output,
    )

    reused = _reuse_day(tmp_path, "BTCUSDT", day)
    assert reused is not None
    reused_source, reused_output = reused
    assert reused_source["sha256"] == "a" * 64
    assert reused_output["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert reused_output["rows"] == 1440

    payload = path.read_bytes()
    path.write_bytes(payload + b"corruption")
    with pytest.raises(ValueError, match="normalized flow SHA-256 mismatch"):
        _reuse_day(tmp_path, "BTCUSDT", day)


def test_reuse_rejects_sidecar_identity_change(tmp_path: Path) -> None:
    day = "2023-01-01"
    path = _flow_path(tmp_path, "BTCUSDT", day)
    output = _write_csv_gzip(minute_flow(day), path)
    source = {
        "symbol": "BTCUSDT",
        "date": day,
        "url": _daily_agg_url("BTCUSDT", day),
        "checksum_url": _daily_agg_url("BTCUSDT", day) + ".CHECKSUM",
        "sha256": "b" * 64,
        "archive_bytes": 123,
    }
    _persist_sidecar(
        tmp_path,
        symbol="BTCUSDT",
        day=day,
        source=source,
        output=output,
    )
    sidecar = path.with_name(f"flow_1m_{day}.source.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["symbol"] = "ETHUSDT"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="identity mismatch"):
        _reuse_day(tmp_path, "BTCUSDT", day)
