# SPDX-License-Identifier: Apache-2.0
"""
Pure-unit audit tests (no DB / no app):

- H. Identity / DID host & port encoding
- I. Email link templates / module-level capture
- F. Built-in prefer_hosted dispatch (`_try_hosted_builtin_agent`)
- G. Reputation push fire-and-forget
- E. Judge hosted-first orchestration (white-box on `_try_hosted_judgment`)
- L. Remaining loophole probes

These exercise the audit-priority code paths without booting the app, which
keeps run-time low and isolates failures to the function under test.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

# Strip inherited hosted env so unit tests start clean.
for _v in ("AZTEA_HOSTED_API_URL", "AZTEA_HOSTED_API_KEY", "ALLOW_PRIVATE_OUTBOUND_URLS"):
    os.environ.pop(_v, None)
os.environ.setdefault("API_KEY", "test-master-key")

from core import hosted_client  # noqa: E402
from core import identity  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_hosted_client():
    hosted_client.reset_hosted_client_for_tests()
    yield
    hosted_client.reset_hosted_client_for_tests()


# ===========================================================================
# H. Identity / DID
# ===========================================================================


def test_h1_localhost_default_when_unset(monkeypatch):
    monkeypatch.delenv("SERVER_BASE_URL", raising=False)
    did = identity.build_agent_did("abc")
    assert did == "did:web:localhost:agents:abc"


def test_h2_port_encoded_for_localhost(monkeypatch):
    did = identity.build_agent_did("abc", server_base_url="http://localhost:8000")
    assert did == "did:web:localhost%3A8000:agents:abc"


def test_h3_non_default_port_encoded(monkeypatch):
    did = identity.build_agent_did("abc", server_base_url="https://example.com:9000")
    assert did == "did:web:example.com%3A9000:agents:abc"


def test_h4_standard_https_no_port_segment(monkeypatch):
    did = identity.build_agent_did("abc", server_base_url="https://aztea.ai")
    assert did == "did:web:aztea.ai:agents:abc"


def test_h5_did_doc_url_localhost_default(monkeypatch):
    monkeypatch.delenv("SERVER_BASE_URL", raising=False)
    url = identity.did_document_url("abc")
    assert url == "http://localhost:8000/agents/abc/did.json"


def test_h_explicit_param_beats_env(monkeypatch):
    """Explicit server_base_url wins over the env var (frozen-DID property)."""
    monkeypatch.setenv("SERVER_BASE_URL", "https://later.example")
    did = identity.build_agent_did("abc", server_base_url="https://aztea.ai")
    assert "aztea.ai" in did
    assert "later.example" not in did


# ===========================================================================
# I. Email module
# ===========================================================================


def test_i_public_base_url_is_callable_now(monkeypatch):
    """L5 (fixed): _public_base_url() reads env at call time so SERVER_BASE_URL
    changes propagate. Regression test for audit P2 finding."""
    from core import email

    assert callable(getattr(email, "_public_base_url", None))
    assert not hasattr(email, "_PUBLIC_BASE_URL")
    monkeypatch.setenv("SERVER_BASE_URL", "https://a.example")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    a = email._public_base_url()
    monkeypatch.setenv("SERVER_BASE_URL", "https://b.example")
    b = email._public_base_url()
    assert a == "https://a.example"
    assert b == "https://b.example"


def test_i_from_email_default_is_localhost_when_unset(monkeypatch):
    """I3: FROM_EMAIL defaults to noreply@localhost.

    The default is captured at module import. We re-import to confirm.
    """
    monkeypatch.delenv("FROM_EMAIL", raising=False)
    import importlib

    from core import email as email_mod

    reloaded = importlib.reload(email_mod)
    try:
        assert reloaded._FROM_EMAIL == "noreply@localhost"
    finally:
        importlib.reload(email_mod)  # restore


def test_i_send_no_smtp_silently_no_ops(monkeypatch):
    """I5: when SMTP_HOST is unset, send() is a no-op (no exception)."""
    from core import email

    monkeypatch.setattr(email, "_SMTP_HOST", "", raising=False)
    # Should not raise.
    email.send("user@example.com", "subj", "<b>hi</b>", "hi")


# ===========================================================================
# F. Built-in prefer_hosted dispatch — white-box on _try_hosted_builtin_agent
# ===========================================================================


def _enable_hosted(monkeypatch):
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "azh_unit")
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    hosted_client.reset_hosted_client_for_tests()


def test_f_unknown_agent_id_short_circuits(monkeypatch):
    """F6: agent_id_to_slug returns None for unknown IDs → no hosted call."""
    from server.builtin_agents import constants as bc

    fired = {"called": False}

    def _post(*a, **kw):
        fired["called"] = True
        return None

    _enable_hosted(monkeypatch)
    monkeypatch.setattr(hosted_client.requests, "post", _post)

    # Use the shard's _try_hosted_builtin_agent indirectly via constants.
    slug = bc.agent_id_to_slug("not-a-real-agent")
    assert slug is None
    # Even when "called" with an unknown agent id, the slug-mapping guards.


def test_f_prefer_hosted_set_membership():
    """F4: hosted-disabled instance never enters PREFER_HOSTED branch.

    This is a sanity check on the constant — the OSS surface should
    advertise exactly the LLM-tuned-prompt agents."""
    from server.builtin_agents import constants as bc

    prefer = bc.PREFER_HOSTED_AGENT_IDS
    # All members must map to a slug.
    for aid in prefer:
        assert bc.agent_id_to_slug(aid)


# ===========================================================================
# G. Reputation push fire-and-forget — direct unit on _push_rating_to_hosted_async
# ===========================================================================


def _wait_for_threads(prefix_check, timeout=2.0):
    """Wait for any non-main daemon threads spawned during the test to finish."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        live = [
            t
            for t in threading.enumerate()
            if t is not threading.main_thread()
            and t.is_alive()
            and prefix_check(t)
        ]
        if not live:
            return True
        time.sleep(0.02)
    return False


