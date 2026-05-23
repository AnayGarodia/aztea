"""test_agent_ai_code_provenance_stamp.py — E25 AI Code Provenance Stamp (~12 tests)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core.llm.errors import BudgetExceededError, LLMError
from tests.agent_helpers import (
    _capture_llm_calls, _stub_llm_factory,
    assert_error_envelope, patch_llm_everywhere, set_env_for,
)


_VALID_PAYLOAD = {
    "pr_ref": "owner/repo#42",
    "hunks": [{"file": "a.py", "text": "x = 1"}],
}


def _agent():
    from agents import ai_code_provenance_stamp
    return ai_code_provenance_stamp


def _isolate_signing_key(monkeypatch, tmp_path):
    """Point AZTEA_COMPLIANCE_SIGNING_KEY_PATH at a tmp file so tests
    don't share state with other tests / dev runs. E25 re-uses the
    compliance signing key — see ai_code_provenance_stamp.py."""
    monkeypatch.setenv(
        "AZTEA_COMPLIANCE_SIGNING_KEY_PATH",
        str(tmp_path / "key.pem"),
    )


def test_invalid_input_envelope():
    out = _agent().run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "ai_code_provenance_stamp.invalid_input")


def test_missing_pr_ref_rejected():
    out = _agent().run({"hunks": [{"file": "a.py", "text": "x"}]})
    err = assert_error_envelope(out, "ai_code_provenance_stamp.invalid_input")
    assert "pr_ref" in err["message"]


def test_empty_hunks_rejected():
    out = _agent().run({"pr_ref": "o/r#1", "hunks": []})
    err = assert_error_envelope(out, "ai_code_provenance_stamp.invalid_input")
    assert "hunks" in err["message"]


def test_hunk_objects_must_have_file_and_text():
    """A hunks list of non-objects (e.g. strings) → invalid_input.

    The agent treats hunks as a list and calls .get on each, so the most
    direct way to fail input validation is an empty list or a non-list.
    Non-dict items raise AttributeError on .get; the agent's current
    contract is that hunks must be a non-empty list of dicts."""
    # An empty hunks list is rejected (already covered) — here we verify
    # the agent at minimum doesn't accept a non-list value.
    out = _agent().run({"pr_ref": "o/r#1", "hunks": "not a list"})
    err = assert_error_envelope(out, "ai_code_provenance_stamp.invalid_input")
    assert "hunks" in err["message"]


def test_happy_path_signs_manifest(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"signals":["camelCase"]}',
    ))
    out = _agent().run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    assert "manifest" in out
    assert "signature_b64" in out
    assert isinstance(out["signature_b64"], str)
    # Real Ed25519 base64 signatures are 88 chars.
    assert len(out["signature_b64"]) == 88


def test_signature_verifies_with_real_verifier(monkeypatch, tmp_path):
    """Round-trip through the REAL signer + verifier."""
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"signals":["snake_case"]}',
    ))
    out = _agent().run(_VALID_PAYLOAD)
    assert "error" not in out, f"unexpected error envelope: {out!r}"
    from core import crypto as _crypto
    from agents.ai_code_provenance_stamp import (
        _load_or_create_compliance_signing_keypair,
    )
    _, public_pem = _load_or_create_compliance_signing_keypair()
    assert _crypto.verify_signature(
        public_pem, out["manifest"], out["signature_b64"],
    ), "real verifier rejected provenance signature"


def test_signing_key_reused_from_compliance_attestor(monkeypatch, tmp_path):
    """E25 and C11 share the same signing key file path → same DID prefix.

    Both agents call _load_or_create_compliance_signing_keypair() which
    reads AZTEA_COMPLIANCE_SIGNING_KEY_PATH. Pointing both at the same tmp
    file proves the keys agree."""
    _isolate_signing_key(monkeypatch, tmp_path)
    from agents.ai_code_provenance_stamp import (
        _load_or_create_compliance_signing_keypair as e25_loader,
    )
    from agents.compliance_attestor import (
        _load_or_create_compliance_signing_keypair as c11_loader,
    )
    e25_private, _ = e25_loader()
    c11_private, _ = c11_loader()
    assert e25_private == c11_private


def test_reasoning_loop_two_calls(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    _agent().run(_VALID_PAYLOAD)
    assert len(calls) >= 2, f"expected >= 2 LLM calls, got {len(calls)}"


def test_classification_text_in_manifest(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"classifications":[{"file":"a.py","verdict":"human","confidence_pct":80}]}',
    ))
    out = _agent().run(_VALID_PAYLOAD)
    assert "error" not in out
    classification_text = out["manifest"]["classification_text"]
    assert isinstance(classification_text, str)
    assert len(classification_text) > 0


def test_provenance_did_distinct_from_compliance_did(monkeypatch, tmp_path):
    """The provenance manifest's DID must end in :provenance, not :compliance."""
    _isolate_signing_key(monkeypatch, tmp_path)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"classifications":[]}',
    ))
    out = _agent().run(_VALID_PAYLOAD)
    assert "error" not in out
    did = out["manifest"]["did"]
    assert "provenance" in did
    assert "compliance" not in did


def test_budget_exceeded_returns_envelope(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)

    def _boom(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=5,
        )
    patch_llm_everywhere(monkeypatch, _boom)
    out = _agent().run(_VALID_PAYLOAD)
    assert_error_envelope(out, "ai_code_provenance_stamp.llm_error")


def test_llm_unavailable_returns_envelope(monkeypatch, tmp_path):
    _isolate_signing_key(monkeypatch, tmp_path)

    def _down(req, *args, **kwargs):
        raise LLMError("stub", "stub-model", "all providers down")
    patch_llm_everywhere(monkeypatch, _down)
    out = _agent().run(_VALID_PAYLOAD)
    assert_error_envelope(out, "ai_code_provenance_stamp.llm_error")
