"""
test_agent_compliance_attestor.py — C11 reference-agent suite (23 tests).

Deepest coverage of the other reference agent. C11 is the signed-receipt
showcase; tests cover input validation, control vocabulary, the LLM-
backed reasoning loop, signing flow, the per-server key lifecycle, and
the structured error envelopes for every failure mode.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from tests.agent_helpers import (
    _capture_llm_calls,
    _make_response,
    _stub_llm_factory,
    assert_error_envelope,
    assert_reasoning_loop,
    patch_llm_everywhere,
)


_FULL_CHECKS = [
    {"check_id": "auth_required_on_protected_routes", "passed": True,
      "evidence": "every route under /api/* has auth"},
    {"check_id": "secrets_not_committed_to_repo", "passed": True,
      "evidence": "trufflehog clean on HEAD"},
    {"check_id": "encryption_in_transit_for_external_traffic", "passed": True,
      "evidence": "https-only ingress"},
    {"check_id": "principle_of_least_privilege_in_iam_diffs", "passed": True,
      "evidence": "no wildcard policies"},
]


def _agent():
    from agents import compliance_attestor
    return compliance_attestor


def _isolate_signing_key(monkeypatch, tmp_path):
    """Point AZTEA_COMPLIANCE_SIGNING_KEY_PATH at a tmp file so tests
    don't share state with other tests / dev runs."""
    monkeypatch.setenv("AZTEA_COMPLIANCE_SIGNING_KEY_PATH",
                        str(tmp_path / "compliance_key.pem"))


# ---------------------------------------------------------------------------
# 1–6. Input validation
# ---------------------------------------------------------------------------


def test_invalid_input_envelope():
    out = _agent().run("nope")  # type: ignore[arg-type]
    assert_error_envelope(out, "compliance_attestor.invalid_input")


def test_missing_control_rejected():
    out = _agent().run({"pr_ref": "o/r#1", "check_results": []})
    err = assert_error_envelope(out, "compliance_attestor.invalid_input")
    assert "control" in err["message"]


def test_missing_pr_ref_rejected():
    out = _agent().run({"control": "SOC2_CC6_1", "check_results": []})
    err = assert_error_envelope(out, "compliance_attestor.invalid_input")
    assert "pr_ref" in err["message"]


def test_check_results_must_be_list():
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": "not a list",
    })
    assert_error_envelope(out, "compliance_attestor.invalid_input")


def test_check_result_item_missing_check_id():
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": [{"passed": True}],
    })
    err = assert_error_envelope(out, "compliance_attestor.invalid_input")
    assert "check_id" in err["message"]


def test_check_result_passed_must_be_bool():
    """Reject ints / strings / None — passed MUST be a bool."""
    for bad in (1, 0, "true", "false", None):
        out = _agent().run({
            "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
            "check_results": [{"check_id": "x", "passed": bad}],
        })
        err = assert_error_envelope(out, "compliance_attestor.invalid_input")
        assert "boolean" in err["message"], (
            f"passed={bad!r}: expected 'boolean' in error: {err!r}"
        )


# ---------------------------------------------------------------------------
# 7–8. Control vocabulary
# ---------------------------------------------------------------------------


def test_control_unknown_lists_known_controls():
    out = _agent().run({
        "control": "NOPE_CONTROL", "pr_ref": "o/r#1", "check_results": [],
    })
    err = assert_error_envelope(out, "compliance_attestor.control_unknown")
    assert isinstance(err["details"]["known_controls"], list)
    assert "SOC2_CC6_1" in err["details"]["known_controls"]


def test_control_not_implemented():
    out = _agent().run({
        "control": "PCI_6_5_1", "pr_ref": "o/r#1", "check_results": [],
    })
    err = assert_error_envelope(out, "compliance_attestor.control_not_implemented")
    assert err["details"]["control"] == "PCI_6_5_1"


# ---------------------------------------------------------------------------
# 9–10. Attestation outcomes
# ---------------------------------------------------------------------------


