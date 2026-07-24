"""ICT day-trading research package.

The actively maintained implementation lives under :mod:`ictbt.easychart_v0`.
The repository no longer contains the legacy top-level ``backtest`` and
``s*_signals`` modules, so importing them here made every subpackage import
fail before tests or research code could run.  Keep the namespace package
side-effect free; callers should import the concrete maintained module they
use.
"""

__all__: list[str] = []
