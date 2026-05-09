"""Server integration tests (auto-split fragment 3/6)."""

from tests.integration.support import *  # noqa: F403


def test_jobs_batch_status_endpoint_returns_aggregate_counts(client):
    worker_owner = _register_user()
    caller = _register_user()
    outsider = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"Batch Status Agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["batch-status"],
    )

    created = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "jobs": [
                {"agent_id": agent_id, "input_payload": {"task": "a"}},
                {"agent_id": agent_id, "input_payload": {"task": "b"}},
            ]
        },
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert created_body["count"] == 2
    assert created_body["total_price_cents"] == 12
    assert created_body["mode"] == "parallel_marketplace_hire"
    assert created_body["parallel_hire_trace"]["summary"].startswith(
        "2 specialist hires"
    )
    assert created_body["marketplace_transaction"]["escrow"] == "opened_per_job"
    batch_id = created_body["batch_id"]

    status = client.get(
        f"/jobs/batch/{batch_id}",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert status.status_code == 200, status.text
    status_body = status.json()
    assert status_body["batch_id"] == batch_id
    assert status_body["count"] == 2
    assert status_body["n_pending"] == 2
    assert status_body["n_complete"] == 0
    assert status_body["n_failed"] == 0
    assert status_body["total_cost_cents"] == 10
    assert status_body["total_charged_cents"] == 12
    assert status_body["parallel_hire_trace"]["batch_id"] == batch_id
    assert status_body["marketplace_transaction"]["rail"] == "jobs.batch"
    assert all(job["batch_id"] == batch_id for job in status_body["jobs"])

    blocked = client.get(
        f"/jobs/batch/{batch_id}",
        headers=_auth_headers(outsider["raw_api_key"]),
    )
    assert blocked.status_code == 404


def test_jobs_batch_create_rejects_total_cap_before_charge(client):
    worker_owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker_owner["raw_api_key"],
        name=f"Batch Cap Agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["batch-cap"],
    )
    created = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "max_total_cents": 5,
            "jobs": [{"agent_id": agent_id, "input_payload": {"task": "a"}}],
        },
    )
    assert created.status_code == 400, created.text
    body = created.json()
    assert "Batch total exceeds max_total_cents" in str(body)


def test_admin_scope_controls_ops_endpoints(client):
    user = _register_user()

    caller_key_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "caller-only", "scopes": ["caller"], "per_job_cap_cents": 500},
    )
    assert caller_key_resp.status_code == 201
    caller_key = caller_key_resp.json()["raw_key"]

    admin_key_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "admin-only", "scopes": ["admin"]},
    )
    assert admin_key_resp.status_code == 201
    admin_key = admin_key_resp.json()["raw_key"]

    blocked_metrics = client.get("/ops/jobs/metrics", headers=_auth_headers(caller_key))
    assert blocked_metrics.status_code == 403

    allowed_metrics = client.get("/ops/jobs/metrics", headers=_auth_headers(admin_key))
    assert allowed_metrics.status_code == 200

    blocked_slo = client.get("/ops/jobs/slo", headers=_auth_headers(caller_key))
    assert blocked_slo.status_code == 403

    allowed_slo = client.get("/ops/jobs/slo", headers=_auth_headers(admin_key))
    assert allowed_slo.status_code == 200
    assert "slo" in allowed_slo.json()

    blocked_sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(caller_key),
        json={"retry_delay_seconds": 0, "sla_seconds": 60, "limit": 10},
    )
    assert blocked_sweep.status_code == 403

    allowed_sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(admin_key),
        json={"retry_delay_seconds": 0, "sla_seconds": 60, "limit": 10},
    )
    assert allowed_sweep.status_code == 200


