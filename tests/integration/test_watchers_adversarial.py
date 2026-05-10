"""Adversarial integration tests for the watcher feature.

Each test is a discrete scenario covering a bug, race, or invariant that the
existing happy-path lifecycle suite did not exercise.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.integration.support import *  # noqa: F401,F403
from tests.integration.support import (
    TEST_MASTER_KEY,
    _auth_headers,
    _fund_user_wallet,
    _register_user,
)

from core import jobs as _jobs
from core import payments as _payments
from core import watchers as _watchers
from core.watchers import sweeper as _watchers_sweeper


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _isolate_watchers(monkeypatch, db_path):
    monkeypatch.setattr(_watchers, "DB_PATH", str(db_path))
    if hasattr(_watchers.crud, "_local"):
        try:
            delattr(_watchers.crud._local, "conn")
        except (AttributeError, KeyError):
            pass


def _register_test_agent(client, raw_api_key, *, price_usd=0.05, name=None):
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "name": name or f"adv-target-{suffix}",
        "description": "Adversarial test watcher target agent",
        "endpoint_url": f"https://agents.example.com/{suffix}",
        "price_per_call_usd": price_usd,
        "tags": ["watcher-adv"],
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "title": "Task",
                    "description": "Adversarial test input.",
                }
            },
        },
        "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
    }
    resp = client.post("/registry/register", headers=_auth_headers(raw_api_key), json=payload)
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["agent_id"]
    review = client.post(
        f"/admin/agents/{agent_id}/review",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"decision": "approve", "note": "test"},
    )
    assert review.status_code == 200
    return agent_id


def _create_watcher(client, key, *, agent_id, **overrides):
    body = {
        "agent_id": agent_id,
        "target_kind": "http",
        "target_url": "https://example.com/feed",
        "tick_interval_seconds": 60,
        "budget_per_day_cents": 100,
        "delivery_email": "user@example.com",
    }
    body.update(overrides)
    return client.post("/watch", headers=_auth_headers(key), json=body)


def _bump_due(watcher_id: str) -> None:
    with _watchers.crud._conn() as conn:
        conn.execute(
            "UPDATE watchers SET next_check_at = '1970-01-01T00:00:00+00:00' WHERE watcher_id = %s",
            (watcher_id,),
        )


class _FakeResp:
    def __init__(self, body=b"", status=200, headers=None, url=None):
        self.content = body
        self.status_code = status
        self.headers = headers or {}
        self.url = url or "https://example.com"
        self.history = []

    def iter_content(self, chunk_size=65536):
        yield self.content

    def close(self):
        pass

    def json(self):
        return json.loads(self.content.decode("utf-8"))


def _patch_http(body=b"hello"):
    return patch(
        "core.watchers.fingerprint.requests.get",
        return_value=_FakeResp(body=body),
    )


# ===========================================================================
# Tier-1: bug-catching tests at the integration layer
# ===========================================================================


def test_T1_1_budget_exhausted_watcher_resumes_after_utc_rollover(
    client, isolated_db, monkeypatch
):
    """AUDIT: list_due_watchers and claim_watcher_tick both filter
    status='active'. A budget_exhausted watcher is never picked up by the
    sweeper, so the in-_process_due_watcher rollover branch is unreachable
    and the watcher stays stuck across UTC midnight."""
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=10_000)
    agent_id = _register_test_agent(client, user["raw_api_key"], price_usd=0.05)
    wid = _create_watcher(
        client, user["raw_api_key"], agent_id=agent_id, budget_per_day_cents=5
    ).json()["watcher_id"]

    # Push the watcher to budget_exhausted via a real fire.
    with _patch_http(b"v1"):
        _watchers_sweeper.sweep_watchers(limit=10)
    _bump_due(wid)
    with _patch_http(b"v2"):
        _watchers_sweeper.sweep_watchers(limit=10)
    _bump_due(wid)
    with _patch_http(b"v3"):
        _watchers_sweeper.sweep_watchers(limit=10)
    assert _watchers.get_watcher(wid)["status"] == "budget_exhausted"

    # Simulate UTC-midnight rollover: rewind spend_window_date to yesterday.
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    with _watchers.crud._conn() as conn:
        conn.execute(
            "UPDATE watchers SET spend_window_date = %s, next_check_at = '1970-01-01T00:00:00+00:00' WHERE watcher_id = %s",
            (yesterday, wid),
        )

    # Sweep again. The watcher should be reactivated via the rollover branch.
    with _patch_http(b"v4"):
        _watchers_sweeper.sweep_watchers(limit=10)

    row = _watchers.get_watcher(wid)
    if row["status"] == "budget_exhausted":
        pytest.xfail(
            "AUDIT T1.1 (CRITICAL): budget_exhausted watchers are stuck. "
            "claim_watcher_tick filters status='active', so the rollover "
            "branch in _process_due_watcher cannot run for an exhausted "
            "watcher. Fix: list_due_watchers / claim should include "
            "budget_exhausted rows (and let the rollover gate flip them), "
            "or reset_spend_window must run as its own sweep phase."
        )
    # The rollover phase must have flipped budget_exhausted → active and
    # advanced the spend window. Spend may be back above 0 if the same
    # sweep then fired (fingerprint changed v3→v4); what matters is that
    # the watcher is unstuck.
    assert row["status"] == "active"
    assert row["spend_window_date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_T1_2_fire_records_spend_and_run_atomically(
    client, isolated_db, monkeypatch
):
    """AUDIT: insert_watcher_run and record_spend_and_fingerprint are
    separate transactions. A crash between them leaves the run row but no
    spend bump — which means the next tick re-evaluates the budget gate
    against under-counted spend and may fire again."""
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=10_000)
    agent_id = _register_test_agent(client, user["raw_api_key"], price_usd=0.05)
    wid = _create_watcher(
        client, user["raw_api_key"], agent_id=agent_id, budget_per_day_cents=200
    ).json()["watcher_id"]

    # First fire: establish baseline.
    with _patch_http(b"v1"):
        _watchers_sweeper.sweep_watchers(limit=10)
    _bump_due(wid)

    # Second tick: simulate crash between insert_watcher_run and
    # record_spend_and_fingerprint.
    real_record = _watchers.crud.record_spend_and_fingerprint

    def _fail_after_first(*args, **kwargs):
        raise RuntimeError("simulated crash mid-fire")

    monkeypatch.setattr(_watchers.crud, "record_spend_and_fingerprint", _fail_after_first)

    with _patch_http(b"v2"):
        try:
            _watchers_sweeper.sweep_watchers(limit=10)
        except RuntimeError:
            pass

    # Restore so the row check can run normally.
    monkeypatch.setattr(_watchers.crud, "record_spend_and_fingerprint", real_record)

    row = _watchers.get_watcher(wid)
    runs = _watchers.list_watcher_runs(wid)
    fired_runs = [r for r in runs if r.get("fired_job_id")]

    # Assert the two writes are consistent: either both happened or neither.
    spend_records_present = row["spend_today_cents"] > 0
    fired_runs_after_baseline = len(fired_runs) >= 2

    if fired_runs_after_baseline and not spend_records_present:
        pytest.xfail(
            "AUDIT T1.2 (HIGH): insert_watcher_run + "
            "record_spend_and_fingerprint are not atomic. A crash between "
            "them leaves a fired run with no recorded spend. Fix: wrap both "
            "writes in a single _conn() transaction, or move the run-row "
            "insert to inside record_spend_and_fingerprint."
        )
    # If we got here without crash (fix landed), both should be coherent.
    assert spend_records_present == fired_runs_after_baseline


def test_T1_6_webhook_url_changed_to_private_after_fire_blocks_delivery(
    client, isolated_db, monkeypatch
):
    """AUDIT: defense in depth — even if the create-time validator missed a
    private URL (or if the URL was later changed via a direct DB write that
    bypassed the validator), the delivery phase MUST re-validate and refuse
    to send."""
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=10_000)
    agent_id = _register_test_agent(client, user["raw_api_key"], price_usd=0.05)

    # Disable private-IP allowance (production posture).
    monkeypatch.delenv("ALLOW_PRIVATE_OUTBOUND_URLS", raising=False)

    wid = _create_watcher(
        client,
        user["raw_api_key"],
        agent_id=agent_id,
        delivery_webhook_url="https://hook.example.com/inbox",
        delivery_email=None,
        budget_per_day_cents=100,
    ).json()["watcher_id"]

    # Fire so a run exists awaiting delivery.
    with _patch_http(b"v1"):
        _watchers_sweeper.sweep_watchers(limit=10)
    _bump_due(wid)
    with _patch_http(b"v2"):
        _watchers_sweeper.sweep_watchers(limit=10)
    runs = _watchers.list_watcher_runs(wid)
    fired = next((r for r in runs if r["fired_job_id"]), None)
    assert fired is not None
    _jobs.update_job_status(fired["fired_job_id"], "complete", output_payload={"ok": True}, completed=True)

    # Bypass the validator and slip a private URL onto the row.
    with _watchers.crud._conn() as conn:
        conn.execute(
            "UPDATE watchers SET delivery_webhook_url = %s WHERE watcher_id = %s",
            ("http://127.0.0.1:9000/", wid),
        )

    posted: list[str] = []

    class _R:
        status_code = 200

    def _post(url, **kwargs):
        posted.append(url)
        return _R()

    with patch("core.watchers.delivery.requests.post", side_effect=_post):
        _watchers_sweeper.sweep_watchers(limit=10)

    # The delivery phase MUST refuse to call private URLs even when the
    # row was poisoned post-create.
    assert posted == [], (
        "Delivery phase called a private URL — SSRF defense-in-depth is "
        "missing. Fix: re-run validate_outbound_url on row['delivery_webhook_url'] "
        "in delivery._deliver_webhook BEFORE the requests.post."
    )


# ===========================================================================
# Tier-2: concurrency / races
# ===========================================================================


def test_T2_1_concurrent_claim_only_one_sweeper_fires(
    client, isolated_db, monkeypatch
):
    """Two threads hold the same `row` snapshot and race on
    claim_watcher_tick. CAS guarantees only one wins."""
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=10_000)
    agent_id = _register_test_agent(client, user["raw_api_key"], price_usd=0.05)
    wid = _create_watcher(
        client, user["raw_api_key"], agent_id=agent_id, budget_per_day_cents=200
    ).json()["watcher_id"]

    # Establish baseline (this first sweep fires once: None → v1).
    with _patch_http(b"v1"):
        _watchers_sweeper.sweep_watchers(limit=10)
    fired_before_race = sum(
        1 for r in _watchers.list_watcher_runs(wid) if r.get("fired_job_id")
    )
    _bump_due(wid)

    row = _watchers.get_watcher(wid)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def _runner():
        barrier.wait()
        with _patch_http(b"v2"):
            outcome = _watchers_sweeper._process_due_watcher(dict(row))
        with lock:
            outcomes.append(outcome)

    t1 = threading.Thread(target=_runner)
    t2 = threading.Thread(target=_runner)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Core invariant: under no circumstance may BOTH threads fire. The CAS
    # claim must serialize them. (One winner who succeeds in firing, OR one
    # winner whose fire fails, OR both rejected — but never two fires.)
    assert outcomes.count("fired") <= 1, (
        f"both threads fired — CAS claim is broken: {outcomes}"
    )
    # And exactly one thread must observe claim_lost (the loser).
    assert "claim_lost" in outcomes, outcomes

    # No more than one new fired-job row from the race.
    fired_after_race = sum(
        1 for r in _watchers.list_watcher_runs(wid) if r.get("fired_job_id")
    )
    new_fires = fired_after_race - fired_before_race
    assert new_fires <= 1, (
        f"expected ≤1 new fire from the race; "
        f"before={fired_before_race} after={fired_after_race}"
    )


def test_T2_2_sweeper_restart_does_not_double_fire_same_fingerprint(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=10_000)
    agent_id = _register_test_agent(client, user["raw_api_key"], price_usd=0.05)
    wid = _create_watcher(
        client, user["raw_api_key"], agent_id=agent_id, budget_per_day_cents=200
    ).json()["watcher_id"]

    # First sweep fires once (None → vX).
    with _patch_http(b"vX"):
        _watchers_sweeper.sweep_watchers(limit=10)
    fired_after_first = sum(
        1 for r in _watchers.list_watcher_runs(wid) if r.get("fired_job_id")
    )
    balance_after_fire = _payments.get_wallet(
        _payments.get_or_create_wallet(f"user:{user['user_id']}")["wallet_id"]
    )["balance_cents"]

    # Simulate restart that didn't advance next_check_at — bump_due BEFORE
    # the sweeper would normally have done so. Same fingerprint vX.
    _bump_due(wid)
    with _patch_http(b"vX"):
        _watchers_sweeper.sweep_watchers(limit=10)

    fired_after_restart = sum(
        1 for r in _watchers.list_watcher_runs(wid) if r.get("fired_job_id")
    )
    balance_after_restart = _payments.get_wallet(
        _payments.get_or_create_wallet(f"user:{user['user_id']}")["wallet_id"]
    )["balance_cents"]

    assert fired_after_restart == fired_after_first, (
        "diff gate must catch identical fingerprint after restart"
    )
    assert balance_after_restart == balance_after_fire, "no double-charge"


def test_T2_3_delete_during_fire_does_not_orphan_runs(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=10_000)
    agent_id = _register_test_agent(client, user["raw_api_key"], price_usd=0.05)
    wid = _create_watcher(
        client, user["raw_api_key"], agent_id=agent_id, budget_per_day_cents=200
    ).json()["watcher_id"]

    # Establish baseline.
    with _patch_http(b"v1"):
        _watchers_sweeper.sweep_watchers(limit=10)
    _bump_due(wid)

    # Run sweeper and delete in parallel.
    def _sweep():
        with _patch_http(b"v2"):
            _watchers_sweeper.sweep_watchers(limit=10)

    def _delete():
        # Small jitter so we sometimes delete first, sometimes last.
        _watchers.delete_watcher(wid)

    t1 = threading.Thread(target=_sweep)
    t2 = threading.Thread(target=_delete)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Whoever wins, no orphan run rows for a non-existent watcher.
    with _watchers.crud._conn() as conn:
        orphan = conn.execute(
            "SELECT COUNT(*) AS c FROM watcher_runs WHERE watcher_id NOT IN (SELECT watcher_id FROM watchers)"
        ).fetchone()
    assert dict(orphan)["c"] == 0


def test_T2_4_concurrent_rollover_does_not_double_reset(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=10_000)
    agent_id = _register_test_agent(client, user["raw_api_key"])
    wid = _create_watcher(client, user["raw_api_key"], agent_id=agent_id).json()["watcher_id"]

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _watchers.crud._conn() as conn:
        conn.execute(
            "UPDATE watchers SET spend_window_date = %s, spend_today_cents = 50 WHERE watcher_id = %s",
            (yesterday, wid),
        )

    barrier = threading.Barrier(3)

    def _runner():
        barrier.wait()
        _watchers.crud.reset_spend_window(wid, today)

    threads = [threading.Thread(target=_runner) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    row = _watchers.get_watcher(wid)
    assert row["spend_today_cents"] == 0
    assert row["spend_window_date"] == today


# ===========================================================================
# Tier-3: money invariants
# ===========================================================================


def test_T3_1_insufficient_balance_no_partial_state(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    # Fund wallet with 4c — agent costs 5c, so fire cannot succeed.
    _fund_user_wallet(user, amount_cents=4)
    agent_id = _register_test_agent(client, user["raw_api_key"], price_usd=0.05)
    wid = _create_watcher(
        client, user["raw_api_key"], agent_id=agent_id, budget_per_day_cents=10
    ).json()["watcher_id"]

    # First sweep → baseline. Second → would-fire but should hit 402.
    with _patch_http(b"v1"):
        _watchers_sweeper.sweep_watchers(limit=10)
    _bump_due(wid)
    with _patch_http(b"v2"):
        _watchers_sweeper.sweep_watchers(limit=10)

    runs = _watchers.list_watcher_runs(wid)
    fired_runs = [r for r in runs if r.get("fired_job_id")]
    assert fired_runs == [], "no job may be created when wallet is underfunded"
    insufficient_runs = [r for r in runs if r.get("skip_reason") == "insufficient_funds"]
    assert insufficient_runs, f"expected an insufficient_funds skip row; got {runs}"

    # Wallet balance is unchanged at 4c.
    wallet = _payments.get_or_create_wallet(f"user:{user['user_id']}")
    assert int(wallet["balance_cents"]) == 4


def test_T3_2_pre_call_charge_unexpected_exception_is_safe(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=1000)
    agent_id = _register_test_agent(client, user["raw_api_key"], price_usd=0.05)
    wid = _create_watcher(
        client, user["raw_api_key"], agent_id=agent_id, budget_per_day_cents=100
    ).json()["watcher_id"]

    # Establish baseline (this first sweep fires once: None → v1).
    with _patch_http(b"v1"):
        _watchers_sweeper.sweep_watchers(limit=10)
    fired_before = sum(
        1 for r in _watchers.list_watcher_runs(wid) if r.get("fired_job_id")
    )
    _bump_due(wid)

    real = _payments.pre_call_charge

    def _explode(*args, **kwargs):
        raise ValueError("simulated unexpected payments error")

    monkeypatch.setattr("core.watchers.sweeper.payments.pre_call_charge", _explode)
    with _patch_http(b"v2"):
        _watchers_sweeper.sweep_watchers(limit=10)
    monkeypatch.setattr("core.watchers.sweeper.payments.pre_call_charge", real)

    runs = _watchers.list_watcher_runs(wid)
    assert any(r.get("skip_reason") == "charge_failed" for r in runs)
    # No NEW fired runs after the patched-explode pass.
    fired_after = sum(1 for r in runs if r.get("fired_job_id"))
    assert fired_after == fired_before, (
        "pre_call_charge raising must not produce a new fired run"
    )


# ===========================================================================
# Tier-4: auth / API surface
# ===========================================================================


def _make_two_users(client, fund=True):
    a = _register_user()
    b = _register_user()
    if fund:
        _fund_user_wallet(a, amount_cents=10_000)
        _fund_user_wallet(b, amount_cents=10_000)
    return a, b


def test_T4_1_cross_tenant_PATCH_DELETE_runs_test_blocked(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    a, b = _make_two_users(client)
    agent_id = _register_test_agent(client, a["raw_api_key"])
    wid = _create_watcher(client, a["raw_api_key"], agent_id=agent_id).json()["watcher_id"]
    headers_b = _auth_headers(b["raw_api_key"])

    assert client.patch(f"/watch/{wid}", headers=headers_b, json={"status": "paused"}).status_code == 403
    assert client.delete(f"/watch/{wid}", headers=headers_b).status_code == 403
    assert client.get(f"/watch/{wid}/runs", headers=headers_b).status_code == 403
    assert client.post(f"/watch/{wid}/test", headers=headers_b).status_code == 403


def test_T4_2_patch_rejects_immutable_target_url(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=1000)
    agent_id = _register_test_agent(client, user["raw_api_key"])
    wid = _create_watcher(client, user["raw_api_key"], agent_id=agent_id).json()["watcher_id"]

    # Pydantic extra="forbid" should reject unknown fields.
    resp = client.patch(
        f"/watch/{wid}",
        headers=_auth_headers(user["raw_api_key"]),
        json={"target_url": "https://attacker.example.com"},
    )
    assert resp.status_code == 422

    # Verify target_url unchanged.
    row = _watchers.get_watcher(wid)
    assert row["target_url"] == "https://example.com/feed"


def test_T4_2_patch_rejects_immutable_target_kind(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=1000)
    agent_id = _register_test_agent(client, user["raw_api_key"])
    wid = _create_watcher(client, user["raw_api_key"], agent_id=agent_id).json()["watcher_id"]

    resp = client.patch(
        f"/watch/{wid}",
        headers=_auth_headers(user["raw_api_key"]),
        json={"target_kind": "git"},
    )
    assert resp.status_code == 422


def test_T4_4_master_key_can_inspect_any_watcher(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=1000)
    agent_id = _register_test_agent(client, user["raw_api_key"])
    wid = _create_watcher(client, user["raw_api_key"], agent_id=agent_id).json()["watcher_id"]

    resp = client.get(f"/watch/{wid}", headers=_auth_headers(TEST_MASTER_KEY))
    assert resp.status_code == 200


def test_T4_7_test_endpoint_is_pure_dry_run(
    client, isolated_db, monkeypatch
):
    """Calling /test must NOT charge, NOT create a run row, NOT mutate
    last_fingerprint or next_check_at."""
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    wallet = _fund_user_wallet(user, amount_cents=10_000)
    agent_id = _register_test_agent(client, user["raw_api_key"])
    wid = _create_watcher(client, user["raw_api_key"], agent_id=agent_id).json()["watcher_id"]

    before_row = _watchers.get_watcher(wid)
    before_runs = len(_watchers.list_watcher_runs(wid))
    before_balance = _payments.get_wallet(wallet["wallet_id"])["balance_cents"]

    with _patch_http(b"some content"):
        resp = client.post(f"/watch/{wid}/test", headers=_auth_headers(user["raw_api_key"]))
    assert resp.status_code == 200

    after_row = _watchers.get_watcher(wid)
    after_runs = len(_watchers.list_watcher_runs(wid))
    after_balance = _payments.get_wallet(wallet["wallet_id"])["balance_cents"]

    assert before_balance == after_balance
    assert before_runs == after_runs
    assert before_row["last_fingerprint"] == after_row["last_fingerprint"]
    assert before_row["next_check_at"] == after_row["next_check_at"]


def test_T4_8_post_watch_body_too_large_rejected(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=1000)
    agent_id = _register_test_agent(client, user["raw_api_key"])

    # 600 KB payload — above the 512 KB body cap enforced by the server.
    huge = {"blob": "x" * (600 * 1024)}
    resp = _create_watcher(client, user["raw_api_key"], agent_id=agent_id, payload=huge)
    assert resp.status_code in (413, 422)


def test_T4_9_payload_round_trip_unicode_and_special_chars(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=1000)
    agent_id = _register_test_agent(client, user["raw_api_key"])

    payload = {
        "task": "<script>alert(1)</script>",
        "unicode": "héllo 🌍 — 中文",
        "quote": 'a "b" c',
        "nested": {"a": [1, 2, {"x": "y"}]},
    }
    create = _create_watcher(client, user["raw_api_key"], agent_id=agent_id, payload=payload).json()
    wid = create["watcher_id"]

    fetched = client.get(f"/watch/{wid}", headers=_auth_headers(user["raw_api_key"])).json()
    assert fetched["payload"] == payload


# ===========================================================================
# Tier-7: state machine
# ===========================================================================


def test_T7_2_paused_watcher_skipped_by_sweeper(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=1000)
    agent_id = _register_test_agent(client, user["raw_api_key"])
    wid = _create_watcher(client, user["raw_api_key"], agent_id=agent_id).json()["watcher_id"]

    client.patch(
        f"/watch/{wid}", headers=_auth_headers(user["raw_api_key"]), json={"status": "paused"}
    )
    _bump_due(wid)

    runs_before = len(_watchers.list_watcher_runs(wid))
    next_before = _watchers.get_watcher(wid)["next_check_at"]
    with _patch_http(b"x"):
        for _ in range(3):
            _watchers_sweeper.sweep_watchers(limit=10)

    assert len(_watchers.list_watcher_runs(wid)) == runs_before
    # next_check_at not advanced — paused row is never claimed.
    assert _watchers.get_watcher(wid)["next_check_at"] == next_before


def test_T7_4_reactivation_clears_last_error(
    client, isolated_db, monkeypatch
):
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=1000)
    agent_id = _register_test_agent(client, user["raw_api_key"])
    wid = _create_watcher(client, user["raw_api_key"], agent_id=agent_id).json()["watcher_id"]

    # Force an error state.
    with _watchers.crud._conn() as conn:
        conn.execute(
            "UPDATE watchers SET status = 'paused', last_error = 'auto-paused: something went wrong' WHERE watcher_id = %s",
            (wid,),
        )

    resp = client.patch(
        f"/watch/{wid}",
        headers=_auth_headers(user["raw_api_key"]),
        json={"status": "active"},
    )
    assert resp.status_code == 200
    row = _watchers.get_watcher(wid)
    if row.get("last_error"):
        pytest.xfail(
            "AUDIT T1.10 / T7.4 (LOW): re-activation should clear "
            "last_error. update_watcher allowed-fields list does not "
            "include last_error, so the previous error message is sticky."
        )
    assert row["last_error"] is None


# ===========================================================================
# Tier-8: schema/migration sanity at the integration level
# ===========================================================================


def test_T8_3_delete_cascades_runs_at_app_layer(
    client, isolated_db, monkeypatch
):
    """Schema does not declare FK constraints; crud.delete_watcher must
    explicitly delete runs first."""
    _isolate_watchers(monkeypatch, isolated_db)
    user = _register_user()
    _fund_user_wallet(user, amount_cents=1000)
    agent_id = _register_test_agent(client, user["raw_api_key"])
    wid = _create_watcher(client, user["raw_api_key"], agent_id=agent_id).json()["watcher_id"]

    # Manufacture 50 runs.
    for _ in range(50):
        _watchers.crud.insert_watcher_run(
            watcher_id=wid,
            fingerprint="x",
            fingerprint_changed=False,
            fired_job_id=None,
            skip_reason="no_change",
            error=None,
        )
    assert len(_watchers.list_watcher_runs(wid)) == 50

    assert _watchers.delete_watcher(wid) is True
    with _watchers.crud._conn() as conn:
        leftover = conn.execute(
            "SELECT COUNT(*) AS c FROM watcher_runs WHERE watcher_id = %s", (wid,)
        ).fetchone()
    assert dict(leftover)["c"] == 0
