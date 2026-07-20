from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "download_binance_um_5m_under_test",
    PROJECT_ROOT / "scripts" / "download_binance_um_5m.py",
)
assert SPEC is not None and SPEC.loader is not None
downloader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = downloader
SPEC.loader.exec_module(downloader)


def test_archive_plan_uses_monthly_middle_and_daily_boundaries() -> None:
    requests = downloader.plan_archives(
        "BTCUSDT",
        start=pd.Timestamp("2025-01-15", tz="UTC"),
        end=pd.Timestamp("2025-03-10", tz="UTC"),
    )

    monthly = [item for item in requests if item.period == "monthly"]
    daily = [item for item in requests if item.period == "daily"]
    assert [item.label for item in monthly] == ["2025-02"]
    assert daily[0].label == "2025-01-15"
    assert daily[-1].label == "2025-03-09"
    assert monthly[0].url.endswith(
        "/futures/um/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-2025-02.zip"
    )


def test_parser_handles_headerless_millisecond_futures_klines() -> None:
    payload = (
        "1735689600000,100,102,99,101,12,1735689899999,0,10,0,0,0\n"
        "1735689900000,101,103,100,102,13,1735690199999,0,11,0,0,0\n"
    ).encode("utf-8")

    parsed = downloader._parse_member(payload)

    assert list(parsed.columns) == [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    assert parsed.iloc[0]["open_time"] == pd.Timestamp(
        "2025-01-01 00:00:00", tz="UTC"
    )
    assert parsed.iloc[1]["open_time"] == pd.Timestamp(
        "2025-01-01 00:05:00", tz="UTC"
    )
    assert parsed.iloc[1]["close"] == 102.0


def test_parser_drops_optional_csv_header() -> None:
    payload = (
        "open_time,open,high,low,close,volume,close_time,q,n,tb,tq,ignore\n"
        "1735689600000,100,102,99,101,12,1735689899999,0,10,0,0,0\n"
    ).encode("utf-8")

    parsed = downloader._parse_member(payload)

    assert len(parsed) == 1
    assert parsed.iloc[0]["open"] == 100.0
