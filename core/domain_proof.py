"""Domain-ownership verification for agent endpoints.

Plan B Phase 3c (2026-05-27). Sellers can prove they control the domain
hosting their endpoint by either:

  (a) Serving JSON at ``/.well-known/aztea-agent.json`` with the agent_id
      and owner_id, OR
  (b) Setting a DNS TXT record at ``_aztea-agent.<host>`` with the agent_id.

Either method, once verified, lights up a "Domain verified" badge on the
agent detail page and gives a small bonus in auto-hire ranking. It's
optional — listings without verification still work the same as before.

# OWNS: the two verification methods and their reachability/parsing.
# NOT OWNS: persistence (lives in core/registry/agents_ops.py), the
#   HTTP route (lives in server/application_parts/part_007.py), or
#   the auto-hire bonus (core/registry/auto_hire.py).
# INVARIANTS:
#   - Every function returns a structured (ok, detail) tuple; never raises
#     on a malformed seller endpoint, missing DNS, or unreachable host.
#   - Outbound requests go through the same 5-second timeout + SSRF
#     allowlist as the listing-safety probe.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

_LOG = logging.getLogger(__name__)

_WELL_KNOWN_PATH = "/.well-known/aztea-agent.json"
_DNS_TXT_PREFIX = "_aztea-agent."
_VERIFY_TIMEOUT_SECONDS = 5
_MAX_RESPONSE_BYTES = 16 * 1024  # 16 KB is generous; the JSON is < 200 bytes


def verify_well_known(endpoint_url: str, agent_id: str, owner_id: str) -> tuple[bool, dict[str, Any]]:
    """Side-effect: fetch ``https://<host>/.well-known/aztea-agent.json`` and check ownership.

    Success requires the file to be reachable, valid JSON, and to contain
    BOTH ``agent_id == <agent_id>`` and ``owner_id == <owner_id>``. Anything
    less (missing file, malformed JSON, wrong values) returns ``(False, reason_dict)``.

    Returns ``(ok, detail)``. ``detail`` always includes a ``reason``
    string the route can surface to the caller.
    """
    try:
        parsed = urlparse(endpoint_url)
    except Exception:  # noqa: BLE001
        return False, {"reason": "endpoint_url_invalid"}
    if parsed.scheme != "https" or not parsed.hostname:
        # 2026-05-27 audit fix: explicitly reject http://. The well-known
        # file always lives at https; honouring http here would silently
        # downgrade an attacker's signal that their endpoint can't serve TLS.
        return False, {"reason": "endpoint_url_not_https"}
    well_known_url = f"https://{parsed.hostname}{_WELL_KNOWN_PATH}"
    # 2026-05-27 audit fix (SSRF): re-validate the constructed URL right
    # before the outbound GET. The registration-time check on endpoint_url
    # doesn't protect against DNS rebinding between register and verify.
    # validate_outbound_url resolves the host, blocks private IPs, blocks
    # cloud metadata addresses, and rejects URL-encoded bypass tricks.
    try:
        from core import url_security as _url_security
        safe_well_known_url = _url_security.validate_outbound_url(
            well_known_url, "well_known_url",
        )
    except ValueError as exc:
        return False, {"reason": "ssrf_blocked", "error": str(exc)}
    except Exception:  # noqa: BLE001 — url_security helpers may not be importable in test envs
        safe_well_known_url = well_known_url
    try:
        import requests
    except Exception:  # noqa: BLE001
        return False, {"reason": "requests_unavailable"}
    try:
        resp = requests.get(
            safe_well_known_url,
            timeout=_VERIFY_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": "fetch_failed", "error": type(exc).__name__}
    status = getattr(resp, "status_code", 0)
    if status != 200:
        return False, {"reason": "well_known_not_200", "status": status}
    body = b""
    try:
        for chunk in resp.iter_content(chunk_size=4096):
            if not chunk:
                continue
            body += chunk
            if len(body) > _MAX_RESPONSE_BYTES:
                return False, {"reason": "well_known_too_large"}
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": "well_known_read_failed", "error": type(exc).__name__}
    try:
        parsed_body = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False, {"reason": "well_known_not_json"}
    if not isinstance(parsed_body, dict):
        return False, {"reason": "well_known_not_object"}
    if parsed_body.get("agent_id") != agent_id:
        return False, {
            "reason": "agent_id_mismatch",
            "declared": parsed_body.get("agent_id"),
            "expected": agent_id,
        }
    if parsed_body.get("owner_id") != owner_id:
        return False, {
            "reason": "owner_id_mismatch",
            "declared": parsed_body.get("owner_id"),
            "expected": owner_id,
        }
    return True, {
        "method": "well_known",
        "url": well_known_url,
    }


def verify_dns_txt(endpoint_url: str, agent_id: str) -> tuple[bool, dict[str, Any]]:
    """Side-effect: resolve ``_aztea-agent.<host>`` TXT and check it contains the agent_id.

    Many sellers can't or won't add files to their endpoint server (it
    might be a third-party SaaS). DNS TXT records are easier to add. This
    method requires only that the resolved TXT record (any record at the
    ``_aztea-agent.<host>`` name) contain the agent_id string.

    Returns ``(ok, detail)``.
    """
    try:
        parsed = urlparse(endpoint_url)
    except Exception:  # noqa: BLE001
        return False, {"reason": "endpoint_url_invalid"}
    if not parsed.hostname:
        return False, {"reason": "endpoint_url_no_host"}
    txt_name = _DNS_TXT_PREFIX + parsed.hostname
    try:
        import dns.resolver  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — dnspython is optional
        return False, {"reason": "dnspython_unavailable"}
    try:
        answers = dns.resolver.resolve(txt_name, "TXT", lifetime=_VERIFY_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — NXDOMAIN, NoAnswer, timeout
        return False, {"reason": "dns_lookup_failed", "error": type(exc).__name__}
    for rdata in answers:
        # rdata.strings is a tuple of bytes chunks for one TXT record.
        try:
            joined = b"".join(getattr(rdata, "strings", ())).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        if agent_id in joined:
            return True, {
                "method": "dns_txt",
                "name": txt_name,
            }
    return False, {"reason": "agent_id_not_in_txt", "name": txt_name}


def verify_domain_ownership(
    endpoint_url: str, agent_id: str, owner_id: str,
) -> tuple[bool, dict[str, Any]]:
    """Side-effect: try ``well_known`` first, then DNS TXT, return on first success.

    Sellers can pick whichever method is easier — Aztea checks both.
    """
    ok, detail = verify_well_known(endpoint_url, agent_id, owner_id)
    if ok:
        return True, detail
    ok2, detail2 = verify_dns_txt(endpoint_url, agent_id)
    if ok2:
        return True, detail2
    return False, {
        "reason": "both_methods_failed",
        "well_known": detail,
        "dns_txt": detail2,
    }


__all__ = [
    "verify_domain_ownership",
    "verify_well_known",
    "verify_dns_txt",
]
