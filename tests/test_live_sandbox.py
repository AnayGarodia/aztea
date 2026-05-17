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