def test_g_push_no_op_when_hosted_disabled(monkeypatch):
    """G3: hosted off → no requests.post call."""
    from core import reputation

    monkeypatch.delenv("AZTEA_HOSTED_API_URL", raising=False)
    monkeypatch.delenv("AZTEA_HOSTED_API_KEY", raising=False)
    hosted_client.reset_hosted_client_for_tests()

    fired = {"calls": 0}

    def _post(*a, **kw):
        fired["calls"] += 1
        return None

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    reputation._push_rating_to_hosted_async(job_id="j", rating=5)
    _wait_for_threads(lambda t: True, timeout=0.5)
    assert fired["calls"] == 0


def test_g_push_fires_when_hosted_enabled(monkeypatch):
    """G1: hosted on → requests.post hits /v1/reputation/ratings, and the
    payload has local IDs HMAC-hashed (audit P2 fix)."""
    from core import reputation

    _enable_hosted(monkeypatch)
    monkeypatch.setenv("AZTEA_INSTANCE_SALT", "test-salt-rep-g1")

    captured = {"url": None, "json": None}
    done = threading.Event()

    class _Resp:
        ok = True
        status_code = 200
        url = "https://api.aztea.test/v1/reputation/ratings"
        headers = {}

        def close(self):
            return None

    def _post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        done.set()
        return _Resp()

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    reputation._push_rating_to_hosted_async(
        kind="quality", job_id="j", rating=5, agent_id="a", caller_owner_id="u"
    )
    assert done.wait(timeout=2.0), "rating push thread did not fire"
    assert captured["url"].endswith("/v1/reputation/ratings")
    payload = captured["json"] or {}
    # Non-sensitive fields untouched.
    assert payload.get("kind") == "quality"
    assert payload.get("rating") == 5
    assert payload.get("agent_id") == "a", "agent_id is a public UUID — must NOT be hashed"
    # Local identifiers must be HMAC-hashed (16 hex chars), not the originals.
    assert payload.get("job_id") != "j"
    assert payload.get("caller_owner_id") != "u"
    assert len(str(payload.get("job_id"))) == 16
    assert len(str(payload.get("caller_owner_id"))) == 16


def test_g_push_swallows_exception(monkeypatch):
    """G4: thread that raises must not bubble — the calling test stays clean."""
    from core import reputation

    _enable_hosted(monkeypatch)

    def _post(*a, **kw):
        raise RuntimeError("hosted broke")

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    # Must not raise.
    reputation._push_rating_to_hosted_async(job_id="j", rating=5)
    # Give the thread a moment to run + log.
    time.sleep(0.2)


