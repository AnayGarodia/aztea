# SPDX-License-Identifier: Apache-2.0
"""Tests for the DNS-rebinding defense in core/outbound_session.py.

The defense pins the hostname to a validated IP for the TCP connect via a
context-var-scoped ``socket.getaddrinfo`` patch. These tests verify:
  - The patch is a pass-through outside any pin context
  - Pinning substitutes the resolved IP for the hostname only
  - Resolution that yields a private/loopback IP raises ValueError
"""
from __future__ import annotations

import socket
from unittest import mock

import pytest

from core import outbound_session


def test_patched_getaddrinfo_passes_through_when_unpinned() -> None:
    """With no pin in the context, getaddrinfo behaves as the original."""
    # localhost always resolves regardless of network.
    rows = socket.getaddrinfo("localhost", 0, type=socket.SOCK_STREAM)
    # Should return at least one entry; we don't care about the specific IPs.
    assert len(rows) >= 1
    # First entry's sockaddr[0] is the IP.
    ip = rows[0][4][0]
    # Must be a loopback IP (or IPv6 ::1).
    assert ip.startswith("127.") or ip == "::1"


def test_pin_context_substitutes_hostname_to_ip() -> None:
    """Within the pin context, getaddrinfo returns the pinned IP for that hostname."""
    with outbound_session._pin_hostname_to_ip("example.test", "127.0.0.1"):
        rows = socket.getaddrinfo("example.test", 80, type=socket.SOCK_STREAM)
        # All rows should reflect the pinned IP (127.0.0.1).
        ips = {row[4][0] for row in rows}
        assert ips == {"127.0.0.1"}
    # Outside the context, the pin is gone — getaddrinfo would now attempt
    # real resolution of example.test (which is reserved by RFC 6761 and
    # typically NXDOMAIN). Verify by ensuring no leak.
    assert "example.test" not in outbound_session._pinned_resolutions.get()


def test_pin_does_not_affect_other_hostnames() -> None:
    """Pinning example.test must not redirect requests to localhost."""
    with outbound_session._pin_hostname_to_ip("example.test", "127.0.0.1"):
        # localhost should still resolve to loopback (not affected by pin).
        rows = socket.getaddrinfo("localhost", 0, type=socket.SOCK_STREAM)
        ips = {row[4][0] for row in rows}
        assert ips & {"127.0.0.1", "::1"}, f"localhost did not resolve normally: {ips}"


def test_resolve_and_validate_blocks_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that resolves to a loopback IP raises (DNS-rebinding blocked)."""
    # Make sure no prior test left ALLOW_PRIVATE_OUTBOUND_URLS set — that
    # env disables the policy in dev mode.
    monkeypatch.delenv("ALLOW_PRIVATE_OUTBOUND_URLS", raising=False)
    # Force the resolver to return 127.0.0.1 for a fake hostname.
    fake_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]
    with mock.patch.object(
        outbound_session, "_original_getaddrinfo", return_value=fake_addrs
    ):
        with pytest.raises(ValueError, match="DNS-rebinding"):
            outbound_session._resolve_and_validate_ip("malicious.example.com")


def test_resolve_and_validate_returns_none_for_ip_literal() -> None:
    """IP literals (already validated upstream) need no pinning."""
    assert outbound_session._resolve_and_validate_ip("8.8.8.8") is None
    assert outbound_session._resolve_and_validate_ip("::1") is None


def test_resolve_and_validate_returns_none_when_dev_override_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ALLOW_PRIVATE_OUTBOUND_URLS=1 disables the pinning (dev mode)."""
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    # Even with a hostname that would resolve to a private IP, we skip.
    assert outbound_session._resolve_and_validate_ip("anything.local") is None
