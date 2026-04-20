"""
onboarding.py — Core onboarding helpers for OpenClaw-compatible agent manifests.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import socket
from urllib.parse import urlparse, unquote as _url_unquote

_ALLOW_PRIVATE_OUTBOUND_URLS = os.environ.get("ALLOW_PRIVATE_OUTBOUND_URLS", "0").strip().lower() in {
    "1", "true", "yes",
}


class ManifestValidationError(ValueError):
    """Raised when an agent.md-like manifest is malformed or incomplete."""


class MetadataValidationError(ValueError):
    """Raised when registration metadata cannot be normalized safely."""


_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)

_REQUIRED_SECTIONS = (
    ("registry_endpoint", "Registry Endpoint"),
    ("registration_flow", "Registration Flow"),
    ("job_claim_flow_expectations", "Job Acceptance/Claim Flow Expectations"),
    ("settlement_flow_expectations", "Settlement Flow Expectations"),
    ("auth_expectations", "Auth Expectations"),
    ("registration_metadata", "Registration Metadata"),
)

_SECTION_ALIASES = {
    "registry_endpoint": {
        "registry endpoint",
    },
    "registration_flow": {
        "registration flow",
    },
    "job_claim_flow_expectations": {
        "job acceptance claim flow expectations",
        "job acceptance flow expectations",
        "job claim flow expectations",
    },
    "settlement_flow_expectations": {
        "settlement flow expectations",
        "settlement expectations",
    },
    "auth_expectations": {
        "auth expectations",
        "authentication expectations",
    },
    "registration_metadata": {
        "registration metadata",
        "metadata",
    },
}


def _normalize_heading(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


for _key, _label in _REQUIRED_SECTIONS:
    _SECTION_ALIASES[_key].add(_normalize_heading(_label))


def _parse_sections(manifest_content: str) -> list[dict]:
    matches = list(_HEADING_RE.finditer(manifest_content))
    sections = []
    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(manifest_content)
        heading = match.group(1).strip()
        body = manifest_content[body_start:body_end].strip()
        sections.append(
            {
                "heading": heading,
                "normalized_heading": _normalize_heading(heading),
                "body": body,
            }
        )
    return sections


def _find_section(sections: list[dict], aliases: set[str]) -> dict | None:
    for section in sections:
        if section["normalized_heading"] in aliases:
            return section
    return None


def _extract_metadata_object(section_body: str, source: str) -> dict:
    match = _JSON_FENCE_RE.search(section_body)
    if match:
        metadata_json = match.group(1).strip()
    else:
        stripped = section_body.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            metadata_json = stripped
        else:
            raise ManifestValidationError(
                f"{source}: Registration Metadata must include a JSON object (prefer a ```json fenced block)."
            )

    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(
            f"{source}: Registration Metadata JSON is malformed (line {exc.lineno}, column {exc.colno})."
        ) from exc
    if not isinstance(metadata, dict):
        raise ManifestValidationError(f"{source}: Registration Metadata must decode to a JSON object.")
    return metadata


def _require_non_empty_text(raw: dict, keys: tuple[str, ...], field_name: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    alias_list = ", ".join(keys)
    raise MetadataValidationError(f"Missing required '{field_name}' field (accepted keys: {alias_list}).")


def _normalize_endpoint_url(url: str) -> str:
    normalized = url.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MetadataValidationError("endpoint_url must be an absolute http(s) URL.")
    if parsed.username or parsed.password:
        raise MetadataValidationError("endpoint_url must not include credentials.")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise MetadataValidationError("endpoint_url hostname is missing.")

    if _ALLOW_PRIVATE_OUTBOUND_URLS:
        return normalized

    if host != _url_unquote(host):
        raise MetadataValidationError("endpoint_url hostname must not contain percent-encoded characters.")
    if host == "localhost" or host.endswith(".localhost"):
        raise MetadataValidationError("endpoint_url cannot target localhost.")

    def _is_disallowed(ip: ipaddress._BaseAddress) -> bool:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            return _is_disallowed(ip.ipv4_mapped)
        return False

    try:
        direct_ip = ipaddress.ip_address(host)
        if _is_disallowed(direct_ip):
            raise MetadataValidationError("endpoint_url cannot target private or reserved IP addresses.")
    except ValueError:
        try:
            rows = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return normalized
        except OSError as exc:
            raise MetadataValidationError("endpoint_url hostname resolution failed.") from exc
        for row in rows:
            sockaddr = row[4]
            if not sockaddr:
                continue
            try:
                resolved = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            if _is_disallowed(resolved):
                raise MetadataValidationError("endpoint_url resolves to a private or reserved IP address.")

    return normalized


def _normalize_tags(raw_tags) -> list[str]:
    if raw_tags is None:
        return []
    if isinstance(raw_tags, str):
        candidates = [piece.strip() for piece in raw_tags.split(",")]
    elif isinstance(raw_tags, list):
        candidates = []
        for index, item in enumerate(raw_tags):
            if not isinstance(item, str):
                raise MetadataValidationError(f"tags[{index}] must be a string.")
            candidates.append(item.strip())
    else:
        raise MetadataValidationError("tags must be a list of strings or a comma-separated string.")

    deduped = []
    seen = set()
    for tag in candidates:
        if not tag or tag in seen:
            continue
        seen.add(tag)
        deduped.append(tag)
    return deduped


def _normalize_input_schema(raw_schema):
    if raw_schema is None:
        return {}
    if isinstance(raw_schema, str):
        stripped = raw_schema.strip()
        if not stripped:
            return {}
        try:
            raw_schema = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise MetadataValidationError(
                f"input_schema must be valid JSON when provided as a string (line {exc.lineno}, column {exc.colno})."
            ) from exc
    if not isinstance(raw_schema, dict):
        raise MetadataValidationError("input_schema must be a JSON object.")
    return raw_schema


def parse_registration_metadata(metadata: dict | str) -> dict:
    """
    Parse and normalize registration metadata for /registry/register ingestion.
    """
    if isinstance(metadata, str):
        stripped = metadata.strip()
        if not stripped:
            raise MetadataValidationError("Registration metadata string is empty.")
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise MetadataValidationError(
                f"Registration metadata JSON is malformed (line {exc.lineno}, column {exc.colno})."
            ) from exc
    elif isinstance(metadata, dict):
        raw = metadata
    else:
        raise MetadataValidationError("Registration metadata must be a dict or JSON string.")

    if not isinstance(raw, dict):
        raise MetadataValidationError("Registration metadata must decode to a JSON object.")

    name = _require_non_empty_text(raw, ("name", "agent_name"), "name")
    description = _require_non_empty_text(raw, ("description", "summary"), "description")
    endpoint_url = _normalize_endpoint_url(
        _require_non_empty_text(raw, ("endpoint_url", "endpoint"), "endpoint_url")
    )

    raw_price = raw.get("price_per_call_usd", raw.get("price_usd"))
    if raw_price is None:
        raise MetadataValidationError("Missing required 'price_per_call_usd' field.")
    try:
        price_per_call_usd = float(raw_price)
    except (TypeError, ValueError) as exc:
        raise MetadataValidationError("price_per_call_usd must be a finite non-negative number.") from exc
    if not math.isfinite(price_per_call_usd) or price_per_call_usd < 0:
        raise MetadataValidationError("price_per_call_usd must be a finite non-negative number.")

    tags = _normalize_tags(raw.get("tags", raw.get("capabilities")))
    input_schema = _normalize_input_schema(raw.get("input_schema"))
    output_schema = _normalize_input_schema(raw.get("output_schema"))
    output_verifier_url = raw.get("output_verifier_url")
    if output_verifier_url is not None:
        output_verifier_url = str(output_verifier_url).strip() or None

    return {
        "name": name,
        "description": description,
        "endpoint_url": endpoint_url,
        "price_per_call_usd": price_per_call_usd,
        "tags": tags,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "output_verifier_url": output_verifier_url,
    }


def validate_manifest_content(manifest_content: str, source: str = "agent.md") -> dict:
    """
    Validate required sections and metadata in an agent.md-like manifest.
    Returns validated sections plus normalized registration metadata.
    """
    if not isinstance(manifest_content, str):
        raise ManifestValidationError(f"{source}: manifest content must be a string.")
    if not manifest_content.strip():
        raise ManifestValidationError(f"{source}: manifest content is empty.")

    sections = _parse_sections(manifest_content)
    if not sections:
        raise ManifestValidationError(f"{source}: no markdown headings found.")

    validated_sections = {}
    missing_sections = []
    for section_key, label in _REQUIRED_SECTIONS:
        section = _find_section(sections, _SECTION_ALIASES[section_key])
        if section is None:
            missing_sections.append(label)
            continue
        if not section["body"]:
            raise ManifestValidationError(f"{source}: required section '{label}' is empty.")
        validated_sections[section_key] = {
            "heading": section["heading"],
            "content": section["body"],
        }

    if missing_sections:
        raise ManifestValidationError(
            f"{source}: missing required section(s): {', '.join(missing_sections)}."
        )

    raw_metadata = _extract_metadata_object(
        validated_sections["registration_metadata"]["content"],
        source=source,
    )
    try:
        normalized_metadata = parse_registration_metadata(raw_metadata)
    except MetadataValidationError as exc:
        raise ManifestValidationError(f"{source}: invalid registration metadata: {exc}") from exc

    return {
        "source": source,
        "sections": validated_sections,
        "registration_metadata": normalized_metadata,
    }


def manifest_metadata_to_registration_payload(metadata: dict | str) -> dict:
    """
    Map normalized manifest metadata to the /registry/register payload shape.
    """
    normalized = parse_registration_metadata(metadata)
    return {
        "name": normalized["name"],
        "description": normalized["description"],
        "endpoint_url": normalized["endpoint_url"],
        "price_per_call_usd": normalized["price_per_call_usd"],
        "tags": normalized["tags"],
        "input_schema": normalized["input_schema"],
        "output_schema": normalized["output_schema"],
        "output_verifier_url": normalized["output_verifier_url"],
    }


def build_registration_payload_from_manifest(
    manifest_content: str,
    source: str = "agent.md",
) -> dict:
    """
    Validate an agent manifest and return a /registry/register-compatible payload.
    """
    validated = validate_manifest_content(manifest_content, source=source)
    return manifest_metadata_to_registration_payload(validated["registration_metadata"])
