"""Shared signed site-map commons (Phase 1 of the agent-readable-web build).

Public surface re-exported so callers can ``from core import site_maps`` and
reach the pieces without knowing the file split:

  normalize  — site_key / fingerprints (pure)
  signing    — Ed25519 manifest sign/verify (wraps core.crypto)
  ranking    — reputation-weighted selection of competing maps (pure)
  freshness  — validate-before-replay policy (pure, callback-driven)
  store        — DB persistence + the consumer_job_id royalty idempotency anchor
  api_discovery — compile-a-site-into-an-API: capture, sign-ready split, SSRF-gated replay

KNOWN DEBT: payouts (royalty settlement) and the live navigator/dispatch/HTTP
wiring (sub-phases 1C-1G) are intentionally NOT in this package yet — they touch
the money hot path and land as a focused, separately-reviewed slice.
"""

from __future__ import annotations

# Import order matters: normalize is a leaf; signing/ranking/freshness depend on
# normalize; store depends on signing; api_discovery depends only on outbound_session
# + url_security (a near-leaf); authoring depends on store+ranking+api_discovery.
from core.site_maps import normalize, graph, signing, ranking, freshness, store, api_discovery, authoring

__all__ = [
    "normalize", "graph", "signing", "ranking", "freshness", "store",
    "api_discovery", "authoring",
]
