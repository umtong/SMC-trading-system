from __future__ import annotations

import pandas as pd
import pytest

from scripts.download_binance_um_microstructure import (
    _archive_url,
    _checksum,
    _months,
)


def test_archive_urls_are_fixed_to_official_usdm_monthly_layout() -> None:
    assert _archive_url("aggTrades", "BTCUSDT", "2024-03") == (
        "https://data.binance.vision/data/futures/um/monthly/aggTrades/"
        "BTCUSDT/BTCUSDT-aggTrades-2024-03.zip"
    )
    assert _archive_url("fundingRate", "ETHUSDT", "2024-03") == (
        "https://data.binance.vision/data/futures/um/monthly/fundingRate/"
        "ETHUSDT/ETHUSDT-fundingRate-2024-03.zip"
    )
    assert _archive_url("markPriceKlines", "BTCUSDT", "2024-03") == (
        "https://data.binance.vision/data/futures/um/monthly/markPriceKlines/"
        "BTCUSDT/1m/BTCUSDT-1m-2024-03.zip"
    )


def test_month_contract_is_half_open_and_spans_boundaries() -> None:
    start = pd.Timestamp("2024-02-15", tz="UTC")
    end = pd.Timestamp("2024-04-01", tz="UTC")
    assert _months(start, end) == ("2024-02", "2024-03")


def test_checksum_parser_rejects_non_sha256_payload() -> None:
    digest = "a" * 64
    assert _checksum(f"{digest}  file.zip\n".encode()) == digest
    with pytest.raises(ValueError, match="invalid SHA-256"):
        _checksum(b"not-a-checksum")


def test_month_contract_rejects_empty_window() -> None:
    point = pd.Timestamp("2024-03-01", tz="UTC")
    with pytest.raises(ValueError, match="end must follow start"):
        _months(point, point)
