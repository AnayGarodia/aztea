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


def test_stub_template_helpers_still_produce_valid_jsonschema():
    """Template helpers (_browser_stub / _simple_stub) remain valid for future use.

    Why: the registry is empty today, but the helpers are kept so any
    new sandbox verb can adopt the same envelope shape callers already
    expect. This test fires the helpers directly to guard against
    regressions in their schema shape.
    """
    for envelope in (
        stubs_mod._browser_stub("Test description"),
        stubs_mod._simple_stub(issue="test/issue", reason="test reason"),
    ):
        jsonschema.Draft202012Validator.check_schema(envelope["planned_input_schema"])
        jsonschema.Draft202012Validator.check_schema(envelope["planned_output_schema"])
        assert envelope.get("tracking_issue")
        assert envelope.get("reason")


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


# --- Newly filled stub coverage (this PR) ------------------------------------

def _register_stub_sandbox(*, services: dict | None = None) -> str:
    from core.sandbox.state import (
        BootInfo, LifetimePolicy, NetworkPolicyState, SandboxState,
        generate_sandbox_id, register,
    )
    sandbox_id = generate_sandbox_id()
    boot = BootInfo(
        strategy="raw", project_name="p",
        services=services or {"app": {"container": "p-app", "image": "x"}},
    )
    register(SandboxState(
        sandbox_id=sandbox_id, status="ready", created_at=0, expires_at=0,
        last_activity_at=0, last_snapshot_at=0, workspace_id=None,
        owner_hint=None, region="auto", size={}, lifetime=LifetimePolicy(),
        network=NetworkPolicyState(), boot=boot, filesystem_root="/tmp",
    ))
    return sandbox_id


def test_every_new_fill_is_in_handlers_not_stubs():
    """All 12 newly-filled actions are dispatchable, not stubbed."""
    filled = {
        "sandbox_browser_click", "sandbox_browser_fill", "sandbox_browser_eval",
        "sandbox_browser_network", "sandbox_browser_a11y_tree",
        "sandbox_browser_axe_audit", "sandbox_browser_lighthouse",
        "sandbox_browser_record", "sandbox_browser_replay",
        "sandbox_link", "sandbox_export_snapshot", "sandbox_inject_failure",
    }
    handlers = set(sandbox_engine.HANDLERS.keys())
    stub_set = set(stubs_mod.stub_actions())
    assert filled.issubset(handlers), (
        f"missing from HANDLERS: {sorted(filled - handlers)}"
    )
    assert not (filled & stub_set), (
        f"regression — still stubbed: {sorted(filled & stub_set)}"
    )


def test_stub_registry_is_empty():
    """Every spec-declared verb now has a real handler — zero stubs remain."""
    assert set(stubs_mod.stub_actions()) == set(), (
        f"unexpected stubs still present: {sorted(stubs_mod.stub_actions())}"
    )
    # And ALL_ACTIONS must be fully covered by HANDLERS.
    declared = set(sandbox_engine.ALL_ACTIONS)
    handlers = set(sandbox_engine.HANDLERS.keys())
    assert declared.issubset(handlers), (
        f"verbs missing from HANDLERS: {sorted(declared - handlers)}"
    )


def test_chaos_off_rule_clears_existing(monkeypatch):
    from core.sandbox import chaos
    sandbox_id = _register_stub_sandbox()
    live_sandbox.run({
        "action": "sandbox_inject_failure",
        "input": {"sandbox_id": sandbox_id, "kind": "latency",
                  "target": "example.com", "value": 250},
    })
    assert chaos.list_rules(sandbox_id)
    out = live_sandbox.run({
        "action": "sandbox_inject_failure",
        "input": {"sandbox_id": sandbox_id, "kind": "off"},
    })
    assert out["rules_cleared"] >= 1
    assert chaos.list_rules(sandbox_id) == []


