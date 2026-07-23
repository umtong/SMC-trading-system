from __future__ import annotations

import csv
import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_quarter_hour_aggtrade_features.py"
SPEC = importlib.util.spec_from_file_location("wave19_aggtrade_builder", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _archive(path: Path, rows: list[list[object]], *, header: bool = False) -> Path:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if header:
        writer.writerow(MODULE.COLS)
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sample.csv", buffer.getvalue())
    return path


@pytest.mark.parametrize("header", [False, True])
def test_process_archive_uses_only_completed_ten_second_windows(tmp_path: Path, header: bool) -> None:
    boundary = 1_704_067_200_000  # 2024-01-01T00:00:00Z
    rows = [
        [1, 100.0, 1.0, 1, 1, boundary - 9_000, False],
        [2, 101.0, 2.0, 2, 2, boundary + 1_000, False],
        [3, 100.5, 1.0, 3, 3, boundary + 9_000, True],
        [4, 999.0, 50.0, 4, 4, boundary + 10_000, False],
    ]
    events: dict[int, object] = {}
    stats = MODULE.process_archive(
        _archive(tmp_path / "sample.zip", rows, header=header),
        events,
        chunk_size=2,
    )

    assert stats == {
        "total_rows": 4,
        "selected_rows": 3,
        "opening_rows": 2,
        "prior_rows": 1,
    }
    event = events[boundary]
    assert event.prior_last_price == pytest.approx(100.0)
    assert event.open_first_price == pytest.approx(101.0)
    assert event.open_last_price == pytest.approx(100.5)
    assert event.total_qty == pytest.approx(3.0)
    assert event.signed_qty == pytest.approx(1.0)
    assert event.trade_count == 2

    row = MODULE.event_rows(events, 2024, 1)[0]
    assert row["order_imbalance_qty"] == pytest.approx(1.0 / 3.0)
    assert row["buyer_taker_qty"] == pytest.approx(2.0)
    assert row["seller_taker_qty"] == pytest.approx(1.0)


def test_prior_context_is_assigned_to_next_quarter_hour(tmp_path: Path) -> None:
    first = 1_704_067_200_000
    second = first + MODULE.QUARTER_MS
    rows = [
        [1, 102.0, 1.0, 1, 1, second - 5_000, False],
        [2, 103.0, 1.0, 2, 2, second + 2_000, False],
    ]
    events: dict[int, object] = {}
    MODULE.process_archive(_archive(tmp_path / "sample.zip", rows), events, chunk_size=10)

    assert first not in events
    assert events[second].prior_last_price == pytest.approx(102.0)
    assert events[second].open_last_price == pytest.approx(103.0)


def test_epoch_ms_normalizes_milliseconds_microseconds_and_nanoseconds() -> None:
    import pandas as pd

    values = pd.Series([
        1_704_067_200_000,
        1_704_067_200_000_000,
        1_704_067_200_000_000_000,
    ])
    result = MODULE.epoch_ms(values)
    assert result.tolist() == [
        1_704_067_200_000,
        1_704_067_200_000,
        1_704_067_200_000,
    ]
