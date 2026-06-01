"""
url_security.py — Shared outbound URL validation (SSRF guard).

# OWNS: SSRF validation for all outbound URLs (agent endpoints, webhooks, git paths)
# NOT OWNS: business logic for what to do after validation fails
# INVARIANTS:
#   - validate_outbound_url / validate_agent_endpoint_url always raise on bad input
#     (legacy API, kept for existing callers)
#   - validate_outbound_url_result / validate_agent_endpoint_url_result return Result
#     (preferred in new code — no hidden control flow)
#   - DNS resolution is I/O and belongs here; callers handle the Result at the boundary
# DECISIONS:
#   - Result variants wrap the raising variants rather than duplicating logic —
#     single implementation path prevents the two APIs diverging silently
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any
from urllib.parse import unquote, urlparse

from core.functional import Err, Ok, Result

_ENV_ALLOW_PRIVATE_VALUES = {"1", "true", "yes"}

# Hosts known to be HTTP request-echo / inspection services. Registering an
# agent endpoint at one of these is almost always a forgotten test stub and
# never legitimate marketplace traffic — and the marketplace has been bitten
# by exactly that (a `qa_payout_curve_agent` pointing at httpbin.org made it
# to production and charged real users $0.05/call to echo their input back).
# Match by exact host *or* registrable domain suffix.
_BLOCKED_AGENT_HOST_SUFFIXES: tuple[str, ...] = (
    "httpbin.org",
    "httpbingo.org",
    "httpbingo.com",
    "requestbin.com",
    "requestbin.net",
    "webhook.site",
    "ngrok.io",
    "ngrok.app",
    "ngrok-free.app",
    "ngrok-free.dev",
    "loca.lt",
    "trycloudflare.com",
    "serveo.net",
    # Added 2026-05-22 after the publish-robustness audit (tests/security/
    # GAP_REPORT.md B8). These services all proxy a publisher's localhost
    # endpoint to a public hostname, defeating the SSRF + listing-safety
    # guarantees by design. Refusing them at registration is the cleanest
    # mitigation; legitimate publishers should self-host.
    "cfargotunnel.com",        # Cloudflare Tunnel (named tunnels)
    "lhr.life",                # localhost.run
    "lhrtunnel.link",          # localhost.run alternative
    "devtunnels.ms",           # Microsoft / VS Code dev tunnels
    "bore.pub",                # bore.pub
    "pinggy.online",           # Pinggy
    "pinggy.link",             # Pinggy alternative
    "zrok.io",                 # OpenZiti zrok
    "pagekite.me",             # PageKite
    "localto.net",             # LocalTo
    "tuna.am",                 # Tuna
)


def _is_blocked_agent_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h:
        return False
    for suffix in _BLOCKED_AGENT_HOST_SUFFIXES:
        if h == suffix or h.endswith("." + suffix):
            return True
    return False


def _allow_private_default() -> bool:
    return (
        os.environ.get("ALLOW_PRIVATE_OUTBOUND_URLS", "0").strip().lower()
        in _ENV_ALLOW_PRIVATE_VALUES
    )


# CGNAT range (100.64.0.0/10, RFC 6598). Used by Alibaba Cloud's instance
# metadata service at 100.100.100.200 and by some CGNAT-deployed ISPs that
# treat the block as routable-but-internal. ipaddress.is_private excludes
# this range (it's not in RFC 1918), so we check it explicitly.
_CGNAT_V4_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")


def _is_cgnat(ip_value: ipaddress._BaseAddress) -> bool:
    return (
        isinstance(ip_value, ipaddress.IPv4Address) and ip_value in _CGNAT_V4_NETWORK
    )


def _is_disallowed_ip(ip_value: ipaddress._BaseAddress) -> bool:
    if (
        ip_value.is_private
        or ip_value.is_loopback
        or ip_value.is_link_local
        or ip_value.is_reserved
        or ip_value.is_multicast
        or ip_value.is_unspecified
    ):
        return True
    if _is_cgnat(ip_value):
        return True
    # Block IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
    if isinstance(ip_value, ipaddress.IPv6Address) and ip_value.ipv4_mapped is not None:
        return _is_disallowed_ip(ip_value.ipv4_mapped)
    return False


# Sentinel endpoint for polling / async workers (e.g. the SDK AgentServer) that
# pull jobs from the async /jobs queue and expose NO inbound HTTP endpoint.
# Aztea must NEVER dial this scheme — it is not a network target. Registration,
# endpoint health monitoring, and the sync /call proxy all special-case it.
# The SDK mirrors this literal in aztea/agent.py::_POLLING_WORKER_ENDPOINT (the
# SDK is a separate package and cannot import core); a contract test keeps the
# two in sync, since a server-side scheme rename would silently break the SDK.
POLLING_WORKER_SCHEME = "poll"
POLLING_WORKER_ENDPOINT = "poll://worker"


def is_polling_worker_endpoint(url: str) -> bool:
    """True when ``url`` is the polling-worker sentinel (no inbound endpoint).

    Polling workers are dispatched via the async job queue, so they have no
    reachable URL; callers must skip SSRF/liveness checks against them.
    """
    return str(url or "").strip().lower().startswith(POLLING_WORKER_SCHEME + "://")


def validate_agent_endpoint_url(
    url: str,
    field_name: str = "endpoint_url",
    *,
    allow_private: bool | None = None,
) -> str:
    """Stricter outbound-URL check used when *registering* an agent.

    Performs the full SSRF check then additionally rejects hosts known to be
    HTTP request-echo / inspection services. These hosts are almost always
    test stubs, and one (``httpbin.org``) was caught in production charging
    users for fake responses.
    """
    # Polling/async workers have no inbound endpoint. Collapse any poll:// input
    # to a fixed sentinel and skip SSRF/host checks — it is never dialed.
    # Discarding the user-supplied netloc prevents smuggling an SSRF target
    # behind the poll:// scheme.
    if is_polling_worker_endpoint(url):
        return POLLING_WORKER_ENDPOINT
    normalized = validate_outbound_url(url, field_name, allow_private=allow_private)
    parsed = urlparse(normalized.strip())
    host = (parsed.hostname or "").strip().lower()
    if _is_blocked_agent_host(host):
        raise ValueError(
            f"{field_name} cannot target HTTP request-echo / inspection hosts "
            f"({host}). Register a real agent endpoint."
        )
    return normalized


def _check_url_shape(parsed: Any, field_name: str) -> str:
    """Pure: enforce scheme, netloc, credential, fragment, port rules; returns lowercase host.

    Why: every caller-supplied URL goes through SSRF validation; failing
    fast on syntactic violations stops a malformed URL from reaching DNS.
    """
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute http(s) URL.")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not include username or password.")
    if parsed.fragment:
        raise ValueError(f"{field_name} must not include URL fragments.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError(f"{field_name} hostname is missing.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} has an invalid port.") from exc
    return host


def _enforce_hostname_safety(host: str, field_name: str) -> None:
    """Pure: reject percent-encoded hostnames + localhost variants.

    Why: percent-encoded forms like ``127%2E0%2E0%2E1`` and ``%00`` null-byte
    tricks evade naive blocklists; the localhost guard fires before DNS so
    the operator's intent is clear in the error message.
    """
    if host != unquote(host):
        raise ValueError(
            f"{field_name} hostname must not contain percent-encoded characters."
        )
    # Localhost variants: bare 'localhost', any *.localhost subdomain, the
    # legacy 'localhost.localdomain' alias still configured on many distros
    # (resolves to 127.0.0.1 via /etc/hosts), and the IPv6 loopback name.
    # See tests/security/GAP_REPORT.md B12.
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host == "localhost.localdomain"
        or host.endswith(".localhost.localdomain")
        or host == "ip6-localhost"
        or host == "ip6-loopback"
    ):
        raise ValueError(
            f"{field_name} blocked by network policy (localhost target)."
        )


def _check_resolved_ips(host: str, field_name: str) -> None:
    """Side-effect: resolve ``host`` and reject if any A/AAAA record is private/reserved.

    Why: a hostname pointing at ``169.254.169.254`` would otherwise bypass
    the direct-IP check; ``getaddrinfo`` is the only practical way to
    enforce SSRF policy on hostnames.
    """
    try:
        resolved_rows = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return
    except OSError as exc:
        raise ValueError(f"{field_name} hostname resolution failed.") from exc
    for row in resolved_rows:
        sockaddr = row[4]
        if not sockaddr:
            continue
        try:
            resolved_ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_disallowed_ip(resolved_ip):
            raise ValueError(
                f"{field_name} blocked by network policy (resolves to a non-public IP)."
            )


def validate_outbound_url(
    url: str,
    field_name: str,
    *,
    allow_private: bool | None = None,
) -> str:
    """Validate a caller-supplied outbound URL and return the stripped form.

    Raises ``ValueError`` (with ``field_name`` prefix) for any of: non-http(s)
    scheme, missing host, embedded credentials, fragments, invalid port,
    percent-encoded hostname, localhost targets, or IPs in private /
    loopback / reserved ranges. Pass ``allow_private=True`` to skip the
    private-IP checks; default consults ``ALLOW_PRIVATE_OUTBOUND_URLS``.
    """
    normalized = url.strip()
    parsed = urlparse(normalized)
    host = _check_url_shape(parsed, field_name)
    if allow_private is None:
        allow_private = _allow_private_default()
    if allow_private:
        return normalized
    _enforce_hostname_safety(host, field_name)
    try:
        direct_ip = ipaddress.ip_address(host)
    except ValueError:
        _check_resolved_ips(host, field_name)
        return normalized
    if _is_disallowed_ip(direct_ip):
        raise ValueError(
            f"{field_name} blocked by network policy (non-public IP)."
        )
    return normalized


# ---------------------------------------------------------------------------
# Result-returning variants — preferred in new code
# ---------------------------------------------------------------------------


def validate_outbound_url_result(
    url: str,
    field_name: str,
    *,
    allow_private: bool | None = None,
) -> "Result[str, str]":
    """Result-returning variant of :func:`validate_outbound_url`.

    Returns ``Ok(normalized_url)`` or ``Err(message)``.  Use this in new
    code so validation failures are explicit in the type signature rather
    than hidden control flow via ``ValueError``.
    """
    try:
        return Ok(validate_outbound_url(url, field_name, allow_private=allow_private))
    except ValueError as exc:
        return Err(str(exc))


def validate_agent_endpoint_url_result(
    url: str,
    field_name: str = "endpoint_url",
    *,
    allow_private: bool | None = None,
) -> "Result[str, str]":
    """Result-returning variant of :func:`validate_agent_endpoint_url`."""
    try:
        return Ok(validate_agent_endpoint_url(url, field_name, allow_private=allow_private))
    except ValueError as exc:
        return Err(str(exc))
