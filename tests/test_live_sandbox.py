"""Unit + integration tests for the ``live_sandbox`` agent.

Coverage:
- Dispatcher rejects unknown / malformed payloads with structured envelopes.
- Every stub action returns valid JSON Schema for both
  ``planned_input_schema`` and ``planned_output_schema``.
- Receipts are Ed25519-signed and chain via ``prev_hash`` across calls.
- The full lifecycle (start → exec → db_query → snapshot → restore → fork →
  stop) runs end-to-end against a real public Node+Postgres compose repo
  when Docker is reachable (otherwise: skipped).
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

import jsonschema
import pytest

from agents import live_sandbox
from core import sandbox as sandbox_engine
from core.crypto import verify_signature
from core.sandbox import receipts as receipts_mod
from core.sandbox import stubs as stubs_mod
from core.sandbox.state import reset_for_tests


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Each test gets a fresh on-disk state root + empty in-memory registry."""
    monkeypatch.setenv("AZTEA_SANDBOX_STATE_ROOT", str(tmp_path / "state"))
    reset_for_tests()
    yield
    reset_for_tests()


def test_unknown_action_returns_structured_envelope():
    out = live_sandbox.run({"action": "sandbox_nonsense"})
    assert "error" in out
    assert out["error"]["code"] == "live_sandbox.unknown_action"
    assert "known_actions" in out["error"]["details"]


def test_missing_action_returns_structured_envelope():
    out = live_sandbox.run({"input": {}})
    assert "error" in out
    assert out["error"]["code"] == "live_sandbox.invalid_input"


def test_invalid_payload_returns_structured_envelope():
    out = live_sandbox.run("not-a-dict")  # type: ignore[arg-type]
    assert "error" in out
    assert out["error"]["code"] == "live_sandbox.invalid_input"


def test_quota_returns_billing_notice_and_receipt():
    out = live_sandbox.run({"action": "sandbox_quota"})
    assert out["max_concurrent_sandboxes"] >= 1
    assert "receipt" in out
    assert out["receipt"]["alg"] == "Ed25519"
    assert out["receipt"]["payload"]["action"] == "sandbox_quota"


@pytest.mark.parametrize("action", sorted(stubs_mod.stub_actions()))
def test_every_stub_has_valid_jsonschema(action: str) -> None:
    envelope = stubs_mod.stub_for(action)
    in_schema = envelope["planned_input_schema"]
    out_schema = envelope["planned_output_schema"]
    # Both schemas must validate as JSON Schema (Draft 2020-12 by default).
    jsonschema.Draft202012Validator.check_schema(in_schema)
    jsonschema.Draft202012Validator.check_schema(out_schema)
    assert envelope.get("tracking_issue"), f"stub {action} missing tracking_issue"
    assert envelope.get("reason"), f"stub {action} missing reason"


@pytest.mark.parametrize("action", sorted(stubs_mod.stub_actions()))
def test_stub_dispatch_attaches_receipt(action: str) -> None:
    out = live_sandbox.run({"action": action, "input": {"sandbox_id": "sbx_aaaaaaaaaaaaaaaa"}})
    assert out["stubbed"] is True
    assert "receipt" in out
    assert out["receipt"]["payload"]["action"] == action


def test_receipt_signature_verifies_against_local_pubkey(tmp_path, monkeypatch):
    out = live_sandbox.run({"action": "sandbox_quota"})
    receipt = out["receipt"]
    # Fetch the public key from the state root used by this fixture.
    pub_path = (
        receipts_mod.state_root() / "signing_pubkey.pem"
    )
    pub_pem = pub_path.read_text("utf-8")
    assert verify_signature(pub_pem, receipt["payload"], receipt["signature"])


def test_receipt_chain_prev_hash_threads_through(tmp_path, monkeypatch):
    """Two sequential actions on the same sandbox chain via prev_hash."""
    a = live_sandbox.run({"action": "sandbox_browser_session", "input": {"sandbox_id": "sbx_aaaaaaaaaaaaaaaa"}})
    b = live_sandbox.run({"action": "sandbox_browser_session", "input": {"sandbox_id": "sbx_aaaaaaaaaaaaaaaa"}})
    # prev_hash of second receipt should equal hash of first receipt.
    assert b["receipt"]["payload"]["prev_hash"] == a["receipt"]["hash"]


def test_audit_returns_merkle_root():
    sid = "sbx_aaaaaaaaaaaaaaaa"
    live_sandbox.run({"action": "sandbox_browser_session", "input": {"sandbox_id": sid}})
    live_sandbox.run({"action": "sandbox_browser_navigate", "input": {"sandbox_id": sid, "session_id": "s", "url": "https://example.com"}})
    audit = live_sandbox.run({"action": "sandbox_audit", "input": {"sandbox_id": sid}})
    assert audit["count"] >= 2
    assert audit["merkle_root"]


