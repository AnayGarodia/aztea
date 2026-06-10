"""In-process authoring + reuse glue for the commons.

# OWNS: turning a navigation into a signed commons deposit, and selecting the
#        best reusable map for a site. The path the built-in site_navigator uses.
# NOT OWNS: store CRUD (store.py), signing primitives (signing.py), ranking math
#           (ranking.py), royalty settlement (payouts.py — platform plumbing).
# INVARIANTS:
#   * Both functions are best-effort and NEVER raise — the commons is additive,
#     so a commons failure must never break a navigation.
# DECISIONS:
#   * reputation / identity_backfill are imported lazily inside the functions so
#     importing core.site_maps stays cheap and can't form an import cycle.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

from core.site_maps import api_discovery, normalize, ranking, signing, store

_LOG = logging.getLogger(__name__)


def author_map(
    *, agent_id: str, owner_id: str, url: str, map_json: dict[str, Any], roles: list[str],
) -> dict[str, Any] | None:
    """Sign + deposit a map authored by ``agent_id``. Returns the stored row, or
    None if the agent has no signing key or anything fails. Never raises.
    """
    try:
        from core.registry.identity_backfill import ensure_agent_signing_keys

        private_pem, _public_pem, did = ensure_agent_signing_keys(agent_id)
        if not (private_pem and did):
            return None
        site_key = normalize.normalize_site_key(url)
        return store.put_map(
            site_key=site_key,
            url_pattern=normalize.url_pattern_from(url),
            author_did=did,
            author_agent_id=agent_id,
            author_owner_id=owner_id,
            map_json=map_json,
            dom_fingerprint=normalize.dom_fingerprint(site_key, roles),
            private_pem=private_pem,
        )
    except Exception:  # noqa: BLE001 — commons is additive; never break the call
        _LOG.warning("site-map authoring failed for %s", url, exc_info=True)
        return None


def find_reusable_map(url: str) -> dict[str, Any] | None:
    """Best active signed map for ``url``'s site_key, or None. Read-only.

    Ranking weights author trust, empirical reliability, recency, and open
    challenges. Best-effort: returns None on any failure.
    """
    try:
        site_key = normalize.normalize_site_key(url)
        maps = store.get_active_maps(site_key)
        if not maps:
            return None
        trust = _trust_by_agent({str(m.get("author_agent_id") or "") for m in maps})
        return ranking.select_best_map(
            maps,
            trust_by_agent=trust,
            open_challenges_by_map=store.open_challenge_counts(site_key),
        )
    except Exception:  # noqa: BLE001 — read-side commons failure must not break the call
        _LOG.warning("site-map reuse lookup failed for %s", url, exc_info=True)
        return None


def _trust_by_agent(agent_ids: set[str]) -> dict[str, float]:
    """Side-effect: batch the 0-100 trust score for each author agent."""
    from core import reputation

    out: dict[str, float] = {}
    for agent_id in agent_ids:
        if not agent_id:
            continue
        try:
            out[agent_id] = float(reputation.compute_trust_metrics(agent_id).get("trust_score") or 0.0)
        except Exception:  # noqa: BLE001 — a missing trust score defaults to 0
            out[agent_id] = 0.0
    return out


# --------------------------------------------------------------------------- API specs
def author_api_spec(
    *, agent_id: str, owner_id: str, page_url: str, capture: dict[str, Any],
    map_id: str | None = None,
) -> dict[str, Any] | None:
    """Sign + deposit a discovered API spec from one captured XHR. Returns the stored
    row, or None on cross-origin endpoint / missing key / any failure. Never raises.

    Fix #2 (cross-origin poisoning): the captured endpoint host must share the page's
    registrable domain — checked FIRST (cheap, no key/DB) so an author can never
    register a spec for one site that points at an attacker's host.
    """
    try:
        parts = api_discovery.split_endpoint(str(capture.get("url") or ""))
        page_host = urlparse(page_url).hostname or ""
        if not api_discovery.same_registrable_domain(parts.host, page_host):
            _LOG.info("api_spec authoring refused (cross-origin): %s vs %s", parts.host, page_host)
            return None
        from core.registry.identity_backfill import ensure_agent_signing_keys

        private_pem, _public_pem, did = ensure_agent_signing_keys(agent_id)
        if not (private_pem and did):
            return None
        return store.put_api_spec(
            site_key=normalize.normalize_site_key(page_url), map_id=map_id,
            author_did=did, author_agent_id=agent_id, author_owner_id=owner_id,
            method=str(capture.get("method") or "GET"), endpoint_scheme=parts.scheme,
            endpoint_host=parts.host, endpoint_port=parts.port,
            path_template=parts.path, query_template=parts.query,
            param_schema={},  # v1: literal spec, no template params
            response_fingerprint=normalize.response_shape_fingerprint(capture.get("json")),
            field_map={"$": "$"},  # v1: whole-body; per-field JSONPath is a follow-up
            private_pem=private_pem,
        )
    except Exception:  # noqa: BLE001 — commons is additive; never break the call
        _LOG.warning("api_spec authoring failed for %s", page_url, exc_info=True)
        return None


def verify_api_spec_signature(spec: dict[str, Any]) -> bool:
    """Rebuild the signed manifest from the stored row and verify it against the
    author agent's public key. False on any failure (so an unverifiable spec is
    simply not reused). The reuse gate that makes the signed-host property matter.
    """
    try:
        from core.registry.identity_backfill import ensure_agent_signing_keys

        _priv, public_pem, _did = ensure_agent_signing_keys(str(spec.get("author_agent_id") or ""))
        if not public_pem:
            return False
        manifest = signing.build_api_spec_manifest(
            site_key=str(spec.get("site_key") or ""),
            author_did=str(spec.get("author_did") or ""),
            method=str(spec.get("method") or "GET"),
            endpoint_scheme=str(spec.get("endpoint_scheme") or "https"),
            endpoint_host=str(spec.get("endpoint_host") or ""),
            endpoint_port=spec.get("endpoint_port"),
            path_template=str(spec.get("path_template") or "/"),
            query_template=str(spec.get("query_template") or ""),
            response_fingerprint=str(spec.get("response_fingerprint") or ""),
            field_map=json.loads(spec.get("field_map") or "null"),
            param_schema=json.loads(spec.get("param_schema") or "{}"),
        )
        return signing.verify_api_spec(public_pem, manifest, str(spec.get("signature") or ""))
    except Exception:  # noqa: BLE001 — a verification failure means "not reusable", not a crash
        _LOG.debug("api_spec signature verification failed", exc_info=True)
        return False


def find_reusable_api_spec(url: str) -> dict[str, Any] | None:
    """Best active, signature-verified, same-domain API spec for ``url``, or None.

    Two reuse gates beyond ranking (fix #2): the endpoint host must share ``url``'s
    registrable domain, and the stored signature must verify against the author's
    key. Best-effort and read-only: None on any failure.
    """
    try:
        site_key = normalize.normalize_site_key(url)
        specs = store.get_active_api_specs(site_key)
        if not specs:
            return None
        request_host = urlparse(url).hostname or ""
        usable = [
            s for s in specs
            if api_discovery.same_registrable_domain(str(s.get("endpoint_host") or ""), request_host)
            and verify_api_spec_signature(s)
        ]
        return _select_best_api_spec(usable) if usable else None
    except Exception:  # noqa: BLE001 — read-side commons failure must not break the call
        _LOG.warning("api_spec reuse lookup failed for %s", url, exc_info=True)
        return None


def _select_best_api_spec(specs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rank specs by reusing ranking.select_best_map via a row adapter — no edit to
    ranking.py. Spec rows lack map_id/fresh_validation_count, so alias
    api_spec_id->map_id and synthesize fresh_validation_count = hit_count - drift_count.
    Per-spec challenges land in Phase F, so the challenge penalty is empty for now.
    """
    trust = _trust_by_agent({str(s.get("author_agent_id") or "") for s in specs})
    adapted = [
        dict(
            s,
            map_id=str(s.get("api_spec_id") or ""),
            fresh_validation_count=max(0, int(s.get("hit_count") or 0) - int(s.get("drift_count") or 0)),
        )
        for s in specs
    ]
    best = ranking.select_best_map(adapted, trust_by_agent=trust, open_challenges_by_map={})
    if best is None:
        return None
    best_id = best.get("api_spec_id")
    return next((s for s in specs if s.get("api_spec_id") == best_id), None)