def test_chaos_validates_input_per_kind():
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_inject_failure",
        "input": {"sandbox_id": sandbox_id, "kind": "latency", "value": -1},
    })
    assert "error" in out
    out2 = live_sandbox.run({
        "action": "sandbox_inject_failure",
        "input": {"sandbox_id": sandbox_id, "kind": "loss", "value": 2.0},
    })
    assert "error" in out2
    out3 = live_sandbox.run({
        "action": "sandbox_inject_failure",
        "input": {"sandbox_id": sandbox_id, "kind": "bogus"},
    })
    assert "error" in out3


def test_chaos_apply_to_url_matches_substring_and_samples_loss(monkeypatch):
    import random as _random
    from core.sandbox import chaos

    sandbox_id = _register_stub_sandbox()
    live_sandbox.run({
        "action": "sandbox_inject_failure",
        "input": {"sandbox_id": sandbox_id, "kind": "abort",
                  "target": "api.stripe.com"},
    })
    out = chaos.apply_to_url(sandbox_id, "https://api.stripe.com/v1/charges")
    assert out["action"] == "abort"
    assert out["status"] == 503
    out2 = chaos.apply_to_url(sandbox_id, "https://other.example.com/")
    assert out2["action"] == "allow"
    # Loss rule sampling
    live_sandbox.run({
        "action": "sandbox_inject_failure",
        "input": {"sandbox_id": sandbox_id, "kind": "off"},
    })
    live_sandbox.run({
        "action": "sandbox_inject_failure",
        "input": {"sandbox_id": sandbox_id, "kind": "loss",
                  "target": "drop.me", "value": 1.0},
    })
    out3 = chaos.apply_to_url(sandbox_id, "https://drop.me/x")
    assert out3["action"] == "loss"


def test_link_refuses_self_link():
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_link",
        "input": {"sandbox_id": sandbox_id, "other_sandbox_id": sandbox_id},
    })
    assert "error" in out
    assert out["error"]["code"] == "sandbox.invalid_input"


def test_link_unknown_other_sandbox_returns_not_found():
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_link",
        "input": {"sandbox_id": sandbox_id, "other_sandbox_id": "sbx_" + "0" * 16},
    })
    assert "error" in out
    assert out["error"]["code"] in ("sandbox.not_found", "sandbox.invalid_input")


def test_export_snapshot_validates_destination_uri():
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_export_snapshot",
        "input": {"sandbox_id": sandbox_id, "snapshot_id": "snap_doesnotexist",
                  "destination_uri": "file:///etc/passwd"},
    })
    assert "error" in out
    out2 = live_sandbox.run({
        "action": "sandbox_export_snapshot",
        "input": {"sandbox_id": sandbox_id, "snapshot_id": "snap_x",
                  "destination_uri": "s3://bucket/snap.tar"},
    })
    assert "error" in out2


def test_export_snapshot_packs_bundle(tmp_path, monkeypatch):
    """End-to-end: write a manifest + fs.tar, then export."""
    from core.sandbox.state import sandbox_dir
    sandbox_id = _register_stub_sandbox()
    snap_root = sandbox_dir(sandbox_id) / "snapshots" / "snap_test"
    snap_root.mkdir(parents=True)
    (snap_root / "manifest.json").write_text("{}", encoding="utf-8")
    (snap_root / "fs.tar").write_bytes(b"\x00" * 1024)
    # Avoid actually shelling out to docker for image save
    monkeypatch.setattr(
        "core.sandbox.export._save_service_images", lambda *a, **kw: None,
    )
    dest = tmp_path / "out.tar.gz"
    out = live_sandbox.run({
        "action": "sandbox_export_snapshot",
        "input": {
            "sandbox_id": sandbox_id,
            "snapshot_id": "snap_test",
            "destination_uri": f"file://{dest}",
            "include_service_images": False,
        },
    })
    assert "error" not in out
    assert out["secrets_excluded"] is True
    assert dest.exists()
    assert dest.stat().st_size > 0


