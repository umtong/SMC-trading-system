from datetime import date

import pandas as pd

from ictbt.easychart_v0.binance_vision import (
    DateInterval,
    intervals_from_manifest,
    merge_intervals,
    months_for_intervals,
    normalize_open_times,
)


def test_merge_intervals_combines_overlapping_and_adjacent_ranges() -> None:
    merged = merge_intervals(
        (
            DateInterval(date(2024, 1, 1), date(2024, 1, 10)),
            DateInterval(date(2024, 1, 10), date(2024, 1, 20)),
            DateInterval(date(2024, 2, 1), date(2024, 2, 5)),
        )
    )

    assert merged == (
        DateInterval(date(2024, 1, 1), date(2024, 1, 20)),
        DateInterval(date(2024, 2, 1), date(2024, 2, 5)),
    )


def test_manifest_uses_warmup_start_and_operating_end() -> None:
    payload = {
        "schema": "ictbt.random_annual_windows.v1",
        "samples": [
            {
                "windows": [
                    {
                        "warmup_start": "2023-12-01",
                        "start": "2024-01-15",
                        "end": "2024-02-12",
                    }
                ]
            }
        ],
    }

    assert intervals_from_manifest(payload) == (
        DateInterval(date(2023, 12, 1), date(2024, 2, 12)),
    )


def test_months_cover_cross_year_half_open_interval() -> None:
    months = months_for_intervals(
        (DateInterval(date(2023, 12, 20), date(2024, 2, 1)),)
    )

    assert months == ("2023-12", "2024-01")


def test_timestamp_unit_detection_handles_ms_and_us() -> None:
    milliseconds = pd.Series([1_704_067_200_000, 1_704_067_500_000])
    microseconds = pd.Series([1_704_067_200_000_000, 1_704_067_500_000_000])

    expected = pd.DatetimeIndex(
        ["2024-01-01T00:00:00Z", "2024-01-01T00:05:00Z"]
    )
    assert normalize_open_times(milliseconds).equals(expected)
    assert normalize_open_times(microseconds).equals(expected)
