"""Ed25519 signing for site maps — wraps core.crypto, never reimplements it.

# OWNS: the canonical site-map manifest, its signing scheme, and sign/verify.
# NOT OWNS: key management (the author agent's PEM comes from the agents table,
#           injected by the caller), DB access, or affordance extraction.
# INVARIANTS:
#   * Signing always goes through core.crypto.sign_payload / verify_signature.
#   * The manifest binds (site_key, author_did, version, dom_fingerprint,
#     map_sha256) so a signature can't be replayed onto a different map.
"""

from __future__ import annotations

import hashlib
from typing import Any

from core import crypto

# Distinct from the output-sig scheme so an offline verifier knows the domain.
SITE_MAP_SIG_SCHEME = "Ed25519+aztea-sitemap-sig/1"
SITE_MAP_SCHEMA = "aztea/site-map/1"


def map_sha256(map_json: Any) -> str:
    """Pure: SHA256 of the canonical map document (the signed-bytes identity)."""
    return hashlib.sha256(crypto.canonical_json(map_json)).hexdigest()


def build_map_manifest(
    *,
    site_key: str,
    url_pattern: str,
    map_json: Any,
    dom_fingerprint: str,
    author_did: str,
    version: int,
) -> dict[str, Any]:
    """Pure: the canonical dict an author signs.

    Why a dict of binding fields (not the raw map): keeping them explicit lets a
    verifier fail loudly if anyone forwards a signature against a different
    site_key / author / version. ``map_sha256`` keeps the manifest compact.
    """
    return {
        "v": SITE_MAP_SCHEMA,
        "scheme": SITE_MAP_SIG_SCHEME,
        "site_key": str(site_key),
        "url_pattern": str(url_pattern),
        "author_did": str(author_did),
        "version": int(version),
        "dom_fingerprint": str(dom_fingerprint),
        "map_sha256": map_sha256(map_json),
    }


def sign_map(private_pem: str, manifest: dict[str, Any]) -> str:
    """Sign ``canonical_json(manifest)`` with the author agent's Ed25519 key."""
    return crypto.sign_payload(private_pem, manifest)


def verify_map(public_pem: str, manifest: dict[str, Any], signature_b64: str) -> bool:
    """Return True iff the signature is valid for the manifest under the public key."""
    return crypto.verify_signature(public_pem, manifest, signature_b64)


# --------------------------------------------------------------------------- API specs
# The "compile a site into an API" replay path. Distinct scheme so an offline
# verifier knows this is an API-spec signature, not a site-map one. The manifest
# binds the IMMUTABLE network identity (scheme/host/port/method) so a signed spec
# can never be re-pointed at another endpoint — the SSRF firewall. There is no
# version field because site_api_specs (migration 0080) has no version column;
# supersede-on-refresh in store.put_api_spec gives "one active per endpoint".
API_SPEC_SIG_SCHEME = "Ed25519+aztea-apispec-sig/1"
API_SPEC_SCHEMA = "aztea/api-spec/1"


def api_spec_sha256(*, field_map: Any, param_schema: Any) -> str:
    """Pure: SHA256 of the templatable surface (field_map + param_schema) as one hash."""
    return hashlib.sha256(
        crypto.canonical_json({"field_map": field_map, "param_schema": param_schema})
    ).hexdigest()


def build_api_spec_manifest(
    *,
    site_key: str,
    author_did: str,
    method: str,
    endpoint_scheme: str,
    endpoint_host: str,
    endpoint_port: int | None,
    path_template: str,
    query_template: str,
    response_fingerprint: str,
    field_map: Any,
    param_schema: Any,
) -> dict[str, Any]:
    """Pure: the canonical dict an author signs for a discovered API spec.

    Binds the immutable network identity (scheme/host/port/method) so a signature
    can never be replayed onto a different endpoint, plus the templatable
    path/query, the response shape, and a hash of (field_map, param_schema) so the
    full replay contract is tamper-evident. Every field is reconstructable from the
    stored row, so an offline verifier can rebuild and check it.
    """
    return {
        "v": API_SPEC_SCHEMA,
        "scheme": API_SPEC_SIG_SCHEME,
        "site_key": str(site_key),
        "author_did": str(author_did),
        "method": str(method),
        "endpoint_scheme": str(endpoint_scheme),
        "endpoint_host": str(endpoint_host),
        "endpoint_port": None if endpoint_port is None else int(endpoint_port),
        "path_template": str(path_template),
        "query_template": str(query_template),
        "response_fingerprint": str(response_fingerprint),
        "templatable_sha256": api_spec_sha256(field_map=field_map, param_schema=param_schema),
    }


def sign_api_spec(private_pem: str, manifest: dict[str, Any]) -> str:
    """Sign ``canonical_json(manifest)`` with the author agent's Ed25519 key."""
    return crypto.sign_payload(private_pem, manifest)


def verify_api_spec(public_pem: str, manifest: dict[str, Any], signature_b64: str) -> bool:
    """Return True iff the signature is valid for the API-spec manifest."""
    return crypto.verify_signature(public_pem, manifest, signature_b64)
