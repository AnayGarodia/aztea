from __future__ import annotations

from ._shim import load_module, warn_once

warn_once()
_mod = load_module("__init__")

__all__ = getattr(_mod, "__all__", [])
__version__ = getattr(_mod, "__version__", "0")

for _name in __all__:
    globals()[_name] = getattr(_mod, _name)

