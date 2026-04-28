# Runbook: Ledger Drift Investigation

**Trigger:** `POST /ops/payments/reconcile` returns `drift_cents != 0` or `mismatch_count > 0`, or an admin notices wallet balances that look wrong.

**Owner:** Anyone with `admin` scope and SSH access to the server.

**Time to resolve:** 15–60 minutes depending on root cause.

---

## Background

`wallets.balance_cents` is a **cached mirror** of the insert-only `transactions` ledger. The invariant is:

```
wallets.balance_cents == SUM(transactions.amount_cents) WHERE wallet_id = X
```

Every code path that writes a transaction row must update the wallet balance in the **same SQL transaction**. Drift means at least one path failed to do this atomically, or wrote a transaction row without a corresponding wallet update (or vice versa).

The reconciliation endpoint computes the authoritative balance from the ledger and flags any wallet where the cached value diverges.

---

## Step 1 — Run reconciliation and read the report

```bash
curl -s -H "Authorization: Bearer $API_KEY" \
  -X POST https://aztea.ai/ops/payments/reconcile | jq .
```

Key fields in the response:

| Field             | Meaning                                                      |
| ----------------- | ------------------------------------------------------------ |
| `drift_cents`     | Sum of all per-wallet deltas (positive = wallets over-reported, negative = under) |
| `mismatch_count`  | Number of wallets where cached balance ≠ ledger total         |
| `invariant_ok`    | `true` only when both fields above are 0                     |
| `wallets`         | Per-wallet breakdown (only included when drift is present)   |

If `invariant_ok: true` — no action needed. The alert was a false positive.

---

## Step 2 — Identify affected wallets

Open a Python shell on the server:

```bash
cd /home/aztea/app && source venv/bin/activate && python
```

```python
import sqlite3, os
conn = sqlite3.connect(os.environ["DB_PATH"])
conn.row_factory = sqlite3.Row

# Find wallets where cached balance diverges from ledger total
rows = conn.execute("""
    SELECT
        w.wallet_id,
        w.owner_id,
        w.balance_cents AS cached,
        COALESCE(SUM(t.amount_cents), 0) AS ledger_total,
        COALESCE(SUM(t.amount_cents), 0) - w.balance_cents AS delta
    FROM wallets w
    LEFT JOIN transactions t ON t.wallet_id = w.wallet_id
    GROUP BY w.wallet_id
    HAVING delta != 0
    ORDER BY ABS(delta) DESC
""").fetchall()

for r in rows:
    print(dict(r))
```

Note the `wallet_id` values — you need them for the next steps.

---

## Step 3 — Find the transaction(s) that caused the drift

For each affected wallet, look at recent transactions:

```python
wallet_id = "PASTE_WALLET_ID_HERE"

txns = conn.execute("""
    SELECT tx_id, type, amount_cents, agent_id, memo, related_tx_id, created_at
    FROM transactions
    WHERE wallet_id = ?
    ORDER BY created_at DESC
    LIMIT 50
""", (wallet_id,)).fetchall()

for t in txns:
    print(dict(t))
```

Look for:

- **Orphaned transactions** — a `charge` or `payout` row with no corresponding wallet update. The `related_tx_id` links to the originating job or charge.
- **Duplicate settlement** — two `payout` rows with the same `related_tx_id` (double-settlement bug; the double-settlement guard should prevent this but check for it).
- **Payout-curve memos** — transactions with `memo` like `payout_curve:{job_id}`. Check that both the debit (agent wallet) and credit (caller wallet) entries exist; a partial write here is the most likely cause of recent drift.
- **Missing compensating entry** — a `charge` exists but no `refund` was written after a job failure.

---

## Step 4 — Trace to the originating job

```python
related_tx_id = "PASTE_RELATED_TX_ID_HERE"

# Find the job
job = conn.execute(
    "SELECT * FROM jobs WHERE job_id = ? OR charge_tx_id = ?",
    (related_tx_id, related_tx_id)
).fetchone()
print(dict(job) if job else "Job not found — tx may be a deposit")

# All transactions for this job
all_txns = conn.execute(
    "SELECT * FROM transactions WHERE related_tx_id = ? ORDER BY created_at",
    (related_tx_id,)
).fetchall()
for t in all_txns:
    print(dict(t))
```

Check the job's `status`, `settled_at`, and `charge_tx_id`. A complete lifecycle should produce exactly:
1. One `charge` on the caller wallet (pre-call)
2. One `payout` on the agent wallet + one `fee` on the platform wallet (post-call settlement)
3. If rated with a payout curve: one `charge` on agent wallet + one `refund` on caller wallet (clawback)
4. If failed/refunded: one `refund` on the caller wallet

Any missing entry in this chain is the root cause.

---

## Step 5 — Apply a correcting entry

**Never UPDATE or DELETE from `transactions` or `wallets.balance_cents` directly.** Write a compensating ledger entry and update the wallet cache in one transaction.

```python
import uuid
from datetime import datetime, timezone

def _now():
    return datetime.now(timezone.utc).isoformat()

# Example: caller wallet was charged but refund was never written after job failure
wallet_id = "PASTE_WALLET_ID_HERE"
correction_cents = 100  # positive = credit to wallet
agent_id = "PASTE_AGENT_ID_OR_system:correction"
related_tx_id = "PASTE_ORIGINAL_CHARGE_TX_ID"
memo = "manual_correction:DESCRIBE_ROOT_CAUSE_HERE"

tx_id = str(uuid.uuid4())

with conn:
    conn.execute(
        "INSERT INTO transactions (tx_id, wallet_id, type, amount_cents, agent_id, related_tx_id, memo, created_at) "
        "VALUES (?, ?, 'refund', ?, ?, ?, ?, ?)",
        (tx_id, wallet_id, correction_cents, agent_id, related_tx_id, memo, _now())
    )
    updated = conn.execute(
        "UPDATE wallets SET balance_cents = balance_cents + ? WHERE wallet_id = ?",
        (correction_cents, wallet_id)
    ).rowcount
    if updated == 0:
        conn.rollback()
        raise RuntimeError(f"Wallet {wallet_id} not found — rolled back")

print(f"Correction applied: tx_id={tx_id}")
```

---

## Step 6 — Verify

Re-run reconciliation and confirm `invariant_ok: true`:

```bash
curl -s -H "Authorization: Bearer $API_KEY" \
  -X POST https://aztea.ai/ops/payments/reconcile | jq '{invariant_ok, drift_cents, mismatch_count}'
```

---

## Step 7 — Document and add a regression test

1. Write a brief note in `docs/runbooks/ledger-drift.md` (this file) under "Historical incidents" describing what caused the drift and how it was fixed.
2. If the root cause was a code path that bypassed the ledger invariant, add a regression test to `tests/test_bug_regressions.py`.

---

## Historical incidents

_Add entries here as incidents are resolved._

| Date       | Root cause                                              | Fix                                  |
| ---------- | ------------------------------------------------------- | ------------------------------------ |
| 2026-04-28 | `core/payout_curve.py` used direct wallet mutations and unsupported transaction types (`payout_curve_clawback`), skipping the rowcount guard | Rewrote clawback to use `charge`/`refund` types with proper guard checks; idempotency key added |
