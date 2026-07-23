"""ICT trading research package.

The active, self-contained implementation lives in :mod:`ictbt.easychart_v0`.
Legacy strategy modules that used to be re-exported from this package root are
not part of the current repository snapshot. Keeping the root import free of
optional-module side effects allows ``import ictbt.easychart_v0`` and its test
suite to work in a clean checkout.
"""

__all__: list[str] = []
