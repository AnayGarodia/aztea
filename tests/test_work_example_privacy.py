"""Privacy guards on _record_public_work_example.

Regression for the 2026-05-01 production audit: aztea_get_examples on
secret_scanner replayed historical inputs (raw AWS keys, sk_<live>_… Stripe
keys) verbatim. These tests pin the per-agent gates that suppress recording.
"""
from __future__ import annotations

from unittest.mock import patch

import server.application as _app  # assembled-namespace module

_record_public_work_example = _app._record_public_work_example
_SENSITIVE_EXAMPLE_AGENT_IDS = _app._SENSITIVE_EXAMPLE_AGENT_IDS


def _capture_calls():
    calls: list[tuple] = []

    def _fake_append(agent_id, example, *, max_examples=20):
        calls.append((agent_id, example, max_examples))

    return calls, _fake_append


def _patch_registry_append(fake):
    # The assembled application module imports `registry` at the top of its
    # shards. _record_public_work_example calls registry.append_agent_output_example,
    # so we monkey-patch the symbol where the call site looks it up.
    return patch.object(_app.registry, "append_agent_output_example", new=fake)


def test_secret_scanner_id_is_on_sensitive_list():
    # Defense-in-depth: spec metadata can drift; the hardcoded list must keep
    # the secret_scanner UUID even if the spec ever loses examples_sensitive.
    assert "1021c65c-d2bf-54ff-823a-897f9deb1029" in _SENSITIVE_EXAMPLE_AGENT_IDS


def test_record_skipped_for_hardcoded_sensitive_agent():
    calls, fake = _capture_calls()
    with _patch_registry_append(fake):
        _record_public_work_example(
            agent={"agent_id": "1021c65c-d2bf-54ff-823a-897f9deb1029", "name": "Secret Scanner"},
            input_payload={"content": "AWS_KEY=AKIAIOSFODNN7EXAMPLE"},
            output_payload={"findings": []},
            job_id="job_test",
        )
    assert calls == []


def test_record_skipped_when_examples_sensitive_flag_set():
    calls, fake = _capture_calls()
    with _patch_registry_append(fake):
        _record_public_work_example(
            agent={"agent_id": "abc-123", "name": "Custom Scanner", "examples_sensitive": True},
            input_payload={"text": "anything"},
            output_payload={"ok": True},
        )
    assert calls == []


def test_record_skipped_for_security_category_agents():
    calls, fake = _capture_calls()
    with _patch_registry_append(fake):
        _record_public_work_example(
            agent={"agent_id": "future-scanner", "name": "Future Scanner", "category": "Security"},
            input_payload={"text": "anything"},
            output_payload={"ok": True},
        )
    assert calls == []


def test_record_proceeds_for_non_sensitive_agent():
    calls, fake = _capture_calls()
    with _patch_registry_append(fake):
        _record_public_work_example(
            agent={"agent_id": "agent-7", "name": "Linter", "category": "Code Quality"},
            input_payload={"code": "x = 1"},
            output_payload={"issues": []},
        )
    assert len(calls) == 1
    agent_id, example, _ = calls[0]
    assert agent_id == "agent-7"
    assert example["input"] == {"code": "x = 1"}


def test_record_skipped_when_private_task_flag_top_level():
    # C-2 (audit 2026-05-19): private_task=true at the top level of the
    # input payload must suppress recording even on non-privacy-gated agents.
    # The pre-fix bug was upstream — the MCP wrapper dropped the flag — but
    # _record_public_work_example must also enforce the gate so any backend
    # path (HTTP API, SDK, job worker) gets the same behavior.
    calls, fake = _capture_calls()
    with _patch_registry_append(fake):
        _record_public_work_example(
            agent={"agent_id": "agent-7", "name": "Linter", "category": "Code Quality"},
            input_payload={"code": "x = 1", "private_task": True},
            output_payload={"issues": []},
        )
    assert calls == []


def test_record_skipped_when_private_task_flag_in_protocol():
    # Same gate must trip when private_task arrives nested in protocol —
    # the path _normalize_input_protocol_from_payload uses when the caller
    # sends a structured protocol envelope.
    calls, fake = _capture_calls()
    with _patch_registry_append(fake):
        _record_public_work_example(
            agent={"agent_id": "agent-7", "name": "Linter", "category": "Code Quality"},
            input_payload={"code": "x = 1", "protocol": {"private_task": True}},
            output_payload={"issues": []},
        )
    assert calls == []
