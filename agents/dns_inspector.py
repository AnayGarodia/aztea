"""
dns_inspector.py — Inspect DNS, SSL, and HTTP metadata for one or more domains

Input:  {
  "domains": ["example.com", "github.com"],   # required, max 10
  "checks": ["dns", "ssl", "http"],            # optional; also: mx, txt, dmarc
  "cert_expiry_warn_days": 30                  # optional, 1-365
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
        "response_ms": int,
        "security_headers": {header: value},   # CSP/XFO/XCTO/Referrer/Permissions/HSTS, present ones only
        "redirect_chain": [{"status": int, "to": str}]
      } | None,
      "mx": [{"host": str, "priority": int}],  # "mx" check; real RRset when dnspython present
      "mx_method": "dns" | "heuristic",
      "possible_mail_ips": [str],              # heuristic path only (mail.<domain> probe)
      "txt": [str], "spf": str | None,         # "txt" check (needs dnspython)
      "dmarc": {"present": bool, "policy": str | None, "record": str},  # "dmarc" check (needs dnspython); "record" only when present
      "issues": [str]
    }
  ],
  "billing_units_actual": int
}

DECISIONS:
  - dnspython is imported lazily and every record check degrades honestly
    when it's absent (mx falls back to the mail.<domain> heuristic with
    mx_method="heuristic"; txt/dmarc report null). The pre-2026-06 "stdlib
    only" rule lapsed when dnspython became a pinned dependency.
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
_CERT_WARN_DAYS_MIN = 1
_CERT_WARN_DAYS_MAX = 365
_INVALID_DOMAIN_CHARS = ("@", " ", "[")
_INVALID_DOMAIN_ISSUES_PREVIEW = 6
_DEFAULT_CHECKS = ("dns", "ssl", "http")
# txt/dmarc are opt-in so the default sync path stays inside its wall budget.
_SUPPORTED_CHECKS = frozenset({"dns", "ssl", "http", "mx", "txt", "dmarc"})
_HTTPS_PORT = 443
# Per-RR-query lifetime for dnspython lookups. Kept tight so a 10-domain
# batch with mx+txt+dmarc stays inside the agent's sync wall budget.
_DNS_QUERY_LIFETIME_S = 2.0
_MAX_TXT_RECORDS = 20
_VALUE_TRUNCATE_CHARS = 300
_MAX_REDIRECTS = 5
# Response headers a security audit cares about; reported when present.
_SECURITY_HEADERS = (
    "Strict-Transport-Security",
    "Content-Security-Policy",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Permissions-Policy",
)

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


class _RecordingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Records each hop of the redirect chain (mutates ``self.chain``).

    Why: "does http:// upgrade to https://?" is a core audit question that
    the final response alone cannot answer.
    """

    def __init__(self) -> None:
        self.chain: list[dict[str, Any]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self.chain.append({"status": int(code), "to": str(newurl)[:_VALUE_TRUNCATE_CHARS]})
        if len(self.chain) > _MAX_REDIRECTS:
            raise urllib.error.HTTPError(
                req.full_url, code, f"redirect chain exceeded {_MAX_REDIRECTS} hops",
                headers, fp,
            )
        # SSRF: the initial domain was validated, but a redirect can point at
        # an internal/metadata host (302 -> http://169.254.169.254/...). Without
        # this guard the agent would follow it AND echo the internal URL +
        # response headers back via redirect_chain / security_headers.
        try:
            validate_outbound_url(newurl, "redirect")
        except Exception as exc:
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"redirect target blocked by security policy: {exc}",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _present_security_headers(headers: Any) -> dict[str, str]:
    """Pure: audit-relevant response headers that are actually present."""
    if not headers:
        return {}
    return {
        name: str(headers.get(name))[:_VALUE_TRUNCATE_CHARS]
        for name in _SECURITY_HEADERS
        if headers.get(name)
    }


def _http_check(domain: str) -> tuple[dict | None, str | None]:
    """Return (http_info_dict, error_or_None)."""
    url = f"http://{domain}"
    start = time.time()
    redirect_handler = _RecordingRedirectHandler()
    opener = urllib.request.build_opener(redirect_handler)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "aztea-dns-inspector/1.0"},
    )
    try:
        with opener.open(req, timeout=_HTTP_TIMEOUT) as resp:
            elapsed_ms = int((time.time() - start) * 1000)
            headers = resp.headers
            status_code = resp.status
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.time() - start) * 1000)
        status_code = exc.code
        headers = exc.headers
    except Exception as exc:
        return None, str(exc)

    return {
        "status_code": status_code,
        "server_header": headers.get("Server", "") if headers else "",
        "hsts": bool(headers and "Strict-Transport-Security" in headers),
        "response_ms": elapsed_ms,
        "security_headers": _present_security_headers(headers),
        "redirect_chain": redirect_handler.chain,
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


def _dnspython_resolver():
    """Lazy: a configured dns.resolver.Resolver, or None when dnspython is absent.

    Lazy import (mirrors the Playwright pattern in browser_agent) so the
    agent stays importable on minimal installs; callers must degrade
    honestly when this returns None.
    """
    try:
        import dns.resolver
    except ImportError:
        return None
    resolver = dns.resolver.Resolver()
    resolver.timeout = _DNS_QUERY_LIFETIME_S
    resolver.lifetime = _DNS_QUERY_LIFETIME_S
    return resolver


# dnspython raises these for "the name resolved but has no such record" —
# a definitive absence. Everything else (timeout, SERVFAIL, no-nameservers)
# is a transport failure where the check could not actually run. Matched by
# class name so the module stays importable without dnspython installed.
_DNS_ABSENT_EXC_NAMES = frozenset({"NXDOMAIN", "NoAnswer"})


def _is_dns_absence(exc: Exception) -> bool:
    """Pure: True when ``exc`` means 'no such record' vs a transport failure."""
    return type(exc).__name__ in _DNS_ABSENT_EXC_NAMES


def _resolve_txt_strings(resolver: Any, name: str) -> list[str]:
    """Side-effect: TXT RRset for ``name`` as decoded strings; raises on lookup failure."""
    answers = resolver.resolve(name, "TXT")
    return [
        b"".join(r.strings).decode("utf-8", "replace")[:_VALUE_TRUNCATE_CHARS]
        for r in answers
    ][:_MAX_TXT_RECORDS]


def _check_mx_heuristic_into(entry: dict[str, Any], domain: str) -> None:
    """Side-effect (mutating ``entry``): legacy ``mail.<domain>`` probe.

    Only used when dnspython is unavailable — it misses every hosted-mail
    setup (Google Workspace resolves MX to aspmx.l.google.com, not
    mail.<domain>), which is why mx_method flags the answer as heuristic.
    """
    entry["mx_method"] = "heuristic"
    entry["mx"] = []
    try:
        entry["possible_mail_ips"] = list(
            {info[4][0] for info in _cached_getaddrinfo("mail." + domain)}
        )
    except Exception:
        _LOG.debug("mail.%s lookup failed", domain, exc_info=True)
        entry["possible_mail_ips"] = []


def _check_mx_into(entry: dict[str, Any], domain: str, resolver: Any) -> None:
    """Side-effect (mutating ``entry``): real MX RRset lookup via dnspython."""
    if resolver is None:
        _check_mx_heuristic_into(entry, domain)
        return
    entry["mx_method"] = "dns"
    try:
        answers = resolver.resolve(domain, "MX")
        entry["mx"] = sorted(
            (
                {"host": str(r.exchange).rstrip("."), "priority": int(r.preference)}
                for r in answers
            ),
            key=lambda m: m["priority"],
        )
    except Exception as exc:
        _LOG.debug("MX lookup failed for %s", domain, exc_info=True)
        entry["mx"] = []
        # Only assert absence on a definitive no-record answer; a transport
        # failure means we don't know, so don't claim "No MX records found".
        if _is_dns_absence(exc):
            entry["issues"].append("No MX records found")


def _check_txt_into(entry: dict[str, Any], domain: str, resolver: Any) -> None:
    """Side-effect (mutating ``entry``): TXT RRset + SPF extraction."""
    if resolver is None:
        # None (vs []) = "check could not run"; callers must not read this
        # as "domain has no TXT records".
        entry["txt"] = None
        entry["spf"] = None
        return
    try:
        txt = _resolve_txt_strings(resolver, domain)
    except Exception as exc:
        _LOG.debug("TXT lookup failed for %s", domain, exc_info=True)
        # Empty list only for a definitive no-record answer; None for a
        # transport failure (the check could not run).
        entry["txt"] = [] if _is_dns_absence(exc) else None
        entry["spf"] = None
        if _is_dns_absence(exc):
            entry["issues"].append("No SPF record found")
        return
    entry["txt"] = txt
    spf = next((t for t in txt if t.lower().startswith("v=spf1")), None)
    entry["spf"] = spf
    if spf is None:
        entry["issues"].append("No SPF record found")


_DMARC_POLICY_RE = re.compile(r"\bp\s*=\s*(none|quarantine|reject)\b", re.IGNORECASE)


def _check_dmarc_into(entry: dict[str, Any], domain: str, resolver: Any) -> None:
    """Side-effect (mutating ``entry``): DMARC record presence + policy."""
    if resolver is None:
        entry["dmarc"] = None
        return
    try:
        records = _resolve_txt_strings(resolver, f"_dmarc.{domain}")
    except Exception as exc:
        _LOG.debug("DMARC lookup failed for %s", domain, exc_info=True)
        if not _is_dns_absence(exc):
            # Transport failure: check could not run — don't claim absence.
            entry["dmarc"] = None
            return
        records = []
    record = next((t for t in records if t.lower().startswith("v=dmarc1")), None)
    if record is None:
        entry["dmarc"] = {"present": False, "policy": None}
        entry["issues"].append("No DMARC record found")
        return
    m = _DMARC_POLICY_RE.search(record)
    entry["dmarc"] = {
        "present": True,
        "policy": m.group(1).lower() if m else None,
        "record": record,
    }


def _check_ssl_into(entry: dict[str, Any], domain: str, warn_days: int) -> None:
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
    if days is not None and days < warn_days:
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


def _inspect_one(
    domain: str, checks: set[str], warn_days: int
) -> tuple[dict[str, Any], bool]:
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
    # Resolve the dnspython resolver once per domain rather than once per
    # record check — Resolver(configure=True) re-reads/parses resolv.conf on
    # every construction. None when dnspython is absent (checks degrade).
    resolver = _dnspython_resolver() if checks & {"mx", "txt", "dmarc"} else None
    if "mx" in checks:
        _check_mx_into(entry, domain, resolver)
    if "txt" in checks:
        _check_txt_into(entry, domain, resolver)
    if "dmarc" in checks:
        _check_dmarc_into(entry, domain, resolver)
    if "ssl" in checks:
        _check_ssl_into(entry, domain, warn_days)
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


def _normalize_run_inputs(payload: dict) -> dict | tuple[list[str], set[str], int]:
    """Pure: validate + normalize ``run`` inputs; returns ``(domains, checks, warn_days)`` or an error envelope."""
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
    checks = {str(c).strip().lower() for c in raw_checks}
    unknown = checks - _SUPPORTED_CHECKS
    if unknown:
        return _err(
            "dns_inspector.unknown_check",
            f"Unknown checks: {sorted(unknown)}. "
            f"Supported: {sorted(_SUPPORTED_CHECKS)}",
        )
    try:
        warn_days = int(payload.get("cert_expiry_warn_days", _CERT_NEAR_EXPIRY_DAYS))
    except (TypeError, ValueError):
        warn_days = -1
    if not (_CERT_WARN_DAYS_MIN <= warn_days <= _CERT_WARN_DAYS_MAX):
        return _err(
            "dns_inspector.invalid_expiry_threshold",
            "cert_expiry_warn_days must be an integer between "
            f"{_CERT_WARN_DAYS_MIN} and {_CERT_WARN_DAYS_MAX}",
        )
    return domains, checks, warn_days


def _aggregate_results(
    domains: list[str], checks: set[str], warn_days: int
) -> tuple[list[dict[str, Any]], int]:
    """Side-effect: parallel-inspect every domain; returns ``(results, ok_count)``."""
    workers = min(_INSPECT_WORKERS, max(1, len(domains)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(lambda d: _inspect_one(d, checks, warn_days), domains))
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

    Why: surfaces trustable trust-store + cert-expiry + mail-auth signals
    from live lookups. Record checks (mx/txt/dmarc) use dnspython when
    available and degrade honestly when it isn't (see module DECISIONS).
    """
    parsed = _normalize_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed
    # Reset the per-call DNS cache so independent run() invocations don't
    # share stale resolutions across batches.
    _reset_dns_cache()
    domains, checks, warn_days = parsed
    try:
        results, ok_count = _aggregate_results(domains, checks, warn_days)
    finally:
        _reset_dns_cache()
    if ok_count == 0 and results:
        return _all_failed_envelope(results)
    return {"results": results, "billing_units_actual": ok_count}
