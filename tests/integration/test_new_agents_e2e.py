"""
test_new_agents_e2e.py — end-to-end integration tests for the seven-agent
slate (post-editorial cut).

Uses the project's isolated_db + client fixtures from
tests/integration/conftest.py. Mocks the LLM provider chain and the
signing key so tests don't depend on real credentials.

2026-05-26 platform-pivot cull: D16 (codebase_reviewer) and C11
(compliance_attestor) moved to ``SUNSET_DEPRECATED_AGENT_IDS``. Their
internal endpoints stay wired (sunset pattern), but direct calls to
``/registry/agents/{id}/call`` now return 410 Gone. The end-to-end
flows in this file that exercised those calls are skipped at module
load until / unless the agents return to the curated catalog; the
non-call coverage (pending-infra gating, manifest exclusion, work
example recording skip) still runs because it doesn't depend on the
call path being open.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


_CULL_SKIP_REASON = (
    "Sunset 2026-05-26 platform-pivot cull: agent is no longer callable "
    "via /registry/agents/{id}/call. Re-enable when D16/C11 graduate "
    "back to CURATED_PUBLIC_BUILTIN_AGENT_IDS."
)

# Module-level env defaults must be set before importing server.application.
os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core import auth, payments
from tests.agent_helpers import (
    _capture_llm_calls,
    _stub_llm_factory,
    patch_llm_everywhere,
    _build_fixture_repo,
)
from tests.integration.helpers import (
    TEST_MASTER_KEY,
    _auth_headers,
    _fund_user_wallet,
    _register_user,
)


# ---------------------------------------------------------------------------
# Helpers specific to this test file
# ---------------------------------------------------------------------------


def _setup_caller(user_funding_cents: int = 50_000) -> tuple[dict, str]:
    """Register a user + fund their wallet. Returns (user_dict, raw_api_key)."""
    user = _register_user()
    _fund_user_wallet(user, amount_cents=user_funding_cents)
    return user, user["raw_api_key"]


def _agent_id_for(slug: str) -> str:
    """Resolve a new-agent slug to its UUID constant via the constants module."""
    from server.builtin_agents import constants as c
    name = {
        "codebase_reviewer": "CODEBASE_REVIEWER_AGENT_ID",
        "compliance_attestor": "COMPLIANCE_ATTESTOR_AGENT_ID",
        "flake_hunter": "FLAKE_HUNTER_AGENT_ID",
    }[slug]
    return getattr(c, name)


# Fully-passing check list for C11 happy path.
_C11_FULL_CHECKS = [
    {"check_id": "auth_required_on_protected_routes", "passed": True},
    {"check_id": "secrets_not_committed_to_repo", "passed": True},
    {"check_id": "encryption_in_transit_for_external_traffic", "passed": True},
    {"check_id": "principle_of_least_privilege_in_iam_diffs", "passed": True},
]


# ---------------------------------------------------------------------------
# 1. D16 end-to-end with fixture repo
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_d16_codebase_reviewer_call_to_call_with_fixture_repo(
    client, monkeypatch, tmp_path,
):
    """Full hire path: ingest a fixture repo, then call /registry/agents/<D16>/call
    with mocked LLM. Assert HTTP 200, structured output with findings + trace."""
    # Ingest a fixture repo under the isolated DB.
    from core import hosted_index as hi
    repo_path, _ = _build_fixture_repo(tmp_path, "bug_revert_fix")
    ingest_result = hi.ingest_repo(owner_id="test-owner", source=str(repo_path))

    # Mock LLM so the reviewer's reasoning loop has deterministic output.
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"note","rationale":"flagged by past revert",'
        '"summary":"PR looks risky","confidence":"medium"}',
    ))

    user, api_key = _setup_caller()
    d16_id = _agent_id_for("codebase_reviewer")
    resp = client.post(
        f"/registry/agents/{d16_id}/call",
        headers=_auth_headers(api_key),
        json={
            "repo_id": ingest_result.repo_id,
            "hunks": [{"file": "hello.py",
                        "text": "def add(a, b):\n    return a - b\n"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # /call returns the agent output, wrapped in the system envelope.
    output = body.get("output", body)
    assert "findings" in output
    assert "trace" in output
    assert len(output["findings"]) == 1


# ---------------------------------------------------------------------------
# 2. C11 all-pass end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_c11_compliance_attestor_call_with_all_checks_passing(
    client, monkeypatch, tmp_path,
):
    """C11 happy path: every required check passes → signed attestation
    surfaces in response."""
    monkeypatch.setenv("AZTEA_COMPLIANCE_SIGNING_KEY_PATH",
                        str(tmp_path / "compliance_key.pem"))
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"All four checks pass.","rationale":"x"}',
    ))

    user, api_key = _setup_caller()
    c11_id = _agent_id_for("compliance_attestor")
    resp = client.post(
        f"/registry/agents/{c11_id}/call",
        headers=_auth_headers(api_key),
        json={
            "control": "SOC2_CC6_1", "pr_ref": "test/repo#1",
            "check_results": _C11_FULL_CHECKS,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    output = body.get("output", body)
    assert output.get("status") == "attested"
    assert "signature_b64" in output
    assert len(output["signature_b64"]) == 88


# ---------------------------------------------------------------------------
# 3. C11 ledger invariant: pre-call charge fires, refund on failure
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_c11_compliance_attestor_charge_then_refund_on_failure(
    client, monkeypatch, tmp_path,
):
    """When attestation fails (a required check is missing), the caller
    should be charged-and-refunded so wallet drift is zero."""
    monkeypatch.setenv("AZTEA_COMPLIANCE_SIGNING_KEY_PATH",
                        str(tmp_path / "compliance_key.pem"))
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"missing","rationale":"x"}',
    ))

    user, api_key = _setup_caller(user_funding_cents=5_000)
    wallet_before = payments.get_wallet_by_owner(f"user:{user['user_id']}")
    balance_before = wallet_before["balance_cents"]

    c11_id = _agent_id_for("compliance_attestor")
    # Only 1 check supplied (need 4) → attestation_incomplete.
    resp = client.post(
        f"/registry/agents/{c11_id}/call",
        headers=_auth_headers(api_key),
        json={
            "control": "SOC2_CC6_1", "pr_ref": "test/repo#1",
            "check_results": _C11_FULL_CHECKS[:1],
        },
    )
    # The agent returns an error envelope; the system may surface
    # status_code = 200 or a 4xx depending on the wrapping. Either way,
    # the wallet must be made whole.
    wallet_after = payments.get_wallet_by_owner(f"user:{user['user_id']}")
    balance_after = wallet_after["balance_cents"]
    assert balance_before == balance_after, (
        f"wallet drift: before={balance_before} after={balance_after}; "
        f"resp={resp.status_code} {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# 4. Pending-infra agent hire refunds cleanly
# ---------------------------------------------------------------------------


def test_pending_agent_hire_via_direct_id_returns_requires_configuration_with_refund(
    client, monkeypatch,
):
    """Hiring a pending-infra agent (no env config) should surface
    requires_configuration AND refund the caller. Ledger drift = 0."""
    # Make sure env vars are NOT set.
    monkeypatch.delenv("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", raising=False)

    user, api_key = _setup_caller(user_funding_cents=5_000)
    wallet_before = payments.get_wallet_by_owner(f"user:{user['user_id']}")
    balance_before = wallet_before["balance_cents"]

    flake_id = _agent_id_for("flake_hunter")
    resp = client.post(
        f"/registry/agents/{flake_id}/call",
        headers=_auth_headers(api_key),
        json={"test_path": "tests/x.py", "repo_root": "/tmp/x"},
    )
    wallet_after = payments.get_wallet_by_owner(f"user:{user['user_id']}")
    balance_after = wallet_after["balance_cents"]
    # Refund must zero out drift on a requires_configuration response.
    assert balance_before == balance_after, (
        f"pending agent failed to refund; before={balance_before} "
        f"after={balance_after}; resp={resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# 5. Catalog listing excludes pending agents by default
# ---------------------------------------------------------------------------


def test_list_agents_excludes_pending_infra_by_default(client):
    """GET /registry/agents should NOT show the five pending-infra agents
    in the default public catalog."""
    user, api_key = _setup_caller()
    resp = client.get("/registry/agents", headers=_auth_headers(api_key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    listed_ids = {a["agent_id"] for a in body.get("agents", body)}

    from server.builtin_agents.constants import PENDING_INFRA_AGENT_IDS
    leaked = PENDING_INFRA_AGENT_IDS & listed_ids
    assert not leaked, (
        f"pending agents leaked into public catalog: {leaked}"
    )


# ---------------------------------------------------------------------------
# 6. D16 + C11 ARE in the public catalog
# ---------------------------------------------------------------------------


def test_list_agents_excludes_codebase_reviewer_and_compliance_attestor_after_cull(client):
    """Post-2026-05-26 cull: both agents are sunset, so the public list
    must NOT surface them. They remain hireable by direct slug, but the
    catalog listing skips sunset entries."""
    user, api_key = _setup_caller()
    resp = client.get("/registry/agents", headers=_auth_headers(api_key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    listed_ids = {a["agent_id"] for a in body.get("agents", body)}

    assert _agent_id_for("codebase_reviewer") not in listed_ids
    assert _agent_id_for("compliance_attestor") not in listed_ids


# ---------------------------------------------------------------------------
# 7. Auto-hire does not route to pending agents
# ---------------------------------------------------------------------------


def test_auto_hire_does_not_route_to_pending_agents(client):
    """Even if a pending agent's match_keywords would otherwise match,
    auto-hire's filter must exclude it."""
    user, api_key = _setup_caller()
    # Use an intent that COULD theoretically match flake_hunter's keywords.
    resp = client.post(
        "/registry/agents/auto-hire",
        headers=_auth_headers(api_key),
        json={"intent": "find flaky tests in my repo", "dry_run": True},
    )
    # The endpoint might return 200 or 404 depending on whether any
    # candidate matches. Either way, no pending agent should appear as
    # chosen_agent_id.
    if resp.status_code == 200:
        body = resp.json()
        chosen = body.get("chosen_agent_id")
        if chosen:
            from server.builtin_agents.constants import PENDING_INFRA_AGENT_IDS
            assert chosen not in PENDING_INFRA_AGENT_IDS, (
                f"auto-hire routed to pending agent {chosen}"
            )


