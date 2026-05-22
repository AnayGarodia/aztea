"""Section B — endpoint URL / SSRF adversarial tests.

Covers ``core.url_security`` and ``core.listing_safety.scan_agent_md_endpoint``.
Tests run at the unit level so they don't need the FastAPI client; the
integration path is covered separately by ``test_publish_robustness_lifecycle.py``.

# OWNS: B1-B12 from the plan.
# NOT OWNS: probe-time behaviour (B5 partial — DNS rebind requires a call-
#   time re-pin guard which lives in the proxy layer; covered as a gap test).
"""
from __future__ import annotations

import pytest

from core.listing_safety import has_block, scan_agent_md_endpoint
from core.url_security import (
    validate_agent_endpoint_url,
    validate_outbound_url,
)


# B1 — Percent-encoded aztea.ai. Real gap: ``urllib.parse.urlparse`` already
# percent-decodes the ``.hostname`` attribute, so ``%61ztea.ai`` looks like
# ``aztea.ai`` to ``_enforce_hostname_safety``'s ``host != unquote(host)``
# check — that comparison is always equal, the percent-encoded form never
# trips. Only the listing-safety endpoint scan (which percent-decodes the
# *raw* URL form) catches this. Defence-in-depth WORKS but only thanks to
# the scanner; SSRF alone would let it pass.
@pytest.mark.security
@pytest.mark.ssrf
@pytest.mark.parametrize(
    "url",
    [
        "https://%61ztea.ai/agent",        # %61 == 'a'
        "https://az%74ea.ai/agent",        # %74 == 't'
        "https://aztea.a%69/agent",        # %69 == 'i'
    ],
)
def test_b1_percent_encoded_aztea_blocked_by_listing_safety(url):
    # Confirm the gap explicitly: SSRF does NOT raise on these inputs.
    # If a future patch makes _enforce_hostname_safety inspect the raw
    # URL string (not the urlparse-normalised host), this xfail should
    # be flipped to assert pytest.raises.
    try:
        validate_agent_endpoint_url(url)
        ssrf_caught = False
    except ValueError:
        ssrf_caught = True
    # Listing-safety must catch it via percent-decode in _candidate_endpoint_forms.
    findings = scan_agent_md_endpoint(url)
    assert has_block(findings), f"defence-in-depth failure for {url!r}"
    if not ssrf_caught:
        pytest.xfail(
            "Gap: SSRF percent-check operates on urlparse-decoded host, so "
            "%61ztea.ai presents to it as 'aztea.ai' literally. The check "
            "is dead code for this attack class. Only listing-safety "
            "(which decodes the raw URL string) catches it. Fix: also "
            "check the raw .geturl() / .netloc for percent sequences."
        )


# B2 — IDN / punycode hostname that decodes to a Cyrillic homoglyph of aztea.
@pytest.mark.security
@pytest.mark.ssrf
def test_b2_idn_punycode_homoglyph():
    # xn--azte-83d.ai is the real punycode for 'azteа.ai' (Cyrillic 'а' in
    # position 4). The IDN-decode pass in _candidate_endpoint_forms folds
    # this to 'aztea.ai' via _HOMOGLYPH_FOLD and the suffix check fires.
    url = "https://xn--azte-83d.ai/run"
    findings = scan_agent_md_endpoint(url)
    assert has_block(findings)


# B3 — URL fragment containing aztea host should NOT block (the actual host
# is attacker.com). Negative-space test.
@pytest.mark.security
@pytest.mark.ssrf
def test_b3_fragment_with_aztea_does_not_misfire():
    # url_security rejects fragments outright. Confirm the right error
    # rather than a misfire on the aztea suffix check.
    with pytest.raises(ValueError, match="fragments"):
        validate_outbound_url(
            "https://attacker.example/run#https://aztea.ai",
            "endpoint_url",
        )


# B4 — Userinfo trick: scheme://aztea.ai@attacker.com → host is attacker.
@pytest.mark.security
@pytest.mark.ssrf
def test_b4_userinfo_host_is_attacker():
    # url_security rejects any URL with credentials outright (good).
    with pytest.raises(ValueError, match="username or password"):
        validate_outbound_url(
            "https://aztea.ai@attacker.example/run", "endpoint_url"
        )


# B5 — DNS rebinding: validate at register time with a public IP, then
# resolve to RFC1918 at call time. This is a structural gap — the proxy
# call path (_proxy_call_agent) does not re-pin DNS, so an agent that
# bound to a CDN at register can later swap A-records.
@pytest.mark.security
@pytest.mark.ssrf
def test_b5_dns_rebind_call_time_revalidation_required():
    # Without an integration harness running real DNS this test asserts on
    # the existence of a call-time validation hook. The hook does not exist
    # today; the xfail above is the documentation.
    import server.application as app

    assert hasattr(app, "_revalidate_endpoint_before_call"), (
        "Call-time DNS re-validation hook missing — DNS rebind is exploitable."
    )


