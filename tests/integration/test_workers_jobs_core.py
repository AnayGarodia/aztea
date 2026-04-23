"""Server integration tests (auto-split fragment 1/6)."""

from tests.integration.support import *  # noqa: F403

def test_worker_claim_heartbeat_and_complete_with_owner_auth(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Worker Flow Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["worker-flow"],
    )
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = job["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    heartbeat = client.post(
        f"/jobs/{job_id}/heartbeat",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120, "claim_token": claim_token},
    )
    assert heartbeat.status_code == 200, heartbeat.text

    caller_view = client.get(f"/jobs/{job_id}", headers=_auth_headers(caller["raw_api_key"]))
    assert caller_view.status_code == 200
    assert "claim_token" not in caller_view.json()

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete"
    settled = _force_settle_completed_job(job_id)
    assert settled["settled_at"] is not None

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 189
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 10
    assert payments.get_wallet(platform_wallet["wallet_id"])["balance_cents"] == 1


def test_worker_complete_after_expired_lease_returns_410_with_timeout_state(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Expired Lease Agent {uuid.uuid4().hex[:6]}",
        tags=["expired-lease"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=1)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
            (expired, expired, job_id),
        )

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert completed.status_code == 410, completed.text
    body = completed.json()
    assert body["error"] == "job.lease_expired"
    assert body["message"] == "Job lease expired before completion."
    job_data = body["details"]["job"]
    assert job_data["status"] == "failed"
    assert job_data["timeout_count"] == 1
    assert job_data["error_message"] == "Job lease expired before completion."
    assert job_data["claim_owner_id"] is None


def test_complete_called_twice_returns_same_state_without_idempotency_key(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Double Complete Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["double-complete"],
    )
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = job["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]
    output_payload = {"ok": True, "result": "stable"}

    first = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": output_payload, "claim_token": claim_token},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"output_payload": output_payload, "claim_token": claim_token},
    )
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    assert second.json()["status"] == "complete"
    assert second.json()["output_payload"] == output_payload
    settled = _force_settle_completed_job(job_id)
    assert settled["settled_at"] is not None

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 189
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 10
    assert payments.get_wallet(platform_wallet["wallet_id"])["balance_cents"] == 1


def test_caller_clarification_after_delay_extends_lease_and_avoids_sweeper_timeout(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Clarification Lease Agent {uuid.uuid4().hex[:6]}",
        tags=["clarification-lease"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 300},
    )
    assert claim.status_code == 200, claim.text

    asked = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "clarification_needed", "payload": {"question": "Need more context."}},
    )
    assert asked.status_code == 201, asked.text

    near_expiry = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
            (near_expiry, near_expiry, job_id),
        )

    answered = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"type": "clarification", "payload": {"answer": "Proceed with latest assumptions."}},
    )
    assert answered.status_code == 201, answered.text

    resumed = jobs.get_job(job_id)
    assert resumed is not None
    assert resumed["status"] == "running"
    assert datetime.fromisoformat(resumed["lease_expires_at"]) > datetime.fromisoformat(near_expiry)

    sweep = client.post(
        "/ops/jobs/sweep",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"retry_delay_seconds": 0, "sla_seconds": 7200, "limit": 100},
    )
    assert sweep.status_code == 200, sweep.text
    summary = sweep.json()
    assert job_id not in summary["timeout_retry_job_ids"]
    assert job_id not in summary["timeout_failed_job_ids"]

    latest = jobs.get_job(job_id)
    assert latest is not None
    assert latest["status"] == "running"
    assert latest["claim_owner_id"] == resumed["claim_owner_id"]


