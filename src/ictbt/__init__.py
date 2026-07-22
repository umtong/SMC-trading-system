"""EasyChart-based SMC/ICT research package.

The repository no longer ships the legacy stage backtest modules that were
previously re-exported here.  Keep the package root side-effect free so
``ictbt.easychart_v0`` can be imported without resolving removed modules.
"""

__all__: tuple[str, ...] = ()
