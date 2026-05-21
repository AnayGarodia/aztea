"""
dns_inspector.py — Inspect DNS, SSL, and HTTP metadata for one or more domains

Input:  {
  "domains": ["example.com", "github.com"],   # required, max 10
  "checks": ["dns", "ssl", "http"]             # optional, default all three
}
Output: {
  "results": [
    {
      "domain": str,
      "a_records": [str],
      "ssl_cert": {
        "issuer": dict,
        "subject": dict,
        "expires_at": str,
        "days_until_expiry": int,
        "san_names": [str]
      } | None,
      "http": {
        "status_code": int,
        "server_header": str,
        "hsts": bool,
        "response_ms": int
      } | None,
      "possible_mail_ips": [str] | None,   # present only when "mx" included in checks
      "issues": [str]
    }
  ],
  "billing_units_actual": int
}

Imports: stdlib only — socket, ssl, urllib, time, datetime. No httpx or third-party DNS.
"""

import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from core.url_security import validate_outbound_url
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_MAX_DOMAINS = 10
_SSL_TIMEOUT = 5
_HTTP_TIMEOUT = 5
_INSPECT_WORKERS = 10
_CERT_DATE_FMT = "%b %d %H:%M:%S %Y %Z"
_CERT_NEAR_EXPIRY_DAYS = 30
_INVALID_DOMAIN_CHARS = ("@", " ", "[")
_INVALID_DOMAIN_ISSUES_PREVIEW = 6
_DEFAULT_CHECKS = ("dns", "ssl", "http")
_HTTPS_PORT = 443

# Per-call DNS cache. A single inspection for one domain previously hit
# the resolver 3-4 times (A records, AAAA records, then SSL connect's
# implicit resolve, then HTTP fetch's implicit resolve). For batch calls
# the duplication compounds. We memoise ``getaddrinfo`` keyed on
# ``(host, family)`` for the lifetime of one ``run(...)``. The cache is
# stored on the call's stack frame via a thread-local so concurrent
# inspections in different threads don't clobber each other.
import threading
_dns_cache_tls = threading.local()


def _cached_getaddrinfo(host: str, family: int | None = None) -> list[tuple]:
    """``socket.getaddrinfo`` with a thread-local per-call cache.

    Caller MUST call ``_reset_dns_cache()`` at the start of each ``run(...)``
    so DNS state doesn't leak across independent invocations.
    """
    cache: dict[tuple[str, int | None], list[tuple]] | None = getattr(
        _dns_cache_tls, "cache", None,
    )
    if cache is None:
        cache = {}
        _dns_cache_tls.cache = cache
    key = (host, family)
    if key in cache:
        return cache[key]
    if family is None:
        result = socket.getaddrinfo(host, None)
    else:
        result = socket.getaddrinfo(host, None, family)
    cache[key] = result
    return result


def _reset_dns_cache() -> None:
    """Drop the per-call DNS cache. Idempotent."""
    _dns_cache_tls.cache = {}


def _dns_check(domain: str) -> tuple[list[str], list[str], str | None]:
    """Return (a_records, aaaa_records, error_or_None)."""
    a_records: list[str] = []
    aaaa_records: list[str] = []
    try:
        infos = _cached_getaddrinfo(domain, socket.AF_INET)
        a_records = list({info[4][0] for info in infos})
    except Exception as exc:
        return [], [], f"DNS lookup failed: {exc}"

    try:
        infos6 = _cached_getaddrinfo(domain, socket.AF_INET6)
        aaaa_records = list({info[4][0] for info in infos6})
    except Exception:
        # WHY: IPv6 is optional — absent AAAA is normal, not an error.
        _LOG.debug("AAAA lookup absent for %s", domain, exc_info=True)

    return a_records, aaaa_records, None


def _flatten_rdns(rdns_seq: tuple) -> dict[str, str]:
    """Pure: flatten an X.509 RDN sequence ``((('CN','x'),),)`` into a single dict."""
    out: dict[str, str] = {}
    for rdn in rdns_seq:
        for name, val in rdn:
            out[name] = val
    return out


