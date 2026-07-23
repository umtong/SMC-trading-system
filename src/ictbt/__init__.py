"""ICT day-trading research engine.

Concrete APIs are imported from their owning packages. Earlier workspace-only
strategy modules are intentionally not imported here because stale eager imports
make every causal EasyChart and microstructure submodule fail during collection.
"""

__all__: list[str] = []