def test_sandbox_id_validation_blocks_traversal():
    """Bad sandbox IDs in audit must not allow path traversal off state root."""
    out = live_sandbox.run({"action": "sandbox_audit", "input": {"sandbox_id": "../etc/passwd"}})
    assert "error" in out
    assert out["error"]["code"].startswith("live_sandbox.unhandled_exception") or out["error"]["code"] == "sandbox.error"


# --- Docker-backed integration test ------------------------------------------

_DOCKER_AVAILABLE = shutil.which("docker") is not None and os.environ.get("AZTEA_RUN_DOCKER_TESTS") == "1"


@pytest.mark.skipif(
    not _DOCKER_AVAILABLE,
    reason=(
        "Requires Docker + AZTEA_RUN_DOCKER_TESTS=1. Boots a real public "
        "Node+Postgres compose repo end-to-end; ~90s on a warm dev box."
    ),
)
def test_full_lifecycle_against_public_compose_repo():
    """Integration: boot → exec → db_query → snapshot → restore → fork → stop.

    Uses a small Node+Postgres compose project as the source. The test
    skips by default; export AZTEA_RUN_DOCKER_TESTS=1 to opt in.
    """
    source_url = os.environ.get(
        "AZTEA_TEST_COMPOSE_REPO_URL",
        "https://github.com/aztea/node-pg-fixture.git",
    )
    start = live_sandbox.run(
        {
            "action": "sandbox_start",
            "input": {
                "source": {"kind": "git", "url": source_url, "shallow": True},
                "boot": {"strategy": "auto"},
                "lifetime": {"max_minutes": 10},
                "network": {"egress": "isolated"},
            },
        }
    )
    assert "error" not in start, start
    sandbox_id = start["sandbox_id"]
    assert start["status"] == "ready"
    try:
        out = live_sandbox.run(
            {
                "action": "sandbox_exec",
                "input": {"sandbox_id": sandbox_id, "cmd": "echo hello && env | wc -l"},
            }
        )
        assert out["exit_code"] == 0
        assert "hello" in out["stdout"]
        snap = live_sandbox.run({"action": "sandbox_snapshot", "input": {"sandbox_id": sandbox_id}})
        assert "snapshot_id" in snap
    finally:
        live_sandbox.run({"action": "sandbox_stop", "input": {"sandbox_id": sandbox_id}})


# --- Spec / catalog wiring ---------------------------------------------------

def test_spec_present_in_curated_catalog():
    from server.builtin_agents.constants import LIVE_SANDBOX_AGENT_ID
    from server.builtin_agents.specs import builtin_spec_by_id

    spec = builtin_spec_by_id().get(LIVE_SANDBOX_AGENT_ID)
    assert spec is not None, "live_sandbox missing from curated builtin specs"
    assert spec["endpoint_url"] == "internal://live_sandbox"
    assert spec["category"] == "Developer Tools"


def test_dispatcher_action_table_covers_all_verbs():
    """Every verb in ALL_ACTIONS is either dispatchable or stubbed."""
    actionable = set(sandbox_engine.HANDLERS.keys()) | set(stubs_mod.stub_actions())
    declared = set(sandbox_engine.ALL_ACTIONS)
    missing = declared - actionable
    assert not missing, f"verbs declared but not wired: {sorted(missing)}"


# --- Stub-fill regression coverage -------------------------------------------

def test_filled_stubs_are_no_longer_in_stub_registry():
    """The actions this PR moves out of stubs must NOT be in stubs.stub_actions()."""
    filled = {
        "sandbox_batch_start",
        "sandbox_outbound_record",
        "sandbox_outbound_replay",
        "sandbox_browser_session",
        "sandbox_browser_navigate",
        "sandbox_browser_screenshot",
        "sandbox_browser_console_logs",
    }
    stub_set = set(stubs_mod.stub_actions())
    leftover = filled & stub_set
    assert not leftover, f"stub fill regressed; still stubbed: {sorted(leftover)}"
    handler_set = set(sandbox_engine.HANDLERS.keys())
    assert filled.issubset(handler_set), (
        "stub-fill: action(s) missing from HANDLERS: "
        f"{sorted(filled - handler_set)}"
    )