def test_payments_reconciliation_and_settlement_trace_endpoints(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Settlement Trace Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["settlement-trace"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert completed.status_code == 200, completed.text
    settled = _force_settle_completed_job(job_id)
    assert settled["settled_at"] is not None

    trace = client.get(
        f"/ops/jobs/{job_id}/settlement-trace",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert trace.status_code == 200, trace.text
    trace_body = trace.json()
    tx_types = {tx["type"] for tx in trace_body["transactions"]}
    assert {"charge", "payout", "fee"}.issubset(tx_types)
    assert trace_body["expected_agent_payout_cents"] == 10
    assert trace_body["expected_platform_fee_cents"] == 1

    preview = client.get(
        "/ops/payments/reconcile",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["invariant_ok"] is True

    run = client.post(
        "/ops/payments/reconcile",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"max_mismatches": 50},
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]

    runs = client.get(
        "/ops/payments/reconcile/runs?limit=5",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert runs.status_code == 200, runs.text
    assert any(item["run_id"] == run_id for item in runs.json()["runs"])


def test_fee_distribution_policies_cover_caller_worker_split():
    caller_policy = payments.compute_success_distribution(
        10,
        platform_fee_pct=10,
        fee_bearer_policy="caller",
    )
    assert caller_policy == {
        "caller_charge_cents": 11,
        "agent_payout_cents": 10,
        "platform_fee_cents": 1,
    }

    worker_policy = payments.compute_success_distribution(
        10,
        platform_fee_pct=10,
        fee_bearer_policy="worker",
    )
    assert worker_policy == {
        "caller_charge_cents": 10,
        "agent_payout_cents": 9,
        "platform_fee_cents": 1,
    }

    split_policy = payments.compute_success_distribution(
        25,
        platform_fee_pct=10,
        fee_bearer_policy="split",
    )
    assert split_policy == {
        "caller_charge_cents": 27,
        "agent_payout_cents": 24,
        "platform_fee_cents": 3,
    }


def test_listing_and_job_create_show_caller_all_in_charge(client):
    worker = _register_user()
    caller = _register_user()
    caller_wallet = _fund_user_wallet(caller, 200)
    before_balance = payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"]

    tag = f"all-in-{uuid.uuid4().hex[:6]}"
    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"All In Charge Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=[tag],
    )

    listings = client.get(
        f"/registry/agents?tag={tag}",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert listings.status_code == 200, listings.text
    listing_agent = next(
        agent for agent in listings.json()["agents"] if agent["agent_id"] == agent_id
    )
    assert listing_agent["caller_charge_cents"] == 11

    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    assert created["caller_charge_cents"] == 11
    assert created["fee_bearer_policy"] == "caller"

    after_balance = payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"]
    assert before_balance - after_balance == 11


def test_topup_session_enforces_daily_limit(client, monkeypatch):
    user = _register_user()
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")

    import sqlite3

    with sqlite3.connect(jobs.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO stripe_sessions (session_id, wallet_id, amount_cents, processed_at) VALUES (?, ?, ?, ?)",
            (
                f"cs_{uuid.uuid4().hex[:10]}",
                wallet["wallet_id"],
                9_500,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    fake_checkout = SimpleNamespace(
        Session=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(
                url="https://checkout.example/session", id="cs_test_123"
            )
        )
    )
    monkeypatch.setattr(server, "_STRIPE_AVAILABLE", True)
    monkeypatch.setattr(server, "_STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(server, "_TOPUP_DAILY_LIMIT_CENTS", 10_000)
    monkeypatch.setattr(
        server, "_stripe_lib", SimpleNamespace(api_key=None, checkout=fake_checkout)
    )

    blocked = client.post(
        "/wallets/topup/session",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 600},
    )
    assert blocked.status_code == 400, blocked.text
    blocked_body = blocked.json()
    assert blocked_body["error"] == "payment.topup_daily_limit_exceeded"
    assert blocked_body["details"]["limit_cents"] == 10_000

    allowed = client.post(
        "/wallets/topup/session",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 500},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["session_id"] == "cs_test_123"


def test_wallet_deposit_enforces_minimum_amount(client):
    user = _register_user()
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")

    below = client.post(
        "/wallets/deposit",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 499, "memo": "too low"},
    )
    assert below.status_code == 422, below.text
    below_body = below.json()
    assert below_body["error"] == error_codes.DEPOSIT_BELOW_MINIMUM
    assert below_body["details"]["minimum_cents"] == server.MINIMUM_DEPOSIT_CENTS
    assert below_body["details"]["attempted_cents"] == 499

    allowed = client.post(
        "/wallets/deposit",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 500, "memo": "ok"},
    )
    assert allowed.status_code == 200, allowed.text


def test_wallet_topup_session_enforces_minimum_amount(client, monkeypatch):
    user = _register_user()
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    fake_checkout = SimpleNamespace(
        Session=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(
                url="https://checkout.example/session", id="cs_test_minimum"
            )
        )
    )
    monkeypatch.setattr(server, "_STRIPE_AVAILABLE", True)
    monkeypatch.setattr(server, "_STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(
        server, "_stripe_lib", SimpleNamespace(api_key=None, checkout=fake_checkout)
    )

    below = client.post(
        "/wallets/topup/session",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 499},
    )
    assert below.status_code == 422, below.text
    below_body = below.json()
    assert below_body["error"] == error_codes.DEPOSIT_BELOW_MINIMUM
    assert below_body["details"]["minimum_cents"] == server.MINIMUM_DEPOSIT_CENTS
    assert below_body["details"]["attempted_cents"] == 499

    allowed = client.post(
        "/wallets/topup/session",
        headers=_auth_headers(user["raw_api_key"]),
        json={"wallet_id": wallet["wallet_id"], "amount_cents": 500},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["session_id"] == "cs_test_minimum"


def test_stripe_webhook_retries_after_transient_deposit_failure(client, monkeypatch):
    user = _register_user()
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    session_id = f"cs_{uuid.uuid4().hex[:10]}"
    amount_cents = 1500
    fake_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": SimpleNamespace(
                id=session_id,
                client_reference_id=wallet["wallet_id"],
                amount_total=amount_cents,
                metadata={},
            )
        },
    }

    class _FakeWebhook:
        @staticmethod
        def construct_event(_payload, _sig, _secret):
            return fake_event

    monkeypatch.setattr(server, "_STRIPE_AVAILABLE", True)
    monkeypatch.setattr(server, "_STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(server, "_STRIPE_WEBHOOK_SECRET", "whsec_test_123")
    monkeypatch.setattr(
        server, "_stripe_lib", SimpleNamespace(api_key=None, Webhook=_FakeWebhook)
    )

    real_deposit = payments.deposit
    attempts = {"count": 0}

    def _flaky_deposit(wallet_id: str, cents: int, memo: str):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary downstream failure")
        return real_deposit(wallet_id, cents, memo)

    monkeypatch.setattr(payments, "deposit", _flaky_deposit)

    first = client.post(
        "/stripe/webhook",
        headers={"stripe-signature": "sig_test"},
        content=b"{}",
    )
    assert first.status_code == 500, first.text
    assert first.json()["status"] == "deposit_failed"

    second = client.post(
        "/stripe/webhook",
        headers={"stripe-signature": "sig_test"},
        content=b"{}",
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ok"

    refreshed_wallet = payments.get_wallet(wallet["wallet_id"])
    assert refreshed_wallet is not None
    assert refreshed_wallet["balance_cents"] == amount_cents


def test_wallet_withdrawals_returns_only_caller_wallet_history(client):
    user = _register_user()
    other = _register_user()
    wallet = payments.get_or_create_wallet(f"user:{user['user_id']}")
    other_wallet = payments.get_or_create_wallet(f"user:{other['user_id']}")

    import sqlite3

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(jobs.DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_connect_transfers (
                transfer_id   TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL,
                stripe_tx_id  TEXT NOT NULL,
                memo          TEXT,
                created_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO stripe_connect_transfers
                (transfer_id, wallet_id, amount_cents, stripe_tx_id, memo, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                wallet["wallet_id"],
                1234,
                "tr_user_123",
                "Withdrawal to bank",
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO stripe_connect_transfers
                (transfer_id, wallet_id, amount_cents, stripe_tx_id, memo, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                other_wallet["wallet_id"],
                4321,
                "tr_other_456",
                "Other withdrawal",
                now,
            ),
        )
        conn.commit()

    response = client.get(
        "/wallets/withdrawals?limit=10", headers=_auth_headers(user["raw_api_key"])
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert len(body["withdrawals"]) == 1
    item = body["withdrawals"][0]
    assert item["wallet_id"] == wallet["wallet_id"]
    assert item["amount_cents"] == 1234
    assert item["status"] == "complete"


def test_outbound_url_validation_blocks_private_targets_by_default(client):
    user = _register_user()

    hook_resp = client.post(
        "/ops/jobs/hooks",
        headers=_auth_headers(user["raw_api_key"]),
        json={"target_url": "http://127.0.0.1:9999/hook"},
    )
    assert hook_resp.status_code == 422
    assert "private/loopback" in hook_resp.json()["message"]

    manifest_resp = client.post(
        "/onboarding/validate",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_url": "http://localhost:8000/agent.md"},
    )
    assert manifest_resp.status_code == 422
    assert "localhost" in manifest_resp.json()["message"]


def test_manifest_url_redirects_are_blocked(client, monkeypatch):
    user = _register_user()
    captured: dict[str, object] = {}

    class _RedirectResponse:
        status_code = 302
        headers = {"Location": "http://127.0.0.1/internal"}
        content = b""
        text = ""

        @staticmethod
        def raise_for_status():
            return None

    def _fake_get(url, timeout=None, allow_redirects=None):
        captured["url"] = url
        captured["allow_redirects"] = allow_redirects
        return _RedirectResponse()

    monkeypatch.setattr(server.http, "get", _fake_get)
    manifest_resp = client.post(
        "/onboarding/validate",
        headers=_auth_headers(user["raw_api_key"]),
        json={"manifest_url": "https://docs.example.com/agent.md"},
    )
    assert manifest_resp.status_code == 502
    assert "redirect" in manifest_resp.json()["message"].lower()
    assert captured["allow_redirects"] is False


def test_job_callback_url_delivery_is_signed_and_contains_terminal_output(
    client, monkeypatch
):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Callback Agent {uuid.uuid4().hex[:6]}",
        tags=["callback"],
    )

    callback_url = "https://hooks.example.com/job-callback"
    callback_secret = "super-secret-callback-key"
    callback_requests: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None, allow_redirects=None):
        callback_requests.append(
            {
                "url": url,
                "data": data,
                "headers": headers or {},
            }
        )
        resp = requests.Response()
        resp.status_code = 204
        resp._content = b""
        return resp

    monkeypatch.setattr(server.http, "post", fake_post)

    created = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "deliver callback"},
            "callback_url": callback_url,
            "callback_secret": callback_secret,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 200, claim.text

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "output_payload": {"ok": True, "source": "specialist"},
            "claim_token": claim.json()["claim_token"],
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete"

    processed = client.post(
        "/ops/jobs/hooks/process",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"limit": 100},
    )
    assert processed.status_code == 200, processed.text
    assert processed.json()["delivered"] >= 1

    callback_match = next(
        (entry for entry in callback_requests if entry["url"] == callback_url), None
    )
    assert callback_match is not None
    payload_bytes = callback_match["data"]
    assert isinstance(payload_bytes, (bytes, bytearray))
    payload = json.loads(payload_bytes.decode("utf-8"))
    assert payload["job_id"] == job_id
    assert payload["status"] == "complete"
    assert payload["output_payload"] == {"ok": True, "source": "specialist"}

    signature = callback_match["headers"].get("X-Aztea-Signature")
    expected_signature = (
        "sha256="
        + hmac.new(
            callback_secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()
    )
    assert signature == expected_signature


def test_job_sweeper_handles_timeouts_sla_and_event_hooks(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Ops Sweeper Agent {uuid.uuid4().hex[:6]}",
        tags=["ops-sweeper"],
    )

    hook_events: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None, allow_redirects=None):
        payload = {}
        if data:
            payload = json.loads(data.decode("utf-8"))
        hook_events.append({"url": url, "headers": headers or {}, "payload": payload})
        resp = requests.Response()
        resp.status_code = 204
        resp._content = b""
        return resp

    monkeypatch.setattr(server.http, "post", fake_post)

    hook_resp = client.post(
        "/ops/jobs/hooks",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"target_url": "https://hooks.example.com/jobs"},
    )
    assert hook_resp.status_code == 201, hook_resp.text

    timeout_job = _create_job_via_api(
        client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2
    )
    timeout_job_id = timeout_job["job_id"]
    claim = client.post(
        f"/jobs/{timeout_job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 60},
    )
    assert claim.status_code == 200

    sla_job = _create_job_via_api(
        client, caller["raw_api_key"], agent_id=agent_id, max_attempts=1
    )
    sla_job_id = sla_job["job_id"]
    retry_job = _create_job_via_api(
        client, caller["raw_api_key"], agent_id=agent_id, max_attempts=3
    )
    retry_job_id = retry_job["job_id"]

    with jobs._conn() as conn:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        retry_due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_expires_at = ? WHERE job_id = ?",
            (expired, timeout_job_id),
        )
        # SLA timeout fires only on ACTIVELY-LEASED jobs (running /
        # awaiting_clarification). Queue wait is not an SLA breach —
        # pending jobs sitting behind a bursty batch must remain
        # claimable. Mark the job claimed by a worker so the sweeper
        # treats the 2-hour-old created_at as an execution SLA breach.
        # See core/jobs/leases.py:list_jobs_past_sla for the rule.
        sla_lease_active = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        conn.execute(
            """
            UPDATE jobs
            SET created_at = ?,
                updated_at = ?,
                status = 'running',
                claim_owner_id = ?,
                claim_token = ?,
                claimed_at = ?,
                lease_expires_at = ?,
                last_heartbeat_at = ?
            WHERE job_id = ?
            """,
            (
                old,
                old,
                f"user:{worker['user_id']}",
                "sla-claim-token",
                old,
                sla_lease_active,
                old,
                sla_job_id,
            ),
        )
        conn.execute(
            """
            UPDATE jobs
            SET status = 'pending',
                next_retry_at = ?,
                last_retry_at = ?,
                claim_owner_id = ?,
                claim_token = ?,
                claimed_at = ?,
                lease_expires_at = ?,
                last_heartbeat_at = ?
            WHERE job_id = ?
            """,
            (
                retry_due,
                retry_due,
                f"user:{worker['user_id']}",
                "stale-claim-token",
                retry_due,
                retry_due,
                retry_due,
                retry_job_id,
            ),
        )

    sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"retry_delay_seconds": 0, "sla_seconds": 60, "limit": 100},
    )
    assert sweep.status_code == 200, sweep.text
    summary = sweep.json()
    assert timeout_job_id in summary["timeout_retry_job_ids"]
    assert timeout_job_id not in summary["timeout_failed_job_ids"]
    assert sla_job_id in summary["sla_failed_job_ids"]
    assert retry_job_id in summary["retry_ready_job_ids"]
    assert summary["retry_ready_count"] >= 1

    process = client.post(
        "/ops/jobs/hooks/process",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"limit": 200},
    )
    assert process.status_code == 200, process.text

    timeout_state = client.get(
        f"/jobs/{timeout_job_id}", headers=_auth_headers(caller["raw_api_key"])
    )
    sla_state = client.get(
        f"/jobs/{sla_job_id}", headers=_auth_headers(caller["raw_api_key"])
    )
    retry_state = client.get(
        f"/jobs/{retry_job_id}", headers=_auth_headers(caller["raw_api_key"])
    )
    assert timeout_state.status_code == 200
    assert sla_state.status_code == 200
    assert retry_state.status_code == 200
    assert timeout_state.json()["status"] == "pending"
    assert timeout_state.json()["next_retry_at"] is None
    assert sla_state.json()["status"] == "failed"
    assert retry_state.json()["status"] == "pending"
    assert retry_state.json()["next_retry_at"] is None
    assert retry_state.json()["last_retry_at"] is None
    assert retry_state.json()["claim_owner_id"] is None
    assert retry_state.json().get("claim_token") is None
    assert retry_state.json()["lease_expires_at"] is None
    assert retry_state.json()["last_heartbeat_at"] is None

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    assert payments.get_wallet(caller_wallet["wallet_id"])[
        "balance_cents"
    ] == 300 - int(timeout_job["caller_charge_cents"]) - int(
        retry_job["caller_charge_cents"]
    )

    events = client.get(
        "/ops/jobs/events", headers=_auth_headers(caller["raw_api_key"])
    )
    assert events.status_code == 200
    event_types = {event["event_type"] for event in events.json()["events"]}
    assert "job.timeout_retry_scheduled" in event_types
    assert "job.sla_expired" in event_types
    assert "retry_ready" in event_types

    hook_event_types = {entry["payload"].get("event_type") for entry in hook_events}
    assert "job.timeout_retry_scheduled" in hook_event_types
    assert "job.sla_expired" in hook_event_types
    assert "retry_ready" in hook_event_types

    metrics = client.get("/ops/jobs/metrics", headers=_auth_headers(TEST_MASTER_KEY))
    assert metrics.status_code == 200
    body = metrics.json()
    assert "status_counts" in body
    assert "alerts" in body
    assert "hook_delivery" in body
    assert "slo" in body
    assert body["retry_ready_last_sweep"] >= 1


