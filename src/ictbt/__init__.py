"""Current EasyChart/SMC research package.

The public repository intentionally contains only ``ictbt.easychart_v0``.
Legacy strategy modules remain outside this repository, so importing the
package root must not eagerly import those unavailable modules.  Consumers
should import the active research API from ``ictbt.easychart_v0``.
"""

__all__: list[str] = []