def test_job_message_stream_receives_clarification_request_from_second_client(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)

    port = _free_tcp_port()
    uvicorn_config = uvicorn.Config(
        server.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)
    uvicorn_server.install_signal_handlers = lambda: None
    uvicorn_thread = threading.Thread(target=uvicorn_server.run, name="test-uvicorn-stream", daemon=True)
    uvicorn_thread.start()

    try:
        deadline = time.time() + 5
        while not uvicorn_server.started and uvicorn_thread.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        assert uvicorn_server.started, "uvicorn server did not start in time"

        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base_url, timeout=5.0) as post_client:
            worker = _register_user()
            caller = _register_user()
            _fund_user_wallet(caller, 300)

            agent_id = _register_agent_via_api(
                post_client,
                worker["raw_api_key"],
                name=f"Stream Agent {uuid.uuid4().hex[:6]}",
                tags=["stream-messages"],
            )
            created = _create_job_via_api(
                post_client,
                caller["raw_api_key"],
                agent_id=agent_id,
                max_attempts=2,
            )
            job_id = created["job_id"]

            ready = threading.Event()
            delivered = threading.Event()
            received: dict[str, dict] = {}
            stream_errors: list[str] = []

            def _consume_stream() -> None:
                try:
                    with httpx.Client(base_url=base_url, timeout=None) as stream_client:
                        with stream_client.stream(
                            "GET",
                            f"/jobs/{job_id}/stream",
                            headers=_auth_headers(caller["raw_api_key"]),
                        ) as response:
                            assert response.status_code == 200
                            ready.set()
                            for line in response.iter_lines():
                                if not line or not line.startswith("data: "):
                                    continue
                                received["message"] = json.loads(line[6:])
                                delivered.set()
                                return
                except Exception as exc:  # pragma: no cover - defensive thread capture
                    stream_errors.append(str(exc))
                finally:
                    ready.set()
                    delivered.set()

            stream_thread = threading.Thread(target=_consume_stream, name="job-stream-subscriber")
            stream_thread.start()

            assert ready.wait(timeout=1), "stream subscriber did not connect in time"
            posted = post_client.post(
                f"/jobs/{job_id}/messages",
                headers=_auth_headers(worker["raw_api_key"]),
                json={"type": "clarification_request", "payload": {"question": "Need more context."}},
            )
            assert posted.status_code == 201, posted.text

            assert delivered.wait(timeout=1), "stream subscriber did not receive a message in time"
            stream_thread.join(timeout=1)
            assert not stream_errors
            assert not stream_thread.is_alive()

            event_payload = received.get("message")
            assert event_payload is not None
            assert event_payload["type"] == "clarification_request"
            assert event_payload["payload"]["question"] == "Need more context."
    finally:
        uvicorn_server.should_exit = True
        uvicorn_thread.join(timeout=5)


def test_job_message_protocol_validation_and_correlation_rules(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Typed Message Agent {uuid.uuid4().hex[:6]}",
        tags=["typed-messages"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = created["job_id"]

    claimed = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 300},
    )
    assert claimed.status_code == 200, claimed.text

    near_expiry = (datetime.now(timezone.utc) + timedelta(seconds=45)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
            (near_expiry, near_expiry, job_id),
        )

    progress = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "progress", "payload": {"message": "halfway done", "percent": 50}},
    )
    assert progress.status_code == 201, progress.text
    assert progress.json()["type"] == "progress"

    updated = jobs.get_job(job_id)
    assert updated is not None
    assert datetime.fromisoformat(updated["lease_expires_at"]) >= datetime.fromisoformat(near_expiry)

    invalid_tool_call = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "tool_call", "payload": {"arguments": {"ticker": "AAPL"}}},
    )
    assert invalid_tool_call.status_code == 400
    assert "tool_call" in invalid_tool_call.json()["message"]

    unknown_correlation = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "tool_result", "payload": {"correlation_id": "missing-correlation", "result": {}}},
    )
    assert unknown_correlation.status_code == 400
    assert "Unknown tool_result correlation_id" in unknown_correlation.json()["message"]

    tool_call = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "tool_call", "payload": {"tool_name": "lookup_filing", "arguments": {"ticker": "AAPL"}}},
    )
    assert tool_call.status_code == 201, tool_call.text
    generated_correlation_id = tool_call.json()["payload"]["correlation_id"]
    assert generated_correlation_id

    tool_result = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "type": "tool_result",
            "payload": {"correlation_id": generated_correlation_id, "result": {"ticker": "AAPL"}},
        },
    )
    assert tool_result.status_code == 201, tool_result.text
    assert tool_result.json()["payload"]["correlation_id"] == generated_correlation_id

    unsupported_legacy = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"type": "legacy-custom-message", "payload": {"text": "still works"}},
    )
    assert unsupported_legacy.status_code == 400
    assert "Unsupported job message type" in unsupported_legacy.json()["message"]


