import json
import textwrap

import pytest

from core import onboarding


VALID_METADATA = {
    "name": "Acme Financial Research Agent",
    "description": "Summarizes filings into a concise brief.",
    "endpoint_url": "https://agents.example.com/analyze",
    "price_per_call_usd": 0.05,
    "tags": ["financial-research", "sec-filings", "financial-research"],
    "input_schema": {
        "type": "object",
        "properties": {"ticker": {"type": "string"}},
        "required": ["ticker"],
    },
}


def _build_manifest(
    *,
    metadata_block: str | None = None,
    overrides: dict[str, str] | None = None,
    drop_sections: set[str] | None = None,
) -> str:
    overrides = overrides or {}
    drop_sections = drop_sections or set()
    metadata_block = metadata_block or f"```json\n{json.dumps(VALID_METADATA, indent=2)}\n```"

    sections = [
        ("Registry Endpoint", "Use POST /registry/register to create listings."),
        ("Registration Flow", "Validate manifest, normalize metadata, then register."),
        ("Job Acceptance/Claim Flow Expectations", "Use lease-based claims with claim_token."),
        ("Settlement Flow Expectations", "Success pays out 90/10. Failure refunds caller."),
        ("Auth Expectations", "Use Authorization: Bearer <API_KEY>."),
        ("Registration Metadata", metadata_block),
    ]

    lines = ["# Example Agent Manifest", ""]
    for heading, body in sections:
        if heading in drop_sections:
            continue
        lines.append(f"## {heading}")
        lines.append(overrides.get(heading, body))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def test_validate_manifest_content_accepts_required_sections_and_metadata():
    manifest = _build_manifest()
    validated = onboarding.validate_manifest_content(manifest, source="https://example.com/agent.md")

    assert validated["source"] == "https://example.com/agent.md"
    assert "registration_metadata" in validated["sections"]
    assert validated["registration_metadata"]["name"] == VALID_METADATA["name"]
    assert validated["registration_metadata"]["tags"] == ["financial-research", "sec-filings"]


def test_build_registration_payload_from_manifest_maps_to_registry_shape():
    manifest = _build_manifest()
    payload = onboarding.build_registration_payload_from_manifest(manifest)

    assert payload == {
        "name": "Acme Financial Research Agent",
        "description": "Summarizes filings into a concise brief.",
        "endpoint_url": "https://agents.example.com/analyze",
        "price_per_call_usd": 0.05,
        "tags": ["financial-research", "sec-filings"],
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    }


def test_validate_manifest_content_rejects_missing_required_section():
    manifest = _build_manifest(drop_sections={"Auth Expectations"})

    with pytest.raises(onboarding.ManifestValidationError, match="Auth Expectations"):
        onboarding.validate_manifest_content(manifest)


def test_validate_manifest_content_rejects_empty_required_section():
    manifest = _build_manifest(overrides={"Settlement Flow Expectations": "   "})

    with pytest.raises(onboarding.ManifestValidationError, match="Settlement Flow Expectations"):
        onboarding.validate_manifest_content(manifest)


def test_validate_manifest_content_rejects_malformed_registration_metadata_json():
    bad_metadata = textwrap.dedent(
        """\
        ```json
        {"name": "Broken Metadata",}
        ```
        """
    ).strip()
    manifest = _build_manifest(metadata_block=bad_metadata)

    with pytest.raises(onboarding.ManifestValidationError, match="malformed"):
        onboarding.validate_manifest_content(manifest)


def test_parse_registration_metadata_normalizes_string_tags_and_schema():
    parsed = onboarding.parse_registration_metadata(
        {
            "name": "Tag Parser",
            "description": "Normalizes tags + schema.",
            "endpoint_url": "https://agents.example.com/parser",
            "price_per_call_usd": "0.1",
            "tags": "alpha, beta, alpha,  , gamma",
            "input_schema": '{"type":"object","properties":{"ticker":{"type":"string"}}}',
        }
    )

    assert parsed["price_per_call_usd"] == 0.1
    assert parsed["tags"] == ["alpha", "beta", "gamma"]
    assert parsed["input_schema"]["type"] == "object"


def test_parse_registration_metadata_rejects_non_http_endpoint():
    with pytest.raises(onboarding.MetadataValidationError, match="http\\(s\\)"):
        onboarding.parse_registration_metadata(
            {
                "name": "Bad Endpoint Agent",
                "description": "bad endpoint",
                "endpoint_url": "ftp://example.com/agent",
                "price_per_call_usd": 0.03,
            }
        )


def test_manifest_metadata_to_registration_payload_rejects_missing_price():
    with pytest.raises(onboarding.MetadataValidationError, match="price_per_call_usd"):
        onboarding.manifest_metadata_to_registration_payload(
            {
                "name": "No Price",
                "description": "Missing price field.",
                "endpoint_url": "https://example.com/agent",
            }
        )