def _days_until(not_after_str: str, domain: str) -> int | None:
    """Pure-ish: parse ``notAfter`` and return whole days remaining; ``None`` on parse failure."""
    try:
        expires_dt = datetime.strptime(not_after_str, _CERT_DATE_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        _LOG.info("Could not parse cert notAfter %r for %s", not_after_str, domain)
        return None
    return (expires_dt - datetime.now(timezone.utc)).days


def _ssl_check(domain: str) -> tuple[dict | None, str | None]:
    """Side-effect: fetch and shape SSL certificate metadata for ``domain``."""
    try:
        with socket.create_connection((domain, _HTTPS_PORT), timeout=_SSL_TIMEOUT) as sock:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
    except Exception as exc:
        return None, str(exc)
    not_after_str = cert.get("notAfter", "")
    san_names = [val for kind, val in cert.get("subjectAltName", ()) if kind == "DNS"]
    return {
        "issuer": _flatten_rdns(cert.get("issuer", ())),
        "subject": _flatten_rdns(cert.get("subject", ())),
        "expires_at": not_after_str,
        "days_until_expiry": _days_until(not_after_str, domain),
        "san_names": san_names,
    }, None


def _http_check(domain: str) -> tuple[dict | None, str | None]:
    """Return (http_info_dict, error_or_None)."""
    url = f"http://{domain}"
    start = time.time()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "aztea-dns-inspector/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            elapsed_ms = int((time.time() - start) * 1000)
            headers = resp.headers
            status_code = resp.status
            server_header = headers.get("Server", "")
            hsts = "Strict-Transport-Security" in headers
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.time() - start) * 1000)
        status_code = exc.code
        server_header = exc.headers.get("Server", "") if exc.headers else ""
        hsts = "Strict-Transport-Security" in exc.headers if exc.headers else False
    except Exception as exc:
        return None, str(exc)

    return {
        "status_code": status_code,
        "server_header": server_header,
        "hsts": hsts,
        "response_ms": elapsed_ms,
    }, None



def _empty_inspection_entry(domain: str) -> dict[str, Any]:
    """Pure: skeleton entry for one domain; per-check helpers fill it in."""
    return {
        "domain": domain,
        "a_records": [],
        "ssl_cert": None,
        "http": None,
        "issues": [],
    }


def _check_dns_into(entry: dict[str, Any], domain: str) -> bool:
    """Side-effect (mutating ``entry``): run DNS A lookup; returns ``True`` if domain is healthy."""
    a_records, _aaaa, dns_error = _dns_check(domain)
    entry["a_records"] = a_records
    if dns_error:
        entry["issues"].append(f"DNS error: {dns_error}")
        return False
    if not a_records:
        entry["issues"].append("No DNS A records found")
    return True


def _check_mx_into(entry: dict[str, Any], domain: str) -> None:
    """Side-effect (mutating ``entry``): heuristic MX probe via ``mail.<domain>``.

    Why: stdlib lacks DNS MX lookup; mail.<domain> is a rough proxy when
    ``dnspython`` isn't installed. A missing record is the common case.
    """
    try:
        mail_ips = list(
            {info[4][0] for info in _cached_getaddrinfo("mail." + domain)}
        )
        entry["possible_mail_ips"] = mail_ips
    except Exception:
        _LOG.debug("mail.%s lookup failed", domain, exc_info=True)
        entry["possible_mail_ips"] = []


def _check_ssl_into(entry: dict[str, Any], domain: str) -> None:
    """Side-effect (mutating ``entry``): SSL handshake + cert decode + near-expiry warning."""
    cert_info, ssl_error = _ssl_check(domain)
    if ssl_error:
        entry["ssl_cert"] = None
        entry["issues"].append(f"SSL connection failed: {ssl_error}")
        return
    entry["ssl_cert"] = cert_info
    if not cert_info:
        return
    days = cert_info.get("days_until_expiry")
    if days is not None and days < _CERT_NEAR_EXPIRY_DAYS:
        entry["issues"].append(f"SSL certificate expires in {days} days")


def _check_http_into(entry: dict[str, Any], domain: str) -> None:
    """Side-effect (mutating ``entry``): HTTP probe + HSTS header check."""
    http_info, http_error = _http_check(domain)
    if http_error:
        entry["http"] = None
        entry["issues"].append(f"HTTP check failed: {http_error}")
        return
    entry["http"] = http_info
    if http_info and not http_info.get("hsts"):
        entry["issues"].append("Missing HSTS header")


def _inspect_one(domain: str, checks: set[str]) -> tuple[dict[str, Any], bool]:
    """Side-effect: run every requested check for ``domain``; returns ``(entry, ok_flag)``."""
    entry = _empty_inspection_entry(domain)
    if any(c in domain for c in _INVALID_DOMAIN_CHARS):
        entry["issues"].append("Invalid domain format")
        return entry, False
    try:
        validate_outbound_url(f"https://{domain}", "domain")
    except Exception as exc:
        entry["issues"].append(f"Domain blocked by security policy: {exc}")
        return entry, False
    domain_ok = True
    if "dns" in checks:
        domain_ok = _check_dns_into(entry, domain)
    if "mx" in checks:
        _check_mx_into(entry, domain)
    if "ssl" in checks:
        _check_ssl_into(entry, domain)
    if "http" in checks:
        _check_http_into(entry, domain)
    return entry, domain_ok