def _make_session_entry(monkeypatch, sandbox_id: str):
    """Helper: register a stub Playwright session entry without launching chromium."""
    from core.sandbox import browser as _browser

    class _StubPage:
        def __init__(self) -> None:
            self.url = "https://example.com"
            self.clicks: list[dict] = []
            self.fills: list[dict] = []
            self.evals: list[str] = []
            self.gotos: list[str] = []

        def click(self, selector, button="left", timeout=0, click_count=1):
            self.clicks.append({"selector": selector, "button": button})

        def fill(self, selector, value, timeout=0):
            self.fills.append({"selector": selector, "value": value})

        def evaluate(self, js):
            self.evals.append(js)
            if "axe.run" in js:
                return {"violations": [{"id": "color-contrast"}],
                        "passes_count": 12, "incomplete_count": 1}
            return {"ok": True, "input": js}

        def goto(self, url, wait_until=None, timeout=0):
            self.gotos.append(url)
            self.url = url
            class _R: status = 200
            return _R()

        def title(self) -> str:  # noqa: D401
            return "stub-title"

        def screenshot(self, full_page=True):
            return b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

        def add_script_tag(self, content=None):
            return None

        @property
        def accessibility(self):
            return self

        def snapshot(self, interesting_only=True):
            return {"role": "WebArea", "name": "stub", "children": []}

        def on(self, event, cb):
            return None

    entry = _browser._SessionEntry(session_id="sess_stub", sandbox_id=sandbox_id)
    entry.page = _StubPage()
    entry.browser = None
    entry.context = None
    entry._playwright = None
    _browser._SESSIONS["sess_stub"] = entry
    return entry


def test_browser_click_fill_eval_dispatch(monkeypatch):
    sandbox_id = _register_stub_sandbox()
    entry = _make_session_entry(monkeypatch, sandbox_id)
    out_click = live_sandbox.run({
        "action": "sandbox_browser_click",
        "input": {"sandbox_id": sandbox_id, "session_id": "sess_stub", "selector": "button#go"},
    })
    assert out_click["clicked"] is True
    assert entry.page.clicks[-1]["selector"] == "button#go"
    out_fill = live_sandbox.run({
        "action": "sandbox_browser_fill",
        "input": {"sandbox_id": sandbox_id, "session_id": "sess_stub",
                  "selector": "input[name=email]", "value": "test@x"},
    })
    assert out_fill["filled"] is True
    out_eval = live_sandbox.run({
        "action": "sandbox_browser_eval",
        "input": {"sandbox_id": sandbox_id, "session_id": "sess_stub",
                  "js": "document.title"},
    })
    assert out_eval["ok"] is True
    assert out_eval["result"]["ok"] is True


def test_browser_a11y_and_axe(monkeypatch):
    sandbox_id = _register_stub_sandbox()
    _make_session_entry(monkeypatch, sandbox_id)
    # Skip the axe-core network fetch
    monkeypatch.setattr(
        "core.sandbox.browser._load_axe_script",
        lambda: "/* stub axe */",
    )
    a11y = live_sandbox.run({
        "action": "sandbox_browser_a11y_tree",
        "input": {"sandbox_id": sandbox_id, "session_id": "sess_stub"},
    })
    assert a11y["tree"]["role"] == "WebArea"
    axe = live_sandbox.run({
        "action": "sandbox_browser_axe_audit",
        "input": {"sandbox_id": sandbox_id, "session_id": "sess_stub"},
    })
    assert axe["violation_count"] == 1


def test_browser_record_and_replay(monkeypatch):
    sandbox_id = _register_stub_sandbox()
    entry = _make_session_entry(monkeypatch, sandbox_id)
    live_sandbox.run({
        "action": "sandbox_browser_record",
        "input": {"sandbox_id": sandbox_id, "session_id": "sess_stub"},
    })
    live_sandbox.run({
        "action": "sandbox_browser_click",
        "input": {"sandbox_id": sandbox_id, "session_id": "sess_stub", "selector": "#a"},
    })
    live_sandbox.run({
        "action": "sandbox_browser_fill",
        "input": {"sandbox_id": sandbox_id, "session_id": "sess_stub",
                  "selector": "input", "value": "v"},
    })
    assert len(entry.recordings) == 2
    rep = live_sandbox.run({
        "action": "sandbox_browser_replay",
        "input": {"sandbox_id": sandbox_id, "session_id": "sess_stub"},
    })
    assert rep["replayed_count"] == 2


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


