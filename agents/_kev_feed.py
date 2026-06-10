"""CISA Known Exploited Vulnerabilities (KEV) feed client.

# OWNS: fetching + caching the CISA KEV catalog and answering "is this CVE
#       on the federal known-exploited list?" for the cve_lookup agent.
# NOT OWNS: deciding what exploit status means for severity/ranking — the
#       agent combines KEV with its keyword heuristic and labels the source.
# INVARIANTS:
#   * Never fetches at import time — the first caller pays the fetch.
#   * ``kev_entries`` returns None on ANY fetch/parse failure so callers
#     degrade to their heuristic; it never raises and never returns a
#     partially-parsed catalog.
#   * The feed URL is a constant — no caller-supplied URLs, so this module
#     is outside core/url_security's SSRF surface (same posture as the
#     constant NVD/OSV endpoints in cve_lookup).
# DECISIONS:
#   - 6h TTL: CISA updates the catalog a few times per week; 6h keeps a
#     long-lived worker at most half a day stale while capping feed load
#     at 4 fetches/day/worker.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

_LOG = logging.getLogger(__name__)

_KEV_FEED_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
)
_KEV_TTL_S = 6 * 3600
_KEV_TIMEOUT_S = 10
_USER_AGENT = "aztea-cve-lookup/1.0"

# Module-level cache: (expires_epoch, {cve_id: entry}). A failed fetch is
# cached for a short cooldown so a dead feed doesn't add 10s of timeout
# latency to every cve_lookup call.
_FETCH_FAILURE_COOLDOWN_S = 300
_kev_cache: tuple[float, dict[str, dict] | None] | None = None
# Agents run on worker threads; without this lock, N concurrent cve_lookup
# calls arriving on a cold/expired cache would each fetch + parse the
# multi-MB catalog (thundering herd). The lock collapses that to one fetch.
_kev_lock = threading.Lock()


def _parse_catalog(payload: dict) -> dict[str, dict]:
    """Pure: index the KEV JSON document by uppercase CVE id."""
    indexed: dict[str, dict] = {}
    for vuln in payload.get("vulnerabilities") or []:
        cve_id = str(vuln.get("cveID") or "").strip().upper()
        if not cve_id:
            continue
        indexed[cve_id] = {
            "date_added": str(vuln.get("dateAdded") or ""),
            "ransomware": str(vuln.get("knownRansomwareCampaignUse") or "").lower()
            == "known",
        }
    return indexed


def _fetch_catalog() -> dict[str, dict] | None:
    """Side-effect: GET the KEV feed; None on any failure (callers degrade)."""
    try:
        resp = requests.get(
            _KEV_FEED_URL, timeout=_KEV_TIMEOUT_S, headers={"User-Agent": _USER_AGENT}
        )
    except Exception:
        _LOG.warning("KEV feed fetch failed", exc_info=True)
        return None
    if resp.status_code != 200:
        _LOG.warning("KEV feed returned status %s", resp.status_code)
        return None
    try:
        catalog = _parse_catalog(resp.json())
    except ValueError:
        _LOG.warning("KEV feed returned non-JSON body", exc_info=True)
        return None
    # An empty catalog means the feed shape changed under us — treat as a
    # failure rather than reporting "nothing is exploited" with confidence.
    return catalog or None


def kev_entries(cve_ids: list[str]) -> dict[str, dict] | None:
    """Look up KEV entries for the given CVE ids (TTL-cached catalog).

    Returns ``{cve_id: {"date_added", "ransomware"}}`` containing ONLY the
    ids present in the catalog, or ``None`` when the catalog is currently
    unavailable (caller must degrade to its heuristic, not assume "clean").
    """
    global _kev_cache
    now = time.time()
    if _kev_cache is None or now >= _kev_cache[0]:
        with _kev_lock:
            # Double-checked: another thread may have refreshed while we
            # waited on the lock, so re-test before fetching.
            if _kev_cache is None or time.time() >= _kev_cache[0]:
                catalog = _fetch_catalog()
                ttl = _KEV_TTL_S if catalog is not None else _FETCH_FAILURE_COOLDOWN_S
                _kev_cache = (now + ttl, catalog)
    catalog = _kev_cache[1]
    if catalog is None:
        return None
    return {
        cid.upper(): catalog[cid.upper()] for cid in cve_ids if cid.upper() in catalog
    }


def reset_cache() -> None:
    """Side-effect: drop the cached catalog. Test seam only."""
    global _kev_cache
    _kev_cache = None