def test_hook_delivery_dead_letter_listing(client, monkeypatch):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    monkeypatch.setattr(server, "_HOOK_DELIVERY_MAX_ATTEMPTS", 1)

    def always_fail_post(
        url, data=None, headers=None, timeout=None, allow_redirects=None
    ):
        raise requests.RequestException("hook unavailable")

    monkeypatch.setattr(server.http, "post", always_fail_post)

    hook_resp = client.post(
        "/ops/jobs/hooks",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"target_url": "https://hooks.example.com/unavailable"},
    )
    assert hook_resp.status_code == 201, hook_resp.text

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Deadletter Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["dead-letter"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id)
    assert created["agent_id"] == agent_id

    processed = client.post(
        "/ops/jobs/hooks/process",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"limit": 50},
    )
    assert processed.status_code == 200, processed.text

    dead_letters = client.get(
        "/ops/jobs/hooks/dead-letter",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert dead_letters.status_code == 200, dead_letters.text
    assert dead_letters.json()["count"] >= 1


def test_jobs_batch_dry_run_does_not_charge_or_create(client):
    worker = _register_user()
    caller = _register_user()
    wallet = _fund_user_wallet(caller, 200)
    balance_before = payments.get_wallet(wallet["wallet_id"])["balance_cents"]

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Batch DryRun Agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["batch-dry-run"],
    )

    resp = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "intent": "preview only",
            "dry_run": True,
            "jobs": [
                {"agent_id": agent_id, "input_payload": {"task": "a"}},
                {"agent_id": agent_id, "input_payload": {"task": "b"}},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "parallel_marketplace_hire_estimate"
    assert body["charge_status"] == "not_charged"
    assert body["batch_id"] is None
    assert body["job_count"] == 2
    assert body["estimated_total_charged_cents"] == 12
    assert len(body["planned_jobs"]) == 2
    assert body["planned_jobs"][0]["fee_split"]["agent_payout_cents"] >= 0

    balance_after = payments.get_wallet(wallet["wallet_id"])["balance_cents"]
    assert balance_after == balance_before


def test_jobs_batch_accepts_input_alias(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Batch Alias Agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["batch-alias"],
    )

    created = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "jobs": [
                {"agent_id": agent_id, "input": {"task": "via_alias"}},
            ]
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["count"] == 1
    job = body["jobs"][0]
    # The `input` alias must reach input_payload.
    assert job["input_payload"]["task"] == "via_alias"


def test_jobs_batch_submits_valid_jobs_when_siblings_are_invalid(client):
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    created = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "jobs": [
                {
                    "agent_id": server._SECRET_SCANNER_AGENT_ID,
                    "input_payload": {},
                },
                {
                    "agent_id": server._PYTHON_EXECUTOR_AGENT_ID,
                    "input_payload": {
                        "code": "print(1)",
                        "timeout": 1,
                        "explain": False,
                    },
                },
            ]
        },
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["count"] == 1
    assert body["submitted_count"] == 2
    assert body["invalid_job_count"] == 1
    assert body["invalid_jobs"][0]["index"] == 0
    assert body["jobs"][0]["agent_id"] == server._PYTHON_EXECUTOR_AGENT_ID