# A bare hostname (no scheme, no path, no IP). Permits ASCII letters, digits,
# dots, hyphens (per RFC 1035-ish). Rejects URLs, IP literals, and any
# value containing characters that mark non-domain inputs (':', '/', '@').
# IPv4-shaped strings are also rejected — DNS lookups on raw IPs are
# meaningless and would otherwise let a caller probe internal hosts.
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+(?<!-)$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _is_valid_public_domain(value: str) -> bool:
    """Pure: True when ``value`` is a syntactically valid bare hostname.

    Rejects URL-shaped inputs ("http://10.0.0.1"), IP literals ("10.0.0.1"),
    and any value containing scheme/path/userinfo separators. SSRF-adjacent:
    DNS resolution itself is harmless, but the SSL + HTTP checks downstream
    will reach the resolved address — letting an internal IP through here
    means an attacker can probe the metadata service or internal services
    via the curated public agent surface.
    """
    if not value or any(ch in value for ch in (":", "/", "@", "?", "#", " ")):
        return False
    if _IPV4_RE.match(value):
        return False
    return bool(_HOSTNAME_RE.match(value))


def _normalize_run_inputs(payload: dict) -> dict | tuple[list[str], set[str]]:
    """Pure: validate + normalize ``run`` inputs; returns ``(domains, checks)`` or an error envelope."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw_domains = payload.get("domains")
    if not raw_domains or not isinstance(raw_domains, list):
        return _err(
            "dns_inspector.missing_domains",
            "domains is required and must be a non-empty list of domain names",
        )
    if len(raw_domains) > _MAX_DOMAINS:
        return _err(
            "dns_inspector.too_many_domains",
            f"domains may contain at most {_MAX_DOMAINS} entries; got {len(raw_domains)}",
        )
    candidates = [str(d).strip().lower() for d in raw_domains if str(d).strip()]
    if not candidates:
        return _err(
            "dns_inspector.invalid_domains", "domains list contains no valid entries",
        )
    rejected = [d for d in candidates if not _is_valid_public_domain(d)]
    if rejected:
        return _err(
            "dns_inspector.invalid_domain_format",
            "domains entries must be bare hostnames (no schemes, paths, IPs). "
            f"Rejected: {rejected[:3]}",
        )
    domains = candidates
    raw_checks = payload.get("checks", list(_DEFAULT_CHECKS))
    if not isinstance(raw_checks, list):
        raw_checks = list(_DEFAULT_CHECKS)
    return domains, {str(c).strip().lower() for c in raw_checks}


def _aggregate_results(
    domains: list[str], checks: set[str]
) -> tuple[list[dict[str, Any]], int]:
    """Side-effect: parallel-inspect every domain; returns ``(results, ok_count)``."""
    workers = min(_INSPECT_WORKERS, max(1, len(domains)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(lambda d: _inspect_one(d, checks), domains))
    results = [entry for entry, _ in outcomes]
    ok_count = sum(1 for _, ok in outcomes if ok)
    return results, ok_count


def _all_failed_envelope(results: list[dict[str, Any]]) -> dict:
    """Pure: roll up all-failed results into one error envelope so the platform refunds."""
    issues: list[str] = []
    for entry in results:
        for issue in entry.get("issues") or []:
            issues.append(str(issue))
    joined = "; ".join(issues[:_INVALID_DOMAIN_ISSUES_PREVIEW]) or "no domain returned a valid answer"
    return _err("dns_ssl.no_results", f"All domains failed inspection: {joined}")


def run(payload: dict) -> dict:
    """Perform live DNS record lookups and SSL/HTTP inspection for one or more domains.

    Why: the agent must surface trustable trust-store + cert-expiry signals
    without pulling in dnspython; stdlib socket + ssl is sufficient when
    callers pass plain hostnames.
    """
    parsed = _normalize_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed
    # Reset the per-call DNS cache so independent run() invocations don't
    # share stale resolutions across batches.
    _reset_dns_cache()
    domains, checks = parsed
    try:
        results, ok_count = _aggregate_results(domains, checks)
    finally:
        _reset_dns_cache()
    if ok_count == 0 and results:
        return _all_failed_envelope(results)
    return {"results": results, "billing_units_actual": ok_count}