# ---------------------------------------------------------------------------
# 8. describe_specialist works for pending agents (direct-ID hire path)
# ---------------------------------------------------------------------------


def test_describe_specialist_works_for_pending_agent(client):
    """Power users can opt in to pending agents by hitting their endpoint
    directly. The describe endpoint must surface the spec."""
    user, api_key = _setup_caller()
    flake_id = _agent_id_for("flake_hunter")
    resp = client.get(
        f"/registry/agents/{flake_id}",
        headers=_auth_headers(api_key),
    )
    # Either 200 with the spec, or 404 if the registry filters pending too
    # aggressively. The plan permits either — but if it's 404, the test
    # documents the current behaviour rather than asserting against it.
    if resp.status_code == 200:
        body = resp.json()
        assert "match_keywords" in body or "name" in body
    else:
        pytest.skip(
            f"pending agent {flake_id} is not directly fetchable "
            f"(status {resp.status_code}) — describe is filtered"
        )


# ---------------------------------------------------------------------------
# 9. MCP manifest excludes pending agents
# ---------------------------------------------------------------------------


def test_mcp_manifest_excludes_pending_infra(client):
    """MCP manifest should mirror the public catalog filter."""
    user, api_key = _setup_caller()
    resp = client.get("/mcp/manifest", headers=_auth_headers(api_key))
    # Some deploys put MCP under /api/mcp; tolerate either.
    if resp.status_code == 404:
        resp = client.get("/api/mcp/manifest", headers=_auth_headers(api_key))
    if resp.status_code == 404:
        pytest.skip("MCP manifest endpoint not exposed in this test config")

    if resp.status_code != 200:
        pytest.skip(f"MCP manifest returned {resp.status_code}")
    body = resp.json()
    tools = body.get("tools", [])
    tool_names = {t.get("name", "") for t in tools}
    # Pending-infra slugs must NOT appear as MCP tool names.
    assert "flake_hunter" not in tool_names
    assert "stripe_connect_settler" not in tool_names