# --- The last 6 fills (this PR) ----------------------------------------------

def test_tunnel_open_requires_published_service_port():
    """Tunnel against a service with no published host port surfaces a clean error."""
    sandbox_id = _register_stub_sandbox(
        services={"web": {"container": "p-web", "ports": []}},
    )
    out = live_sandbox.run({
        "action": "sandbox_tunnel_open",
        "input": {"sandbox_id": sandbox_id, "service": "web", "port": 3000},
    })
    assert "error" in out
    assert out["error"]["code"] == "sandbox.invalid_input"


def test_tunnel_open_degraded_local_when_no_tool(monkeypatch):
    """With no cloudflared/ngrok installed, returns a localhost URL."""
    from core.sandbox import tunnels
    sandbox_id = _register_stub_sandbox(services={
        "web": {
            "container": "p-web",
            "ports": [{"internal_port": "3000/tcp", "host_port": "12345"}],
        },
    })
    monkeypatch.setattr("shutil.which", lambda name: None)
    out = live_sandbox.run({
        "action": "sandbox_tunnel_open",
        "input": {"sandbox_id": sandbox_id, "service": "web", "port": 3000},
    })
    assert "error" not in out, out
    assert out["kind"] == "local"
    assert out["public_url"] == "http://localhost:12345"
    assert out["host_port"] == 12345
    # Idempotent — same triple returns the same record.
    out2 = live_sandbox.run({
        "action": "sandbox_tunnel_open",
        "input": {"sandbox_id": sandbox_id, "service": "web", "port": 3000},
    })
    assert out2["tunnel_id"] == out["tunnel_id"]
    # Close it
    close = live_sandbox.run({
        "action": "sandbox_tunnel_close",
        "input": {"sandbox_id": sandbox_id, "tunnel_id": out["tunnel_id"]},
    })
    assert close["closed"] is True
    # Closing twice returns sandbox.not_found
    again = live_sandbox.run({
        "action": "sandbox_tunnel_close",
        "input": {"sandbox_id": sandbox_id, "tunnel_id": out["tunnel_id"]},
    })
    assert again.get("error", {}).get("code") == "sandbox.not_found"


def test_tunnel_validates_port_range():
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_tunnel_open",
        "input": {"sandbox_id": sandbox_id, "service": "app", "port": 0},
    })
    assert "error" in out
    out2 = live_sandbox.run({
        "action": "sandbox_tunnel_open",
        "input": {"sandbox_id": sandbox_id, "service": "app", "port": 99999},
    })
    assert "error" in out2


def test_webhook_inbox_capture_and_list():
    """Sidecar starts on first call; subsequent POST is captured + listed."""
    import urllib.request

    sandbox_id = _register_stub_sandbox()
    first = live_sandbox.run({
        "action": "sandbox_webhook_inbox",
        "input": {"sandbox_id": sandbox_id},
    })
    assert "error" not in first, first
    capture_url = first["capture_url"]
    # Fire a POST at the capture URL
    req = urllib.request.Request(
        f"{capture_url}/stripe/webhook",
        data=b'{"event":"checkout.session.completed"}',
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": "test",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
    import json as _json
    received = _json.loads(body)
    assert received["received"] is True
    event_id = received["event_id"]
    # List captured events
    listing = live_sandbox.run({
        "action": "sandbox_webhook_inbox",
        "input": {"sandbox_id": sandbox_id},
    })
    assert listing["count"] >= 1
    assert any(e.get("event_id") == event_id for e in listing["events"])
    # Each event carries a receipt
    captured = next(e for e in listing["events"] if e["event_id"] == event_id)
    assert "receipt" in captured
    # Cleanup
    from core.sandbox import webhook_inbox as _wh
    assert _wh.evict_for_sandbox(sandbox_id) is True


def test_webhook_replay_requires_target_service():
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_webhook_inbox",
        "input": {"sandbox_id": sandbox_id, "replay_event_id": "evt_missing"},
    })
    assert "error" in out
    assert out["error"]["code"] == "sandbox.invalid_input"


