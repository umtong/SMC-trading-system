from __future__ import annotations

import io
import zipfile

import pandas as pd
import pytest

from scripts.download_binance_um_5m_range import (
    _archive_url,
    _months,
    _read_archive,
    _validate_range,
)


def test_months_and_archive_url_use_half_open_official_contract() -> None:
    assert _months("2020-12-04", "2021-02-01") == (
        "2020-12",
        "2021-01",
    )
    assert _archive_url("BTCUSDT", "2021-01") == (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        "BTCUSDT/5m/BTCUSDT-5m-2021-01.zip"
    )


def archive_bytes(*, header: bool = False) -> bytes:
    rows = [
        [
            1_609_459_200_000,
            100.0,
            101.0,
            99.0,
            100.5,
            10.0,
            1_609_459_499_999,
            1000.0,
            10,
            5.0,
            500.0,
            0,
        ],
        [
            1_609_459_500_000,
            100.5,
            102.0,
            100.0,
            101.0,
            20.0,
            1_609_459_799_999,
            2000.0,
            20,
            10.0,
            1000.0,
            0,
        ],
    ]
    frame = pd.DataFrame(rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "BTCUSDT-5m-2021-01.csv",
            frame.to_csv(index=False, header=header),
        )
    return buffer.getvalue()


@pytest.mark.parametrize("header", [False, True])
def test_read_archive_accepts_official_header_variants(header: bool) -> None:
    frame = _read_archive(
        archive_bytes(header=header),
        symbol="BTCUSDT",
        month="2021-01",
    )

    assert len(frame) == 2
    assert frame.index[0] == pd.Timestamp("2021-01-01T00:00:00Z")
    assert frame.index[1] == pd.Timestamp("2021-01-01T00:05:00Z")
    assert frame.iloc[1].close == pytest.approx(101.0)


def test_validate_range_rejects_a_missing_five_minute_bar() -> None:
    frame = _read_archive(
        archive_bytes(),
        symbol="BTCUSDT",
        month="2021-01",
    )
    _validate_range(
        frame,
        symbol="BTCUSDT",
        start=pd.Timestamp("2021-01-01T00:00:00Z"),
        end=pd.Timestamp("2021-01-01T00:10:00Z"),
    )

    missing = frame.drop(pd.Timestamp("2021-01-01T00:05:00Z"))
    with pytest.raises(ValueError, match="missing 1"):
        _validate_range(
            missing,
            symbol="BTCUSDT",
            start=pd.Timestamp("2021-01-01T00:00:00Z"),
            end=pd.Timestamp("2021-01-01T00:10:00Z"),
        )


def test_read_archive_rejects_invalid_ohlc() -> None:
    payload = archive_bytes()
    with zipfile.ZipFile(io.BytesIO(payload)) as source:
        raw = pd.read_csv(source.open(source.namelist()[0]), header=None)
    raw.iloc[0, 2] = 98.0  # high below open/close
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bad.csv", raw.to_csv(index=False, header=False))
    with pytest.raises(ValueError, match="invalid OHLC"):
        _read_archive(buffer.getvalue(), symbol="BTCUSDT", month="2021-01")
