"""Workspace audit-trail tests for `quant_patch_validator`.

# OWNS: verifying the `_workspace_id`-driven artifact writes (qpv/report.json,
#        qpv/signature_divergence.json) and best-effort guarantees.
# NOT OWNS: dispatch-layer workspace mechanics (covered in
#            tests/integration/test_workspaces_dispatch.py).
"""

from __future__ import annotations

import json

import pytest

from agents.quant_patch_validator import run as validator_run


_TRIVIAL_REF = "def f(x): return x * 2\n"


@pytest.fixture
def captured_artifacts(monkeypatch):
    """Capture every workspace artifact the agent tries to write."""
    captured: list[dict] = []

    def fake_write_artifact(ws_id, path, body, content_type, **kwargs):
        captured.append(
            {
                "workspace_id": ws_id,
                "path": path,
                "body": body,
                "content_type": content_type,
                "kwargs": kwargs,
            }
        )

    import core.workspaces as ws_mod
    monkeypatch.setattr(ws_mod, "write_artifact", fake_write_artifact)
    return captured


def test_artifact_written_on_success(captured_artifacts):
    out = validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": _TRIVIAL_REF,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
            "_workspace_id": "ws_test_success",
        }
    )
    assert out["verdict"] == "equivalent"
    assert any(a["path"] == "qpv/report.json" for a in captured_artifacts)


def test_artifact_written_on_signature_divergence(captured_artifacts):
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def g(x): return x",  # different name
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
            "_workspace_id": "ws_test_sig_div",
        }
    )
    assert out["verdict"] == "signature_divergence"
    paths = [a["path"] for a in captured_artifacts]
    assert "qpv/signature_divergence.json" in paths


def test_artifact_not_written_when_no_workspace_id(captured_artifacts):
    out = validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": _TRIVIAL_REF,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
            # no _workspace_id
        }
    )
    assert out["verdict"] == "equivalent"
    assert not captured_artifacts


def test_workspace_write_failure_does_not_fail_call(monkeypatch):
    """Best-effort guarantee: workspace I/O failure must not fail validation."""

    def boom(*args, **kwargs):
        raise RuntimeError("workspace temporarily unavailable")

    import core.workspaces as ws_mod
    monkeypatch.setattr(ws_mod, "write_artifact", boom)

    out = validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": _TRIVIAL_REF,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
            "_workspace_id": "ws_will_fail",
        }
    )
    # The call must still succeed and produce a valid verdict.
    assert out["verdict"] == "equivalent"
    assert "error" not in out


def test_artifact_body_is_valid_json(captured_artifacts):
    validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": _TRIVIAL_REF,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
            "_workspace_id": "ws_test_json",
        }
    )
    report_artifacts = [a for a in captured_artifacts if a["path"] == "qpv/report.json"]
    assert report_artifacts, "no qpv/report.json captured"
    body = report_artifacts[0]["body"]
    body_str = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    parsed = json.loads(body_str)
    assert "verdict" in parsed
    assert "fuzz_stats" in parsed


def test_artifact_content_type_is_application_json(captured_artifacts):
    validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": _TRIVIAL_REF,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
            "_workspace_id": "ws_test_ct",
        }
    )
    report_artifacts = [a for a in captured_artifacts if a["path"] == "qpv/report.json"]
    assert report_artifacts
    assert report_artifacts[0]["content_type"] == "application/json"