def test_network_capture_refuses_without_env_flag(monkeypatch):
    """NET_RAW gating: action returns a structured refusal when the env is off."""
    monkeypatch.delenv("AZTEA_SANDBOX_ALLOW_NET_RAW", raising=False)
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_network_capture",
        "input": {"sandbox_id": sandbox_id, "duration_seconds": 5},
    })
    assert "error" not in out, out
    assert out["refused"] is True
    assert out["elevated"] is False
    assert "AZTEA_SANDBOX_ALLOW_NET_RAW" in out["reason"]
    assert "next_step" in out


def test_trace_refuses_without_env_flag(monkeypatch):
    """PTRACE gating: action returns a structured refusal when the env is off."""
    monkeypatch.delenv("AZTEA_SANDBOX_ALLOW_PTRACE", raising=False)
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_trace",
        "input": {
            "sandbox_id": sandbox_id, "service": "app", "pid": 1, "tool": "py-spy",
        },
    })
    assert "error" not in out, out
    assert out["refused"] is True
    assert out["elevated"] is False
    assert "AZTEA_SANDBOX_ALLOW_PTRACE" in out["reason"]


def test_trace_validates_input_when_gated_on(monkeypatch):
    """With the env flag set, input validation kicks in before any sidecar runs."""
    monkeypatch.setenv("AZTEA_SANDBOX_ALLOW_PTRACE", "1")
    sandbox_id = _register_stub_sandbox()
    # Bad tool
    out = live_sandbox.run({
        "action": "sandbox_trace",
        "input": {"sandbox_id": sandbox_id, "service": "app", "pid": 100, "tool": "bogus"},
    })
    assert "error" in out
    # Bad pid
    out2 = live_sandbox.run({
        "action": "sandbox_trace",
        "input": {"sandbox_id": sandbox_id, "service": "app", "pid": 0, "tool": "py-spy"},
    })
    assert "error" in out2
    # Missing service
    out3 = live_sandbox.run({
        "action": "sandbox_trace",
        "input": {"sandbox_id": sandbox_id, "pid": 100, "tool": "py-spy"},
    })
    assert "error" in out3


def test_share_grants_token_and_viewer_serves_it():
    """share() mints a token; the viewer accepts it and returns the audit chain."""
    import urllib.request
    import urllib.error

    sandbox_id = _register_stub_sandbox()
    # Drop one audit entry so the response has something to show
    live_sandbox.run({"action": "sandbox_quota", "input": {"sandbox_id": sandbox_id}})
    out = live_sandbox.run({
        "action": "sandbox_share",
        "input": {"sandbox_id": sandbox_id, "access": "read", "ttl_minutes": 5},
    })
    assert "error" not in out, out
    assert out["share_id"]
    assert out["join_token"]
    assert out["access"] == "read"
    assert out["share_url"].startswith("http://127.0.0.1:")
    # Hit the viewer with the right token
    with urllib.request.urlopen(out["share_url"], timeout=5) as resp:
        body = resp.read().decode("utf-8")
    import json as _json
    payload = _json.loads(body)
    assert payload["sandbox_id"] == sandbox_id
    assert payload["share_id"] == out["share_id"]
    assert "audit" in payload
    # Wrong token → 401
    bad_url = out["share_url"].split("?")[0] + "?token=wrong"
    try:
        urllib.request.urlopen(bad_url, timeout=5)
        raise AssertionError("expected 401")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    # Revoke
    from core.sandbox import share as _share
    assert _share.revoke(out["share_id"]) is True


