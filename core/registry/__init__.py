"""Agent registry: listings, semantic search, embeddings cache, reputation enrichment.

This package replaces the legacy ``core/registry.py`` module. It is split into:

- ``core_schema`` — SQLite schema creation, connection helpers, row
  serialisation, and shared constants (status enums, defaults).
- ``agents_ops`` — agent CRUD, moderation/review, reputation enrichment,
  endpoint health telemetry, and the semantic/text search implementation.

``core.embeddings`` is re-exported unchanged so ``registry.embeddings`` keeps
working (integration tests monkeypatch it to stub out the sentence-transformers
model during unit runs).

Callers should continue to use ``from core import registry`` and invoke
``registry.register_agent(...)``, ``registry.get_agents(...)``,
``registry.semantic_search(...)``, etc.
"""

from __future__ import annotations

from core import embeddings

from . import agents_ops
from . import core_schema
from . import pricing

_SKIP = frozenset({"embeddings"})

for _mod in (core_schema, agents_ops, pricing):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        if _name in _SKIP:
            continue
        globals()[_name] = getattr(_mod, _name)