def test_jobs_batch_all_invalid_returns_partial_shape_not_http_exception(client):
    """When every job is rejected the response must keep the partial-success
    shape (top-level count/submitted_count/invalid_jobs) so callers parse one
    body for both partial and full failures. The 2026-05-07 eval saw the
    422 path return invalid_jobs nested inside `detail` and concluded a
    preflight had blocked the batch."""
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    response = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "jobs": [
                {
                    "agent_id": server._SECRET_SCANNER_AGENT_ID,
                    "input_payload": {},
                },
                {
                    "agent_id": server._PYTHON_EXECUTOR_AGENT_ID,
                    "input_payload": {},
                },
            ]
        },
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["count"] == 0
    assert body["submitted_count"] == 2
    assert body["invalid_job_count"] == 2
    assert isinstance(body["invalid_jobs"], list)
    assert len(body["invalid_jobs"]) == 2
    # Marketplace-transaction block must signal that no escrow opened.
    assert body["marketplace_transaction"]["escrow"] == "not_opened"


def test_jobs_dispute_returns_404_for_missing_job(client):
    """All jobs.* MCP actions must agree on the not-found status code so that
    the eval no longer sees `dispute=403, status=404, cancel=404` for the
    same bogus job_id."""
    caller = _register_user()
    _fund_user_wallet(caller, 50)

    response = client.post(
        "/jobs/00000000-0000-0000-0000-000000000000/dispute",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"reason": "test against bogus id"},
    )
    assert response.status_code == 404, response.text


