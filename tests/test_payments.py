"""
test_payments.py — End-to-end payment flow smoke test.

Covers two layers:
  - Direct: calls payments.py functions in-process (no HTTP, no Groq dependency)
  - HTTP:   calls the live server (requires `uvicorn server:app --port 8000`)

Usage:
    uvicorn server:app --port 8000   # in one terminal
    python test_payments.py          # in another
"""

import os
import sqlite3
import sys
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

# This file is an executable integration smoke test script.
# Skip it during pytest collection to avoid side-effectful import execution.
if __name__ != "__main__":
    import pytest

    pytest.skip(
        "test_payments.py is an integration script. Run `python test_payments.py`.",
        allow_module_level=True,
    )

HOST     = os.environ.get("SERVER_BASE_URL", "http://localhost:8000")
KEY      = os.environ.get("API_KEY", "")
AGENT_ID = "00000000-0000-0000-0000-000000000001"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {KEY}",
}

failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    mark = "✓" if condition else "✗"
    suffix = f"  →  {detail}" if (not condition and detail) else ""
    print(f"  {mark}  {label}{suffix}")
    if not condition:
        failures += 1


def post(path, body):
    return requests.post(f"{HOST}{path}", headers=HEADERS, json=body, timeout=15)


def get(path):
    return requests.get(f"{HOST}{path}", headers=HEADERS, timeout=15)


# ============================================================================
# PART A — Direct payment layer tests (no HTTP, no Groq)
# These always run and prove the ledger math is correct.
# ============================================================================

print("\n══════════════════════════════════════════")
print("  PART A — Direct payments layer")
print("══════════════════════════════════════════")

import payments  # noqa: E402 (import after dotenv load)

payments.init_payments_db()

# Use unique owner IDs so repeated test runs don't collide
run_id = uuid.uuid4().hex[:8]
caller_id  = f"test-caller-{run_id}"
agent_id   = f"agent:test-{run_id}"
plat_id    = payments.PLATFORM_OWNER_ID


print("\n── A1. Wallet creation ──")
caller  = payments.get_or_create_wallet(caller_id)
agent_w = payments.get_or_create_wallet(agent_id)
check("caller wallet created with 0 balance", caller["balance_cents"] == 0)
check("idempotent: second call returns same wallet_id",
      payments.get_or_create_wallet(caller_id)["wallet_id"] == caller["wallet_id"])


print("\n── A2. Deposit ──")
tx_id = payments.deposit(caller["wallet_id"], 1000, "test deposit")
check("deposit returns a tx_id", bool(tx_id))
w = payments.get_wallet(caller["wallet_id"])
check("balance is 1000¢ after deposit", w["balance_cents"] == 1000)

try:
    payments.deposit(caller["wallet_id"], 0)
    check("deposit of 0 raises ValueError", False)
except ValueError:
    check("deposit of 0 raises ValueError", True)

try:
    payments.deposit(caller["wallet_id"], -50)
    check("deposit of negative raises ValueError", False)
except ValueError:
    check("deposit of negative raises ValueError", True)


print("\n── A3. Insufficient balance ──")
try:
    payments.pre_call_charge(caller["wallet_id"], 9999_00, "test-agent")
    check("InsufficientBalanceError raised when broke", False)
except payments.InsufficientBalanceError as e:
    check("InsufficientBalanceError raised when broke", True)
    check("error carries balance_cents", e.balance_cents == 1000)
    check("error carries required_cents", e.required_cents == 9999_00)
w = payments.get_wallet(caller["wallet_id"])
check("balance unchanged after failed charge", w["balance_cents"] == 1000)


print("\n── A4. Successful call payout (10¢ agent) ──")
plat = payments.get_or_create_wallet(plat_id)
plat_before = payments.get_wallet(plat["wallet_id"])["balance_cents"]