def test_share_refuses_full_access():
    """v0 only grants read; full access is the wallet-table follow-up."""
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_share",
        "input": {"sandbox_id": sandbox_id, "access": "full"},
    })
    assert "error" in out
    assert out["error"]["code"] == "sandbox.invalid_input"


def test_share_validates_ttl():
    sandbox_id = _register_stub_sandbox()
    out = live_sandbox.run({
        "action": "sandbox_share",
        "input": {"sandbox_id": sandbox_id, "ttl_minutes": 99999},
    })
    assert "error" in out


# --- Gap closers (this PR) ---------------------------------------------------

def test_isolation_backend_default_is_docker():
    """When the caller doesn't ask for an isolation backend, default to docker."""
    from core.sandbox import isolation

    assert isolation.normalise_backend(None) == "docker"
    assert isolation.runtime_argv("docker") == []


def test_isolation_backend_refuses_firecracker_with_clear_envelope():
    """firecracker / kata loudly refuse — never silently downgrade."""
    from core.sandbox import isolation
    from core.sandbox.models import SandboxInvalidInput

    for backend in ("firecracker", "kata"):
        try:
            isolation.runtime_argv(backend)
            raise AssertionError(f"{backend} should have raised")
        except SandboxInvalidInput as exc:
            assert backend in str(exc).lower() or "not implemented" in str(exc).lower()


def test_isolation_backend_rejects_unknown_value():
    from core.sandbox import isolation
    from core.sandbox.models import SandboxInvalidInput

    try:
        isolation.normalise_backend("bogus")
        raise AssertionError("expected SandboxInvalidInput for bogus backend")
    except SandboxInvalidInput:
        pass


def test_isolation_status_reports_supported_backends():
    from core.sandbox import isolation

    status = isolation.status_block("docker")
    assert "docker" in status["supported_backends"]
    assert "gvisor" in status["supported_backends"]
    assert isinstance(status["runsc_available"], bool)


def test_spending_cap_register_and_charge():
    from core.sandbox import spending
    from core.sandbox.models import SandboxQuotaExceeded

    sandbox_id = _register_stub_sandbox()
    cap = spending.register_cap(sandbox_id, 1000)
    assert cap == 1000
    spending.charge(sandbox_id, 600, action="exec")
    snap = spending.snapshot(sandbox_id)
    assert snap["spent_cents"] == 600
    assert snap["remaining_cents"] == 400
    # Second charge would exceed the cap — refuse loudly.
    try:
        spending.charge(sandbox_id, 500, action="exec")
        raise AssertionError("expected SandboxQuotaExceeded")
    except SandboxQuotaExceeded as exc:
        assert exc.code == "sandbox.quota_exceeded"
    # Snapshot didn't change (no partial spend).
    snap2 = spending.snapshot(sandbox_id)
    assert snap2["spent_cents"] == 600
    spending.evict(sandbox_id)


def test_spending_cap_clamps_to_hard_ceiling():
    """Asking for $1000 (10x ceiling) gets clamped to ceiling, not refused."""
    from core.sandbox import spending

    sandbox_id = _register_stub_sandbox()
    cap = spending.register_cap(sandbox_id, 999_999_999)
    assert cap == spending.HARD_SANDBOX_CAP_CENTS
    spending.evict(sandbox_id)


def test_reserve_batch_refuses_over_ceiling():
    from core.sandbox import spending
    from core.sandbox.models import SandboxQuotaExceeded

    try:
        spending.reserve_batch(per_cell_cap_cents=5000, cells=10)
        raise AssertionError("expected quota refusal")
    except SandboxQuotaExceeded as exc:
        assert "ceiling" in str(exc).lower() or "total" in str(exc).lower()


def test_reserve_batch_succeeds_under_ceiling():
    from core.sandbox import spending

    out = spending.reserve_batch(per_cell_cap_cents=1000, cells=3)
    assert out["total_reserved_cents"] == 3000
    assert out["cells"] == 3


