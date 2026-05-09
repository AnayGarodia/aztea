"""
email_deliverability_checker.py — Live DNS-based email deliverability audit (MX/SPF/DKIM/DMARC)

Input:  {"domain": str, "selector": str}   # selector optional, default "default"
Output: {domain, mx_records, mx_found, spf{...}, dkim{...}, dmarc{...},
         score, verdict, recommendations, checked_at}
"""

# OWNS: live DNS-based email deliverability auditing (MX, SPF, DKIM, DMARC) for a
#       single domain; composite scoring and human-readable recommendations.
# NOT OWNS: SMTP connectivity testing, IP reputation checks, blacklist lookups,
#           SSL/TLS certificate inspection (see dns_inspector.py).
# INVARIANTS:
#   * Never raise — always return a structured error dict via agent_error.
#   * DNS timeout is fixed at _DNS_TIMEOUT seconds per lookup; never block indefinitely.
#   * Each per-protocol lookup failure is appended to that section's issues list; the
#     overall call succeeds with partial data rather than aborting.
# DECISIONS:
#   * dnspython is imported lazily so the module can be imported without the package
#     installed; the error surfaces at call time with a clear install hint.
#   * If the caller passes an email address (contains "@"), the domain part is extracted
#     silently — better UX than a hard error for a common mistake.
#   * DMARC pct defaults to 100 when the tag is absent (RFC 7489 §6.3).

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_DNS_TIMEOUT = 5  # seconds per individual DNS lookup
_SCORE_MAX = 100

# Score contribution constants
_SCORE_MX_FOUND = 20
_SCORE_SPF_FOUND_VALID = 20
_SCORE_SPF_FOUND_PERMISSIVE = 10
_SCORE_DKIM_FOUND = 25
_SCORE_DMARC_FOUND = 15
_SCORE_DMARC_QUARANTINE = 10
_SCORE_DMARC_REJECT = 20
_SCORE_DMARC_NONE = 5
_SCORE_DMARC_PCT_FULL = 5  # bonus when pct == 100 and policy != "none"

_DMARC_PCT_DEFAULT = 100

# Private/loopback domain patterns that must be rejected
_PRIVATE_DOMAIN_PATTERNS = ("localhost", ".local", ".internal")


def _import_resolver():
    """Return dns.resolver or raise ImportError with an install hint."""
    try:
        import dns.resolver  # type: ignore[import-untyped]
        return dns.resolver
    except ImportError as exc:
        raise ImportError("dnspython not installed: pip install dnspython") from exc


def _resolve(resolver, name: str, rdtype: str) -> list[str]:
    """Resolve *name* for *rdtype*; return rdata strings (empty list on any miss)."""
    import dns.exception  # type: ignore[import-untyped]
    try:
        answers = resolver.resolve(name, rdtype, lifetime=_DNS_TIMEOUT)
        return [rdata.to_text().strip('"') for rdata in answers]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer,
            dns.resolver.NoNameservers, dns.exception.Timeout):
        return []
    except Exception as exc:
        _LOG.warning("DNS resolve %s %s failed: %s", rdtype, name, exc)
        return []


def _validate_domain(raw: str) -> tuple[str, dict | None]:
    """Return (cleaned_domain, error_or_None). Extracts domain part from email addresses."""
    value = (raw or "").strip()
    if not value:
        return "", _err(
            "email_deliverability_checker.missing_domain",
            "domain is required",
        )

    # Quality-of-life: accept email addresses by stripping the local part.
    if "@" in value:
        value = value.split("@", 1)[1].strip()

    # Basic structure check: must contain at least one dot and only valid chars.
    if not value or "." not in value or not re.match(r"^[a-zA-Z0-9.\-]+$", value):
        return "", _err(
            "email_deliverability_checker.invalid_domain",
            f"'{value}' does not look like a valid domain name",
        )

    lower = value.lower()
    if lower == "localhost" or any(lower.endswith(p) for p in _PRIVATE_DOMAIN_PATTERNS):
        return "", _err(
            "email_deliverability_checker.private_domain",
            f"'{value}' is a private or loopback domain and cannot be checked",
        )

    return lower, None


