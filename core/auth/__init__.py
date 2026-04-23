"""User accounts and API keys (split package; mirrors legacy ``core.auth`` namespace)."""

from __future__ import annotations

from . import schema
from . import users

for _mod in (schema, users):
    for _n in dir(_mod):
        if _n.startswith("__"):
            continue
        globals()[_n] = getattr(_mod, _n)
