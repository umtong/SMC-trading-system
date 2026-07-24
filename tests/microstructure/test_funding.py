from __future__ import annotations

import pandas as pd
import pytest

from ictbt.microstructure import normalize_funding_rates


def test_normalize_public_archive_funding_shape() -> None:
    source = pd.DataFrame(
        [
            [1_700_000_000_000, 8, 0.0001],
            [1_700_028_800_000, 8, -0.0002],
        ]
    )
    funding = normalize_funding_rates(source, symbol="btcusdt")

    assert funding.symbol.unique().tolist() == ["BTCUSDT"]
    assert funding.index.tz is not None
    assert funding.iloc[0].funding_interval_hours == pytest.approx(8.0)
    assert funding.iloc[1].funding_rate == pytest.approx(-0.0002)
    assert pd.isna(funding.iloc[0].mark_price)


def test_normalize_rest_funding_shape_preserves_mark_price() -> None:
    source = pd.DataFrame(
        {
            "symbol": ["ETHUSDT"],
            "fundingTime": [1_700_000_000_000],
            "fundingRate": ["0.00005"],
            "markPrice": ["2500.25"],
        }
    )
    funding = normalize_funding_rates(source, symbol="ETHUSDT")

    assert funding.iloc[0].funding_rate == pytest.approx(0.00005)
    assert funding.iloc[0].mark_price == pytest.approx(2500.25)
    assert pd.isna(funding.iloc[0].funding_interval_hours)


def test_normalize_funding_rejects_duplicate_times_or_symbol_mismatch() -> None:
    duplicate = pd.DataFrame(
        {
            "symbol": ["BTCUSDT", "BTCUSDT"],
            "fundingTime": [1_700_000_000_000, 1_700_000_000_000],
            "fundingRate": [0.0001, 0.0002],
            "markPrice": [40_000, 40_100],
        }
    )
    with pytest.raises(ValueError, match="unique"):
        normalize_funding_rates(duplicate, symbol="BTCUSDT")

    mismatch = duplicate.iloc[:1].copy()
    mismatch["symbol"] = "ETHUSDT"
    with pytest.raises(ValueError, match="unexpected symbol"):
        normalize_funding_rates(mismatch, symbol="BTCUSDT")


def test_normalize_funding_rejects_zero_interval_and_bad_mark() -> None:
    zero_interval = pd.DataFrame([[1_700_000_000_000, 0, 0.0001]])
    with pytest.raises(ValueError, match="positive"):
        normalize_funding_rates(zero_interval, symbol="BTCUSDT")

    bad_mark = pd.DataFrame(
        {
            "symbol": ["BTCUSDT"],
            "fundingTime": [1_700_000_000_000],
            "fundingRate": [0.0001],
            "markPrice": [0.0],
        }
    )
    with pytest.raises(ValueError, match="mark prices"):
        normalize_funding_rates(bad_mark, symbol="BTCUSDT")