charge_id = payments.pre_call_charge(caller["wallet_id"], 10, agent_id)
check("charge returns tx_id", bool(charge_id))
w = payments.get_wallet(caller["wallet_id"])
check("caller balance is 990¢ after 10¢ charge", w["balance_cents"] == 990)

payments.post_call_payout(
    agent_w["wallet_id"], plat["wallet_id"], charge_id, 10, agent_id
)
agent_bal = payments.get_wallet(agent_w["wallet_id"])["balance_cents"]
plat_bal  = payments.get_wallet(plat["wallet_id"])["balance_cents"]
check("agent received 9¢ (90%)", agent_bal == 9, f"got {agent_bal}¢")
check("platform received 1¢ (10%)", plat_bal == plat_before + 1, f"got {plat_bal - plat_before}¢")
check("money conserved: caller -10¢ = agent +9¢ + platform +1¢",
      (1000 - 990) == (agent_bal + (plat_bal - plat_before)))


print("\n── A5. Failed call refund ──")
charge_id2 = payments.pre_call_charge(caller["wallet_id"], 10, agent_id)
w = payments.get_wallet(caller["wallet_id"])
check("balance is 980¢ after second charge", w["balance_cents"] == 980)

payments.post_call_refund(caller["wallet_id"], charge_id2, 10, agent_id)
w = payments.get_wallet(caller["wallet_id"])
check("balance restored to 990¢ after refund", w["balance_cents"] == 990, f"got {w['balance_cents']}¢")

txs = payments.get_wallet_transactions(caller["wallet_id"])
types = [t["type"] for t in txs]
check("refund transaction recorded", "refund" in types)
refund_tx = next(t for t in txs if t["type"] == "refund")
check("refund links to original charge via related_tx_id",
      refund_tx["related_tx_id"] == charge_id2)


print("\n── A6. Transaction ledger integrity ──")
txs = payments.get_wallet_transactions(caller["wallet_id"])
total = sum(t["amount_cents"] for t in txs)
w = payments.get_wallet(caller["wallet_id"])
check("sum of all transactions == current balance",
      total == w["balance_cents"], f"sum={total}, balance={w['balance_cents']}")
check("transactions are newest-first",
      txs[0]["created_at"] >= txs[-1]["created_at"])


print("\n── A7. balance_cents CHECK constraint ──")
conn = sqlite3.connect("registry.db")
try:
    conn.execute(
        "UPDATE wallets SET balance_cents = -1 WHERE wallet_id = ?",
        (caller["wallet_id"],),
    )
    conn.commit()
    check("DB rejects negative balance_cents", False, "constraint not enforced")
except sqlite3.IntegrityError:
    check("DB rejects negative balance_cents", True)
finally:
    conn.close()


# ============================================================================
# PART B — HTTP endpoint tests (requires server running)
# ============================================================================

print("\n══════════════════════════════════════════")
print("  PART B — HTTP endpoints")
print("══════════════════════════════════════════")

print("\n── B1. Health ──")
try:
    r = get("/health")
    check("server is up", r.status_code == 200)
except requests.ConnectionError:
    print("  ✗  cannot connect to server — is `uvicorn server:app --port 8000` running?")
    print(f"\n{'─'*42}")
    print("  Part A: all direct tests above  |  Part B: skipped (no server)")
    sys.exit(1 if failures else 0)


print("\n── B2. GET /wallets/me ──")
r = get("/wallets/me")
check("returns 200", r.status_code == 200, str(r.status_code))
my_wallet = r.json()
check("wallet_id present", "wallet_id" in my_wallet)
check("balance_cents present", "balance_cents" in my_wallet)
check("transactions list present", isinstance(my_wallet.get("transactions"), list))
my_wallet_id = my_wallet["wallet_id"]
print(f"     wallet_id: {my_wallet_id}  balance: {my_wallet['balance_cents']}¢")


print("\n── B3. First call with 0 balance → HTTP 402 ──")
# Drain to 0 if somehow already funded from a previous run
current = my_wallet["balance_cents"]
if current > 0:
    print(f"     (wallet has {current}¢ from prior run — draining not needed, skip 402 test)")
    wallet_id = my_wallet_id
    skip_402 = True