def test_incomplete_attestation_lists_missing_checks():
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": [
            {"check_id": "auth_required_on_protected_routes", "passed": True},
        ],
    })
    err = assert_error_envelope(out, "compliance_attestor.attestation_incomplete")
    missing = err["details"]["missing"]
    assert len(missing) == 3  # 4 required - 1 supplied
    assert "secrets_not_committed_to_repo" in missing


def test_attestation_failed_with_per_check_rationale(monkeypatch, tmp_path):
    """One failed check fires the LLM rationale path."""
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"rationale":"the failed check breaks the control","summary":"x"}',
    ))
    checks = list(_FULL_CHECKS)
    checks[0] = dict(checks[0], passed=False,
                     evidence="auth missing on /admin")
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": checks,
    })
    err = assert_error_envelope(out, "compliance_attestor.attestation_failed")
    assert "failed" in err["details"]
    assert len(err["details"]["failed"]) == 1
    assert err["details"]["failed"][0]["check_id"] == "auth_required_on_protected_routes"


# ---------------------------------------------------------------------------
# 11–13. Happy path + signature verification
# ---------------------------------------------------------------------------


def test_attestation_signed_when_all_pass(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"all four required checks pass","rationale":"x"}',
    ))
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": _FULL_CHECKS,
    })
    assert out.get("status") == "attested", f"unexpected: {out!r}"
    assert "attestation" in out
    assert "signature_b64" in out
    assert isinstance(out["signature_b64"], str)
    # Real Ed25519 base64 signatures are 88 chars.
    assert len(out["signature_b64"]) == 88


def test_signature_verifies_with_real_verifier(monkeypatch, tmp_path):
    """Production crypto: round-trip with the REAL signer/verifier."""
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"x","rationale":"y"}',
    ))
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": _FULL_CHECKS,
    })
    assert out.get("status") == "attested"
    from core import crypto as _crypto
    from agents.compliance_attestor import _load_or_create_compliance_signing_keypair
    _, public_pem = _load_or_create_compliance_signing_keypair()
    assert _crypto.verify_signature(
        public_pem, out["attestation"], out["signature_b64"],
    ), "real verifier rejected attestor's signature"


def test_manifest_round_trips_through_canonical_json(monkeypatch, tmp_path):
    """The manifest dict must remain JSON-serialisable + idempotent
    after a round-trip."""
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"s","rationale":"r"}',
    ))
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": _FULL_CHECKS,
    })
    manifest = out["attestation"]
    round_tripped = json.loads(json.dumps(manifest, sort_keys=True))
    assert round_tripped == manifest


# ---------------------------------------------------------------------------
# 14. Reasoning loop invariant
# ---------------------------------------------------------------------------


def test_reasoning_loop_invariant(monkeypatch, tmp_path):
    """One LLM call per failed check + one final summary => ≥ 2 calls when
    there's at least one failure. Even on success, the summary fires."""
    _isolate_signing_key(monkeypatch, tmp_path)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    # One failure → at least 2 LLM calls (rationale + summary).
    checks = list(_FULL_CHECKS)
    checks[0] = dict(checks[0], passed=False)
    _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": checks,
    })
    assert len(calls) >= 2


# ---------------------------------------------------------------------------
# 15. Budget exhaustion
# ---------------------------------------------------------------------------


def test_budget_exceeded_with_low_budget(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    from core.llm.errors import BudgetExceededError

    def _bust(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "low budget",
            budget_cents=1, spent_cents=0, estimated_next_cents=10,
        )
    patch_llm_everywhere(monkeypatch, _bust)

    checks = list(_FULL_CHECKS)
    checks[0] = dict(checks[0], passed=False)
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": checks, "budget_cents": 1,
    })
    assert_error_envelope(out, "compliance_attestor.budget_exceeded")


# ---------------------------------------------------------------------------
# 16. LLM unavailable
# ---------------------------------------------------------------------------


def test_llm_unavailable_returns_envelope(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    from core.llm.errors import LLMError

    def _down(req, *args, **kwargs):
        raise LLMError("stub", "stub-model", "all providers down")
    patch_llm_everywhere(monkeypatch, _down)

    checks = list(_FULL_CHECKS)
    checks[0] = dict(checks[0], passed=False)
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": checks,
    })
    assert_error_envelope(out, "compliance_attestor.llm_unavailable")


