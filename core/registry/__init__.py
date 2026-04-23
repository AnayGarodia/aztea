"""Agent registry (split package; mirrors legacy ``core.registry`` namespace)."""

from __future__ import annotations

from core import embeddings

from . import agents_ops
from . import core_schema

_SKIP = frozenset({"embeddings"})

for _mod in (core_schema, agents_ops):
    for _n in dir(_mod):
        if _n.startswith("__"):
            continue
        if _n in _SKIP:
            continue
        globals()[_n] = getattr(_mod, _n)
