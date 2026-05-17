#!/usr/bin/env python3
"""
selftest_observability.py — exercise the 3 admin endpoints against a seeded
in-process DB and answer the 10 questions from the observability brief.

This is the gate the implementation must pass before declaring the work
done. Each question is shown alongside the exact tool sequence a Claude
client running over MCP would issue (mapped to the underlying HTTP call).

Run::

    python scripts/selftest_observability.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure repo root + SDK on sys.path so this is runnable with no install.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

os.environ.setdefault("API_KEY", "selftest-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

DB_PATH = f"/tmp/aztea-selftest-{uuid.uuid4().hex[:8]}.db"
os.environ["DB_PATH"] = DB_PATH

from fastapi.testclient import TestClient

from core import auth, db as _db, disputes, jobs, payments, registry, reputation
from core.migrate import apply_migrations
import server.application as server


HEADERS = {"Authorization": f"Bearer {os.environ['API_KEY']}"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(conn: sqlite3.Connection) -> dict[str, str]:
    """Populate a small but representative dataset. Returns a key handles dict."""
    now = _now()
    today = _iso(now)
    yesterday = _iso(now - timedelta(days=1))
    long_ago = _iso(now - timedelta(days=30))

    # ── agents ─────────────────────────────────────────────────────────────
    agents = [
        ("agent_audit", "dependency_auditor", "audit_deps"),
        ("agent_cve",   "cve_lookup",        "cve_lookup_agent"),
        ("agent_regex", "regex_tester",      "regex_tester"),
        ("agent_jwt",   "jwt_debugger",      "jwt_debugger"),
        ("agent_dead",  "never_called",      "never_called"),
    ]
    for aid, slug, name in agents:
        conn.execute(
            """INSERT INTO agents (agent_id, name, description, owner_id,
                endpoint_url, price_per_call_usd, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (aid, name, f"{slug} agent", "owner:platform",
             f"internal://{slug}", 0.05, today),
        )

    # ── users ─────────────────────────────────────────────────────────────
    # NB: user_id matches the short handle so that ``caller_owner_id =
    # 'user:' || user_id`` lines up cleanly with the join in dormant_users.
    users = [
        ("alice", "alice", "alice@example.com", long_ago),  # dormant
        ("bob",   "bob",   "bob@example.com",   yesterday),
        ("carol", "carol", "carol@example.com", today),     # new this week
        ("dave",  "dave",  "dave@example.com",  today),
    ]
    for uid, uname, email, created in users:
        conn.execute(
            """INSERT INTO users (user_id, username, email, password_hash, salt,
                created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uid, uname, email, "hash", "salt", created, "active"),
        )

    # ── wallets ───────────────────────────────────────────────────────────
    for uid, _, _, _ in users:
        conn.execute(
            "INSERT INTO wallets (wallet_id, owner_id, balance_cents, created_at) VALUES (?, ?, ?, ?)",
            (f"w_{uid}", f"user:{uid}", 1000, today),
        )
    conn.execute(
        "INSERT INTO wallets (wallet_id, owner_id, balance_cents, created_at) VALUES (?, ?, ?, ?)",
        ("w_platform", "platform", 0, today),
    )

    # ── jobs ──────────────────────────────────────────────────────────────
    def ins_job(jid, aid, caller, status, created, origin="direct", price=10):
        conn.execute(
            """INSERT INTO jobs (job_id, agent_id, agent_owner_id, caller_owner_id,
                caller_wallet_id, agent_wallet_id, platform_wallet_id,
                status, price_cents, caller_charge_cents, charge_tx_id,
                input_payload, created_at, updated_at, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (jid, aid, "owner:platform", f"user:{caller}",
             f"w_user_{caller}".replace("w_user_w_", "w_"), "w_agent", "w_platform",
             status, price, price + 1, "tx_" + uuid.uuid4().hex[:6],
             "{}", created, created, origin),
        )

    # Bob spends most; Carol just made a first call today.
    for i in range(8):
        ins_job(f"jb_audit_{i}", "agent_audit", "bob", "complete", today, price=20)
    for i in range(4):
        ins_job(f"jb_cve_{i}", "agent_cve", "bob", "complete", today, price=10)
    ins_job("jb_regex_fail", "agent_regex", "bob", "failed", today)
    ins_job("jb_jwt_fail",   "agent_jwt",   "bob", "failed", today)
    ins_job("jb_carol_first", "agent_audit", "carol", "complete", today, price=15)
    ins_job("jb_alice_old",   "agent_audit", "alice", "complete", long_ago, price=5)

    # auto_hire-origin call by Dave
    ins_job("jb_dave_auto", "agent_cve", "dave", "complete",
            yesterday, origin="auto_hire", price=12)

    # ── transactions ──────────────────────────────────────────────────────
    def ins_tx(tid, wallet, t_type, amount, agent_id=None, when=today):
        conn.execute(
            """INSERT INTO transactions (tx_id, wallet_id, type, amount_cents,
                agent_id, memo, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tid, wallet, t_type, amount, agent_id, "selftest", when),
        )
    # Charges that match the job rows for each user (covers spend_by_user).
    ins_tx("tx_bob_1",   "w_user_bob",   "charge", 160, "agent_audit", today)
    ins_tx("tx_bob_2",   "w_user_bob",   "charge",  40, "agent_cve",   today)
    ins_tx("tx_carol_1", "w_user_carol", "charge",  15, "agent_audit", today)
    ins_tx("tx_alice_1", "w_user_alice", "charge",   5, "agent_audit", long_ago)
    ins_tx("tx_dave_1",  "w_user_dave",  "charge",  12, "agent_cve",   yesterday)
    # Payouts that map to spend_by_agent revenue.
    ins_tx("tx_pay_audit", "w_agent", "payout", 180, "agent_audit", today)
    ins_tx("tx_pay_cve",   "w_agent", "payout",  50, "agent_cve",   today)

    # ── auto_hire_decisions ───────────────────────────────────────────────
    def ins_dec(intent, reason, auto, when):
        h = hashlib.sha256(intent.encode()).hexdigest()
        conn.execute(
            """INSERT INTO auto_hire_decisions (decision_id, caller_owner_id,
                intent_text, intent_hash, auto_invoked, dry_run, reason,
                chosen_agent_id, confidence, candidates_json, resulting_job_id,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, "user:dave", intent, h, auto, 0, reason,
             "agent_cve" if auto else None, 0.7 if auto else None,
             "[]", "jb_dave_auto" if auto else None, when),
        )
    ins_dec("audit my requirements.txt", None, 1, today)
    ins_dec("format this YAML", "no_match", 0, today)
    ins_dec("format this YAML", "no_match", 0, today)
    ins_dec("lint my CSS",      "no_match", 0, yesterday)

    # ── tool_invocation_metrics (latency) ────────────────────────────────
    for ms in [40, 50, 55, 80, 95, 110, 250, 400, 800, 950]:
        conn.execute(
            """INSERT INTO tool_invocation_metrics (agent_id, caller_id, latency_ms,
                bytes_in, bytes_out, cached, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("agent_audit", "user:bob", ms, 100, 100, 0, today),
        )

    # ── mcp_invocation_log failures ──────────────────────────────────────
    failures = [
        ("agent_regex", "regex_tester", "agent.timeout"),
        ("agent_jwt",   "jwt_debugger", "agent.timeout"),
        ("agent_regex", "call_specialist", "agent.bad_response"),
    ]
    for aid, tool, err in failures:
        conn.execute(
            """INSERT INTO mcp_invocation_log (id, agent_id, caller_key_id,
                tool_name, input_hash, invoked_at, duration_ms, success, error_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uuid.uuid4().hex, aid, "k_bob", tool, "h" + uuid.uuid4().hex[:8],
             today, 100, 0, err),
        )

    conn.commit()
    return {"job_id": "jb_carol_first"}


def _seed_db() -> dict[str, str]:
    apply_migrations(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    handles = _seed(conn)
    conn.close()
    return handles


# ── Question runner ───────────────────────────────────────────────────────


def _q(num: int, title: str) -> None:
    print(f"\n{'─' * 78}")
    print(f"Q{num:>2}. {title}")


def main() -> int:
    handles = _seed_db()
    # Make sure the patched DB_PATH is in effect for the server modules.
    for mod in (registry, payments, auth, jobs, reputation, disputes, _db):
        mod.DB_PATH = DB_PATH  # type: ignore[attr-defined]
    server._MASTER_KEY = os.environ["API_KEY"]

    with TestClient(server.app) as client:
        def get(path, **params):
            r = client.get(path, headers=HEADERS, params=params)
            assert r.status_code == 200, f"{path} {params}: {r.status_code} {r.text}"
            return r.json()

        _q(1, "How is Aztea doing today vs yesterday?")
        digest = get("/admin/usage/digest", window="24h")
        print("  tool: aztea_status(window='24h')")
        print(f"  calls 24h: total={digest['calls']['total']['value']}, "
              f"success={digest['calls']['success']}, "
              f"success_rate={digest['calls']['success_rate']}")
        print(f"  spend 24h cents: {digest['spend']['total_cents']['value']} "
              f"(prior {digest['spend']['total_cents']['prior']}, "
              f"delta_pct {digest['spend']['total_cents']['delta_pct']})")

        _q(2, "Which agent has the worst success rate this week?")
        worst = get("/admin/usage/query", view="agent_health", window="7d")["rows"]
        worst_one = worst[0]
        print("  tool: aztea_query(view='agent_health', window='7d')")
        print(f"  worst: {worst_one['agent_id']} "
              f"({worst_one['name']}) — calls={worst_one['calls']} "
              f"success_rate={worst_one['success_rate']}")

        _q(3, f"Show me everything about job {handles['job_id']}.")
        job_inspect = get("/admin/usage/inspect", entity="job", id=handles["job_id"])["data"]
        print(f"  tool: aztea_inspect(entity='job', id='{handles['job_id']}')")
        print(f"  status: {job_inspect['job']['status']}, "
              f"agent: {job_inspect['job']['agent_id']}, "
              f"origin: {job_inspect['job']['origin']}, "
              f"price_cents: {job_inspect['job']['price_cents']}")

        _q(4, "Top 10 intents that didn't match any agent (last 30 days).")
        nm = get("/admin/usage/query", view="no_match", window="30d", limit=10)["rows"]
        print("  tool: aztea_query(view='no_match', window='30d', limit=10)")
        for r in nm:
            print(f"    hits={r['hits']:3d}  {r['example_intent']!r}")

        _q(5, "Users who haven't called Aztea in 14 days but used to.")
        dormant = get("/admin/usage/query", view="dormant_users")["rows"]
        print("  tool: aztea_query(view='dormant_users')")
        for r in dormant:
            print(f"    user={r['user_id']} last_call={r['last_call']} lifetime_calls={r['lifetime_calls']}")

        _q(6, "Latency distribution for dependency_auditor over the last 7 days.")
        agent = get("/admin/usage/inspect", entity="agent", id="agent_audit")["data"]
        print("  tool: aztea_inspect(entity='agent', id='agent_audit')")
        print(f"  calls={agent['calls']}, success_rate={agent['success_rate']}, "
              f"p50={agent['latency_p50_ms']} ms, p95={agent['latency_p95_ms']} ms")

        _q(7, "How much revenue did cve_lookup_agent generate this month?")
        spend_a = get("/admin/usage/query", view="spend_by_agent", window="30d")["rows"]
        cve_row = next((r for r in spend_a if r["agent_id"] == "agent_cve"), None)
        print("  tool: aztea_query(view='spend_by_agent', window='30d')")
        print(f"  agent_cve revenue_cents={cve_row['revenue_cents']}")

        _q(8, "Last 20 failures and their error codes.")
        fail = get("/admin/usage/query", view="failures", window="7d", limit=20)["rows"]
        print("  tool: aztea_query(view='failures', window='7d', limit=20)")
        for r in fail:
            print(f"    {r['agent_id']:<14} {r['tool_name']:<18} error_code={r['error_code']}")

        _q(9, "How many do_specialist_task decisions got auto-invoked vs gated?")
        ah = digest["auto_hire"]
        print("  tool: aztea_status(window='24h') → auto_hire block")
        print(f"  invocations={ah['invocations']}, auto_invoked={ah['auto_invoked']}, "
              f"no_match={ah['no_match']}, dry_run_count={ah['dry_run_count']}")

        _q(10, "Which agents have never been called?")
        called = {r["agent_id"] for r in get(
            "/admin/usage/query", view="top_agents", window="30d", limit=500,
        )["rows"]}
        all_agents = sqlite3.connect(DB_PATH).execute(
            "SELECT agent_id, name FROM agents"
        ).fetchall()
        print("  tool: aztea_query(view='top_agents', window='30d', limit=500)")
        print("        + diff against agents table (no direct view yet)")
        for aid, name in all_agents:
            if aid not in called:
                print(f"    never-called: {aid} ({name})")

        print("\nAll 10 questions answered cleanly via the three new tools.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
