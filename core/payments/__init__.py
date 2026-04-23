"""Payment ledger (split package; mirrors legacy ``core.payments`` namespace)."""

from __future__ import annotations

from . import base
from . import trust_disputes

# ``from .base import *`` omits leading-underscore names; tests and callers expect
# ``payments._local``, ``payments._conn``, etc. on the package.
for _mod in (base, trust_disputes):
    for _n in dir(_mod):
        if _n.startswith("__"):
            continue
        if _n == "_payments_core":
            continue
        globals()[_n] = getattr(_mod, _n)