# ---------------------------------------------------------------------------
# 17–18. Signing-key lifecycle
# ---------------------------------------------------------------------------


def test_signing_key_persists_across_calls(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"s","rationale":"r"}',
    ))
    out_1 = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": _FULL_CHECKS,
    })
    out_2 = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#2",  # different PR
        "check_results": _FULL_CHECKS,
    })
    # Same key → same DID. Signatures differ only because pr_ref differs.
    assert out_1["attestation"]["did"] == out_2["attestation"]["did"]


def test_signing_key_atomic_create_handles_race(monkeypatch, tmp_path):
    """If the key file ALREADY exists at write time (concurrent creation),
    the loader must re-read instead of clobbering."""
    _isolate_signing_key(monkeypatch, tmp_path)
    # Pre-populate the key file.
    from agents.compliance_attestor import _load_or_create_compliance_signing_keypair
    pem_before, _ = _load_or_create_compliance_signing_keypair()
    # Second call must return the SAME key (not regenerate).
    pem_after, _ = _load_or_create_compliance_signing_keypair()
    assert pem_before == pem_after


# ---------------------------------------------------------------------------
# 19. Control name case-insensitive
# ---------------------------------------------------------------------------


def test_control_name_case_insensitive(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"s","rationale":"r"}',
    ))
    out = _agent().run({
        "control": "soc2_cc6_1",  # lowercase
        "pr_ref": "o/r#1", "check_results": _FULL_CHECKS,
    })
    assert out["control"] == "SOC2_CC6_1"


# ---------------------------------------------------------------------------
# 20. Evidence truncation
# ---------------------------------------------------------------------------


def test_evidence_field_truncated_at_1000_chars(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"s","rationale":"r"}',
    ))
    checks = list(_FULL_CHECKS)
    checks[0] = dict(checks[0], evidence="x" * 10_000)
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1", "check_results": checks,
    })
    # In the manifest, the first check's evidence must be clipped.
    manifest_check_0 = out["attestation"]["checks"][0]
    assert len(manifest_check_0["evidence"]) <= 1000


# ---------------------------------------------------------------------------
# 21. DID shape
# ---------------------------------------------------------------------------


def test_manifest_includes_correct_did(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"s","rationale":"r"}',
    ))
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": _FULL_CHECKS,
    })
    did = out["attestation"]["did"]
    # Format: did:web:<host>:attestations:compliance (host may be percent-encoded)
    assert did.startswith("did:web:"), f"bad DID: {did!r}"
    assert "attestations" in did and "compliance" in did


# ---------------------------------------------------------------------------
# 22. status field never leaks "attested" on failure
# ---------------------------------------------------------------------------


def test_status_field_attested_only_on_success(monkeypatch, tmp_path):
    """Failure paths must NOT include status: 'attested' in the response."""
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"s","rationale":"r"}',
    ))
    # Incomplete attestation
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1",
        "check_results": [_FULL_CHECKS[0]],
    })
    assert out.get("status") != "attested"
    # Failed attestation
    checks = list(_FULL_CHECKS)
    checks[0] = dict(checks[0], passed=False)
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1", "check_results": checks,
    })
    assert out.get("status") != "attested"


# ---------------------------------------------------------------------------
# 23. Trace serialises when step errored
# ---------------------------------------------------------------------------


def test_trace_serialises_when_step_errored(monkeypatch, tmp_path):
    """Failed LLM step should still produce a serialisable trace in the
    error envelope."""
    _isolate_signing_key(monkeypatch, tmp_path)
    from core.llm.errors import LLMError

    def _down(req, *args, **kwargs):
        raise LLMError("stub", "stub-model", "down")
    patch_llm_everywhere(monkeypatch, _down)

    checks = list(_FULL_CHECKS)
    checks[0] = dict(checks[0], passed=False)
    out = _agent().run({
        "control": "SOC2_CC6_1", "pr_ref": "o/r#1", "check_results": checks,
    })
    # error envelope with trace in details
    err = out["error"]
    assert "trace" in err["details"]
    json.dumps(err["details"]["trace"])
    assert_reasoning_loop(out)
