from __future__ import annotations


def test_package_root_does_not_import_removed_legacy_modules() -> None:
    import ictbt
    import ictbt.easychart_v0

    assert ictbt.__all__ == []
