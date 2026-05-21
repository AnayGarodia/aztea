from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agents import hcl_terraform_analyzer
from server.builtin_agents.constants import BROWSER_AGENT_ID
from server.builtin_agents.specs import builtin_agent_specs


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def test_browser_screenshot_only_schema_does_not_require_html():
    spec = next(item for item in builtin_agent_specs() if item["agent_id"] == BROWSER_AGENT_ID)
    assert "screenshot_only" in spec["input_schema"]["properties"]["action"]["enum"]
    assert "html" not in spec["output_schema"]["required"]


def test_hcl_fallback_returns_findings_when_checkov_missing():
    hcl = """
    resource "aws_security_group_rule" "ssh" {
      cidr_blocks = ["0.0.0.0/0"]
      from_port = 22
    }
    """
    with patch.object(hcl_terraform_analyzer, "_checkov_available", return_value=(False, None)):
        result = hcl_terraform_analyzer.run({"hcl_content": hcl})

    assert result["tool"] == "aztea-static-hcl-fallback"
    assert result["failed_count"] == 1
    assert result["findings"][0]["check_id"] == "AZTEA_HCL_001"


def test_sdk_disputes_namespace_is_callable():
    from aztea import AzteaClient

    client = AzteaClient(api_key="az_test", base_url="https://example.test")
    assert client.disputes() is client.disputes


def test_sdk_verify_accepts_server_canonical_v2_payload():
    from aztea._client_internals._verify import verify_job

    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    output_payload = {"ok": True}
    output_hash = hashlib.sha256(_canonical(output_payload)).hexdigest()
    sigil = {
        "v": "aztea/output-sig/2",
        "job_id": "job-1",
        "agent_id": "agent-1",
        "output_hash": output_hash,
    }
    signature = private_key.sign(_canonical(sigil))

    class FakeClient:
        def get_job_signature(self, job_id: str):
            assert job_id == "job-1"
            return {
                "job_id": "job-1",
                "agent_id": "agent-1",
                "agent_did": "did:web:aztea.ai:agents:agent-1",
                "alg": "Ed25519+aztea-output-sig/2",
                "signature": base64.b64encode(signature).decode("ascii"),
                "output_hash": output_hash,
                "signed_payload_b64": base64.b64encode(_canonical(output_payload)).decode("ascii"),
                "public_key_jwk": {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "x": base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("="),
                },
            }

    assert verify_job(FakeClient(), "job-1")["verified"] is True
