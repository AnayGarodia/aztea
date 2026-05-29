"""Plan B Phase 3c (2026-05-27) — domain ownership badge.

Optional verification: well-known JSON file OR DNS TXT record. Either
method, once verified, sets ``domain_verified=true`` on the agent and
gives a +5 bonus in auto-hire ranking.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from core import domain_proof, registry
from core.registry import auto_hire


# ---------------------------------------------------------------------------
# verify_well_known
# ---------------------------------------------------------------------------


def test_well_known_rejects_non_https_endpoint():
    """internal:// and http:// both rejected — well-known only probed over HTTPS."""
    ok, detail = domain_proof.verify_well_known(
        "internal://my-builtin", "agt_x", "user:y",
    )
    assert ok is False
    assert detail["reason"] == "endpoint_url_not_https"
    # http:// also rejected — security audit fix on 2026-05-27.
    ok2, detail2 = domain_proof.verify_well_known(
        "http://example.com/run", "agt_x", "user:y",
    )
    assert ok2 is False
    assert detail2["reason"] == "endpoint_url_not_https"


def test_well_known_rejects_non_200():
    fake_resp = type("R", (), {"status_code": 404, "iter_content": lambda self, **k: iter([])})()
    with patch("requests.get", return_value=fake_resp):
        ok, detail = domain_proof.verify_well_known(
            "https://example.com/run", "agt_x", "user:y",
        )
    assert ok is False
    assert detail["reason"] == "well_known_not_200"
    assert detail["status"] == 404


def test_well_known_rejects_non_json():
    fake_resp = type("R", (), {
        "status_code": 200,
        "iter_content": lambda self, **k: iter([b"<html>not json</html>"]),
    })()
    with patch("requests.get", return_value=fake_resp):
        ok, detail = domain_proof.verify_well_known(
            "https://example.com/run", "agt_x", "user:y",
        )
    assert ok is False
    assert detail["reason"] == "well_known_not_json"


def test_well_known_rejects_agent_id_mismatch():
    fake_resp = type("R", (), {
        "status_code": 200,
        "iter_content": lambda self, **k: iter([b'{"agent_id":"agt_wrong","owner_id":"user:y"}']),
    })()
    with patch("requests.get", return_value=fake_resp):
        ok, detail = domain_proof.verify_well_known(
            "https://example.com/run", "agt_x", "user:y",
        )
    assert ok is False
    assert detail["reason"] == "agent_id_mismatch"


def test_well_known_blocks_ssrf_via_url_security():
    """Audit fix 2026-05-27: re-validate the constructed well-known URL
    through core.url_security before the outbound GET. Stops DNS rebinding
    between registration and verification from reaching private IPs."""
    # Force validate_outbound_url to raise — simulates the host newly
    # resolving to a private IP after registration.
    from unittest.mock import patch
    from core import url_security
    with patch.object(url_security, "validate_outbound_url", side_effect=ValueError("private IP")):
        ok, detail = domain_proof.verify_well_known(
            "https://attacker.example/run", "agt_x", "user:y",
        )
    assert ok is False
    assert detail["reason"] == "ssrf_blocked"


def test_well_known_accepts_matching_json():
    fake_resp = type("R", (), {
        "status_code": 200,
        "iter_content": lambda self, **k: iter([b'{"agent_id":"agt_x","owner_id":"user:y"}']),
    })()
    with patch("requests.get", return_value=fake_resp):
        ok, detail = domain_proof.verify_well_known(
            "https://example.com/run", "agt_x", "user:y",
        )
    assert ok is True
    assert detail["method"] == "well_known"
    assert detail["url"] == "https://example.com/.well-known/aztea-agent.json"


# ---------------------------------------------------------------------------
# verify_dns_txt
# ---------------------------------------------------------------------------


def test_dns_txt_returns_false_when_dnspython_missing(monkeypatch):
    # Simulate import failure by patching sys.modules. Many CI envs DO have
    # dnspython, so this test stubs the import path directly.
    import sys
    monkeypatch.setitem(sys.modules, "dns", None)
    monkeypatch.setitem(sys.modules, "dns.resolver", None)
    ok, detail = domain_proof.verify_dns_txt("https://example.com/run", "agt_x")
    assert ok is False
    # Either dnspython_unavailable or dns_lookup_failed; both indicate
    # an honest negative.
    assert detail["reason"] in {"dnspython_unavailable", "dns_lookup_failed"}


# ---------------------------------------------------------------------------
# Persistence + auto-hire bonus
# ---------------------------------------------------------------------------


def _register_test_agent() -> str:
    import os
    os.environ["AZTEA_SKIP_REGISTER_ENDPOINT_PROBE"] = "1"
    aid = f"test-domain-{uuid.uuid4().hex[:8]}"
    registry.register_agent(
        name=f"DomainAgent_{aid}",
        description="domain verification test",
        endpoint_url="https://example.com/run",
        price_per_call_usd=0.05,
        tags=["test"],
        input_schema={"type": "object"},
        owner_id=f"user:{uuid.uuid4().hex[:8]}",
        embed_listing=False,
        agent_id=aid,
    )
    return aid


def test_mark_agent_domain_verified_persists():
    aid = _register_test_agent()
    registry.mark_agent_domain_verified(aid, method="well_known")
    refetched = registry.get_agent(aid, include_unapproved=True)
    assert refetched["domain_verified"] == 1
    assert refetched["domain_verification_method"] == "well_known"
    assert refetched["domain_verified_at"]


def test_auto_hire_grants_bonus_for_verified_domain():
    """A verified agent ranks above an otherwise-equal unverified peer."""
    # Use the helper from the Phase 2 test file's pattern: build minimal candidates
    verified_raw = {
        "agent_id": "id-verified",
        "name": "Verified",
        "description": "Counts words.",
        "input_schema": {"type": "object"},
        "tags": ["text"],
        "review_status": "approved",
        "domain_verified": 1,
    }
    unverified_raw = dict(verified_raw, domain_verified=0, agent_id="id-unverified", name="Unverified")
    verified = auto_hire.CandidateAgent(
        agent_id="id-verified", slug="verified", name="Verified",
        description="Counts words.", tags=["text"], category="text",
        price_per_call_usd=0.05, trust_score=80.0, success_rate=0.95,
        stability_tier="general_availability",
        input_schema={"type": "object"},
        raw=verified_raw,
    )
    unverified = auto_hire.CandidateAgent(
        agent_id="id-unverified", slug="unverified", name="Unverified",
        description="Counts words.", tags=["text"], category="text",
        price_per_call_usd=0.05, trust_score=80.0, success_rate=0.95,
        stability_tier="general_availability",
        input_schema={"type": "object"},
        raw=unverified_raw,
    )
    delta_v, why_v = auto_hire._apply_domain_verified_bonus(verified)
    delta_u, why_u = auto_hire._apply_domain_verified_bonus(unverified)
    assert delta_v == 5.0
    assert delta_u == 0.0
    assert "domain verified" in why_v[0]
    assert why_u == []