def test_job_message_filters_and_agent_channel_routing(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 300)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Agent Channel Filters {uuid.uuid4().hex[:6]}",
        tags=["typed-messages", "agent-channel"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = created["job_id"]

    claimed = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 300},
    )
    assert claimed.status_code == 200, claimed.text

    cad_message = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "type": "agent_message",
            "channel": "cad",
            "to_id": "agent:cad-specialist",
            "payload": {"body": {"task": "generate-step-file"}},
        },
    )
    assert cad_message.status_code == 201, cad_message.text

    video_message = client.post(
        f"/jobs/{job_id}/messages",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "type": "agent_message",
            "channel": "video",
            "to_id": "agent:video-specialist",
            "payload": {"body": {"task": "render-preview"}},
        },
    )
    assert video_message.status_code == 201, video_message.text

    filtered = client.get(
        f"/jobs/{job_id}/messages?type=agent_message&channel=cad&to_id=agent:cad-specialist",
        headers=_auth_headers(worker["raw_api_key"]),
    )
    assert filtered.status_code == 200, filtered.text
    messages = filtered.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["message_id"] == cad_message.json()["message_id"]
    assert messages[0]["payload"]["channel"] == "cad"

    rejected_type = client.get(
        f"/jobs/{job_id}/messages?type=unknown-new-type",
        headers=_auth_headers(worker["raw_api_key"]),
    )
    assert rejected_type.status_code == 400
    assert "Unsupported job message type filter" in rejected_type.json()["message"]