# ---------------------------------------------------------------------------
# 10. Work-example recording for D16 public task
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_work_example_recorded_for_d16_public_task(client, monkeypatch, tmp_path):
    """D16 is in CURATED_PUBLIC and not in the sensitive category, so its
    work-examples should be recorded for the public ring buffer."""
    # This is mostly a smoke check that the public path doesn't crash;
    # detailed recording semantics are tested in the work-example suite.
    from core import hosted_index as hi
    repo_path, _ = _build_fixture_repo(tmp_path, "bug_revert_fix")
    ingest_result = hi.ingest_repo(owner_id="test-owner", source=str(repo_path))
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"low"}',
    ))

    user, api_key = _setup_caller()
    d16_id = _agent_id_for("codebase_reviewer")
    resp = client.post(
        f"/registry/agents/{d16_id}/call",
        headers=_auth_headers(api_key),
        json={
            "repo_id": ingest_result.repo_id,
            "hunks": [{"file": "x.py", "text": "x"}],
        },
    )
    # We don't strictly assert the example was recorded — that's tested
    # elsewhere — but the call must succeed without crashing the ring.
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 11. private_task=True drops work-example recording
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_d16_with_private_task_flag_drops_recording(client, monkeypatch, tmp_path):
    """When private_task=true, the work-example ring should not record
    the call. We exercise the privacy gate without crashing."""
    from core import hosted_index as hi
    repo_path, _ = _build_fixture_repo(tmp_path, "bug_revert_fix")
    ingest_result = hi.ingest_repo(owner_id="test-owner", source=str(repo_path))
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"low"}',
    ))

    user, api_key = _setup_caller()
    d16_id = _agent_id_for("codebase_reviewer")
    resp = client.post(
        f"/registry/agents/{d16_id}/call",
        headers=_auth_headers(api_key),
        json={
            "repo_id": ingest_result.repo_id,
            "hunks": [{"file": "x.py", "text": "x"}],
            "private_task": True,
        },
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 12. Signed attestation roundtrip through real verifier
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_c11_signed_attestation_verifies_with_real_verifier(
    client, monkeypatch, tmp_path,
):
    """A C11 signed attestation returned via the HTTP path must verify
    cleanly with the production crypto.verify_signature helper, using
    the public key from the agent's keypair on disk."""
    monkeypatch.setenv("AZTEA_COMPLIANCE_SIGNING_KEY_PATH",
                        str(tmp_path / "compliance_key.pem"))
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"s","rationale":"r"}',
    ))

    user, api_key = _setup_caller()
    c11_id = _agent_id_for("compliance_attestor")
    resp = client.post(
        f"/registry/agents/{c11_id}/call",
        headers=_auth_headers(api_key),
        json={
            "control": "SOC2_CC6_1", "pr_ref": "test/repo#1",
            "check_results": _C11_FULL_CHECKS,
        },
    )
    assert resp.status_code == 200, resp.text
    output = resp.json().get("output", resp.json())
    assert output.get("status") == "attested"

    from core import crypto
    from agents.compliance_attestor import _load_or_create_compliance_signing_keypair
    _, public_pem = _load_or_create_compliance_signing_keypair()
    assert crypto.verify_signature(
        public_pem, output["attestation"], output["signature_b64"],
    ), "real verifier rejected attestor's signature"


# ---------------------------------------------------------------------------
# 13. 429 rate-limit doesn't break envelope shape
# ---------------------------------------------------------------------------


def test_429_rate_limit_does_not_break_envelope_shape(client, monkeypatch):
    """If the rate limiter fires on an agent hire, the response must still
    be a structured envelope, not a raw 500."""
    # We can't easily induce a 429 in the test fixture without configuring
    # the rate limiter. This test documents the contract — that envelope
    # shape holds across all status codes — but skips when the limiter
    # isn't active.
    user, api_key = _setup_caller()
    d16_id = _agent_id_for("codebase_reviewer")

    # Issue many requests in quick succession.
    statuses = set()
    for _ in range(15):
        resp = client.post(
            f"/registry/agents/{d16_id}/call",
            headers=_auth_headers(api_key),
            json={"repo_id": "nope", "hunks": [{"file": "x", "text": "y"}]},
        )
        statuses.add(resp.status_code)
        if resp.status_code == 429:
            # Found one — assert envelope.
            assert "error" in resp.text or "detail" in resp.text
            return
    pytest.skip(
        f"no 429 induced; statuses={statuses}. Rate-limiter likely "
        "deferred in this test config."
    )
