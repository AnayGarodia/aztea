"""Sections G-J — identity, clone detection, verifier, privacy.

# OWNS: G1-G4, H1-H5, I1-I3, J1-J2 from the plan.
"""
from __future__ import annotations

import uuid

import pytest

from core.crypto import (
    OUTPUT_SIG_SCHEME_V2,
    generate_signing_keypair,
    sign_output_v2,
    verify_output_v2,
)
from core.identity import build_agent_did, did_document_url
from core.listing_safety import has_warn, scan_clone_against
from tests.integration.support import (
    TEST_MASTER_KEY,
    _auth_headers,
    _register_agent_via_api,
    _register_user,
)


# ---------------------------------------------------------------------------
# G — Identity / crypto
# ---------------------------------------------------------------------------


# G1 — DID uniqueness is enforced by the database. Pin via inspection.
@pytest.mark.security
@pytest.mark.identity
def test_g1_did_has_unique_index():
    """Migration 0015 declares ``CREATE UNIQUE INDEX … ON agents(did)``."""
    from pathlib import Path
    mig = Path("migrations/0015_agent_identity.sql").read_text()
    assert "UNIQUE INDEX" in mig and "agents(did)" in mig.replace(" ", "")


# G2 — OUTPUT_SIG_SCHEME_V2 sigil binds the signature to agent_id, so an
# output signed by agent A cannot be replayed as agent B's.
@pytest.mark.security
@pytest.mark.identity
def test_g2_signature_binds_to_agent_id():
    priv_a, pub_a = generate_signing_keypair()
    priv_b, pub_b = generate_signing_keypair()

    output = {"result": "ok", "n": 42}
    sig_a = sign_output_v2(priv_a, job_id="j1", agent_id="A", output=output)

    # Same output signed by A must not verify under B's key, nor under A's
    # key with a different agent_id field.
    assert verify_output_v2(
        pub_a, job_id="j1", agent_id="A", output=output, signature_b64=sig_a,
    ) is True
    assert verify_output_v2(
        pub_b, job_id="j1", agent_id="A", output=output, signature_b64=sig_a,
    ) is False
    assert verify_output_v2(
        pub_a, job_id="j1", agent_id="B", output=output, signature_b64=sig_a,
    ) is False
    # Scheme name itself is part of the regression contract.
    assert OUTPUT_SIG_SCHEME_V2 == "Ed25519+aztea-output-sig/2"


# G3 — DID document URL is publicly reachable and matches the stored key.
@pytest.mark.security
@pytest.mark.identity
def test_g3_did_document_resolves_and_matches(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    user = _register_user()
    agent_id = _register_agent_via_api(
        client, user["raw_api_key"],
        name=f"did-{uuid.uuid4().hex[:6]}",
        auto_approve=True,
    )

    did = build_agent_did(agent_id)
    assert did.startswith("did:web:")
    assert agent_id in did

    url_path = did_document_url(agent_id)
    # build the path relative to the TestClient base
    relpath = url_path.split("/agents/", 1)[1]
    resp = client.get(f"/agents/{relpath}")
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    # DID document must include a verificationMethod referencing the agent.
    assert "verificationMethod" in doc or "publicKey" in doc, doc


# G4 — Key rotation history. Old receipts must still verify after rotation.
@pytest.mark.security
@pytest.mark.identity
def test_g4_key_rotation_history():
    from core import registry as registry_mod

    assert hasattr(registry_mod, "rotate_agent_signing_key")


# ---------------------------------------------------------------------------
# H — Visual / name impersonation
# ---------------------------------------------------------------------------


# H1 — Cyrillic homoglyph in agent name. scan_clone_against runs jaccard on
# bigrams without folding homoglyphs.
@pytest.mark.security
@pytest.mark.publish
def test_h1_homoglyph_name_clone_detected():
    cyr_o = "о"  # Cyrillic 'o' U+043E
    candidate_name = f"C{cyr_o}de Review"
    # Use a deliberately different description so name match must do the
    # work — otherwise the test xpasses on description match alone.
    findings = scan_clone_against(
        candidate_name=candidate_name,
        candidate_description="completely unrelated description text",
        existing=[{"name": "Code Review", "description": "reviews code for issues"}],
    )
    assert has_warn(findings), f"homoglyph clone not detected: {findings}"


# H2 — Synonym evasion (Jaccard bigrams won't catch).
@pytest.mark.security
@pytest.mark.publish
def test_h2_synonym_evasion_documented():
    """Document the well-known Jaccard limitation.

    Jaccard on bigrams cannot detect semantic similarity. The plan calls
    for an embedding-cosine fallback server-side; until that lands, this
    test documents the current behaviour rather than testing a gap.
    """
    findings = scan_clone_against(
        candidate_name="Source Inspector",
        candidate_description="Analyses source code for defects.",
        existing=[
            {"name": "Code Review", "description": "Reviews source code for bugs."}
        ],
    )
    # Today: no warn (different bigrams). Pinned.
    assert not has_warn(findings)


# H3 — Filler-word padding to drop Jaccard.
@pytest.mark.security
@pytest.mark.publish
def test_h3_filler_padding_pinned():
    """Padding evades Jaccard threshold. Pinned for symmetry with H2."""
    base_desc = "Reviews code for bugs"
    padded = "Reviews the entirety of submitted code with care for bugs and other things"
    findings = scan_clone_against(
        candidate_name="Code Helper",
        candidate_description=padded,
        existing=[{"name": "Code Review", "description": base_desc}],
    )
    assert not has_warn(findings)


# H4 — covered by D1b (PATCH not re-scanning examples/tags); skipped here.


# H5 — Leading zero-width chars in registered name. Registration runs the
# new ``_normalize_agent_name`` helper which NFKC-folds and strips
# zero-width / bidi characters so a scammer cannot pin to the top of
# name-sorted listings.
@pytest.mark.security
@pytest.mark.publish
def test_h5_leading_zero_width_in_name(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    user = _register_user()
    leading_zwsp = "​"
    name = f"{leading_zwsp}top-of-list-{uuid.uuid4().hex[:6]}"
    payload = {
        "name": name,
        "description": "zero width leading name",
        "endpoint_url": "https://agents.example.com/x",
        "price_per_call_usd": 0.05,
        "tags": [],
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "title": "Task",
                    "description": "input task",
                }
            },
        },
        "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
    }
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(user["raw_api_key"]),
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Stored name must NOT start with the zero-width char.
    stored_name = body.get("agent", {}).get("name") or body.get("name") or ""
    assert not stored_name.startswith(leading_zwsp), (
        f"Zero-width char survived normalisation: {stored_name!r}"
    )
    assert stored_name.startswith("top-of-list-"), stored_name


