"""User accounts, scoped API keys, and agent-scoped worker keys.

This package replaces the legacy ``core/auth.py`` module. The implementation is split
into two cohesive submodules whose public surface is merged here so existing callers
(including monkeypatches targeting ``core.auth.<name>``) continue to work:

- ``core.auth.schema``: SQLite schema, connection helpers, password/key hashing,
  legal-acceptance state, and constants such as ``KEY_PREFIX``, ``AGENT_KEY_PREFIX``,
  and ``VALID_KEY_SCOPES``.
- ``core.auth.users``: user registration, login, API-key lifecycle (create, rotate,
  revoke, verify), and agent-scoped worker keys.

Import style::

    from core import auth

    user = auth.register_user(username="...", email="...", password="...")
    ctx = auth.verify_api_key(raw_key)

All network-facing validation of user-supplied fields happens in
``core.models`` (Pydantic). This package is the persistence/identity layer only.
"""

from __future__ import annotations

from . import schema
from . import users
from . import profile

# Re-export every public symbol from the submodules so ``core.auth.<name>`` keeps
# working. Underscored private helpers are included on purpose — integration tests
# and monkeypatches (e.g. ``monkeypatch.setattr(core.auth, "_local", ...)``)
# historically relied on them being importable from the package root.
for _mod in (schema, users, profile):
    for _name in dir(_mod):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_mod, _name)
