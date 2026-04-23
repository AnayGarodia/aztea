"""Wallets, insert-only ledger, and settlement helpers.

This package replaces the legacy ``core/payments.py`` module. All monetary
amounts are integer cents — no floats are stored or exchanged. The
``transactions`` table is insert-only; wallet balances are a cache maintained
in the same SQLite transaction as the ledger insert that caused them to change.

Split layout:

- ``base`` — wallet CRUD, ledger insert helpers, pre/post-call charging and
  refunds, settlement distribution math, Stripe/top-up bookkeeping.
- ``trust_disputes`` — dispute-deposit escrow, resolution payouts, and
  reconciliation helpers that require wallet + disputes state at once.
- ``variable_pricing`` — zero-sum compensating refunds when an agent's
  reported actual usage comes in below the pre-charge estimate.

``__init__`` re-exports the full public surface (including underscored helpers
that tests and the server shards reach for, e.g. ``payments._local``,
``payments._conn``, ``payments._insert_tx``). ``_payments_core`` is
intentionally excluded — it is only an internal alias inside
``trust_disputes`` and callers should continue to use the public helpers.
"""

from __future__ import annotations

from . import base
from . import trust_disputes
from . import variable_pricing

for _mod in (base, trust_disputes, variable_pricing):
    for _n in dir(_mod):
        if _n.startswith("__"):
            continue
        if _n == "_payments_core":
            continue
        globals()[_n] = getattr(_mod, _n)
