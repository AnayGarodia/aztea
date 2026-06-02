# SPDX-License-Identifier: Apache-2.0
"""
outbound_session.py — pooled `requests.Session` for outbound agent dispatch.

OWNS: a single module-level ``requests.Session`` shared by every site that
      proxies to an external agent endpoint. Per-host HTTPAdapter mounting
      keeps each remote host's pool independent so a saturating fan-out
      against one agent does not back up calls to another.
NOT OWNS: SSRF validation rules (live in ``core/url_security.py``). We
      defer to that module for the IP-allowed/blocked policy.
INVARIANTS:
  - Pool is process-local. Multi-worker deploys give each worker its own
    pool — this is intentional.
  - ``pool_block=False``: a saturating pool raises ``urllib3.exceptions.
    MaxRetryError`` / ``requests.ConnectionError`` immediately rather than
    queueing. Callers translate this to a structured 503 with
    ``outbound.pool_saturated``.
  - ``recycle()`` closes and rebuilds the session, dropping all keepalive
    connections. Called periodically by the sweeper to protect against
    stale keepalives behind a CDN/load-balancer DNS rotation.
  - DNS rebinding defense: every ``post()`` resolves the hostname ourselves,
    validates the IP, and pins the resolution for the duration of the call
    via a context-var-scoped ``socket.getaddrinfo`` patch. The TCP connect
    can only land on the IP we just validated; TLS SNI + cert validation
    continue to use the original hostname so HTTPS still works end-to-end.
DECISIONS:
  - We do NOT swap every ``http.post`` site over to this module — only the
    explicit agent-dispatch site in ``part_008.py``. One-off paths (Stripe
    webhook ingest, hosted-client calls, etc.) keep bare ``requests``
    because pool reuse is not the bottleneck there.
  - We use ``requests`` rather than ``httpx`` for now. HTTP/2 via httpx is
    a Phase-4 follow-up if Phase 1-3 don't close the latency target.
  - The ``socket.getaddrinfo`` patch is installed at module load. The
    patched function is a no-op outside our pinning context (it just calls
    the original), so it does not affect other code paths.
"""
from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import logging
import os
import socket
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core import observability as _observability
from core import url_security as _url_security

logger = logging.getLogger(__name__)

# Captured at module load. If a caller (typically a test using
# ``monkeypatch.setattr(server.http, "post", fake_post)``) replaces
# ``requests.post`` after import, ``post()`` below detects the swap and
# delegates to the replacement instead of hitting the real socket. This
# preserves the existing test suite's ability to mock outbound calls at the
# canonical boundary without forcing every test file to learn about the
# pooled session abstraction.
_REAL_REQUESTS_POST = requests.post


# ── DNS-rebinding defense ─────────────────────────────────────────────────
#
# ``socket.getaddrinfo`` is monkey-patched at module load. When the
# ``_pinned_resolutions`` context-var is non-empty, the patched function
# substitutes the requested hostname with the pinned IP literal before
# resolving. Outside our pinning context the patch is a pure pass-through.
#
# Thread / asyncio safety: context-vars are per-context (per-thread + per-
# asyncio-task), so concurrent ``post()`` calls do not see each other's
# pins. The patch itself is read-only against ``socket.getaddrinfo``.

_pinned_resolutions: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "aztea_outbound_pinned_ips", default={}
)
_original_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    """Substitute the pinned IP for ``host`` when present in the context-var."""
    if isinstance(host, str):
        pinned = _pinned_resolutions.get()
        if pinned and host in pinned:
            return _original_getaddrinfo(pinned[host], port, *args, **kwargs)
    return _original_getaddrinfo(host, port, *args, **kwargs)


# Install once, idempotently. Re-imports (e.g. test runs) see the same
# patched function and do not re-wrap.
if socket.getaddrinfo is not _patched_getaddrinfo:
    socket.getaddrinfo = _patched_getaddrinfo  # type: ignore[assignment]


