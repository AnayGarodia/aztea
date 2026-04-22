"""
url_security.py — Shared outbound URL validation (SSRF guard).

Both the HTTP app (``server.application``) and core/onboarding.py need to reject URLs that target
private / loopback / reserved IPs, credentialed URLs, and localhost. The
logic lives here so there is exactly one implementation.

The validator raises plain ``ValueError``. Callers that need a domain-specific
exception type (e.g. ``MetadataValidationError``) should catch and re-raise.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import unquote, urlparse

_ENV_ALLOW_PRIVATE_VALUES = {"1", "true", "yes"}


def _allow_private_default() -> bool:
    return os.environ.get("ALLOW_PRIVATE_OUTBOUND_URLS", "0").strip().lower() in _ENV_ALLOW_PRIVATE_VALUES


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
    # Block IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
    if isinstance(ip_value, ipaddress.IPv6Address) and ip_value.ipv4_mapped is not None:
        return _is_disallowed_ip(ip_value.ipv4_mapped)
    return False


def validate_outbound_url(
    url: str,
    field_name: str,
    *,
    allow_private: bool | None = None,
) -> str:
    """
    Validate a caller-supplied outbound URL and return the stripped form.

    Raises ``ValueError`` with a human-readable, ``field_name``-prefixed message
    on any of:
      - non-http(s) scheme or missing host,
      - embedded credentials or fragments,
      - invalid port,
      - percent-encoded hostname (SSRF evasion),
      - localhost / *.localhost targets,
      - direct or resolved IP in private / loopback / reserved ranges.

    Set ``allow_private=True`` to skip the private-IP checks explicitly.
    By default the ``ALLOW_PRIVATE_OUTBOUND_URLS`` environment variable is
    consulted (truthy values: ``1``, ``true``, ``yes``).
    """
    normalized = url.strip()
    parsed = urlparse(normalized)
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

    if allow_private is None:
        allow_private = _allow_private_default()
    if allow_private:
        return normalized

    # Reject URL-encoded characters in the hostname (e.g. 127%2E0%2E0%2E1 or %00 null-byte tricks)
    if host != unquote(host):
        raise ValueError(f"{field_name} hostname must not contain percent-encoded characters.")

    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(
            f"{field_name} cannot target localhost unless ALLOW_PRIVATE_OUTBOUND_URLS=1."
        )

    try:
        direct_ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved_rows = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return normalized
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
                    f"{field_name} cannot target hostnames resolving to private/loopback/reserved IPs "
                    "unless ALLOW_PRIVATE_OUTBOUND_URLS=1."
                )
        return normalized

    if _is_disallowed_ip(direct_ip):
        raise ValueError(
            f"{field_name} cannot target private/loopback/reserved IPs unless "
            "ALLOW_PRIVATE_OUTBOUND_URLS=1."
        )
    return normalized
