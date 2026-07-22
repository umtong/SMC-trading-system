"""ICT day-trading research engine.

The repository currently ships the causal ``easychart_v0`` engine.  Earlier
workspace-only strategy modules are intentionally not imported here: keeping
stale eager imports made every submodule import fail before pytest collection.
Concrete APIs should be imported from their owning package, for example
``ictbt.easychart_v0``.
"""

__all__: list[str] = []