def _resolve_and_validate_ip(hostname: str) -> str | None:
    """Resolve ``hostname`` to a public IP. None when no pin is needed.

    Returns None when the hostname is already an IP literal (no resolution
    to pin) or when ``ALLOW_PRIVATE_OUTBOUND_URLS`` is set (dev override).
    Raises ``ValueError`` when resolution yields a disallowed IP — a
    DNS-rebinding attempt that bypassed the upstream check.
    """
    if not hostname:
        return None
    if _url_security._allow_private_default():
        return None
    try:
        ipaddress.ip_address(hostname)
        return None
    except ValueError:
        pass
    try:
        # Use the original getaddrinfo so we never recurse through our patch.
        addrs = _original_getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        logger.debug("outbound_session: DNS resolution failed for %s: %s", hostname, exc)
        return None
    for addr_info in addrs:
        ip_str = addr_info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _url_security._is_disallowed_ip(ip_obj):
            raise ValueError(
                f"outbound_session: hostname {hostname} resolved to disallowed "
                f"IP {ip_str} (DNS-rebinding attempt blocked)."
            )
        return ip_str
    return None


@contextlib.contextmanager
def _pin_hostname_to_ip(hostname: str, ip: str) -> Iterator[None]:
    """Within the context, ``socket.getaddrinfo(hostname, ...)`` returns ``ip``."""
    current = dict(_pinned_resolutions.get())
    current[hostname] = ip
    token = _pinned_resolutions.set(current)
    try:
        yield
    finally:
        _pinned_resolutions.reset(token)


@contextlib.contextmanager
def pinned_ip_for_url(url: str) -> Iterator[None]:
    """Public DNS-rebinding defense reusable by ANY socket-based client.

    Both ``requests`` and ``httpx`` (sync) resolve through ``socket.getaddrinfo``,
    so the same context-var pin protects either. Resolves the URL's host, validates
    the IP against ``url_security`` policy, and pins that IP for the duration of the
    block so a TTL=0 rebind to a private IP at connect time cannot land. No-op when
    the host is an IP literal or ``ALLOW_PRIVATE_OUTBOUND_URLS`` is set. Raises
    ``ValueError`` on a rebind attempt (host resolves to a disallowed IP). Callers
    should still call ``url_security.validate_outbound_url(url)`` first — this closes
    the resolve→connect TOCTOU gap that validation alone leaves open.
    """
    hostname = urlparse(url).hostname or ""
    pinned_ip = _resolve_and_validate_ip(hostname)
    if pinned_ip is not None and hostname:
        with _pin_hostname_to_ip(hostname, pinned_ip):
            yield
    else:
        yield


# Why named: separates pool/adapter knobs from runtime call sites. Tuned for
# a buyer-agent fan-out workload (e.g. 64-job manage_workflow hire_batch
# against one host). Each remote host gets its own adapter so saturation on
# host A doesn't block host B.
_POOL_CONNECTIONS = 16        # pool slots per host
_POOL_MAXSIZE = 32            # max simultaneous connections per host
_RETRY_TOTAL = 1              # one retry on transient 5xx; main retry policy lives in SDKs
_RETRY_STATUS_FORCELIST = (502, 503, 504)
_RECYCLE_SECONDS_DEFAULT = 300

_lock = threading.Lock()
_session: requests.Session | None = None
_session_built_at: float = 0.0
_mounted_hosts: set[str] = set()


def _build_session() -> requests.Session:
    """Pure-ish: construct a configured Session. Side effect: logger only."""
    sess = requests.Session()
    sess.headers.setdefault("User-Agent", "aztea-outbound-pool/1")
    # No default adapter mount here — we lazy-mount per host the first time
    # we see one. urllib3's default adapter still backs unmatched URLs
    # (extremely cheap fallback).
    return sess


def _adapter() -> HTTPAdapter:
    """Pure: construct one HTTPAdapter with the project's pool + retry policy."""
    retry = Retry(
        total=_RETRY_TOTAL,
        status_forcelist=_RETRY_STATUS_FORCELIST,
        allowed_methods=frozenset({"GET", "POST", "PUT", "DELETE", "PATCH"}),
        backoff_factor=0.0,  # caller-driven backoff
        raise_on_status=False,
    )
    return HTTPAdapter(
        pool_connections=_POOL_CONNECTIONS,
        pool_maxsize=_POOL_MAXSIZE,
        pool_block=False,
        max_retries=retry,
    )


def _ensure_session() -> requests.Session:
    """Lazy-init the module-level session under the build lock."""
    global _session, _session_built_at
    with _lock:
        if _session is None:
            _session = _build_session()
            _session_built_at = time.monotonic()
            _mounted_hosts.clear()
        return _session