def test_jobs_batch_status_compact_omits_duplicate_detail(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Batch Compact Agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["batch-compact"],
    )
    created = client.post(
        "/jobs/batch",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "jobs": [
                {"agent_id": agent_id, "input_payload": {"task": "a"}},
                {"agent_id": agent_id, "input_payload": {"task": "b"}},
            ]
        },
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["batch_id"]

    full = client.get(
        f"/jobs/batch/{batch_id}",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert full.status_code == 200
    full_body = full.json()
    assert "input_payload" in full_body["jobs"][0]

    compact = client.get(
        f"/jobs/batch/{batch_id}?include=minimal",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert compact.status_code == 200
    compact_body = compact.json()
    compact_jobs = compact_body["jobs"]
    assert len(compact_jobs) == 2
    sample = compact_jobs[0]
    # Compact items must not embed the full job payload.
    assert "input_payload" not in sample
    assert "detail" not in sample
    assert "fee_split" in sample
    assert sample["receipt"]["status"] == "pending"
    # Marketing fields must be gone.
    trace = compact_body["parallel_hire_trace"]
    assert "claude_summary_hint" not in compact_body
    assert "caller_instruction" not in trace
    assert "marketplace_summary" in trace


def test_sync_builtin_call_signs_output_and_signature_verifies(client, monkeypatch):
    """A sync builtin call must record an Ed25519 signature, and the
    /jobs/{id}/signature endpoint must return a verifiable receipt."""
    import json as _json
    import base64 as _b64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization

    caller = _register_user()
    _fund_user_wallet(caller, 500)

    monkeypatch.setattr(
        server.agent_secret_scanner,
        "run",
        lambda payload: {
            "total_findings": 0,
            "findings": [],
            "files_scanned": 1,
        },
    )

    sync_call = client.post(
        f"/registry/agents/{server._SECRET_SCANNER_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"content": "print(1)"},
    )
    assert sync_call.status_code == 200, sync_call.text
    job_id = sync_call.json()["job_id"]

    sig = client.get(
        f"/jobs/{job_id}/signature",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert sig.status_code == 200, sig.text
    sig_body = sig.json()
    assert sig_body["signature"]
    assert sig_body["alg"] == "ed25519"
    assert sig_body["agent_did"]
    assert sig_body["output_hash"]

    # Resolve the agent's public key from its in-DB DID document.
    agent_row = registry.get_agent(server._SECRET_SCANNER_AGENT_ID, include_unapproved=True)
    public_pem = agent_row.get("signing_public_key")
    assert public_pem, "Builtin agent must have a public signing key."
    public_key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
    assert isinstance(public_key, Ed25519PublicKey)

    # Re-derive canonical bytes the same way the server signs them.
    job_payload = client.get(
        f"/jobs/{job_id}",
        headers=_auth_headers(caller["raw_api_key"]),
    ).json()
    output_payload = job_payload["output_payload"]
    signed_bytes = _json.dumps(
        output_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    sig_pad = "=" * (-len(sig_body["signature"]) % 4)
    try:
        signature_bytes = _b64.urlsafe_b64decode(sig_body["signature"] + sig_pad)
    except Exception:
        signature_bytes = _b64.b64decode(sig_body["signature"] + sig_pad)
    public_key.verify(signature_bytes, signed_bytes)


def test_sync_call_response_includes_pricing_units_block(client, monkeypatch):
    """A sync registry call must surface a structured pricing_units block so
    callers don't have to parse pricing semantics out of the description text."""
    monkeypatch.setattr(
        server.agent_secret_scanner,
        "run",
        lambda payload: {
            "total_findings": 0,
            "findings": [],
            "files_scanned": 1,
        },
    )
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    resp = client.post(
        f"/registry/agents/{server._SECRET_SCANNER_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"content": "print(1)"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "pricing_units" in body, "sync response must include pricing_units"
    units = body["pricing_units"]
    assert units["pricing_model"] in {"fixed", "per_call"}
    assert "caller_charge_cents" in units
    assert units["caller_charge_cents"] >= 0
    assert "platform_fee_pct" in units
    assert units["fee_bearer_policy"] == "caller"
    assert units["unit"]


def test_jobs_batch_dry_run_via_query_param_does_not_charge(client):
    """Defense-in-depth: older clients that forward `?dry_run=true` as a
    query param must get the same no-charge estimate as the body field."""
    worker = _register_user()
    caller = _register_user()
    wallet = _fund_user_wallet(caller, 200)
    balance_before = payments.get_wallet(wallet["wallet_id"])["balance_cents"]

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Batch QP Agent {uuid.uuid4().hex[:6]}",
        price=0.05,
        tags=["batch-qp-dry-run"],
    )

    resp = client.post(
        "/jobs/batch?dry_run=true",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "jobs": [
                {"agent_id": agent_id, "input_payload": {"task": "a"}},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "parallel_marketplace_hire_estimate"
    assert body["charge_status"] == "not_charged"
    assert body["batch_id"] is None

    balance_after = payments.get_wallet(wallet["wallet_id"])["balance_cents"]
    assert balance_after == balance_before


def test_sync_builtin_lazy_provisions_signing_keys_when_missing(client, monkeypatch):
    """If an agent record is missing signing keys at sign time, the sync
    path must lazy-provision them so receipts still verify. Simulates the
    prod regression where the lifespan backfill silently failed."""
    import json as _json
    import base64 as _b64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization

    # Wipe the secret_scanner agent's signing keys to mimic the bad prod state.
    with registry._conn() as conn:
        conn.execute(
            "UPDATE agents SET signing_public_key = NULL, signing_private_key = NULL, did = NULL "
            "WHERE agent_id = %s",
            (server._SECRET_SCANNER_AGENT_ID,),
        )

    monkeypatch.setattr(
        server.agent_secret_scanner,
        "run",
        lambda payload: {
            "total_findings": 0,
            "findings": [],
            "files_scanned": 1,
        },
    )

    caller = _register_user()
    _fund_user_wallet(caller, 500)

    sync_call = client.post(
        f"/registry/agents/{server._SECRET_SCANNER_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"content": "print(1)"},
    )
    assert sync_call.status_code == 200, sync_call.text
    job_id = sync_call.json()["job_id"]

    sig = client.get(
        f"/jobs/{job_id}/signature",
        headers=_auth_headers(caller["raw_api_key"]),
    )
    assert sig.status_code == 200, sig.text
    sig_body = sig.json()
    assert sig_body["signature"], "Lazy-provisioned key must produce a signature"
    assert sig_body["agent_did"]

    # The agent row should now have a key (provisioned during the call).
    agent_row = registry.get_agent(server._SECRET_SCANNER_AGENT_ID, include_unapproved=True)
    assert agent_row["signing_private_key"]
    assert agent_row["signing_public_key"]
    public_key = serialization.load_pem_public_key(
        agent_row["signing_public_key"].encode("utf-8")
    )
    assert isinstance(public_key, Ed25519PublicKey)

    # End-to-end verification proves the receipt is now usable.
    job_payload = client.get(
        f"/jobs/{job_id}", headers=_auth_headers(caller["raw_api_key"])
    ).json()
    signed_bytes = _json.dumps(
        job_payload["output_payload"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    sig_pad = "=" * (-len(sig_body["signature"]) % 4)
    try:
        signature_bytes = _b64.urlsafe_b64decode(sig_body["signature"] + sig_pad)
    except Exception:
        signature_bytes = _b64.b64decode(sig_body["signature"] + sig_pad)
    public_key.verify(signature_bytes, signed_bytes)


def test_ops_identity_backfill_admin_provisions_missing_keys(client):
    """POST /ops/identity/backfill must require admin and idempotently
    provision keys for every built-in agent."""
    user = _register_user()
    admin_resp = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "admin-backfill", "scopes": ["admin"]},
    )
    assert admin_resp.status_code == 201
    admin_key = admin_resp.json()["raw_key"]

    # Wipe one agent's keys to ensure the endpoint actually does work.
    with registry._conn() as conn:
        conn.execute(
            "UPDATE agents SET signing_public_key = NULL, signing_private_key = NULL, did = NULL "
            "WHERE agent_id = %s",
            (server._SECRET_SCANNER_AGENT_ID,),
        )

    caller_only = client.post(
        "/auth/keys",
        headers=_auth_headers(user["raw_api_key"]),
        json={"name": "caller-only-bk", "scopes": ["caller"], "per_job_cap_cents": 500},
    ).json()["raw_key"]

    blocked = client.post("/ops/identity/backfill", headers=_auth_headers(caller_only))
    assert blocked.status_code == 403

    resp = client.post("/ops/identity/backfill", headers=_auth_headers(admin_key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_builtin_agents"] >= 1
    assert body["failed_count"] == 0

    agent_row = registry.get_agent(server._SECRET_SCANNER_AGENT_ID, include_unapproved=True)
    assert agent_row["signing_private_key"]
    assert agent_row["did"]
