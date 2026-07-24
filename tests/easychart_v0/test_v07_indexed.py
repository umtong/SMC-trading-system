from __future__ import annotations

import pytest

from ictbt.easychart_v0.v07 import build_v07_scene_family_result
from ictbt.easychart_v0.v07_indexed import (
    build_v07_lifecycle_index,
    build_v07_scene_family_result_indexed,
)
from tests.easychart_v0.test_v07 import fixture_book


@pytest.mark.parametrize(
    "kwargs",
    (
        {},
        {"c_low": 100.2, "c_close": 100.25},
        {"boundary_price": 100.5},
        {"next_open": 102.85},
        {"c_high": 103.0, "c_close": 102.95, "next_open": 100.8},
    ),
)
def test_indexed_builder_is_dataclass_identical_to_frozen_builder(
    kwargs: dict[str, float],
) -> None:
    book = fixture_book(**kwargs)

    frozen = build_v07_scene_family_result(book)
    indexed = build_v07_scene_family_result_indexed(book)

    assert indexed == frozen


def test_reusing_lifecycle_index_does_not_change_result() -> None:
    book = fixture_book()
    lifecycle = build_v07_lifecycle_index(book)

    first = build_v07_scene_family_result_indexed(
        book,
        lifecycle_index=lifecycle,
    )
    second = build_v07_scene_family_result_indexed(
        book,
        lifecycle_index=lifecycle,
    )

    assert first == second == build_v07_scene_family_result(book)