def _check_mx(resolver, domain: str) -> dict:
    """Return MX section dict."""
    mx_records: list[str] = []
    issues: list[str] = []

    import dns.exception  # type: ignore[import-untyped]
    try:
        for rdata in resolver.resolve(domain, "MX", lifetime=_DNS_TIMEOUT):
            mx_records.append(f"{rdata.preference} {rdata.exchange.to_text().rstrip('.')}")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer,
            dns.resolver.NoNameservers, dns.exception.Timeout):
        issues.append("No MX records found")
    except Exception as exc:
        _LOG.warning("MX lookup for %s failed: %s", domain, exc)
        issues.append(f"MX lookup error: {exc}")

    return {"mx_records": mx_records, "mx_found": len(mx_records) > 0, "issues": issues}


_SPF_POLICY_MAP = {"+all": "pass", "~all": "softfail", "-all": "fail", "?all": "neutral"}


def _parse_spf_policy(mechanism: str | None) -> str | None:
    return _SPF_POLICY_MAP.get(mechanism) if mechanism else None


def _check_spf(resolver, domain: str) -> dict:
    txt_records = _resolve(resolver, domain, "TXT")
    spf_records = [r for r in txt_records if r.startswith("v=spf1")]
    issues: list[str] = []

    if not spf_records:
        return {"record": None, "found": False, "valid": False, "mechanism": None,
                "policy": None, "includes": [], "issues": ["No SPF record found"]}

    if len(spf_records) > 1:
        issues.append("Multiple SPF records found — only one is allowed")

    record = spf_records[0]
    tokens = record.split()

    # Find the last `all` qualifier (rightmost wins per RFC 7208 §5.1).
    mechanism: str | None = None
    for token in reversed(tokens):
        if re.match(r"^[+~\-?]all$", token, re.IGNORECASE):
            mechanism = token.lower()
            break
        if token.lower() == "all":
            mechanism = "+all"  # implicit "+" qualifier
            break

    policy = _parse_spf_policy(mechanism)
    includes = [t[len("include:"):] for t in tokens if t.lower().startswith("include:")]

    if policy == "pass":
        issues.append("SPF record allows all senders — too permissive")

    return {"record": record, "found": True, "valid": True, "mechanism": mechanism,
            "policy": policy, "includes": includes, "issues": issues}


def _check_dkim(resolver, domain: str, selector: str) -> dict:
    dkim_name = f"{selector}._domainkey.{domain}"
    txt_records = _resolve(resolver, dkim_name, "TXT")
    issues: list[str] = []

    if not txt_records:
        return {"selector": selector, "record": None, "found": False,
                "has_public_key": False, "key_type": "rsa",
                "issues": [f"No DKIM record found for selector '{selector}'"]}

    # Concatenate multi-string TXT records (split across 255-byte chunks).
    record = " ".join(txt_records)

    # Extract key type.
    key_type_match = re.search(r"\bk=([^;\s]+)", record)
    key_type = key_type_match.group(1) if key_type_match else "rsa"

    # Extract public key value (p= tag).
    p_match = re.search(r"\bp=([^;\s]*)", record)
    p_value = p_match.group(1) if p_match else ""
    has_public_key = bool(p_value)

    if not has_public_key:
        issues.append("DKIM public key is empty (key revoked)")

    return {"selector": selector, "record": record, "found": True,
            "has_public_key": has_public_key, "key_type": key_type, "issues": issues}


def _parse_dmarc_tag(record: str, tag: str) -> str | None:
    match = re.search(rf"\b{re.escape(tag)}=([^;\s]+)", record)
    return match.group(1) if match else None


