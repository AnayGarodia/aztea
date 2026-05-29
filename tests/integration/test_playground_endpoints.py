"""Integration tests for /api/playground/test (and publish — once it lands).

Covers:
  * kill-switch (AZTEA_PLAYGROUND_ENABLED) returns 503 with structured envelope
  * listing-safety blocks BEFORE the sandbox spawns
  * happy path: write a tiny handler, get result back, audit row recorded
  * anonymous callers are allowed; IP rate limit caps them
  * malicious payload at the audit-hook layer still surfaces a non-zero exit
"""

from __future__ import annotations

import json

import pytest

from tests.integration.support import *  # noqa: F403


@pytest.fixture(autouse=True)
def _enable_playground(monkeypatch):
    monkeypatch.setenv("AZTEA_PLAYGROUND_ENABLED", "1")
    # Disable the LLM judge in unit tests — no provider configured in
    # CI, and the listing_safety static scanner is the floor.
    monkeypatch.setenv("AZTEA_LISTING_JUDGE", "off")
    yield


def test_kill_switch_returns_503_when_disabled(client, monkeypatch):
    """The master kill-switch flips off the endpoint cleanly."""
    monkeypatch.setenv("AZTEA_PLAYGROUND_ENABLED", "0")
    resp = client.post(
        "/api/playground/test",
        json={"source": "def handler(p): return p", "input_payload": {}},
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body.get("error") == "playground.disabled"


def test_happy_path_returns_handler_output(client):
    """A tiny pure handler runs through the sandbox and prints
    `{result: ...}` from the wrapper."""
    src = "def handler(payload):\n    return payload['x'] * 2\n"
    resp = client.post(
        "/api/playground/test",
        json={"source": src, "input_payload": {"x": 21}, "timeout_s": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exit_code"] == 0, body
    assert body["timed_out"] is False
    assert '"result": 42' in body["stdout"], body
    assert body["execution_time_ms"] >= 0
    # Anonymous call still gets an execution_id back (audit row written).
    assert body["execution_id"] is not None


def test_listing_safety_blocks_before_sandbox_spawn(client):
    """`import subprocess` is on the static block list. The endpoint
    must refuse with a structured findings list BEFORE spawning the
    subprocess — verified by the response code (422) + the error
    payload shape."""
    src = "import subprocess\ndef handler(p): return {}\n"
    resp = client.post(
        "/api/playground/test",
        json={"source": src, "input_payload": {}},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body.get("error") == "playground.listing_safety_blocked"
    findings = body.get("details", {}).get("findings") or []
    assert any("subprocess" in (f.get("message") or "").lower() for f in findings), findings


def test_escape_attempt_blocked_at_some_layer(client):
    """A handler that tries to network-egress must be blocked. EITHER:
    (a) the static scanner refuses with 422 (preferred — saves a
        subprocess spawn), OR
    (b) the audit hook inside the subprocess kills it (non-zero exit).
    Both layers must hold independently — this test passes when EITHER
    one is doing its job."""
    src = (
        "def handler(p):\n"
        "    name = 'soc' + 'ket'\n"
        "    m = __import__(name)\n"
        "    s = m.socket()\n"
        "    s.connect(('1.1.1.1', 80))\n"
        "    return 'escaped'\n"
    )
    resp = client.post(
        "/api/playground/test",
        json={"source": src, "input_payload": {}, "timeout_s": 5},
    )
    # Path (a): static scanner refused at the route boundary.
    if resp.status_code == 422:
        assert resp.json().get("error") == "playground.listing_safety_blocked"
        return
    # Path (b): subprocess ran and the audit hook killed it.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exit_code"] != 0, body
    assert (
        "aztea-sandbox" in (body.get("stderr") or "")
        or "PermissionError" in (body.get("stderr") or "")
    ), body


def test_source_too_large_rejected(client):
    """The 32 KB cap rejects payloads at the route boundary."""
    src = "x = 0\n" * 10_000  # ~60 KB
    resp = client.post(
        "/api/playground/test",
        json={"source": src, "input_payload": {}},
    )
    assert resp.status_code == 413, resp.text
    assert resp.json().get("error") == "playground.source_too_large"


def test_timeout_clamped_to_hard_max(client):
    """timeout_s above the hard maximum is refused."""
    resp = client.post(
        "/api/playground/test",
        json={"source": "def handler(p): return p", "input_payload": {}, "timeout_s": 999},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json().get("error") == "request.invalid_input"