# B6 — IPv4-mapped IPv6 (::ffff:10.0.0.1) is already handled by
# _is_disallowed_ip's ipv4_mapped check. Pin it.
@pytest.mark.security
@pytest.mark.ssrf
def test_b6_ipv4_mapped_ipv6_blocked(fake_dns):
    fake_dns({"ipv4mapped.example": ["::ffff:10.0.0.1"]})
    with pytest.raises(ValueError, match="non-public"):
        validate_outbound_url("https://ipv4mapped.example/run", "endpoint_url")


# B7 — Multi-A-record: one public + one private. The check resolves all
# rows and refuses if any are disallowed. Confirm.
@pytest.mark.security
@pytest.mark.ssrf
def test_b7_multi_a_record_one_private_refused(fake_dns):
    fake_dns({"split.example": ["8.8.8.8", "10.0.0.5"]})
    with pytest.raises(ValueError, match="non-public"):
        validate_outbound_url("https://split.example/run", "endpoint_url")


# B8 — Tunnel host drift. _BLOCKED_AGENT_HOST_SUFFIXES misses several
# newer tunnels.
@pytest.mark.security
@pytest.mark.ssrf
@pytest.mark.parametrize(
    "host",
    [
        "abc.ngrok.io",
        "abc.webhook.site",
        "abc.trycloudflare.com",
        # Added 2026-05-22 to the blocklist:
        "abc.cfargotunnel.com",
        "abc.lhr.life",
        "abc.devtunnels.ms",
        "abc.bore.pub",
        "abc.pinggy.online",
        "abc.zrok.io",
    ],
)
def test_b8_tunnel_host_drift(host, fake_dns):
    # All tunnels resolve cleanly to public IPs; the blocklist is the
    # only enforcement.
    fake_dns({host: ["8.8.8.8"]})
    with pytest.raises(ValueError, match="echo|inspection"):
        validate_agent_endpoint_url(f"https://{host}/run")


# B9 — Probe must not follow redirects. Confirmed in code; we inspect the
# raw shard source rather than the namespace-injected module because the
# shard is not importable standalone (it depends on Any / http / etc.
# being bound by the parent application module at load time).
@pytest.mark.security
@pytest.mark.probe
def test_b9_probe_disables_redirects():
    from pathlib import Path
    src = Path("server/application_parts/part_003.py").read_text()
    assert "allow_redirects=False" in src, (
        "Probe call must set allow_redirects=False — a 302 to private IP "
        "would otherwise be followed by the requests library."
    )


# B10 — IPv6 literal in brackets. urlparse handles the brackets, host
# becomes the inner address; the IP check then fires.
@pytest.mark.security
@pytest.mark.ssrf
@pytest.mark.parametrize(
    "url",
    [
        "http://[::1]:8080/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fe80::1]/",
    ],
)
def test_b10_ipv6_literal_variants_blocked(url):
    with pytest.raises(ValueError):
        validate_outbound_url(url, "endpoint_url")


# B11 — Cloud metadata endpoints.
@pytest.mark.security
@pytest.mark.ssrf
@pytest.mark.parametrize(
    "label,host,ip",
    [
        ("aws_imds", "169.254.169.254", "169.254.169.254"),
        ("gcp_metadata", "metadata.google.internal", "169.254.169.254"),
        ("alibaba_metadata", "100.100.100.200", "100.100.100.200"),
    ],
)
def test_b11_cloud_metadata_endpoints_blocked(label, host, ip, fake_dns):
    fake_dns({host: [ip]})
    if host == host.replace(".", "").isdigit() or host == ip:
        with pytest.raises(ValueError):
            validate_outbound_url(f"http://{host}/", "endpoint_url")
    else:
        with pytest.raises(ValueError):
            validate_outbound_url(f"http://{host}/", "endpoint_url")


# B12 — Localhost aliases beyond the literal "localhost".
@pytest.mark.security
@pytest.mark.ssrf
@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0/",                    # any-address shorthand
        "http://[::]/",                        # IPv6 any
        "http://0/",                          # resolves via DNS to 0.0.0.0 → unspecified
        "http://localhost.localdomain/",
    ],
)
def test_b12_localhost_aliases_blocked(url):
    with pytest.raises(ValueError):
        validate_outbound_url(url, "endpoint_url")
