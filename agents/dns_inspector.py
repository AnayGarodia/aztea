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

import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core.url_security import validate_outbound_url

_MAX_DOMAINS = 10
_SSL_TIMEOUT = 5
_HTTP_TIMEOUT = 5
_CERT_DATE_FMT = "%b %d %H:%M:%S %Y %Z"


def _dns_check(domain: str) -> tuple[list[str], list[str], str | None]:
    """Return (a_records, aaaa_records, error_or_None)."""
    a_records: list[str] = []
    aaaa_records: list[str] = []
    try:
        infos = socket.getaddrinfo(domain, None, socket.AF_INET)
        a_records = list({info[4][0] for info in infos})
    except Exception as exc:
        return [], [], f"DNS lookup failed: {exc}"

    try:
        infos6 = socket.getaddrinfo(domain, None, socket.AF_INET6)
        aaaa_records = list({info[4][0] for info in infos6})
    except Exception:
        pass  # IPv6 is optional

    return a_records, aaaa_records, None


def _ssl_check(domain: str) -> tuple[dict | None, str | None]:
    """Return (cert_info_dict, error_or_None)."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((domain, 443), timeout=_SSL_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
    except Exception as exc:
        return None, str(exc)

    def _flatten_rdns(rdns_seq) -> dict:
        out = {}
        for rdn in rdns_seq:
            for name, val in rdn:
                out[name] = val
        return out

    subject = _flatten_rdns(cert.get("subject", ()))
    issuer = _flatten_rdns(cert.get("issuer", ()))
    not_after_str = cert.get("notAfter", "")

    days_until_expiry = None
    try:
        expires_dt = datetime.strptime(not_after_str, _CERT_DATE_FMT).replace(
            tzinfo=timezone.utc
        )
        now = datetime.now(timezone.utc)
        days_until_expiry = (expires_dt - now).days
    except Exception:
        expires_dt = None

    san_names: list[str] = []
    for entry in cert.get("subjectAltName", ()):
        kind, val = entry
        if kind == "DNS":
            san_names.append(val)

    return {
        "issuer": issuer,
        "subject": subject,
        "expires_at": not_after_str,
        "days_until_expiry": days_until_expiry,
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
        hsts = (
            "Strict-Transport-Security" in exc.headers if exc.headers else False
        )
    except Exception as exc:
        return None, str(exc)

    return {
        "status_code": status_code,
        "server_header": server_header,
        "hsts": hsts,
        "response_ms": elapsed_ms,
    }, None


def run(payload: dict) -> dict:
    raw_domains = payload.get("domains")
    if not raw_domains or not isinstance(raw_domains, list):
        raise ValueError("domains is required and must be a non-empty list of domain names")
    if len(raw_domains) > _MAX_DOMAINS:
        raise ValueError(f"domains may contain at most {_MAX_DOMAINS} entries; got {len(raw_domains)}")

    domains = [str(d).strip().lower() for d in raw_domains if str(d).strip()]
    if not domains:
        raise ValueError("domains list contains no valid entries")

    raw_checks = payload.get("checks", ["dns", "ssl", "http"])
    if not isinstance(raw_checks, list):
        raw_checks = ["dns", "ssl", "http"]
    checks = {str(c).strip().lower() for c in raw_checks}

    results = []
    successfully_inspected = 0

    for domain in domains:
        entry: dict = {
            "domain": domain,
            "a_records": [],
            "ssl_cert": None,
            "http": None,
            "issues": [],
        }
        domain_ok = True

        # --- Format guard ---
        if any(c in domain for c in ("@", " ", "[")):
            results.append({"domain": domain, "a_records": [], "ssl_cert": None, "http": None, "issues": ["Invalid domain format"]})
            continue

        # --- SSRF guard ---
        try:
            validate_outbound_url(f"https://{domain}", "domain")
        except Exception as exc:
            results.append({
                "domain": domain,
                "a_records": [],
                "ssl_cert": None,
                "http": None,
                "issues": [f"Domain blocked by security policy: {exc}"],
            })
            continue

        # --- DNS ---
        if "dns" in checks:
            a_records, _aaaa, dns_error = _dns_check(domain)
            entry["a_records"] = a_records
            if dns_error:
                entry["issues"].append(f"DNS error: {dns_error}")
                domain_ok = False
            elif not a_records:
                entry["issues"].append("No DNS A records found")

        # --- MX (stdlib limitation note) ---
        # Full MX record lookup requires dnspython; socket only resolves hostnames.
        # We attempt mail.<domain> as a rough proxy for MX reachability.
        if "mx" in checks:
            try:
                mail_ips = list({
                    info[4][0]
                    for info in socket.getaddrinfo("mail." + domain, None)
                })
                entry["possible_mail_ips"] = mail_ips
            except Exception:
                entry["possible_mail_ips"] = []

        # --- SSL ---
        if "ssl" in checks:
            cert_info, ssl_error = _ssl_check(domain)
            if ssl_error:
                entry["ssl_cert"] = None
                entry["issues"].append(f"SSL connection failed: {ssl_error}")
            else:
                entry["ssl_cert"] = cert_info
                if cert_info and cert_info.get("days_until_expiry") is not None:
                    days = cert_info["days_until_expiry"]
                    if days < 30:
                        entry["issues"].append(
                            f"SSL certificate expires in {days} days"
                        )

        # --- HTTP ---
        if "http" in checks:
            http_info, http_error = _http_check(domain)
            if http_error:
                entry["http"] = None
                entry["issues"].append(f"HTTP check failed: {http_error}")
            else:
                entry["http"] = http_info
                if http_info and not http_info.get("hsts"):
                    entry["issues"].append("Missing HSTS header")

        if domain_ok:
            successfully_inspected += 1

        results.append(entry)

    return {
        "results": results,
        "billing_units_actual": successfully_inspected,
    }
