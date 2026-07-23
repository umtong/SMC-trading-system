"""ICT trading research package.

The active EasyChart implementation lives under :mod:`ictbt.easychart_v0`.
Legacy root-level engines were removed from the source tree, so this package
initializer intentionally avoids importing names from modules that do not
exist.  Import concrete strategy APIs from their owning subpackage instead of
relying on eager root re-exports.
"""

__all__: list[str] = []