else:
    r = post(f"/registry/agents/{AGENT_ID}/call", {"ticker": "AAPL"})
    check("status is 402", r.status_code == 402, str(r.status_code))
    detail = r.json()["detail"]
    check("error is insufficient_balance", detail.get("error") == "insufficient_balance")
    check("balance_cents is 0", detail.get("balance_cents") == 0)
    check("required_cents >= 1", detail.get("required_cents", 0) >= 1)
    check("wallet_id in error body", "wallet_id" in detail)
    wallet_id = detail["wallet_id"]
    check("wallet_id matches /wallets/me", wallet_id == my_wallet_id)
    skip_402 = False


print("\n── B4. Deposit ──")
r = post("/wallets/deposit", {"wallet_id": wallet_id, "amount_cents": 500, "memo": "test top-up"})
check("deposit returns 200", r.status_code == 200, str(r.status_code))
body = r.json()
check("balance_cents is 500", body["balance_cents"] == 500, str(body.get("balance_cents")))
check("tx_id present", "tx_id" in body)


print("\n── B5. GET /wallets/{wallet_id} ──")
r = get(f"/wallets/{wallet_id}")
check("returns 200", r.status_code == 200)
body = r.json()
check("balance_cents is 500", body["balance_cents"] == 500, str(body.get("balance_cents")))
check("transactions list present", isinstance(body.get("transactions"), list))
check("deposit is in transaction history", any(t["type"] == "deposit" for t in body["transactions"]))


print("\n── B6. Agent call with funds ──")
r = post(f"/registry/agents/{AGENT_ID}/call", {"ticker": "AAPL"})
status = r.status_code
print(f"     upstream HTTP {status}")

w = get(f"/wallets/{wallet_id}").json()
txs = w["transactions"]
types = [t["type"] for t in txs]

if status == 200:
    check("response has ticker", "ticker" in r.json())
    check("response has signal", "signal" in r.json())
    check("charge recorded", "charge" in types)
    check("no refund on success", "refund" not in types)
    check("balance is 499¢ (charged 1¢)", w["balance_cents"] == 499, str(w["balance_cents"]))
    print("     ✓  success path fully verified")
elif status in (429, 503):
    check("charge recorded before upstream fail", "charge" in types, str(types))
    check("refund fired on LLM rate limit", "refund" in types, str(types))
    check("balance restored to 500¢", w["balance_cents"] == 500, str(w["balance_cents"]))
    print("     (Groq rate-limited → refund path verified. Try again when limit resets.)")
else:
    check("charge recorded", "charge" in types, str(types))
    check("refund fired on failure", "refund" in types, str(types))
    check("balance restored", w["balance_cents"] == 500, str(w["balance_cents"]))


print("\n── B7. Error cases ──")
r = post("/wallets/deposit", {"wallet_id": "00000000-0000-0000-0000-000000000000", "amount_cents": 10})
check("deposit to unknown wallet → 400", r.status_code == 400, str(r.status_code))

r = post("/wallets/deposit", {"wallet_id": wallet_id, "amount_cents": -1})
check("negative deposit → 400", r.status_code == 400, str(r.status_code))

r = get("/wallets/00000000-0000-0000-0000-000000000000")
check("GET unknown wallet → 404", r.status_code == 404, str(r.status_code))

r = post(f"/registry/agents/{AGENT_ID}/call", {"ticker": "!INVALID!"})
check("invalid ticker → 422 or 400", r.status_code in (400, 422), str(r.status_code))

r = get("/registry/agents/99999999-0000-0000-0000-000000000000")
check("GET unknown agent → 404", r.status_code == 404)


# ============================================================================
print(f"\n{'═'*42}")
if failures == 0:
    print("  All checks passed.")
else:
    print(f"  {failures} check(s) failed.")
    sys.exit(1)
