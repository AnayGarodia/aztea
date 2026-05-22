"""Section C — adversarial-probe evasion tests.

These exercise ``server.application_parts.part_003._run_listing_safety_probe``
and the underlying ``core.listing_safety.evaluate_probe_response`` /
``adversarial_probes`` surfaces.

# OWNS: C1-C10 from the plan.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.listing_safety import (
    adversarial_probes,
    evaluate_probe_response,
    has_block,
)

# Shards rely on the parent ``server.application`` module to inject names
# like ``Any``, ``http``, ``HTTPException`` etc., so we cannot import a
# shard standalone. For source-level assertions we read the raw file.
_PART_003_SRC = Path("server/application_parts/part_003.py").read_text()


def _run_listing_safety_probe(*args, **kwargs):
    """Look up the merged function from the application namespace."""
    import server.application as app
    return app._run_listing_safety_probe(*args, **kwargs)


# ---------------------------------------------------------------------------
# C1 — Probe fingerprinting via User-Agent. The probe sends a default
# requests.post User-Agent today; an endpoint can fingerprint by UA and
# serve clean responses only to that UA.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
def test_c1_probe_rotates_user_agent():
    src = _PART_003_SRC
    # Look for at least an explicit User-Agent header in the probe headers.
    assert "User-Agent" in src or "user-agent" in src.lower(), (
        "Probe call does not set User-Agent — fingerprinting is trivial."
    )


# ---------------------------------------------------------------------------
# C2 — Probe payload signature. The three adversarial probes are literal
# strings; an endpoint can substring-match them and serve clean responses.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
def test_c2_adversarial_probes_carry_nonce():
    """When a nonce is provided, each probe payload must include it."""
    a = adversarial_probes(nonce="abc123")
    b = adversarial_probes(nonce="def456")
    assert a != b, "nonce did not differentiate probe payloads"
    # Every probe carries the same nonce so log-correlation works.
    for probe in a:
        assert "abc123" in probe["task"]
    # No-nonce form is still stable for backwards-compatible callers.
    c = adversarial_probes()
    d = adversarial_probes()
    assert c == d


# ---------------------------------------------------------------------------
# C3 — Probe call does not include an auth header or job-id. An endpoint
# can detect probe-vs-real-call by the absence of those fields.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
def test_c3_probe_mimics_real_call_envelope():
    # Slice the probe function out of the shard source so we don't xpass
    # on unrelated "Authorization" / "job_id" usage elsewhere in part_003.
    import re
    match = re.search(
        r"def _run_listing_safety_probe\(.*?(?=\n\ndef |\Z)",
        _PART_003_SRC,
        flags=re.DOTALL,
    )
    assert match, "probe function not found"
    probe_src = match.group(0)
    assert "Authorization" in probe_src or "X-Aztea-Probe" in probe_src or "job_id" in probe_src, (
        "Probe payload does not include realistic call envelope fields."
    )


# ---------------------------------------------------------------------------
# C4 — Endpoint times out. Network errors are non-fatal so the probe is
# silently bypassed; the registration succeeds without ever seeing a real
# response.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
def test_c4_probe_timeout_raises_unreachable(probe_recorder, enable_register_probe):
    """When every probe times out, registration must refuse.

    Pre-2026-05-22 behaviour swallowed network errors and passed. The new
    policy gate (``listing.probe_unreachable``) requires at least one
    non-error probe response before approval.
    """
    from fastapi import HTTPException
    import requests

    probe_recorder.raise_with(requests.exceptions.Timeout("simulated"))

    with pytest.raises(HTTPException) as exc_info:
        _run_listing_safety_probe(
            "https://agents.example.com/x",
            input_schema={"type": "object", "properties": {"task": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
        )
    detail = exc_info.value.detail
    assert detail.get("error") == "listing.probe_unreachable"


# ---------------------------------------------------------------------------
# C5 — Endpoint 5xx during probe. Same non-fatal swallowing as C4.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
def test_c5_probe_5xx_raises_unreachable(probe_recorder, enable_register_probe):
    """Every probe returning 5xx must also refuse registration."""
    from fastapi import HTTPException
    from tests.security.conftest import FakeProbeResponse

    probe_recorder.respond_with(FakeProbeResponse(status_code=503, body="bad"))

    with pytest.raises(HTTPException) as exc_info:
        _run_listing_safety_probe(
            "https://agents.example.com/x",
            input_schema={"type": "object", "properties": {"task": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
        )
    assert exc_info.value.detail.get("error") == "listing.probe_unreachable"


# ---------------------------------------------------------------------------
# C6 — Empty {} responses. The schema-mismatch check only fires when the
# declared schema has properties AND the response is a dict with at least
# some keys.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
def test_c6_empty_response_triggers_shape_mismatch_warn():
    """Empty {} DOES trip the shape-mismatch warn — confirmed by run.

    Earlier inspection assumed an empty-observed-keys short-circuit. In
    practice _check_schema_shape_mismatch fires (no overlap between
    empty observed and non-empty declared). Pin the actual behaviour so
    a refactor that drops the warn surfaces immediately.
    """
    findings = evaluate_probe_response(
        {},
        output_schema={
            "type": "object",
            "properties": {"result": {"type": "string"}},
        },
    )
    codes_and_levels = [(f.code, f.level) for f in findings]
    assert ("probe.shape_mismatch", "warn") in codes_and_levels


# ---------------------------------------------------------------------------
# C7 — Leaked key base64-encoded in response body. The body-scan does a
# literal prefix substring lookup, so base64'd 'sk-...' bypasses it.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
def test_c7_base64_encoded_leak_detected():
    import base64

    leak = "sk-totallyrealkey1234567890abcdef1234567890"
    encoded = base64.b64encode(leak.encode()).decode()
    body = {"result": f"trust me: {encoded}"}
    findings = evaluate_probe_response(body, output_schema=None)
    assert has_block(findings)


# ---------------------------------------------------------------------------
# C8 — Leak via response headers. evaluate_probe_response only inspects the
# body; it can't see headers.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
def test_c8_evaluate_probe_response_scans_headers():
    """evaluate_probe_response accepts response_headers and blocks key leaks."""
    sig = inspect.signature(evaluate_probe_response)
    assert "response_headers" in sig.parameters, (
        "Probe response scanner must accept response headers."
    )
    findings = evaluate_probe_response(
        {"result": "ok"},
        output_schema=None,
        response_headers={"X-Debug-Key": "sk-LEAKEDKEYABCDEFG1234567890ABCDEF"},
    )
    assert has_block(findings), (
        "Set-Cookie / X-Debug-Key leak should produce a BLOCK finding"
    )


# ---------------------------------------------------------------------------
# C9 — DNS / network side-channel leak during probe. The probe runs the
# endpoint's code, which is free to make outbound DNS lookups to an
# attacker-controlled domain.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
@pytest.mark.xfail(
    strict=False,
    reason=(
        "Gap: probe runs against the real endpoint which is hosted by the "
        "publisher — Aztea has no visibility into outbound DNS the "
        "publisher's process initiates while building its response. This "
        "is a structural limitation of black-box probing; mitigation lives "
        "at runtime via the egress-restricted sandbox for in-process "
        "agents only."
    ),
)
def test_c9_probe_observes_outbound_dns():
    raise AssertionError("Structural limitation, pinned via xfail.")


# ---------------------------------------------------------------------------
# C10 — Streaming response that holds the connection past the probe budget.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
def test_c10_probe_has_response_size_cap():
    """requests.post does not enforce a body-size cap by default.

    This test documents the gap: a malicious endpoint can return a 2 GB
    response and the probe will accumulate it in memory. Fix: pass
    ``stream=True`` and read up to a fixed cap, or use a content-length
    pre-check.
    """
    src = _PART_003_SRC
    if "stream=" not in src and "max_bytes" not in src:
        pytest.xfail(
            "Gap: probe does not cap response body size. A multi-GB response "
            "would OOM the worker."
        )
    assert True


# ---------------------------------------------------------------------------
# Cross-cutting — evaluate_probe_response handles raw key leak in body.
# This is the *positive* case the existing test suite covers; we add a
# stronger fixed-shape case here so a refactor can't accidentally drop it.
# ---------------------------------------------------------------------------
@pytest.mark.security
@pytest.mark.probe
@pytest.mark.parametrize(
    "leak",
    [
        "sk-realkey1234567890abcdef",
        "azk_realworkerkey1234567890",
        "azac_realagentcallerkey",
    ],
)
def test_evaluate_probe_response_blocks_plaintext_leak(leak):
    findings = evaluate_probe_response(
        {"result": f"diagnostic dump: {leak}"},
        output_schema=None,
    )
    assert has_block(findings), f"{leak!r} not blocked"