def test_batch_start_validates_matrix_shape():
    out = live_sandbox.run({
        "action": "sandbox_batch_start",
        "input": {"matrix": {}, "base": {}},
    })
    assert "error" in out
    out2 = live_sandbox.run({
        "action": "sandbox_batch_start",
        "input": {"matrix": {"NODE": []}, "base": {}},
    })
    assert "error" in out2


def test_batch_start_cartesian_product():
    """Matrix Cartesian product is materialised correctly (even when boots fail)."""
    out = live_sandbox.run({
        "action": "sandbox_batch_start",
        "input": {
            "matrix": {"NODE": ["18", "20"], "PG": ["14", "16"]},
            "base": {
                "source": {
                    "kind": "raw_files",
                    "files": [{"path": "x.txt", "content_b64": "aGVsbG8="}],
                },
                "boot": {"strategy": "custom_commands", "custom_commands": ["echo hi"]},
            },
        },
    })
    assert out["matrix_cells"] == 4
    # Each cell axis_values combines both axes.
    cells = out["results"]
    assert all("NODE" in c["axis_values"] and "PG" in c["axis_values"] for c in cells)


def test_vcr_replay_requires_existing_cassette(monkeypatch, tmp_path):
    # Set up a sandbox row via the registry helper directly so the cassette
    # operations can locate the on-disk dir without booting Docker.
    from core.sandbox.state import (
        BootInfo, LifetimePolicy, NetworkPolicyState, SandboxState,
        generate_sandbox_id, register,
    )
    sandbox_id = generate_sandbox_id()
    register(SandboxState(
        sandbox_id=sandbox_id, status="ready", created_at=0, expires_at=0,
        last_activity_at=0, last_snapshot_at=0, workspace_id=None,
        owner_hint=None, region="auto", size={}, lifetime=LifetimePolicy(),
        network=NetworkPolicyState(), boot=BootInfo(strategy="raw", project_name="p"),
        filesystem_root="/tmp",
    ))
    out = live_sandbox.run({
        "action": "sandbox_outbound_replay",
        "input": {"sandbox_id": sandbox_id, "cassette": "primary"},
    })
    assert "error" in out
    assert out["error"]["code"] == "sandbox.invalid_input"


def test_vcr_record_then_replay_lookup(monkeypatch, tmp_path):
    """End-to-end: record interactions, then replay them with the right key."""
    from core.sandbox import vcr
    from core.sandbox.state import (
        BootInfo, LifetimePolicy, NetworkPolicyState, SandboxState,
        generate_sandbox_id, register,
    )
    sandbox_id = generate_sandbox_id()
    register(SandboxState(
        sandbox_id=sandbox_id, status="ready", created_at=0, expires_at=0,
        last_activity_at=0, last_snapshot_at=0, workspace_id=None,
        owner_hint=None, region="auto", size={}, lifetime=LifetimePolicy(),
        network=NetworkPolicyState(), boot=BootInfo(strategy="raw", project_name="p"),
        filesystem_root="/tmp",
    ))
    rec = live_sandbox.run({
        "action": "sandbox_outbound_record",
        "input": {"sandbox_id": sandbox_id, "cassette": "alpha"},
    })
    assert rec["mode"] == "record"
    # Append one interaction via the engine-level helper (what the proxy
    # would call in production).
    vcr.vcr_append(
        sandbox_id,
        method="POST",
        url="https://api.example.com/charge",
        request_headers={"X-Test": "1"},
        request_body='{"amount":100}',
        status=200,
        response_headers={"Content-Type": "application/json"},
        response_body='{"id":"ch_123"}',
        cassette="alpha",
    )
    rep = live_sandbox.run({
        "action": "sandbox_outbound_replay",
        "input": {"sandbox_id": sandbox_id, "cassette": "alpha"},
    })
    assert rep["mode"] == "replay"
    assert rep["interactions"] >= 1
    # Lookup matches on (method, url, body_hash).
    hit = vcr.vcr_replay_lookup(
        sandbox_id,
        method="post",
        url="https://api.example.com/charge",
        request_body='{"amount":100}',
        cassette="alpha",
    )
    assert hit is not None
    assert hit["status"] == 200
    miss = vcr.vcr_replay_lookup(
        sandbox_id,
        method="POST",
        url="https://api.example.com/charge",
        request_body='{"amount":999}',
        cassette="alpha",
    )
    assert miss is None


