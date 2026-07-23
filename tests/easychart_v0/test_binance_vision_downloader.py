from __future__ import annotations

import csv
import hashlib
import io
import zipfile

from scripts.download_binance_vision_klines import (
    _expected_sha256,
    _month_range,
    _normalize_epoch_to_ms,
    _rows_to_frame,
    _validate_frame,
    _zip_rows,
)


def _archive(rows: list[list[object]], *, header: bool = False) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        if header:
            writer.writerow(
                [
                    "open_time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "close_time",
                    "quote_volume",
                    "trade_count",
                    "taker_buy_volume",
                    "taker_buy_quote_volume",
                    "ignore",
                ]
            )
        writer.writerows(rows)
        archive.writestr("sample.csv", text.getvalue())
    return stream.getvalue()


def _row(open_time: int, close: float = 101.0) -> list[object]:
    return [
        open_time,
        100.0,
        102.0,
        99.0,
        close,
        10.0,
        open_time + 299_999,
        1005.0,
        5,
        6.0,
        603.0,
        0,
    ]


def test_month_range_is_inclusive() -> None:
    assert _month_range("2024-11", "2025-01") == (
        "2024-11",
        "2024-12",
        "2025-01",
    )


def test_epoch_normalization_accepts_seconds_milliseconds_and_microseconds() -> None:
    assert _normalize_epoch_to_ms(1_700_000_000) == 1_700_000_000_000
    assert _normalize_epoch_to_ms(1_700_000_000_000) == 1_700_000_000_000
    assert _normalize_epoch_to_ms(1_700_000_000_000_000) == 1_700_000_000_000


def test_checksum_parser_accepts_standard_binance_payload() -> None:
    digest = hashlib.sha256(b"payload").hexdigest()
    assert _expected_sha256(
        f"{digest}  sample.zip\n".encode(),
        url="sample",
    ) == digest


def test_zip_parser_and_validation_preserve_real_gaps_without_filling() -> None:
    start = 1_700_000_100_000
    payload = _archive(
        [_row(start), _row(start + 300_000), _row(start + 900_000)],
        header=True,
    )
    rows = _zip_rows(payload, source="sample")
    frame = _rows_to_frame(rows, source="sample")
    checked, duplicates, gap_count, missing = _validate_frame(
        frame,
        symbol="BTCUSDT",
        interval="5m",
    )

    assert len(checked) == 3
    assert duplicates == 0
    assert gap_count == 1
    assert missing == 1


def test_duplicate_open_time_keeps_last_archive_row() -> None:
    start = 1_700_000_100_000
    frame = _rows_to_frame(
        [_row(start, 101.0), _row(start, 101.5), _row(start + 300_000)],
        source="sample",
    )
    checked, duplicates, _, _ = _validate_frame(
        frame,
        symbol="BTCUSDT",
        interval="5m",
    )

    assert duplicates == 1
    assert checked.iloc[0]["close"] == 101.5
