#!/usr/bin/env python3
"""Minimal ledger audit smoke check.

Prints the current reconciliation summary and exits non-zero when the wallet
cache diverges from the insert-only ledger.
"""

from __future__ import annotations

import json
import sys

from core import payments


def main() -> int:
    payments.init_payments_db()
    summary = payments.compute_ledger_invariants()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("invariant_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