def _check_dmarc(resolver, domain: str) -> dict:
    txt_records = _resolve(resolver, f"_dmarc.{domain}", "TXT")
    dmarc_records = [r for r in txt_records if r.startswith("v=DMARC1")]
    issues: list[str] = []

    if not dmarc_records:
        return {"record": None, "found": False, "policy": None, "subdomain_policy": None,
                "pct": _DMARC_PCT_DEFAULT, "rua": None, "issues": ["No DMARC record found"]}

    record = dmarc_records[0]
    policy = _parse_dmarc_tag(record, "p")
    sp_raw = _parse_dmarc_tag(record, "sp")
    subdomain_policy = sp_raw if sp_raw else policy

    pct_raw = _parse_dmarc_tag(record, "pct")
    try:
        pct = int(pct_raw) if pct_raw is not None else _DMARC_PCT_DEFAULT
    except ValueError:
        pct = _DMARC_PCT_DEFAULT

    rua = _parse_dmarc_tag(record, "rua")

    if policy == "none":
        issues.append("DMARC policy is 'none' — emails are not rejected or quarantined")
    if pct < _DMARC_PCT_DEFAULT:
        issues.append(f"DMARC only applies to {pct}% of emails")
    if not rua:
        issues.append("No DMARC aggregate reporting address configured")

    return {"record": record, "found": True, "policy": policy,
            "subdomain_policy": subdomain_policy, "pct": pct, "rua": rua, "issues": issues}


def _compute_score(mx: dict, spf: dict, dkim: dict, dmarc: dict) -> int:
    score = 0
    if mx["mx_found"]:
        score += _SCORE_MX_FOUND
    if spf["found"]:
        score += _SCORE_SPF_FOUND_PERMISSIVE if spf["policy"] == "pass" else _SCORE_SPF_FOUND_VALID
    if dkim["found"] and dkim["has_public_key"]:
        score += _SCORE_DKIM_FOUND
    if dmarc["found"]:
        score += _SCORE_DMARC_FOUND
        dp = dmarc.get("policy")
        if dp == "reject":
            score += _SCORE_DMARC_REJECT
        elif dp == "quarantine":
            score += _SCORE_DMARC_QUARANTINE
        elif dp == "none":
            score += _SCORE_DMARC_NONE
        if dp != "none" and dmarc["pct"] == _DMARC_PCT_DEFAULT:
            score += _SCORE_DMARC_PCT_FULL
    return min(score, _SCORE_MAX)


def _verdict(score: int) -> str:
    if score >= 80:
        return "pass"
    return "warn" if score >= 50 else "fail"


def _build_recommendations(domain: str, mx: dict, spf: dict, dkim: dict, dmarc: dict) -> list[str]:
    recs: list[str] = []
    if not mx["mx_found"]:
        recs.append("Add MX records to receive email")
    if not spf["found"]:
        recs.append("Add an SPF record: `v=spf1 include:your-mail-provider.com ~all`")
    elif spf["policy"] == "pass":
        recs.append("Change SPF `+all` to `~all` or `-all`")
    if not dkim["found"]:
        recs.append("Configure DKIM signing with your mail provider and publish the public key")
    if not dmarc["found"]:
        recs.append(f"Add a DMARC record: `v=DMARC1; p=quarantine; rua=mailto:dmarc@{domain}`")
    elif dmarc.get("policy") == "none":
        recs.append("Strengthen DMARC policy from `none` to `quarantine` or `reject`")
    return recs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(payload: dict) -> dict:
    """Check email deliverability DNS records for a domain.

    Required: ``domain`` (str) — bare domain name or email address.
    Optional: ``selector`` (str, default "default") — DKIM selector to probe.

    Returns a structured report with MX, SPF, DKIM, DMARC findings, a
    0-100 composite score, a pass/warn/fail verdict, and recommendations.
    """
    try:
        resolver_mod = _import_resolver()
    except ImportError as exc:
        return _err(
            "email_deliverability_checker.missing_dependency",
            str(exc),
        )

    raw_domain = payload.get("domain", "")
    if not isinstance(raw_domain, str):
        raw_domain = str(raw_domain)

    domain, validation_error = _validate_domain(raw_domain)
    if validation_error:
        return validation_error

    selector = str(payload.get("selector", "default")).strip() or "default"

    resolver = resolver_mod.Resolver()
    resolver.lifetime = _DNS_TIMEOUT

    mx = _check_mx(resolver, domain)
    spf = _check_spf(resolver, domain)
    dkim = _check_dkim(resolver, domain, selector)
    dmarc = _check_dmarc(resolver, domain)

    score = _compute_score(mx, spf, dkim, dmarc)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "domain": domain,
        "mx_records": mx["mx_records"],
        "mx_found": mx["mx_found"],
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        "score": score,
        "verdict": _verdict(score),
        "recommendations": _build_recommendations(domain, mx, spf, dkim, dmarc),
        "checked_at": checked_at,
    }