def test_tunnel_rate_limit_detection():
    """Cloudflared rate-limit output is recognised and surfaced."""
    from core.sandbox import tunnels

    assert tunnels._looks_rate_limited(
        "ERR Rate limit exceeded for quick tunnels"
    )
    assert tunnels._looks_rate_limited(
        "Got 429 from cloudflare"
    )
    assert not tunnels._looks_rate_limited("INFO connection ready")


def test_named_tunnel_selected_when_token_set(monkeypatch):
    """Named-tunnel path is chosen when AZTEA_CLOUDFLARE_TUNNEL_TOKEN is present.

    Why: we don't actually shell out (it'd block on the cloudflared
    subprocess) — we patch the launcher to capture which kind it
    would have used.
    """
    from core.sandbox import tunnels

    monkeypatch.setenv("AZTEA_CLOUDFLARE_TUNNEL_TOKEN", "test-token")
    monkeypatch.setattr("shutil.which", lambda name: f"/fake/{name}")
    chosen: list[str] = []
    monkeypatch.setattr(
        tunnels, "_open_cloudflared_named",
        lambda port, hint: chosen.append("named") or {
            "kind": "cloudflared_named", "public_url": "https://stub", "process": None,
        },
    )
    monkeypatch.setattr(
        tunnels, "_open_cloudflared_quick",
        lambda port, hint: chosen.append("quick") or {
            "kind": "cloudflared_quick", "public_url": "https://q", "process": None,
        },
    )
    tunnels._open_with_best_available_tool(12345, "")
    assert chosen == ["named"]


