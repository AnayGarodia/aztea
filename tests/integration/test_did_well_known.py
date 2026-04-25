"""Integration tests for the public DID document and job-signature endpoints."""

import base64

from tests.integration.support import *  # noqa: F403


def test_did_document_endpoint_returns_valid_did_doc(client):
    owner = _register_user()
    aid = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"DID Doc Test {uuid.uuid4().hex[:6]}",
    )

    resp = client.get(f"/agents/{aid}/did.json")
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    # @context includes the DID v1 context.
    assert "https://www.w3.org/ns/did/v1" in doc["@context"]
    # id matches what the registration produced.
    agent = registry.get_agent(aid, include_unapproved=True)
    assert doc["id"] == agent["did"]
    # Exactly one verificationMethod with a JWK.
    vm = doc["verificationMethod"]
    assert len(vm) == 1
    jwk = vm[0]["publicKeyJwk"]
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    raw = base64.urlsafe_b64decode(jwk["x"] + "==")
    assert len(raw) == 32  # Ed25519 raw public key


def test_did_document_404_for_unknown_agent(client):
    resp = client.get(f"/agents/{uuid.uuid4()}/did.json")
    assert resp.status_code == 404


def test_did_document_404_when_agent_has_no_keypair(client):
    """If somehow an agent exists without a signing key (legacy / migration in
    progress), the DID endpoint must 404 rather than expose a half-document."""
    owner = _register_user()
    aid = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"No Key Agent {uuid.uuid4().hex[:6]}",
    )
    # Manually clear the signing key.
    with registry._conn() as conn:
        conn.execute(
            "UPDATE agents SET signing_public_key = NULL, did = NULL WHERE agent_id = ?",
            (aid,),
        )
    resp = client.get(f"/agents/{aid}/did.json")
    assert resp.status_code == 404


def test_completing_job_attaches_signature_and_signature_endpoint_returns_it(client):
    owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    aid = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"Sign Job Agent {uuid.uuid4().hex[:6]}",
    )

    create = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": aid, "input_payload": {"task": "analyze"}, "max_attempts": 1},
    )
    assert create.status_code == 201, create.text
    job_id = create.json()["job_id"]

    claim = client.post(
        f"/jobs/{job_id}/claim",
        headers=_auth_headers(owner["raw_api_key"]),
        json={"lease_seconds": 120},
    )
    assert claim.status_code == 200, claim.text
    token = claim.json()["claim_token"]

    output_payload = {"summary": "looks good", "score": 7}
    complete = client.post(
        f"/jobs/{job_id}/complete",
        headers=_auth_headers(owner["raw_api_key"]),
        json={"output_payload": output_payload, "claim_token": token},
    )
    assert complete.status_code == 200, complete.text

    # The signature endpoint returns a valid Ed25519 signature.
    sig_resp = client.get(f"/jobs/{job_id}/signature")
    assert sig_resp.status_code == 200, sig_resp.text
    sig_body = sig_resp.json()
    assert sig_body["alg"] == "ed25519"
    assert sig_body["did"].startswith("did:web:")
    assert sig_body["agent_id"] == aid
    assert sig_body["verify_url"].endswith(f"/agents/{aid}/did.json")
    # Signature must verify against the agent's public key for the
    # canonicalised output payload — the same payload the caller can
    # observe on /jobs/{id}.
    agent = registry.get_agent(aid, include_unapproved=True)
    from core import crypto as _crypto
    job = jobs.get_job(job_id)
    assert _crypto.verify_signature(
        agent["signing_public_key"],
        job["output_payload"],
        sig_body["signature"],
    ) is True


def test_signature_endpoint_404_for_pending_job(client):
    owner = _register_user()
    caller = _register_user()
    _fund_user_wallet(caller, 200)
    aid = _register_agent_via_api(
        client,
        owner["raw_api_key"],
        name=f"Pending Sig Agent {uuid.uuid4().hex[:6]}",
    )
    create = client.post(
        "/jobs",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"agent_id": aid, "input_payload": {"task": "x"}},
    )
    job_id = create.json()["job_id"]
    sig_resp = client.get(f"/jobs/{job_id}/signature")
    assert sig_resp.status_code == 404