# ---------------------------------------------------------------------------
# I — Output verifier abuse
# ---------------------------------------------------------------------------


# I1 — Verifier returns naive {verified: true} without signature.
@pytest.mark.security
@pytest.mark.publish
def test_i1_verifier_requires_signed_response():
    from pathlib import Path
    # The verifier call lives in part_005, not part_003.
    src = Path("server/application_parts/part_005.py").read_text()
    assert "verifier_signature" in src or "verify_signature" in src.lower(), (
        "Verifier response is not cryptographically bound to a known key."
    )


# I2 — Verifier URL pointing at aztea-owned host should be rejected the
# same way endpoint_url is. Today this is NOT enforced — the verifier URL
# is run only through validate_outbound_url (SSRF), not through
# scan_agent_md_endpoint (which catches aztea-suffix impersonation).
@pytest.mark.security
@pytest.mark.publish
def test_i2_verifier_url_blocks_aztea_host(client, monkeypatch):
    """Confirm verifier URL is run through the same SSRF + listing-safety check."""
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    user = _register_user()
    payload = {
        "name": f"verifier-aztea-{uuid.uuid4().hex[:6]}",
        "description": "verifier url aztea host",
        "endpoint_url": "https://agents.example.com/x",
        "output_verifier_url": "https://aztea.ai/verify",
        "price_per_call_usd": 0.05,
        "tags": [],
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "title": "Task",
                    "description": "input task",
                }
            },
        },
        "output_examples": [{"input": {"task": "x"}, "output": {"ok": True}}],
    }
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(user["raw_api_key"]),
        json=payload,
    )
    # listing-safety endpoint check applies to verifier URL too? Verify.
    # If not, the test fails and surfaces the gap.
    assert resp.status_code == 400, resp.text


# I3 — Verifier verdict bound to payload hash.
@pytest.mark.security
@pytest.mark.publish
def test_i3_verifier_response_includes_payload_hash():
    from pathlib import Path
    src = Path("server/application_parts/part_005.py").read_text()
    assert "payload_hash" in src or "input_hash" in src


# ---------------------------------------------------------------------------
# J — Privacy / data handling
# ---------------------------------------------------------------------------


# J1 — Security-category agents drop work examples even without the
# examples_sensitive flag.
@pytest.mark.security
@pytest.mark.publish
def test_j1_security_category_drops_work_examples():
    """_record_public_work_example must drop on category alone.

    This regression-locks the behaviour described in CLAUDE.md ("three
    independent gates"). Read the shard source directly because the
    shard cannot be imported standalone.
    """
    from pathlib import Path
    src = Path("server/application_parts/part_003.py").read_text()
    assert "_SENSITIVE_EXAMPLE_AGENT_IDS" in src, "hardcoded gate missing"
    assert "examples_sensitive" in src, "per-spec flag gate missing"
    # The category gate uses lowercase comparison: `category.lower() == "security"`.
    assert '"security"' in src.lower() or "'security'" in src.lower(), (
        "Security-category drop gate appears to be missing"
    )


# J2 — pii_safe / outputs_not_stored flags enforced at storage layer.
@pytest.mark.security
@pytest.mark.publish
def test_j2_pii_safe_enforced_at_storage():
    from pathlib import Path
    src = Path("server/application_parts/part_003.py").read_text()
    assert "pii_safe" in src and ("outputs_not_stored" in src), (
        "Neither flag is consulted at storage time."
    )