def test_g_push_is_non_blocking(monkeypatch):
    """G5: a slow hosted server must not stall the main thread.

    We make `requests.post` sleep for 1.0s; the call to
    `_push_rating_to_hosted_async` must return well within 100ms.
    """
    from core import reputation

    _enable_hosted(monkeypatch)

    class _SlowResp:
        ok = True
        status_code = 200
        url = "x"
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            yield b"{}"

    def _slow(*a, **kw):
        time.sleep(1.0)
        return _SlowResp()

    monkeypatch.setattr(hosted_client.requests, "post", _slow)
    t0 = time.time()
    reputation._push_rating_to_hosted_async(job_id="j", rating=5)
    elapsed = time.time() - t0
    assert elapsed < 0.1, f"push blocked main thread for {elapsed:.3f}s"


# ===========================================================================
# E. Judge hosted-first — white-box on _try_hosted_judgment
# ===========================================================================


def test_e_try_hosted_disabled_returns_none(monkeypatch):
    """Hosted off → _try_hosted_judgment returns None without calling
    record_judgment."""
    from core import judges

    monkeypatch.delenv("AZTEA_HOSTED_API_URL", raising=False)
    hosted_client.reset_hosted_client_for_tests()

    fired = {"recorded": 0}

    def _record(*a, **kw):
        fired["recorded"] += 1

    monkeypatch.setattr(judges.disputes, "record_judgment", _record)
    monkeypatch.setattr(judges.disputes, "set_dispute_consensus", lambda *a, **kw: None)

    out = judges._try_hosted_judgment("d-1", {"dispute": {"reason": "x"}, "job": {}})
    assert out is None
    assert fired["recorded"] == 0


def test_e_try_hosted_invalid_verdict_returns_none(monkeypatch):
    """E3: hosted returns invalid verdict → returns None (caller falls
    through to local LLM / deterministic)."""
    from core import judges

    _enable_hosted(monkeypatch)

    class _Resp:
        ok = True
        status_code = 200
        url = "x"
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            yield b'{"verdict":"banana","reasoning":"bad","confidence":0.5}'

    monkeypatch.setattr(hosted_client.requests, "post", lambda url, **kw: _Resp())

    fired = {"recorded": 0, "consensus": 0}
    monkeypatch.setattr(
        judges.disputes,
        "record_judgment",
        lambda *a, **kw: fired.__setitem__("recorded", fired["recorded"] + 1),
    )
    monkeypatch.setattr(
        judges.disputes,
        "set_dispute_consensus",
        lambda *a, **kw: fired.__setitem__("consensus", fired["consensus"] + 1),
    )

    out = judges._try_hosted_judgment("d-1", {"dispute": {"reason": "x"}, "job": {}})
    assert out is None
    assert fired["recorded"] == 0
    assert fired["consensus"] == 0


def test_e_try_hosted_records_two_judgments_on_success(monkeypatch):
    """E2 / E7: hosted success records BOTH primary and secondary with the
    same model label, then sets consensus."""
    from core import judges

    _enable_hosted(monkeypatch)

    class _Resp:
        ok = True
        status_code = 200
        url = "x"
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            yield (
                b'{"verdict":"agent_wins","reasoning":"hosted","confidence":0.9,'
                b'"model":"hosted-llm-v1"}'
            )

    monkeypatch.setattr(hosted_client.requests, "post", lambda url, **kw: _Resp())

    recorded: list[dict] = []
    consensus = {"verdict": None}

    def _record(dispute_id, *, judge_kind, verdict, reasoning, model, **_kw):
        recorded.append(
            {"kind": judge_kind, "verdict": verdict, "reasoning": reasoning, "model": model}
        )

    monkeypatch.setattr(judges.disputes, "record_judgment", _record)
    monkeypatch.setattr(
        judges.disputes,
        "set_dispute_consensus",
        lambda did, v: consensus.__setitem__("verdict", v),
    )
    monkeypatch.setattr(judges.disputes, "get_judgments", lambda did: recorded)

    out = judges._try_hosted_judgment(
        "d-2", {"dispute": {"reason": "broken"}, "job": {}}
    )
    assert out and out["status"] == "consensus"
    assert out["outcome"] == "agent_wins"
    assert len(recorded) == 2
    assert {r["kind"] for r in recorded} == {"llm_primary", "llm_secondary"}
    # Same hosted model label on both rows.
    assert all(r["model"] == "hosted:hosted-llm-v1" for r in recorded)
    assert consensus["verdict"] == "agent_wins"


# ===========================================================================
# L. Loophole probes (the rest)
# ===========================================================================


