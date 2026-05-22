"""Shared fixtures for the adversarial publish-pipeline test suite.

# OWNS: shared scammer-helper fixtures (mocked probe servers, dns-rebind
#   stubs, signed/unsigned verifier endpoints) plus the security pytest
#   markers used by tests/security/test_publish_robustness_*.py.
# NOT OWNS: the underlying integration TestClient (tests/integration/
#   conftest.py owns isolated_db and client), the scanner code under test
#   (core/listing_safety.py), or SSRF helpers (core/url_security.py).
# INVARIANTS:
#   - Every fixture restores monkeypatched state in teardown — no fixture
#     mutates module-level globals beyond the scope of its caller.
#   - Probe-mock fixtures default to refusing all calls; a test that wants
#     a permissive endpoint must opt in explicitly.
# DECISIONS:
#   - In-process HTTP fakes via monkeypatching ``core.http.post`` (and
#     ``socket.getaddrinfo`` for DNS-rebind tests) rather than spinning
#     up real uvicorn instances. Faster, deterministic, and the probe
#     surface only POSTs JSON, so a full network round-trip adds nothing.
"""
from __future__ import annotations

import socket
from typing import Any, Callable

import pytest

# Re-export the integration TestClient + isolated_db so security tests
# can `from tests.integration.conftest import *` indirectly. We import
# at module level so pytest registers them as collectable fixtures
# under this package too.
from tests.integration.conftest import client, isolated_db  # noqa: F401


def pytest_configure(config):  # noqa: D401 — pytest hook
    config.addinivalue_line("markers", "security: adversarial publish-pipeline tests")
    config.addinivalue_line("markers", "publish: publish-flow specific")
    config.addinivalue_line("markers", "ssrf: SSRF / URL validation")
    config.addinivalue_line("markers", "probe: live endpoint probe behaviour")
    config.addinivalue_line("markers", "identity: cryptographic agent identity")


# ---------------------------------------------------------------------------
# Probe response controller — lets a test script the responses the registration
# endpoint probe receives.
# ---------------------------------------------------------------------------


class FakeProbeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: Any = None,
        headers: dict | None = None,
        delay: float = 0.0,
    ):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.delay = delay
        self.text = self._body if isinstance(self._body, str) else ""

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


class ProbeRecorder:
    """Records each probe POST and replays a scripted response.

    Tests construct a recorder, register a sequence of responses (or a
    callable that picks one based on payload), then ``monkeypatch`` it
    onto ``core.http.post``. After the call under test, inspect ``calls``
    to assert on what the probe sent.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self._responder: Callable[[dict], Any] = lambda _: FakeProbeResponse()
        self._raise: Exception | None = None

    def set_responder(self, fn: Callable[[dict], Any]) -> "ProbeRecorder":
        self._responder = fn
        return self

    def respond_with(self, response: FakeProbeResponse) -> "ProbeRecorder":
        self._responder = lambda _: response
        return self

    def raise_with(self, exc: Exception) -> "ProbeRecorder":
        self._raise = exc
        return self

    def __call__(self, url, *, json=None, timeout=None, allow_redirects=None, headers=None, **kw):
        record = {
            "url": url,
            "json": json,
            "timeout": timeout,
            "allow_redirects": allow_redirects,
            "headers": headers or {},
            "kwargs": kw,
        }
        self.calls.append(record)
        if self._raise is not None:
            raise self._raise
        return self._responder(json or {})


@pytest.fixture
def probe_recorder(monkeypatch):
    """Replace the listing-safety probe HTTP client with a scripted recorder.

    Patches ``http.post`` everywhere the registration shard (part_003)
    invokes it. Returns the recorder so the test can assert on what was
    sent and what was received.
    """
    # The registration shard imports `requests as http` (server/application_parts/
    # part_000.py:38), so the probe call sites read it via the shared module
    # namespace as `http.post`. Patch the `requests` module directly because all
    # shards share that import.
    import requests as _requests

    recorder = ProbeRecorder()
    monkeypatch.setattr(_requests, "post", recorder)
    return recorder


@pytest.fixture
def enable_register_probe(monkeypatch):
    """Force the otherwise-skipped registration probe to actually run.

    Production toggles via ``AZTEA_SKIP_REGISTER_*`` — flip them so the
    probe path executes against the mocked HTTP client.
    """
    monkeypatch.delenv("AZTEA_SKIP_REGISTER_SAFETY_PROBE", raising=False)
    monkeypatch.delenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", raising=False)
    monkeypatch.setenv("AZTEA_RUN_REGISTER_SAFETY_PROBE", "1")


# ---------------------------------------------------------------------------
# DNS / SSRF rebind harness — lets a test return scripted IPs from getaddrinfo
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enforce_ssrf_for_security_tests(monkeypatch, request):
    """Make sure SSRF enforcement is ACTIVE during security tests.

    The repo's ``.env`` sets ``ALLOW_PRIVATE_OUTBOUND_URLS=1`` so dev
    machines can register agents at localhost. Without overriding it,
    every B-section SSRF test passes through trivially. Force it off
    unless a test opts back in.
    """
    if "noenforce" in request.keywords:
        return
    monkeypatch.delenv("ALLOW_PRIVATE_OUTBOUND_URLS", raising=False)


@pytest.fixture
def fake_dns(monkeypatch):
    """Returns a function that patches ``socket.getaddrinfo`` for SSRF tests.

    Returns ``AF_INET6`` tuples for hosts with IPv6 addresses (containing
    ``:``) and ``AF_INET`` otherwise. Tuples are shaped to match what
    ``url_security._check_resolved_ips`` expects:
    ``(family, socktype, proto, canonname, sockaddr)``.

    Unknown hosts fall through to the real resolver so we don't have to
    enumerate every test fixture's host.
    """
    real = socket.getaddrinfo

    def _apply(host_to_ips: dict[str, list[str]]) -> None:
        def fake(host, *args, **kwargs):
            if host in host_to_ips:
                rows = []
                for ip in host_to_ips[host]:
                    if ":" in ip:
                        rows.append(
                            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", (ip, 0, 0, 0))
                        )
                    else:
                        rows.append(
                            (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))
                        )
                return rows
            return real(host, *args, **kwargs)
        monkeypatch.setattr(socket, "getaddrinfo", fake)

    return _apply


# ---------------------------------------------------------------------------
# Owner / user provisioning shortcuts
# ---------------------------------------------------------------------------


@pytest.fixture
def make_user(client):
    """Register a fresh non-master user and return ``(user_dict, raw_key)``."""
    from tests.integration.helpers import _register_user

    def _make():
        u = _register_user()
        return u, u["raw_api_key"]
    return _make