def _ensure_host_mount(session: requests.Session, url: str) -> None:
    """Mount a per-host HTTPAdapter on first use so each host gets its own pool."""
    host = urlparse(url).netloc
    if not host or host in _mounted_hosts:
        return
    with _lock:
        if host in _mounted_hosts:
            return
        # Mount both schemes for the host. urllib3 keys pools by (scheme, host,
        # port), but session.mount key prefix matching does scheme-aware
        # lookup, so we mount both to cover http and https variants.
        for scheme in ("https://", "http://"):
            session.mount(f"{scheme}{host}", _adapter())
        _mounted_hosts.add(host)


def post(url: str, **kwargs: Any) -> requests.Response:
    """Pooled-session POST. Mirrors ``requests.post(url, **kwargs)``.

    Side effects: TCP/TLS handshake on cache miss; reuses keepalive connection
    otherwise. Increments ``outbound_pool_saturation_total`` when the pool
    can't supply a connection.

    DNS-rebinding defense: resolves the hostname ourselves, validates the
    resolved IP against ``url_security`` policy, and pins that IP for the
    TCP connect via a context-var-scoped ``socket.getaddrinfo`` patch. An
    attacker with TTL=0 DNS who passes the upstream URL check by returning
    a public IP then tries to redirect the connect to a private IP cannot
    succeed — the second resolution would yield a disallowed IP and we
    raise before opening the socket.

    Caller policy: callers SHOULD also validate the URL upstream via
    ``url_security.validate_outbound_url``. We re-run that check here as
    defense in depth so a future caller forgetting the validation cannot
    bypass the SSRF gate.

    Test mockability: if ``requests.post`` has been replaced (typically by
    ``monkeypatch.setattr(server.http, "post", fake_post)`` in the
    integration suite), delegate to the replacement instead of running the
    pooled path. Tests do not need to know about this module's existence.
    """
    if requests.post is not _REAL_REQUESTS_POST:
        return requests.post(url, **kwargs)
    _url_security.validate_outbound_url(url, "outbound_session.post")
    sess = _ensure_session()
    _ensure_host_mount(sess, url)

    hostname = urlparse(url).hostname or ""
    pinned_ip = _resolve_and_validate_ip(hostname)

    pin_ctx: contextlib.AbstractContextManager[None]
    if pinned_ip is not None and hostname:
        pin_ctx = _pin_hostname_to_ip(hostname, pinned_ip)
    else:
        pin_ctx = contextlib.nullcontext()

    with pin_ctx:
        try:
            return sess.post(url, **kwargs)
        except requests.ConnectionError:
            # urllib3's MaxRetryError wrapping when pool_block=False and the
            # pool can't supply a connection. Bubble up to the route, but tag
            # the saturation counter first.
            host = urlparse(url).netloc or "unknown"
            try:
                _observability.outbound_pool_saturation_total.labels(host=host).inc()
            except Exception:  # pragma: no cover
                pass
            raise


def recycle() -> None:
    """Drop all keepalive connections. Called by the sweeper on a cadence.

    Protects against stale keepalives when a remote host's IP rotates
    (CDN, load balancer behind round-robin DNS). The next call rebuilds the
    pool on demand.
    """
    global _session, _session_built_at
    with _lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:  # pragma: no cover
                pass
        _session = None
        _session_built_at = 0.0
        _mounted_hosts.clear()
    try:
        _observability.outbound_session_recycles_total.inc()
    except Exception:  # pragma: no cover
        pass


def maybe_recycle() -> bool:
    """Call from the sweeper. Recycles if older than the configured TTL."""
    ttl = _recycle_seconds()
    with _lock:
        age = time.monotonic() - _session_built_at if _session is not None else 0.0
    if _session is not None and age >= ttl:
        recycle()
        return True
    return False


def _recycle_seconds() -> float:
    """Read at call time so env changes propagate without restart."""
    raw = os.environ.get("AZTEA_OUTBOUND_SESSION_RECYCLE_SECONDS", "").strip()
    if not raw:
        return float(_RECYCLE_SECONDS_DEFAULT)
    try:
        v = float(raw)
        return v if v > 0 else float(_RECYCLE_SECONDS_DEFAULT)
    except ValueError:
        return float(_RECYCLE_SECONDS_DEFAULT)


def close() -> None:
    """Shutdown hook: closes the session. Safe to call multiple times."""
    global _session, _session_built_at
    with _lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:  # pragma: no cover
                pass
            _session = None
            _session_built_at = 0.0
            _mounted_hosts.clear()
