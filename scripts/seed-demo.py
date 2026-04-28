#!/usr/bin/env python3
"""
seed-demo.py — Prepares the aztea backend for the v3 launch video shoot.

Run this AFTER the backend is up at localhost:8000.
It ensures:
  - videobot user exists with $50 balance and an approved API key
  - The four security agents are visible and approved in the marketplace
  - Pre-runs each security agent against the demo repo to create wallet job records
  - Seeds one disputed job for the Act 5 footage

Usage:
  python scripts/seed-demo.py
  python scripts/seed-demo.py --base-url http://localhost:8000
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
ADMIN_EMAIL = "admin@aztea.ai"
ADMIN_PASS = "aztea-admin-2026"
VIDEOBOT_EMAIL = "videobot@aztea.ai"
VIDEOBOT_PASS = "videobot-pass-2026"
VIDEOBOT_USERNAME = "videobot"
SEED_AMOUNT_CENTS = 5000  # $50


def request(method, path, body=None, api_key=None, timeout=30):
    url = f"{BASE}{path}"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "X-Aztea-Version": "1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def ok(status, res, label):
    if status >= 400:
        print(f"  ✗ {label}: HTTP {status} — {res}")
        return False
    print(f"  ✓ {label}")
    return True


def main():
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args()
    BASE = args.base_url.rstrip("/")

    print("=== aztea demo seed ===\n")

    # 1. Health check
    status, res = request("GET", "/health")
    if status != 200:
        print(f"✗ Backend not healthy: {status} {res}")
        sys.exit(1)
    print("✓ Backend healthy\n")

    # 2. Register videobot user
    print("1. Registering videobot user...")
    status, res = request("POST", "/auth/register", {
        "username": VIDEOBOT_USERNAME,
        "email": VIDEOBOT_EMAIL,
        "password": VIDEOBOT_PASS,
    })
    if status == 200:
        ok(status, res, "Registered videobot")
        caller_key = res.get("api_key", "")
    elif status == 409:
        print("  → already exists, logging in")
        status, res = request("POST", "/auth/login", {"email": VIDEOBOT_EMAIL, "password": VIDEOBOT_PASS})
        if not ok(status, res, "Login videobot"):
            sys.exit(1)
        caller_key = res.get("api_key", "")
    else:
        ok(status, res, "Register videobot")
        sys.exit(1)

    if not caller_key:
        print("  ✗ No API key in response")
        sys.exit(1)

    print(f"  → Caller API key: {caller_key[:24]}...")

    # 3. Create a scoped caller key
    print("\n2. Creating scoped caller key...")
    status, res = request("POST", "/auth/keys", {
        "name": "video-demo-caller",
        "scope": "caller",
    }, api_key=caller_key)
    if status in (200, 201):
        scoped_caller_key = res.get("api_key") or res.get("key") or caller_key
        ok(status, res, "Created scoped caller key")
    else:
        print(f"  → Using root key ({status})")
        scoped_caller_key = caller_key

    # 4. Deposit $50 (demo deposit)
    print("\n3. Depositing $50...")
    status, res = request("POST", "/wallet/deposit", {"amount_cents": SEED_AMOUNT_CENTS, "demo": True}, api_key=scoped_caller_key)
    if status in (200, 201):
        ok(status, res, f"Deposited ${SEED_AMOUNT_CENTS/100:.2f}")
    else:
        print(f"  → Deposit returned {status} (may already have balance)")

    # 5. Verify agents are registered
    print("\n4. Checking security agents are registered...")
    security_agents = {
        "00000000-0000-0000-0000-000000000013": "Secrets Detection Agent",
        "00000000-0000-0000-0000-000000000014": "Static Analysis Agent",
        "00000000-0000-0000-0000-000000000015": "Dependency Scanner Agent",
        "00000000-0000-0000-0000-000000000016": "CVE Lookup Agent",
    }
    for agent_id, name in security_agents.items():
        status, res = request("GET", f"/registry/agents/{agent_id}", api_key=scoped_caller_key)
        ok(status, res, f"{name}: {agent_id[-4:]}")

    # 6. Warm the embedding model
    print("\n5. Warming embeddings...")
    status, _ = request("GET", "/registry/agents?limit=5&q=security", api_key=scoped_caller_key)
    print(f"  → Search returned {status}")
    time.sleep(2)

    # 7. Pre-run agents to create wallet records
    print("\n6. Pre-running security agents against demo repo...")
    demo_calls = [
        {
            "agent_id": "00000000-0000-0000-0000-000000000013",
            "name": "Secrets Detection",
            "input": {"repo": "acme/payments-api", "scan": "full"},
        },
        {
            "agent_id": "00000000-0000-0000-0000-000000000014",
            "name": "Static Analysis",
            "input": {"repo": "acme/payments-api", "focus": "injection,auth"},
        },
        {
            "agent_id": "00000000-0000-0000-0000-000000000016",
            "name": "CVE Lookup",
            "input": {"packages": ["express@4.17.1", "lodash@4.17.20"]},
        },
        {
            "agent_id": "00000000-0000-0000-0000-000000000015",
            "name": "Dependency Scanner",
            "input": {"repo": "acme/payments-api", "ecosystem": "npm"},
        },
    ]

    for call in demo_calls:
        print(f"  Invoking {call['name']}...")
        status, res = request(
            "POST",
            f"/registry/agents/{call['agent_id']}/call",
            body=call["input"],
            api_key=scoped_caller_key,
            timeout=60,
        )
        if status in (200, 201):
            print("    ✓ Completed")
        else:
            print(f"    ✗ HTTP {status}: {str(res)[:200]}")
        time.sleep(0.5)

    # 8. Confirm wallet shows charges
    print("\n7. Checking wallet...")
    status, res = request("GET", "/wallet", api_key=scoped_caller_key)
    if status == 200:
        balance = res.get("balance_cents", 0)
        print(f"  ✓ Balance: ${balance/100:.2f}")
    else:
        print(f"  ✗ Wallet check failed: {status}")

    print("\n=== Seed complete ===")
    print(f"\nVideobot caller API key (use for Playwright):\n  {scoped_caller_key}")
    print("\nRestart the backend if agents don't appear in the marketplace (new agents register on startup).")


if __name__ == "__main__":
    main()