def test_jobs_protocol_envelope_persists_across_create_and_complete(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 400)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Protocol Envelope Agent {uuid.uuid4().hex[:6]}",
        tags=["protocol-envelope"],
    )

    created = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "agent_id": agent_id,
            "input_payload": {"task": "convert design to preview"},
            "input_artifacts": [
                {
                    "name": "design.step",
                    "mime": "model/step",
                    "url_or_base64": "https://example.com/design.step",
                    "size_bytes": 128,
                }
            ],
            "preferred_input_formats": ["model/step", "application/sla"],
            "preferred_output_formats": ["video/mp4", "image/png"],
            "communication_channel": "design-review",
            "protocol_metadata": {"workflow_id": "wf-123"},
        },
    )
    assert created.status_code == 201, created.text
    created_job = created.json()
    protocol = created_job["input_payload"]["protocol"]
    assert protocol["communication_channel"] == "design-review"
    assert protocol["preferred_output_formats"] == ["video/mp4", "image/png"]
    assert protocol["metadata"]["workflow_id"] == "wf-123"

    claim = client.post(
        f"/jobs/{created_job['job_id']}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text

    completed = client.post(
        f"/jobs/{created_job['job_id']}/complete",
        headers=_auth_headers(worker["raw_api_key"]),
        json={
            "claim_token": claim.json()["claim_token"],
            "output_payload": {"summary": "render complete"},
            "output_artifacts": [
                {
                    "name": "preview.mp4",
                    "mime": "video/mp4",
                    "url_or_base64": "https://example.com/preview.mp4",
                    "size_bytes": 256,
                }
            ],
            "output_format": "video/mp4",
            "protocol_metadata": {"engine": "renderer-v2"},
        },
    )
    assert completed.status_code == 200, completed.text
    output_protocol = completed.json()["output_payload"]["protocol"]
    assert output_protocol["output_format"] == "video/mp4"
    assert output_protocol["metadata"]["engine"] == "renderer-v2"
    assert output_protocol["output_artifacts"][0]["mime"] == "video/mp4"


def test_concurrent_complete_and_sweeper_timeout_race_has_no_lost_work(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Race Complete Agent {uuid.uuid4().hex[:6]}",
        tags=["race-complete"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with jobs._conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
            (expired, expired, job_id),
        )

    start = threading.Event()
    thread_errors: list[str] = []
    results: dict[str, object] = {}

    def _complete() -> None:
        try:
            start.wait()
            resp = client.post(
                f"/jobs/{job_id}/complete",
                headers=_auth_headers(worker["raw_api_key"]),
                json={"output_payload": {"ok": True, "race": "done"}, "claim_token": claim_token},
            )
            results["complete_status"] = resp.status_code
            results["complete_body"] = resp.json()
        except Exception as exc:  # pragma: no cover - defensive thread capture
            thread_errors.append(str(exc))

    def _sweep() -> None:
        try:
            start.wait()
            results["sweep_summary"] = server._sweep_jobs(
                retry_delay_seconds=0,
                sla_seconds=7200,
                limit=100,
                actor_owner_id="test:race",
            )
        except Exception as exc:  # pragma: no cover - defensive thread capture
            thread_errors.append(str(exc))

    complete_thread = threading.Thread(target=_complete, name="race-complete-thread")
    sweep_thread = threading.Thread(target=_sweep, name="race-sweep-thread")
    complete_thread.start()
    sweep_thread.start()
    start.set()
    complete_thread.join(timeout=5)
    sweep_thread.join(timeout=5)

    assert not complete_thread.is_alive()
    assert not sweep_thread.is_alive()
    assert not thread_errors

    first_status = int(results["complete_status"])
    assert first_status in {200, 410}

    final_response = results["complete_body"]
    if first_status == 410:
        retry = client.post(
            f"/jobs/{job_id}/complete",
            headers=_auth_headers(worker["raw_api_key"]),
            json={"output_payload": {"ok": True, "race": "done"}, "claim_token": claim_token},
        )
        assert retry.status_code == 200, retry.text
        final_response = retry.json()

    assert final_response["status"] in {"complete", "failed"}
    if final_response["status"] == "complete":
        assert final_response["output_payload"] == {"ok": True, "race": "done"}
        settled = _force_settle_completed_job(job_id)
        assert settled["settled_at"] is not None
    else:
        assert "lease expired" in (final_response.get("error_message") or "").lower()

    sweep_summary = results["sweep_summary"]
    if final_response["status"] == "complete":
        assert job_id not in sweep_summary["timeout_failed_job_ids"]
    else:
        assert job_id in sweep_summary["timeout_failed_job_ids"]

    final_job = jobs.get_job(job_id)
    assert final_job is not None
    assert final_job["status"] == final_response["status"]
    if final_job["status"] == "failed":
        assert final_job["settled_at"] is not None


def test_master_complete_records_auditable_claim_event(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Master Claim Event Agent {uuid.uuid4().hex[:6]}",
        tags=["master-claim-event"],
    )
    created = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = created["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]

    completed = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete"

    messages = client.get(f"/jobs/{job_id}/messages", headers=_auth_headers(TEST_MASTER_KEY))
    assert messages.status_code == 200, messages.text
    claim_events = [
        item for item in messages.json()["messages"] if item["type"] == "claim_event"
    ]
    bypass_events = [
        item for item in claim_events if item["payload"].get("event_type") == "master_claim_bypass"
    ]
    assert len(bypass_events) == 1
    event = bypass_events[0]
    assert event["from_id"] == "master"
    assert event["payload"]["claim_owner_id"] == f"user:{worker['user_id']}"
    assert event["payload"]["claim_token_sha256"] == hashlib.sha256(
        claim_token.encode("utf-8")
    ).hexdigest()
    metadata = event["payload"].get("metadata") or {}
    assert metadata.get("action") == "complete"
    assert metadata.get("status") == "running"


def test_idempotency_key_replays_complete_without_double_settlement(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Idempotent Complete Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["idempotency-complete"],
    )
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = job["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200
    claim_token = claim.json()["claim_token"]
    idem_headers = {
        **_auth_headers(worker["raw_api_key"]),
        "Idempotency-Key": "complete-idem-1",
    }

    first = client.post(
        f"/jobs/{job_id}/complete",
        headers=idem_headers,
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/jobs/{job_id}/complete",
        headers=idem_headers,
        json={"output_payload": {"ok": True}, "claim_token": claim_token},
    )
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    settled = _force_settle_completed_job(job_id)
    assert settled["settled_at"] is not None

    caller_wallet = payments.get_or_create_wallet(f"user:{caller['user_id']}")
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    assert payments.get_wallet(caller_wallet["wallet_id"])["balance_cents"] == 189
    assert payments.get_wallet(agent_wallet["wallet_id"])["balance_cents"] == 10
    assert payments.get_wallet(platform_wallet["wallet_id"])["balance_cents"] == 1

    stored_agent = registry.get_agent(agent_id)
    assert stored_agent is not None
    assert stored_agent["total_calls"] == 1
    assert stored_agent["success_rate"] == 1.0


def test_idempotency_key_rejects_payload_mismatch(client):
    worker = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    agent_id = _register_agent_via_api(
        client,
        worker["raw_api_key"],
        name=f"Idempotent Payload Agent {uuid.uuid4().hex[:6]}",
        price=0.10,
        tags=["idempotency-mismatch"],
    )
    job = _create_job_via_api(client, caller["raw_api_key"], agent_id=agent_id, max_attempts=2)
    job_id = job["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(worker["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200
    claim_token = claim.json()["claim_token"]
    idem_headers = {
        **_auth_headers(worker["raw_api_key"]),
        "Idempotency-Key": "complete-idem-mismatch",
    }

    first = client.post(
        f"/jobs/{job_id}/complete",
        headers=idem_headers,
        json={"output_payload": {"result": "v1"}, "claim_token": claim_token},
    )
    assert first.status_code == 200, first.text

    mismatch = client.post(
        f"/jobs/{job_id}/complete",
        headers=idem_headers,
        json={"output_payload": {"result": "v2"}, "claim_token": claim_token},
    )
    assert mismatch.status_code == 409
    assert "different request payload" in mismatch.json()["message"]