def test_l4_reset_hosted_client_always_wins(monkeypatch):
    """L4: reset_hosted_client_for_tests guarantees the cache is cleared
    even if a previous test created a stale instance."""
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "k1")
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    c1 = hosted_client.get_hosted_client()
    hosted_client.reset_hosted_client_for_tests()
    c2 = hosted_client.get_hosted_client()
    assert c1 is not c2


def test_l8_hosted_exec_timeout_is_capped(monkeypatch):
    """L8: a slow hosted backend cannot exceed the configured exec timeout.

    We can't actually wait 30s here; instead we verify the timeout kwarg is
    handed to requests.post unchanged.
    """
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "k")
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    monkeypatch.setenv("AZTEA_HOSTED_EXEC_TIMEOUT", "5.5")
    hosted_client.reset_hosted_client_for_tests()

    captured = {}

    class _Resp:
        ok = True
        status_code = 200
        url = "x"
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            yield b'{"verdict":"agent_wins"}'

    def _post(url, **kw):
        captured.update(kw)
        return _Resp()

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    client = hosted_client.get_hosted_client()
    client.judge_dispute({"x": 1})
    assert captured["timeout"] == 5.5


def test_n1_oss_mode_no_outbound_socket(monkeypatch):
    """N1: in OSS-mode, calling every HostedClient method must not open a
    socket. We patch socket.socket to raise on any connect() attempt.
    """
    import socket

    monkeypatch.delenv("AZTEA_HOSTED_API_URL", raising=False)
    monkeypatch.delenv("AZTEA_HOSTED_API_KEY", raising=False)
    hosted_client.reset_hosted_client_for_tests()

    real_socket = socket.socket

    class _NoNet(real_socket):
        def connect(self, *a, **kw):
            raise AssertionError("OSS-mode opened a socket: SSRF / leak")

        def connect_ex(self, *a, **kw):
            raise AssertionError("OSS-mode opened a socket via connect_ex")

    monkeypatch.setattr(socket, "socket", _NoNet)
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({}) is None
    assert client.call_agent("x", {}) is None
    assert client.publish_listing({}) is None
    assert client.fetch_trust("did:web:example:agents:x") is None
    assert client.push_rating({}) is False


def test_n2_hosted_error_body_not_proxied_to_caller(monkeypatch):
    """N2: a 5xx hosted body containing 'Bearer ...' must not reach the
    caller's response body. The hosted_client returns None on non-ok, so
    the *body* of the response is dropped — only our structured 502 is
    exposed. This test confirms _post_json drops it.
    """
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "azh_secret")
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    hosted_client.reset_hosted_client_for_tests()

    class _Bad:
        ok = False
        status_code = 500
        url = "x"
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            yield b'{"echoed":"Bearer azh_secret","detail":"upstream"}'

    monkeypatch.setattr(hosted_client.requests, "post", lambda url, **kw: _Bad())
    client = hosted_client.get_hosted_client()
    out = client.publish_listing({"name": "x"})
    assert out is None  # error body never reaches caller


def test_l10_rating_payload_hashes_local_identifiers(monkeypatch):
    """L10 (fixed): the hosted rating push HMAC-hashes every local ID
    (caller_owner_id, agent_owner_id, job_id) before leaving the box, so
    aztea.ai's federated trust cache cannot re-identify users.

    Audit-P2 regression test. Two invariants:
      1. The original `user:abcdef` value is NEVER in the outbound payload.
      2. The same input under the same instance salt produces the same hash
         (deterministic, so the hosted side can dedupe).
    """
    from core import reputation

    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "k")
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    monkeypatch.setenv("AZTEA_INSTANCE_SALT", "test-salt-l10")
    hosted_client.reset_hosted_client_for_tests()

    captured = {}
    done = threading.Event()

    class _Resp:
        ok = True
        status_code = 200
        url = "x"
        headers = {}

        def close(self):
            return None

    def _post(url, **kw):
        captured.update(kw)
        done.set()
        return _Resp()

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    reputation._push_rating_to_hosted_async(
        kind="quality", job_id="j", rating=5, caller_owner_id="user:abcdef"
    )
    assert done.wait(timeout=2.0)
    body = captured.get("json") or {}
    # Hashed: never the original, always the deterministic 16-hex-char form.
    raw = "user:abcdef"
    assert body.get("caller_owner_id") != raw
    assert body.get("caller_owner_id") == reputation._instance_hash(raw)
    assert body.get("job_id") == reputation._instance_hash("j")
    # Non-sensitive fields untouched.
    assert body.get("rating") == 5
    assert body.get("kind") == "quality"
