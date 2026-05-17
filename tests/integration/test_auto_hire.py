"""Integration tests for POST /registry/agents/auto-hire (the aztea_do route).

Each gate has a dedicated test so we know exactly which protection broke
when a regression lands. Tests use the existing TestClient + isolated_db
fixtures and patch a built-in agent's price/quality/schema as needed.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.integration.helpers import (
    TEST_MASTER_KEY,
    _auth_headers,
    _fund_user_wallet,
    _register_user,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _signed_in_user(client) -> tuple[dict, str]:
    """Register + fund a user, return (user_record, raw_api_key)."""
    user = _register_user()
    _fund_user_wallet(user, amount_cents=500)  # $5
    raw = user.get("raw_api_key") or user.get("api_key") or ""
    assert raw, "test helper failed to produce an api key"
    return user, raw


def _post_auto_hire(client, raw_api_key: str, body: dict):
    return client.post(
        "/registry/agents/auto-hire",
        headers=_auth_headers(raw_api_key),
        json=body,
    )


def _stub_candidate(*, price: float = 0.04):
    """Build a passing-quality stub agent for decide() to choose."""
    from core.registry import auto_hire as ah

    return ah.CandidateAgent(
        agent_id="agt-stub",
        slug="stub_agent",
        name="Stub Agent",
        description="A test agent that does the stub thing.",
        tags=["test"],
        category="test",
        price_per_call_usd=price,
        trust_score=92.0,
        success_rate=0.99,
        stability_tier="stable",
        input_schema={},
        raw={"agent_id": "agt-stub", "call_count": 100},
    )


def _passthrough_decide(monkeypatch, candidates_factory):
    """Make the live ranker run against a single fixed candidate.

    Patches the endpoint's candidate-build step so the test is independent
    of whatever real agents are seeded in the integration DB.
    """
    import server.application as server_app

    def _fake_active_agents():
        cands = candidates_factory()
        return [c.raw for c in cands]

    # auto_hire.decide sees the candidates in the order we hand them to it.
    # Re-route the endpoint to use our list by patching _mcp_active_agents.
    monkeypatch.setattr(server_app, "_mcp_active_agents", _fake_active_agents)

    # CandidateAgent.from_agent_record reads several fields from `raw`. The
    # raw dict our stub returns is minimal, so we override from_agent_record
    # to return the candidate verbatim when raw["agent_id"]=="agt-stub".
    from core.registry import auto_hire as ah

    real_from_record = ah.CandidateAgent.from_agent_record

    def _from_record(record):
        if record.get("agent_id") == "agt-stub":
            cands = candidates_factory()
            return next((c for c in cands if c.agent_id == "agt-stub"), real_from_record(record))
        return real_from_record(record)

    monkeypatch.setattr(ah.CandidateAgent, "from_agent_record", staticmethod(_from_record))


# ── Tests ──────────────────────────────────────────────────────────────────


def test_auto_hire_dry_run_does_not_invoke(client, monkeypatch):
    """dry_run=True → would_invoke without charge or any registry_call delegation."""
    _, raw = _signed_in_user(client)

    from core.registry import auto_hire as ah
    import server.application as server_app

    fake_agent = ah.CandidateAgent(
        agent_id="agt-stub",
        slug="stub_agent",
        name="Stub Agent",
        description="",
        tags=[],
        category="",
        price_per_call_usd=0.04,
        trust_score=92.0,
        success_rate=0.99,
        stability_tier="stable",
        input_schema={},
        raw={"agent_id": "agt-stub"},
    )
    monkeypatch.setattr(
        ah,
        "decide",
        lambda **_: ah.Decision(
            auto_invoked=True,
            chosen=fake_agent,
            payload={"task": "stub"},
            confidence=0.92,
        ),
    )

    invoked = {"called": False}

    def _should_not_be_called(**_):
        invoked["called"] = True
        raise AssertionError("dry_run must not delegate to registry_call")

    monkeypatch.setattr(server_app, "registry_call", _should_not_be_called)

    resp = _post_auto_hire(
        client,
        raw,
        {"intent": "do the stub thing", "max_cost_usd": 0.20, "dry_run": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("auto_invoked") is False
    assert body.get("reason") == "dry_run"
    assert body.get("would_invoke") is True
    assert body.get("agent", {}).get("slug") == "stub_agent"
    assert isinstance(body.get("confidence"), (int, float))
    assert invoked["called"] is False
    # Compact shape: the dry_run payload is intentionally tight so the
    # model can call it speculatively per turn without polluting the
    # context window. Verbose fields (`payload`, `mode`, `delegation`,
    # `charge_status`) live on the gated and hired response shapes only.
    assert "payload" not in body, "dry_run must not return the full agent payload"
    assert "mode" not in body
    assert "charge_status" not in body
    assert "delegation" not in body
    assert "estimated_cost_cents" in body


def test_auto_hire_gates_when_price_exceeds_max(client, monkeypatch):
    """max_cost_usd below the agent's price → reason=price_exceeds_max."""
    _, raw = _signed_in_user(client)
    _passthrough_decide(monkeypatch, lambda: [_stub_candidate(price=0.10)])

    resp = _post_auto_hire(
        client,
        raw,
        {"intent": "do the stub thing", "max_cost_usd": 0.005},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_invoked"] is False
    assert body["reason"] == "price_exceeds_max"
    assert body["candidates"], "should surface the would-be top match"
    assert body["next_step"]


def test_auto_hire_no_match_when_intent_is_gibberish(client, monkeypatch):
    """An intent that matches nothing returns no_match cleanly."""
    _, raw = _signed_in_user(client)
    _passthrough_decide(monkeypatch, lambda: [_stub_candidate()])

    resp = _post_auto_hire(
        client,
        raw,
        {"intent": "zzzz qqqq xxxx vvvvvv", "max_cost_usd": 0.50},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_invoked"] is False
    assert body["reason"] in {"no_match", "low_confidence"}


def test_auto_hire_disabled_via_env_falls_back(client, monkeypatch):
    """AZTEA_AUTO_INVOKE_ENABLED=0 short-circuits with reason=disabled."""
    _, raw = _signed_in_user(client)
    _passthrough_decide(monkeypatch, lambda: [_stub_candidate()])

    with patch("core.feature_flags.auto_invoke_enabled", return_value=False):
        resp = _post_auto_hire(
            client,
            raw,
            {"intent": "do the stub thing"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_invoked"] is False
    assert body["reason"] == "disabled"


def test_auto_hire_empty_intent_rejected(client):
    """Pydantic blocks empty intents at the boundary."""
    _, raw = _signed_in_user(client)
    resp = _post_auto_hire(client, raw, {"intent": "   "})
    # FastAPI returns 422 for ValidationError. The body is empty but not blank
    # is min_length=1, but whitespace-stripping doesn't run inside Pydantic v2
    # by default — empty after .strip() still passes min_length. So decide()
    # is the real backstop and returns reason=empty_intent at 200.
    assert resp.status_code in (200, 422)
    if resp.status_code == 200:
        assert resp.json()["reason"] == "empty_intent"


def test_auto_hire_invokes_with_high_confidence_and_low_price(client, monkeypatch):
    """Confident, cheap, in-budget → real invocation. We patch decide() to
    avoid depending on the production catalog ranking, then confirm the
    delegation reaches registry_call (which we stub to a benign success).
    """
    _, raw = _signed_in_user(client)

    from core.registry import auto_hire as ah

    fake_agent = ah.CandidateAgent(
        agent_id="agt-stub",
        slug="stub_agent",
        name="Stub Agent",
        description="",
        tags=[],
        category="",
        price_per_call_usd=0.04,
        trust_score=92.0,
        success_rate=0.99,
        stability_tier="stable",
        input_schema={},
        raw={"agent_id": "agt-stub"},
    )

    def _fake_decide(**_kwargs):
        return ah.Decision(
            auto_invoked=True,
            chosen=fake_agent,
            payload={"task": "stub"},
            confidence=0.92,
        )

    # Stub registry_call so we don't need a real agent endpoint up.
    import server.application as server_app
    from fastapi.responses import JSONResponse

    def _fake_registry_call(*, request, agent_id, body, caller):  # noqa: ANN001
        return JSONResponse(
            content={
                "job_id": "job-stub-1",
                "status": "complete",
                "output": {"echo": "ok"},
                "latency_ms": 12,
                "cached": False,
                "cost_cents": 4,
            }
        )

    monkeypatch.setattr(ah, "decide", _fake_decide)
    monkeypatch.setattr(server_app, "registry_call", _fake_registry_call)

    resp = _post_auto_hire(
        client,
        raw,
        {"intent": "do the stub thing", "max_cost_usd": 0.50},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_invoked"] is True
    assert body["agent"]["slug"] == "stub_agent"
    assert body["job_id"] == "job-stub-1"
    assert body["output"] == {"echo": "ok"}
    assert body["cost_usd"] == pytest.approx(0.04)


def test_auto_hire_failed_call_surfaces_refund_signal(client, monkeypatch):
    """When the underlying registry_call raises HTTPException (refund-on-
    failure), the auto-hire endpoint translates it to a structured response
    that still says auto_invoked=true and includes the inner error detail.
    """
    _, raw = _signed_in_user(client)

    from core.registry import auto_hire as ah
    import server.application as server_app
    from fastapi import HTTPException

    fake_agent = ah.CandidateAgent(
        agent_id="agt-stub",
        slug="stub_agent",
        name="Stub Agent",
        description="",
        tags=[],
        category="",
        price_per_call_usd=0.04,
        trust_score=92.0,
        success_rate=0.99,
        stability_tier="stable",
        input_schema={},
        raw={"agent_id": "agt-stub"},
    )

    monkeypatch.setattr(
        ah,
        "decide",
        lambda **_: ah.Decision(
            auto_invoked=True,
            chosen=fake_agent,
            payload={"task": "stub"},
            confidence=0.92,
        ),
    )

    def _fake_registry_call(*, request, agent_id, body, caller):  # noqa: ANN001
        raise HTTPException(
            status_code=502,
            detail={
                "error_code": "AGENT_INTERNAL_ERROR",
                "message": "agent crashed",
                "data": {"refunded_cents": 4},
            },
        )

    monkeypatch.setattr(server_app, "registry_call", _fake_registry_call)

    resp = _post_auto_hire(
        client,
        raw,
        {"intent": "do the stub thing", "max_cost_usd": 0.50},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["auto_invoked"] is True
    assert body["agent"]["slug"] == "stub_agent"
    err = body["error"]
    assert isinstance(err, dict)
    assert "refunded" in (body.get("next_step") or "").lower()


def test_auto_hire_unauthenticated(client):
    """Missing API key → 401, never reach the gate logic."""
    resp = client.post(
        "/registry/agents/auto-hire",
        json={"intent": "test"},
    )
    assert resp.status_code in (401, 403)


def test_auto_hire_propagates_rendered_output_when_format_set(client, monkeypatch):
    """output_format=markdown → response includes `rendered_output` string
    formatted from the underlying agent output."""
    _, raw = _signed_in_user(client)

    from core.registry import auto_hire as ah
    import server.application as server_app
    from fastapi.responses import JSONResponse

    fake_agent = ah.CandidateAgent(
        agent_id="agt-stub",
        slug="stub_agent",
        name="Stub Agent",
        description="",
        tags=[],
        category="",
        price_per_call_usd=0.04,
        trust_score=92.0,
        success_rate=0.99,
        stability_tier="stable",
        input_schema={},
        raw={"agent_id": "agt-stub"},
    )

    monkeypatch.setattr(
        ah,
        "decide",
        lambda **_: ah.Decision(
            auto_invoked=True,
            chosen=fake_agent,
            payload={"task": "stub"},
            confidence=0.92,
        ),
    )

    def _fake_registry_call(*, request, agent_id, body, caller):  # noqa: ANN001
        # Mirror what the real registry_call does when output_format is set:
        # decorate the response with `rendered_output`. We assert here that
        # output_format made it into the body so we know the auto-hire route
        # is forwarding it correctly.
        body_dict = body.root if hasattr(body, "root") else body
        assert body_dict.get("output_format") == "markdown"
        from core import output_formats as _output_formats

        agent_output = {
            "score": 80,
            "summary": "Looks fine.",
            "severity_counts": {"critical": 0, "high": 0, "medium": 1},
            "issues": [{"severity": "medium", "title": "magic number"}],
        }
        return JSONResponse(
            content={
                "job_id": "job-1",
                "status": "complete",
                "output": agent_output,
                "latency_ms": 5,
                "cached": False,
                "cost_cents": 4,
                "rendered_output": _output_formats.render(agent_output, format="markdown"),
                "rendered_output_format": "markdown",
            }
        )

    monkeypatch.setattr(server_app, "registry_call", _fake_registry_call)

    resp = _post_auto_hire(
        client,
        raw,
        {
            "intent": "review my code",
            "max_cost_usd": 0.50,
            "output_format": "markdown",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_invoked"] is True
    assert "rendered_output" in body
    assert body["rendered_output_format"] == "markdown"
    assert "## Code Review" in body["rendered_output"]
    assert "magic number" in body["rendered_output"]


# ── Unit tests for the keyword routing layer ───────────────────────────────


def _kw_candidate(slug: str, name: str, *, match=None, block=None):
    """Build a minimal CandidateAgent for scorer unit tests."""
    from core.registry import auto_hire as ah

    return ah.CandidateAgent(
        agent_id=f"agt-{slug}",
        slug=slug,
        name=name,
        description=f"{name} test fixture.",
        tags=[],
        category="",
        price_per_call_usd=0.01,
        trust_score=50.0,
        success_rate=1.0,
        stability_tier="stable",
        input_schema={},
        raw={"agent_id": f"agt-{slug}", "call_count": 100},
        match_keywords=list(match or []),
        block_keywords=list(block or []),
    )


def test_match_keywords_boost_correct_agent_for_dependency_audit_intent():
    """Locks in the routing fix: 'audit my npm deps for vulnerabilities'
    must score dependency_auditor strictly higher than json_schema_validator."""
    from core.registry import auto_hire as ah

    intent = "Find vulnerabilities in this package.json: {\"axios\":\"0.21.0\"}"

    auditor = _kw_candidate(
        "dependency_auditor",
        "Dependency Auditor",
        match=["vulnerabilities", "package.json", "audit", "dependencies"],
    )
    validator = _kw_candidate(
        "json_schema_validator",
        "JSON Schema Validator",
        block=["vulnerabilities", "package.json", "cve"],
    )

    auditor_score = ah._score_candidate(auditor, intent).score
    validator_score = ah._score_candidate(validator, intent).score

    assert auditor_score > validator_score, (
        f"dependency_auditor={auditor_score} must beat "
        f"json_schema_validator={validator_score} for vulnerability-audit intent"
    )


def test_block_keywords_apply_negative_score():
    """A block_keyword present in intent should reduce the candidate's score."""
    from core.registry import auto_hire as ah

    blocked = _kw_candidate(
        "json_schema_validator",
        "JSON Schema Validator",
        block=["vulnerabilities"],
    )
    unblocked = _kw_candidate("json_schema_validator", "JSON Schema Validator")

    blocked_score = ah._score_candidate(
        blocked, "Find vulnerabilities in this manifest"
    ).score
    unblocked_score = ah._score_candidate(
        unblocked, "Find vulnerabilities in this manifest"
    ).score

    assert blocked_score < unblocked_score
