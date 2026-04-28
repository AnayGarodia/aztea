from __future__ import annotations

from ._shim import load_module

_mod = load_module("errors")

for _name in dir(_mod):
    if _name.startswith("_"):
        continue
    globals()[_name] = getattr(_mod, _name)