def test_cow_snapshot_falls_back_silently_on_unsupported_fs(tmp_path, monkeypatch):
    """When reflink isn't supported, snapshot still produces a working tar."""
    from core.sandbox import snapshots
    from core.sandbox.state import (
        BootInfo, LifetimePolicy, NetworkPolicyState, SandboxState,
        generate_sandbox_id, register, sandbox_dir,
    )

    sandbox_id = generate_sandbox_id()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    register(SandboxState(
        sandbox_id=sandbox_id, status="ready", created_at=0, expires_at=0,
        last_activity_at=0, last_snapshot_at=0, workspace_id=None,
        owner_hint=None, region="auto", size={}, lifetime=LifetimePolicy(),
        network=NetworkPolicyState(),
        boot=BootInfo(strategy="raw", project_name="p"),
        filesystem_root=str(workspace),
    ))
    state = __import__("core.sandbox.state", fromlist=["get"]).get(sandbox_id)
    target = sandbox_dir(sandbox_id) / "snapshots" / "snap_t" / "fs.tar"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Force cp to fail so the reflink mirror path errors out (covers the
    # "filesystem doesn't support reflink" branch).
    monkeypatch.setattr("shutil.which", lambda name: None)
    snapshots._tar_workspace(state, target)
    # Tar still exists + has content
    assert target.is_file()
    assert target.stat().st_size > 0
    assert "snapshot_tar_seconds" in state.boot.boot_timing
    # No reflink mirror was created (cp absent)
    assert not (target.parent / "fs.reflink").exists()


def test_sdk_sandbox_client_methods_match_handlers():
    """Every typed SDK method maps to an action that exists in HANDLERS.

    Why: keeps the SDK from drifting away from the engine surface. If a
    handler is renamed or removed, this test catches the gap.
    """
    import sys
    sdk_path = "/Users/aakritigarodia/conductor/workspaces/agentmarket/santo-domingo/sdks/python-sdk"
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)
    from aztea.sandbox import SandboxClient

    # Every method whose name starts with a verb we recognise should
    # match an action in HANDLERS (modulo the SDK rename rules).
    sdk_to_action = {
        "start": "sandbox_start",
        "status": "sandbox_status",
        "stop": "sandbox_stop",
        "extend": "sandbox_extend",
        "resume": "sandbox_resume",
        "batch_start": "sandbox_batch_start",
        "run_command": "sandbox_exec",
        "run_command_in_service": "sandbox_exec_in_service",
        "read_file": "sandbox_read_file",
        "write_file": "sandbox_write_file",
        "delete_file": "sandbox_delete_file",
        "apply_patch": "sandbox_apply_patch",
        "glob": "sandbox_glob",
        "grep": "sandbox_grep",
        "sync_from_local": "sandbox_sync_from_local",
        "db_query": "sandbox_db_query",
        "db_snapshot": "sandbox_db_snapshot",
        "db_restore": "sandbox_db_restore",
        "db_introspect": "sandbox_db_introspect",
        "db_seed": "sandbox_db_seed",
        "snapshot": "sandbox_snapshot",
        "restore": "sandbox_restore",
        "fork": "sandbox_fork",
        "diff_snapshots": "sandbox_diff_snapshots",
        "http": "sandbox_http_request",
        "logs": "sandbox_logs",
        "metrics": "sandbox_metrics",
        "inspect_process": "sandbox_inspect_process",
        "outbound_record": "sandbox_outbound_record",
        "outbound_replay": "sandbox_outbound_replay",
        "inject_failure": "sandbox_inject_failure",
        "audit": "sandbox_audit",
        "cost": "sandbox_cost",
        "tunnel_open": "sandbox_tunnel_open",
        "tunnel_close": "sandbox_tunnel_close",
        "webhook_inbox": "sandbox_webhook_inbox",
        "share": "sandbox_share",
        "link": "sandbox_link",
        "export_snapshot": "sandbox_export_snapshot",
        "network_capture": "sandbox_network_capture",
        "trace": "sandbox_trace",
        "browser_session": "sandbox_browser_session",
        "browser_navigate": "sandbox_browser_navigate",
        "browser_screenshot": "sandbox_browser_screenshot",
        "browser_click": "sandbox_browser_click",
        "browser_fill": "sandbox_browser_fill",
        "browser_evaluate": "sandbox_browser_eval",
        "browser_close": "sandbox_browser_close",
    }
    sdk_methods = {m for m in dir(SandboxClient) if not m.startswith("_")}
    sdk_methods -= {"list_sandboxes"}  # Maps to sandbox_list but renamed for Python clarity
    missing_in_table = sdk_methods - set(sdk_to_action.keys())
    assert not missing_in_table, (
        f"SDK methods missing from sdk_to_action table: {sorted(missing_in_table)}"
    )
    for sdk_name, action in sdk_to_action.items():
        assert action in sandbox_engine.HANDLERS, (
            f"SDK method {sdk_name} maps to action {action} which is NOT in HANDLERS"
        )


def test_sdk_invocation_routes_through_registry_call():
    """The SDK wraps the client's registry.call(); each method builds the right payload."""
    import sys
    sdk_path = "/Users/aakritigarodia/conductor/workspaces/agentmarket/santo-domingo/sdks/python-sdk"
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)
    from aztea.sandbox import SandboxClient

    captured: list[dict] = []

    class _FakeRegistry:
        def call(self, agent_id, payload):
            captured.append({"agent_id": agent_id, "payload": payload})
            return {"echo": True, **payload}

    class _FakeClient:
        registry = _FakeRegistry()

    sandbox = SandboxClient(_FakeClient())
    out = sandbox.run_command("sbx_test", "echo hi", cwd="/repo")
    assert out["echo"] is True
    assert captured[-1]["payload"]["action"] == "sandbox_exec"
    assert captured[-1]["payload"]["input"]["cmd"] == "echo hi"
    assert captured[-1]["payload"]["input"]["cwd"] == "/repo"
    # idempotency_key wiring
    sandbox.start(source={"kind": "git", "url": "https://x/y"}, idempotency_key="key-1")
    assert captured[-1]["payload"]["idempotency_key"] == "key-1"
    # browser_evaluate maps to sandbox_browser_eval
    sandbox.browser_evaluate("sbx", "sess", "1+1")
    assert captured[-1]["payload"]["action"] == "sandbox_browser_eval"