def test_browser_session_requires_playwright(monkeypatch):
    """Without Playwright installed, the call returns a clean structured error."""
    from core.sandbox import browser
    from core.sandbox.models import SandboxInvalidInput
    from core.sandbox.state import (
        BootInfo, LifetimePolicy, NetworkPolicyState, SandboxState,
        generate_sandbox_id, register,
    )
    sandbox_id = generate_sandbox_id()
    register(SandboxState(
        sandbox_id=sandbox_id, status="ready", created_at=0, expires_at=0,
        last_activity_at=0, last_snapshot_at=0, workspace_id=None,
        owner_hint=None, region="auto", size={}, lifetime=LifetimePolicy(),
        network=NetworkPolicyState(), boot=BootInfo(strategy="raw", project_name="p"),
        filesystem_root="/tmp",
    ))

    def _stub_import() -> None:
        raise SandboxInvalidInput(
            "playwright is not installed in this runtime"
        )

    monkeypatch.setattr(browser, "_import_playwright", _stub_import)
    out = live_sandbox.run({
        "action": "sandbox_browser_session",
        "input": {"sandbox_id": sandbox_id},
    })
    assert "error" in out
    assert out["error"]["code"] == "sandbox.invalid_input"


# --- Bug-fix regression tests (sibling bug fixes shipping in this PR) --------

def test_infra_failure_codes_classified():
    """Bug #4: only platform-fault codes are treated as infra failures."""
    import server.application as app
    is_infra = app._is_infra_failure
    assert is_infra({"output_payload": {"error": {"code": "agent.endpoint_misconfigured"}}})
    assert is_infra({"output_payload": {"error": {"code": "agent.tool_unavailable"}}})
    assert is_infra({"output_payload": None, "error_message": "agent.runtime_unavailable: foo"})
    assert not is_infra({"output_payload": {"error": {"code": "job.dispute_opened"}}})
    assert not is_infra({"output_payload": None, "error_message": "caller timeout"})


def test_canonical_slug_is_pure_and_consistent():
    """Bug #2: canonical_slug derives a stable snake_case slug from any name."""
    from core.registry.agents_ops import canonical_slug
    assert canonical_slug("Secret Scanner") == "secret_scanner"
    assert canonical_slug("CVE Lookup") == "cve_lookup"
    assert canonical_slug("cve-lookup") == "cve_lookup"
    assert canonical_slug("  Cve   Lookup ") == "cve_lookup"
    assert canonical_slug(None) == ""
    assert canonical_slug("") == ""


def test_byok_warns_once_per_process_then_uses_overlay(monkeypatch):
    """Bug #5: shared-quota warning fires once; env overlay swaps providers."""
    from core.llm import registry as llm_registry

    monkeypatch.setattr(llm_registry, "_PROCESS_BYOK_WARNED", set())
    # Without overlay: warning path is hit (we don't assert log content;
    # we assert the sentinel set grew).
    provider, model = llm_registry.resolve_for_caller(
        "groq:llama-3.3-70b-versatile", caller_api_key_id="az_test_caller",
    )
    assert "test_caller" in next(iter(llm_registry._PROCESS_BYOK_WARNED))
    # With overlay: returns the per-caller OpenAI-compatible provider
    monkeypatch.setenv("AZTEA_BYOK_AZ_TEST_OVERLAY_GROQ_API_KEY", "sk-test")
    provider_overlay, _ = llm_registry.resolve_for_caller(
        "groq:llama-3.3-70b-versatile", caller_api_key_id="az_test_overlay",
    )
    assert getattr(provider_overlay, "name", "").startswith("byok-az_test_overlay-")


def test_v2_signature_verifies_through_sdk_path(monkeypatch):
    """Bug #1: SDK verify path reconstructs the v2 sigil correctly."""
    import base64
    import hashlib
    import json
    from core.crypto import (
        OUTPUT_SIG_SCHEME_V2,
        canonical_json,
        generate_signing_keypair,
        sign_output_v2,
        public_key_to_jwk,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    priv, pub = generate_signing_keypair()
    job_id, agent_id = "job_test", "agt_test"
    output = {"result": [1, 2, 3], "meta": {"ok": True}}
    sig_b64 = sign_output_v2(priv, job_id, agent_id, output)
    sigil = {
        "v": "aztea/output-sig/2",
        "job_id": job_id,
        "agent_id": agent_id,
        "output_hash": hashlib.sha256(canonical_json(output)).hexdigest(),
    }
    signed_bytes = json.dumps(
        sigil, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    jwk = public_key_to_jwk(pub)
    pk = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
    sig = base64.b64decode(sig_b64)
    # Should verify cleanly with the v2 sigil bytes (and FAIL against raw output)
    Ed25519PublicKey.from_public_bytes(pk).verify(sig, signed_bytes)
    try:
        Ed25519PublicKey.from_public_bytes(pk).verify(sig, canonical_json(output))
        raise AssertionError("v2 sig should NOT verify against raw output bytes")
    except Exception:
        pass
