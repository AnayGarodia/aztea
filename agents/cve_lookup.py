"""
cve_lookup.py — Real-time CVE lookup via NIST NVD API

Supports two modes:

Mode 1: Direct CVE ID lookup (new)
  Input:  {"cve_id": "CVE-2021-44228"}
       or {"cve_ids": ["CVE-2021-44228", "CVE-2019-10744"]}
  Output: {
    "results": [per-CVE dicts],
    "billing_units_actual": int,
    # Plus top-level fields mirrored from first result when single cve_id used
  }

Mode 2: Package-based CVE search (original)
  Input:  {
    "packages": ["express@4.17.1", "lodash@4.17.20"],
    "include_patched": false
  }
  Output: {
    "results": [{
      "package": str, "version": str,
      "cve": str, "cvss": float,
      "severity": "critical|high|medium|low|none",
      "description": str,
      "published": str,
      "last_modified": str,
      "affected_range": str,
      "fixed_in": str,
      "exploit_available": bool
    }],
    "total_vulnerable": int,
    "total_packages_checked": int,
    "summary": str,
    "source": str
  }
"""

import logging
import math
import os
import re
import time
from typing import Any, Literal

import requests
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_OSV_API = "https://api.osv.dev/v1/query"
_NVD_TIMEOUT = 10
# NVD's public API allows ~5 req/s without a key; sleep 0.7s between calls.
_NVD_RATE_DELAY = 0.7
_CVE_ID_PATTERN = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)

# In-process TTL cache for CVE-id lookups. CVE records change infrequently
# (NVD aggregates upstream), so caching for 1h converts a cold 10 s NVD
# round-trip into a sub-millisecond replay — critical for staying inside
# the 8 s sync gateway budget. Keys are uppercase CVE ids; values are
# ``(expires_epoch, record)``. Capped so a pathological caller can't
# OOM the worker by listing thousands of distinct CVEs.
_CVE_CACHE_TTL_S = 3600
_CVE_CACHE_MAX_ENTRIES = 1024
_cve_cache: dict[str, tuple[float, dict]] = {}

# Hard caps surfaced as 4xx errors from `run`. Documented for callers.
_MAX_IDS_PER_CALL = 10
_MAX_PACKAGES_PER_CALL = 10
_MAX_PACKAGE_NAME_CHARS = 200
_MAX_RESULTS_RETURNED = 50
_TOP_EXPLOITS_PREVIEW = 3

# CVSS thresholds (FIRST.org).
_CVSS_CRITICAL = 9.0
_CVSS_HIGH = 7.0
_CVSS_MEDIUM = 4.0

# Substrings in NVD error messages that warrant trying OSV instead.
_NVD_FALLBACK_MARKERS = (
    "timed out",
    "could not reach",
    "rate limit",
    "returned status 5",
)
# CVE descriptions containing these markers are flagged with exploit_available=True.
_EXPLOIT_KEYWORDS = (
    "exploit",
    "poc",
    "proof-of-concept",
    "metasploit",
    "actively exploited",
)

CveLookupMode = Literal["single", "list"]


def _cvss_to_severity(score: float) -> str:
    """Pure: bucket a numeric CVSS base score into FIRST.org severity labels."""
    if score >= _CVSS_CRITICAL:
        return "critical"
    if score >= _CVSS_HIGH:
        return "high"
    if score >= _CVSS_MEDIUM:
        return "medium"
    if score > 0.0:
        return "low"
    return "none"


# CVSS v3 lookup tables — directly from the v3.1 specification
# (https://www.first.org/cvss/specification-document). Used to compute a
# numeric base score when OSV ships only a vector string.
_CVSS3_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.20}
_CVSS3_AC = {"L": 0.77, "H": 0.44}
_CVSS3_UI = {"N": 0.85, "R": 0.62}
_CVSS3_PR_U = {"N": 0.85, "L": 0.62, "H": 0.27}
_CVSS3_PR_C = {"N": 0.85, "L": 0.68, "H": 0.50}
_CVSS3_CIA = {"N": 0.0, "L": 0.22, "H": 0.56}


def _cvss3_parse_parts(vector: str) -> dict[str, str] | None:
    """Pure: split a vector string into key/value pairs, returning None when required keys are missing."""
    parts = dict(p.split(":", 1) for p in vector.split("/") if ":" in p)
    if not (parts.get("AV") and parts.get("AC") and parts.get("PR") and parts.get("UI")):
        return None
    return parts


def _cvss3_impact(parts: dict[str, str], scope: str) -> float:
    """Pure: CVSS 3.1 ISS+impact math given parsed metric letters."""
    c = _CVSS3_CIA.get(parts.get("C", "N"), 0)
    i = _CVSS3_CIA.get(parts.get("I", "N"), 0)
    a = _CVSS3_CIA.get(parts.get("A", "N"), 0)
    impact_iss = 1 - (1 - c) * (1 - i) * (1 - a)
    if scope == "C":
        return 7.52 * (impact_iss - 0.029) - 3.25 * (impact_iss - 0.02) ** 15
    return 6.42 * impact_iss


def _cvss3_from_vector(vector: str) -> float:
    """Pure: compute CVSS 3.1 base score from a vector string; 0.0 on parse error."""
    try:
        parts = _cvss3_parse_parts(vector)
        if parts is None:
            return 0.0
        scope = parts.get("S", "U")
        impact = _cvss3_impact(parts, scope)
        if impact <= 0:
            return 0.0
        av = _CVSS3_AV.get(parts["AV"], 0)
        ac = _CVSS3_AC.get(parts["AC"], 0)
        ui = _CVSS3_UI.get(parts["UI"], 0)
        pr_table = _CVSS3_PR_C if scope == "C" else _CVSS3_PR_U
        pr = pr_table.get(parts["PR"], 0)
        exploit = 8.22 * av * ac * pr * ui
        base = (1.08 * (impact + exploit)) if scope == "C" else (impact + exploit)
        return math.ceil(min(base, 10.0) * 10) / 10.0  # CVSS spec rounds up to 1 decimal
    except (ValueError, KeyError, TypeError):
        return 0.0


_OSV_SCORE_TRAIL_RE = re.compile(r"(?:^|[^\d.])(\d+\.\d+)$")


def _osv_severity_score(sev: dict[str, Any]) -> tuple[str, float]:
    """Pure: ``(version_bucket, score)`` for one OSV severity entry; ``("", 0)`` if unusable.

    Why: OSV's ``severity`` array carries both numeric scores and CVSS
    vectors; teasing the score out lets ``_parse_osv_cvss`` keep the v3-vs-v2
    preference logic in one place.
    """
    if not isinstance(sev, dict):
        return ("", 0.0)
    score_raw = str(sev.get("score") or "").strip()
    if not score_raw:
        return ("", 0.0)
    bucket = "v2" if "V2" in str(sev.get("type") or "").upper() else "v3"
    try:
        numeric = float(score_raw)
        if 0.0 <= numeric <= 10.0:
            return (bucket, numeric)
    except ValueError:
        pass
    if score_raw.upper().startswith(("CVSS:3.0", "CVSS:3.1")):
        return (bucket, _cvss3_from_vector(score_raw))
    m = _OSV_SCORE_TRAIL_RE.search(score_raw)
    if m:
        try:
            val = float(m.group(1))
            if 0.0 <= val <= 10.0:
                return (bucket, val)
        except ValueError:
            pass
    return ("", 0.0)


def _parse_osv_cvss(severity_entries: list) -> float:
    """Pure: extract the best CVSS base score from an OSV ``severity`` array.

    Why: OSV emits both numeric scores and full CVSS vector strings. Vector
    strings need ``_cvss3_from_vector`` to produce a number — without that
    step the agent would silently report 0.0. Prefer v3 over v2 when both
    are present.
    """
    best_v3 = 0.0
    best_v2 = 0.0
    for sev in severity_entries or []:
        bucket, score = _osv_severity_score(sev)
        if bucket == "v3":
            best_v3 = max(best_v3, score)
        elif bucket == "v2":
            best_v2 = max(best_v2, score)
    return best_v3 if best_v3 > 0 else best_v2


def _extract_cvss(metrics: dict) -> float:
    """Pure: pull a numeric CVSS score from NVD metrics, preferring v3.1."""
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key, [])
        if entries:
            try:
                return float(entries[0]["cvssData"]["baseScore"])
            except (KeyError, IndexError, TypeError, ValueError):
                pass
    return 0.0


_SUPPORTED_ECOSYSTEMS = {"npm", "pypi", "python", "pip", "auto", ""}
_KNOWN_UNSUPPORTED_ECOSYSTEMS = {
    "maven", "cargo", "gradle", "gomod", "go", "nuget", "rubygems",
    "packagist", "composer", "hex", "pub", "swift",
}


class UnsupportedEcosystemError(ValueError):
    """Raised when the caller requests an OSV ecosystem we don't query.

    1.7.0 contract: returning empty results for an unsupported ecosystem
    is a false negative on a security tool — every Maven query for
    log4j-core 2.14 came back as "0 vulnerabilities" instead of an
    actionable error. The agent's run() catches this and returns a
    structured error envelope per CLAUDE.md.
    """


def _pkg_ecosystems(pkg_name: str, hint: str | None = None) -> list[str]:
    """Return OSV ecosystem candidates for a package name.

    ``hint`` may be ``"npm"`` / ``"pypi"`` / ``None`` (or ``"auto"``). When the
    caller is explicit we honour it and skip the multi-ecosystem fan-out;
    otherwise we use the package-name shape to guess (scoped or path-style
    names are npm-only) and fall back to trying both ecosystems.

    Raises ``UnsupportedEcosystemError`` for ecosystems we know exist
    in OSV but explicitly do not query (Maven, Cargo, Go, etc.). The
    agent's run() converts this to a structured error so buyers see
    "unsupported_ecosystem" instead of an empty-results false negative.
    """
    h = (hint or "").strip().lower()
    if h in _KNOWN_UNSUPPORTED_ECOSYSTEMS:
        raise UnsupportedEcosystemError(
            f"ecosystem '{h}' is recognised but not queried by this agent; "
            "supported ecosystems: npm, pypi"
        )
    if h == "npm":
        return ["npm"]
    if h in {"pypi", "python", "pip"}:
        return ["PyPI"]
    if pkg_name.startswith("@") or "/" in pkg_name:
        return ["npm"]
    return ["PyPI", "npm"]


_OSV_USER_AGENT = "aztea-cve-lookup/1.0"
_OSV_DESCRIPTION_MAX_CHARS = 400
_DATE_PREFIX_CHARS = 10  # "YYYY-MM-DD"


def _osv_extract_fixed_in(vuln: dict) -> str:
    """Pure: first ``fixed`` event from OSV's affected/ranges tree, '' if none."""
    for affected in vuln.get("affected") or []:
        for r in affected.get("ranges") or []:
            for ev in r.get("events") or []:
                if "fixed" in ev:
                    return ev["fixed"]
    return ""


def _osv_vuln_to_record(vuln: dict) -> dict | None:
    """Pure: shape one OSV vuln record into the agent's CVE schema; ``None`` if id missing."""
    vuln_id = vuln.get("id", "")
    aliases = vuln.get("aliases") or []
    cve_id = next((a for a in aliases if a.startswith("CVE-")), vuln_id)
    if not cve_id:
        return None
    cvss = _parse_osv_cvss(vuln.get("severity") or [])
    return {
        "cve": cve_id,
        "cvss": cvss,
        "severity": _cvss_to_severity(cvss),
        "description": (vuln.get("summary") or vuln.get("details") or "")[:_OSV_DESCRIPTION_MAX_CHARS],
        "published": (vuln.get("published") or "")[:_DATE_PREFIX_CHARS],
        "last_modified": (vuln.get("modified") or "")[:_DATE_PREFIX_CHARS],
        "fixed_in": _osv_extract_fixed_in(vuln),
    }


def _osv_fetch(pkg_name: str, version: str, ecosystem: str) -> list[dict]:
    """Side-effect: POST to OSV.dev and return the raw ``vulns`` array."""
    body: dict = {"package": {"name": pkg_name, "ecosystem": ecosystem}}
    if version:
        body["version"] = version
    try:
        resp = requests.post(
            _OSV_API, json=body, timeout=_NVD_TIMEOUT,
            headers={"User-Agent": _OSV_USER_AGENT},
        )
    except Exception:
        _LOG.warning("OSV query failed for %s/%s", ecosystem, pkg_name, exc_info=True)
        return []
    if resp.status_code != 200:
        _LOG.info("OSV non-200 for %s: status=%s", pkg_name, resp.status_code)
        return []
    try:
        return resp.json().get("vulns", []) or []
    except ValueError:
        _LOG.warning("OSV non-JSON for %s", pkg_name, exc_info=True)
        return []


def _query_osv(
    pkg_name: str, version: str, ecosystem_hint: str | None = None
) -> list[dict]:
    """Side-effect: query OSV.dev for CVEs, deduped by CVE id across ecosystems.

    Why: with no ecosystem hint we try both PyPI and npm because OSV is
    ecosystem-keyed; the dedupe makes the dual-fetch invisible to callers.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for ecosystem in _pkg_ecosystems(pkg_name, ecosystem_hint):
        for vuln in _osv_fetch(pkg_name, version, ecosystem):
            record = _osv_vuln_to_record(vuln)
            if record is None or record["cve"] in seen:
                continue
            seen.add(record["cve"])
            out.append(record)
    return out


def _hydrate_cve_details(cve_id: str, fallback: dict) -> dict:
    """Hydrate package-mode matches from NVD so severity is canonical across modes."""
    detailed = _fetch_cve(cve_id)
    if "error" in detailed:
        return dict(fallback)
    cvss = float(detailed.get("cvss") or 0.0)
    return {
        "cve": str(detailed.get("cve_id") or cve_id),
        "cvss": cvss,
        "severity": str(detailed.get("severity") or _cvss_to_severity(cvss)),
        "description": str(
            detailed.get("description") or fallback.get("description") or ""
        )[:400],
        "published": str(detailed.get("published") or fallback.get("published") or "")[
            :10
        ],
        "last_modified": str(
            detailed.get("last_modified") or fallback.get("last_modified") or ""
        )[:10],
        "fixed_in": str(fallback.get("fixed_in") or ""),
        "source": "osv+nvd",
    }


def _nvd_headers() -> dict:
    """Build NVD request headers, including API key if configured via NVD_API_KEY env var."""
    headers = {"User-Agent": "aztea-cve-lookup/1.0"}
    nvd_key = os.environ.get("NVD_API_KEY")
    if nvd_key:
        headers["apiKey"] = nvd_key
    return headers


_NVD_KEYWORD_RESULTS_PER_PAGE = 20
_CVE_REFERENCES_LIMIT = 5


def _nvd_vuln_to_record(vuln_item: dict) -> dict:
    """Pure: shape one NVD ``vulnerabilities[]`` row into the agent's CVE schema."""
    cve_obj = vuln_item.get("cve", {})
    cvss = _extract_cvss(cve_obj.get("metrics", {}))
    desc = next(
        (d["value"] for d in cve_obj.get("descriptions", []) if d.get("lang") == "en"),
        "",
    )[:_OSV_DESCRIPTION_MAX_CHARS]
    return {
        "cve": cve_obj.get("id", ""),
        "cvss": cvss,
        "severity": _cvss_to_severity(cvss),
        "description": desc,
        "published": cve_obj.get("published", "")[:_DATE_PREFIX_CHARS],
        "last_modified": cve_obj.get("lastModified", "")[:_DATE_PREFIX_CHARS],
        "fixed_in": "",
    }


def _search_nvd_packages(pkg_name: str) -> tuple[list[dict], bool]:
    """Side-effect: search NVD by keyword. Returns ``(results, reached_nvd)``.

    Why: ``reached_nvd=False`` signals "NVD was unreachable" so the caller
    can fall back to OSV without misreporting "no CVEs found".
    """
    try:
        resp = requests.get(
            _NVD_API,
            params={"keywordSearch": pkg_name, "resultsPerPage": _NVD_KEYWORD_RESULTS_PER_PAGE},
            timeout=_NVD_TIMEOUT,
            headers=_nvd_headers(),
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            return [], False
        if resp.status_code != 200:
            _LOG.info("NVD non-200 for %s: status=%s", pkg_name, resp.status_code)
            return [], False
        data = resp.json()
    except Exception:
        _LOG.warning("NVD query failed for %s", pkg_name, exc_info=True)
        return [], False
    return [_nvd_vuln_to_record(v) for v in data.get("vulnerabilities", [])], True


def _nvd_request_cve(cve_id: str) -> tuple[dict | None, dict | None]:
    """Side-effect: GET one CVE by id. Returns ``(data, error_envelope)`` — exactly one is non-None."""
    try:
        resp = requests.get(
            _NVD_API, params={"cveId": cve_id},
            timeout=_NVD_TIMEOUT, headers=_nvd_headers(),
        )
    except requests.exceptions.Timeout:
        return None, {"cve_id": cve_id, "error": "NVD API timed out"}
    except Exception as e:
        return None, {"cve_id": cve_id, "error": f"Could not reach NVD API: {type(e).__name__}"}
    if resp.status_code == 404:
        return None, {"cve_id": cve_id, "error": "not found"}
    if resp.status_code == 429:
        return None, {"cve_id": cve_id, "error": "NVD API rate limit reached"}
    if resp.status_code != 200:
        return None, {"cve_id": cve_id, "error": f"NVD API returned status {resp.status_code}"}
    return resp.json(), None


def _nvd_extract_cwes(cve: dict) -> list[str]:
    """Pure: pluck the CWE-* identifiers out of NVD's weaknesses/description tree."""
    out: list[str] = []
    for w in cve.get("weaknesses", []):
        for wd in w.get("description", []):
            if wd.get("lang") == "en" and wd.get("value", "").startswith("CWE-"):
                out.append(wd["value"])
    return out


def _nvd_data_to_record(cve_id: str, data: dict) -> dict:
    """Pure: shape an NVD response payload into the agent's detailed-CVE schema."""
    vulns = data.get("vulnerabilities", [])
    if not vulns:
        return {"cve_id": cve_id, "error": "not found"}
    cve = vulns[0].get("cve", {})
    cvss = _extract_cvss(cve.get("metrics", {}))
    desc = next(
        (d["value"] for d in cve.get("descriptions", []) if d.get("lang") == "en"),
        "",
    )
    references = [ref.get("url", "") for ref in cve.get("references", [])[:_CVE_REFERENCES_LIMIT]]
    return {
        "cve_id": cve.get("id", cve_id),
        "cvss": cvss,
        "severity": _cvss_to_severity(cvss),
        "description": desc,
        "published": cve.get("published", "")[:_DATE_PREFIX_CHARS],
        "last_modified": cve.get("lastModified", "")[:_DATE_PREFIX_CHARS],
        "cwe_ids": _nvd_extract_cwes(cve),
        "references": references,
        "source": "nvd",
    }


def _cache_get(cve_id: str) -> dict | None:
    """Pure-ish: return a cached CVE record if still fresh, else None.

    Why: NVD round-trips routinely cost 8-10 s for a single id — well past
    the 8 s sync budget. Serving repeat lookups from in-process cache
    converts those into sub-millisecond replays and was the primary cause
    of the cve_lookup ``timed out`` reports in the 2026-05-18 test.
    """
    entry = _cve_cache.get(cve_id.upper())
    if not entry:
        return None
    expires_at, record = entry
    if time.time() >= expires_at:
        _cve_cache.pop(cve_id.upper(), None)
        return None
    return record


def _cache_put(cve_id: str, record: dict) -> None:
    """Side-effect: cache a successful lookup; never caches error envelopes."""
    if record.get("error"):
        return
    if len(_cve_cache) >= _CVE_CACHE_MAX_ENTRIES:
        # Drop the oldest entry. Simple FIFO is enough — LRU would be over-
        # engineered for a 1024-entry bound.
        oldest = next(iter(_cve_cache))
        _cve_cache.pop(oldest, None)
    _cve_cache[cve_id.upper()] = (time.time() + _CVE_CACHE_TTL_S, record)


def _fetch_cve(cve_id: str) -> dict:
    """Side-effect: fetch one CVE by id from NVD; returns the agent's CVE schema or ``{cve_id, error}``.

    Memoized for ``_CVE_CACHE_TTL_S`` seconds so repeated queries within
    the same worker process don't pay the NVD round-trip twice.
    """
    cached = _cache_get(cve_id)
    if cached is not None:
        return cached
    data, err = _nvd_request_cve(cve_id)
    if err is not None:
        return err
    record = _nvd_data_to_record(cve_id, data or {})
    _cache_put(cve_id, record)
    return record


def _fetch_cve_from_osv(cve_id: str) -> dict:
    try:
        resp = requests.get(
            f"https://api.osv.dev/v1/vulns/{cve_id}",
            timeout=_NVD_TIMEOUT,
            headers={"User-Agent": "aztea-cve-lookup/1.0"},
        )
        if resp.status_code == 404:
            return {"cve_id": cve_id, "error": "not found"}
        if resp.status_code != 200:
            return {
                "cve_id": cve_id,
                "error": f"OSV returned status {resp.status_code}",
            }
        data = resp.json()
    except Exception as exc:
        return {
            "cve_id": cve_id,
            "error": f"Could not reach OSV API: {type(exc).__name__}",
        }

    aliases = data.get("aliases") or []
    cvss = _parse_osv_cvss(data.get("severity") or [])
    return {
        "cve_id": next(
            (alias for alias in aliases if str(alias).startswith("CVE-")), cve_id
        ),
        "cvss": cvss,
        "severity": _cvss_to_severity(cvss),
        "description": (data.get("summary") or data.get("details") or "")[:1200],
        "published": str(data.get("published") or "")[:10],
        "last_modified": str(data.get("modified") or "")[:10],
        "cwe_ids": [],
        "references": [
            ref.get("url", "")
            for ref in (data.get("references") or [])[:5]
            if ref.get("url")
        ],
        "source": "osv",
    }


def _parse_package_version(pkg: str) -> tuple[str, str]:
    if "@" in pkg:
        parts = pkg.rsplit("@", 1)
        return parts[0].strip(), parts[1].strip()
    if "==" in pkg:
        parts = pkg.split("==", 1)
        return parts[0].strip(), parts[1].strip()
    return pkg.strip(), ""


def _version_in_range(version: str, affected_range: str) -> bool:
    """Very lightweight semver range check for < X.Y.Z style ranges."""
    if not version or not affected_range:
        return True
    m = re.match(r"<\s*(\d+\.\d+(?:\.\d+)?)", affected_range)
    if not m:
        return True
    try:
        threshold = tuple(int(x) for x in m.group(1).split("."))
        current = tuple(int(x) for x in version.split(".")[:3])
        return current < threshold
    except ValueError:
        return True


def _should_fallback_to_osv(error_msg: str) -> bool:
    """Pure: True when an NVD error message looks transient (warrants OSV retry)."""
    lowered = error_msg.lower()
    return any(marker in lowered for marker in _NVD_FALLBACK_MARKERS)


def _fetch_cve_with_fallback(cve_id: str) -> dict:
    """Side-effect: NVD primary, OSV fallback on transient NVD failures."""
    result = _fetch_cve(cve_id)
    if result.get("error") and _should_fallback_to_osv(str(result.get("error") or "")):
        fallback = _fetch_cve_from_osv(cve_id)
        if "error" not in fallback:
            return fallback
    return result


def _normalize_cve_id_keys_inplace(results: list[dict]) -> None:
    """Side-effect (mutating): set both ``cve`` and ``cve_id`` on every record.

    Why: NVD vs OSV produce different key names; downstream renderers and
    judges break on the path-dependent shape, so we mirror both keys.
    """
    for r in results:
        cve_value = r.get("cve") or r.get("cve_id")
        if cve_value:
            r.setdefault("cve", cve_value)
            r.setdefault("cve_id", cve_value)


def _run_cve_id_mode(cve_ids: list[str], *, mode: CveLookupMode) -> dict:
    """Side-effect: orchestrate NVD lookups for each CVE id and shape the response."""
    results: list[dict] = []
    for cve_id in cve_ids:
        results.append(_fetch_cve_with_fallback(cve_id))
        time.sleep(_NVD_RATE_DELAY)
    _normalize_cve_id_keys_inplace(results)
    successful = [r for r in results if "error" not in r]
    if not successful and results:
        return _err("cve_lookup.not_found", "No matching CVE records were found.")
    output: dict[str, Any] = {
        "results": results,
        "billing_units_actual": len(successful),
    }
    # Mirror first result at top level for the legacy single-id call shape.
    if mode == "single" and successful:
        for key, val in successful[0].items():
            output[key] = val
    return output


def _validate_cve_ids(
    cve_id_single: Any, cve_ids_list: Any
) -> tuple[list[str], CveLookupMode] | dict:
    """Pure: normalize the cve_id / cve_ids inputs. Returns ``(ids, mode)`` or an error envelope."""
    if cve_id_single is not None and cve_ids_list is not None:
        return _err(
            "cve_lookup.mutually_exclusive_ids",
            "Pass either cve_id or cve_ids, not both.",
        )
    if cve_ids_list:
        ids_to_lookup, mode = cve_ids_list, "list"
    elif cve_id_single is not None:
        ids_to_lookup, mode = [cve_id_single], "single"
    else:
        return _err("cve_lookup.missing_id", "cve_id or cve_ids is required")
    if not isinstance(ids_to_lookup, list):
        return _err("cve_lookup.invalid_input", "cve_ids must be a list of CVE ID strings")
    if len(ids_to_lookup) > _MAX_IDS_PER_CALL:
        return _err(
            "cve_lookup.too_many_ids",
            f"At most {_MAX_IDS_PER_CALL} CVE IDs can be looked up per call. "
            f"You provided {len(ids_to_lookup)}.",
        )
    normalized: list[str] = []
    for raw_id in ids_to_lookup:
        if not isinstance(raw_id, str):
            return _err(
                "cve_lookup.invalid_input",
                f"Each CVE ID must be a string, got: {type(raw_id).__name__}",
            )
        upper_id = raw_id.strip().upper()
        if not _CVE_ID_PATTERN.match(upper_id):
            return _err(
                "cve_lookup.invalid_id_format",
                f"Invalid CVE ID format: {raw_id!r}. Expected pattern: CVE-YYYY-NNNNN",
            )
        normalized.append(upper_id)
    return (normalized, mode)


def _validate_package_inputs(packages: Any) -> dict | None:
    """Pure: validate the packages list shape; ``None`` if valid, error envelope otherwise."""
    if not isinstance(packages, list):
        return _err(
            "cve_lookup.invalid_input",
            'packages must be a list of strings (e.g. ["express@4.17.1"])',
        )
    if not packages:
        return _err(
            "cve_lookup.missing_input",
            "Provide one of: cve_id (single), cve_ids (list), or packages (list of name@version).",
        )
    if len(packages) > _MAX_PACKAGES_PER_CALL:
        return _err(
            "cve_lookup.too_many_packages",
            f"At most {_MAX_PACKAGES_PER_CALL} packages can be checked per call. "
            f"You provided {len(packages)}.",
        )
    for raw_pkg in packages:
        if not isinstance(raw_pkg, str):
            return _err(
                "cve_lookup.invalid_input",
                'Each package must be a string like "express@4.17.1".',
            )
        if len(raw_pkg) > _MAX_PACKAGE_NAME_CHARS:
            return _err(
                "cve_lookup.package_name_too_long",
                f"Package name is too long (max {_MAX_PACKAGE_NAME_CHARS} chars): "
                f"{raw_pkg[:40]}...",
            )
    return None


def _has_exploit_marker(description: str) -> bool:
    """Pure: True when the description contains a known exploit-availability keyword."""
    lowered = (description or "").lower()
    return any(kw in lowered for kw in _EXPLOIT_KEYWORDS)


def _build_pkg_result(
    pkg_name: str, pkg_version: str, hydrated: dict, fallback_fixed_in: str
) -> dict:
    """Pure: shape one package-mode CVE row for the response array."""
    fixed_in = hydrated.get("fixed_in") or fallback_fixed_in or ""
    return {
        "package": pkg_name,
        "version": pkg_version or "unknown",
        "cve": hydrated["cve"],
        "cvss": hydrated["cvss"],
        "severity": hydrated["severity"],
        "description": hydrated["description"],
        "published": hydrated["published"],
        "last_modified": hydrated["last_modified"],
        "affected_range": f"< {fixed_in}" if fixed_in else "see advisory",
        "fixed_in": fixed_in or "see advisory",
        "exploit_available": _has_exploit_marker(str(hydrated.get("description") or "")),
    }


def _audit_one_package(
    raw_pkg: str, *, ecosystem_hint: str | None, include_patched: bool, seen_cves: set[str]
) -> tuple[list[dict], bool]:
    """Side-effect: query OSV+NVD for one package; returns ``(rows, used_nvd_hydration)``."""
    pkg_name, pkg_version = _parse_package_version(str(raw_pkg))
    pkg_cves = _query_osv(pkg_name, pkg_version, ecosystem_hint)
    rows: list[dict] = []
    for item in pkg_cves:
        if item["cve"] in seen_cves:
            continue
        seen_cves.add(item["cve"])
        hydrated = _hydrate_cve_details(item["cve"], item)
        time.sleep(_NVD_RATE_DELAY)
        fixed_in = hydrated.get("fixed_in") or item.get("fixed_in") or ""
        if not include_patched and fixed_in and pkg_version:
            if not _version_in_range(pkg_version, f"< {fixed_in}"):
                continue
        rows.append(_build_pkg_result(pkg_name, pkg_version, hydrated, fixed_in))
    return rows, bool(pkg_cves)


def _summarise_package_run(all_results: list[dict]) -> str:
    """Pure: human-readable summary line for the package-mode response."""
    severity_counts = {
        sev: sum(1 for r in all_results if r["severity"] == sev)
        for sev in ("critical", "high", "medium")
    }
    pkg_names = {r["package"] for r in all_results}
    parts = [f"Found {len(all_results)} CVE(s) across {len(pkg_names)} package(s)."]
    for sev in ("critical", "high", "medium"):
        if severity_counts[sev]:
            parts.append(f"{severity_counts[sev]} {sev}")
    exploitable = [r["cve"] for r in all_results if r["exploit_available"]]
    if exploitable:
        parts.append(f"Exploits known for: {', '.join(exploitable[:_TOP_EXPLOITS_PREVIEW])}.")
    return " ".join(parts)


def _ecosystem_hint(payload: dict) -> str | None:
    """Pure: normalize the ``ecosystem`` payload key to ``"npm"|"pypi"|None``."""
    raw = str(payload.get("ecosystem") or "auto").strip().lower() or "auto"
    return None if raw in {"", "auto"} else raw


def _run_package_mode(payload: dict) -> dict:
    """Side-effect: orchestrate package-mode lookups and shape the response."""
    packages = payload.get("packages") or []
    err = _validate_package_inputs(packages)
    if err is not None:
        return err
    include_patched = bool(payload.get("include_patched", False))
    ecosystem_hint = _ecosystem_hint(payload)
    all_results: list[dict] = []
    seen_cves: set[str] = set()
    used_source = "osv"
    for raw_pkg in packages:
        try:
            rows, hit = _audit_one_package(
                str(raw_pkg),
                ecosystem_hint=ecosystem_hint,
                include_patched=include_patched,
                seen_cves=seen_cves,
            )
        except UnsupportedEcosystemError as exc:
            # 1.7.0 contract: surface unsupported-ecosystem queries as
            # structured errors instead of returning 0 vulnerabilities
            # (which is a false negative on a security tool — buyers
            # treat empty results as "your packages are safe" when in
            # fact the agent never queried OSV at all).
            return {
                "error": {
                    "code": "cve_lookup.unsupported_ecosystem",
                    "message": str(exc),
                    "details": {
                        "ecosystem_hint": ecosystem_hint,
                        "supported": ["npm", "pypi"],
                        "package": str(raw_pkg),
                    },
                }
            }
        if hit:
            used_source = "osv+nvd"
        all_results.extend(rows)
    all_results.sort(key=lambda x: x["cvss"], reverse=True)
    pkg_names_with_vulns = {r["package"] for r in all_results}
    return {
        "results": all_results[:_MAX_RESULTS_RETURNED],
        "total_vulnerable": len(pkg_names_with_vulns),
        "total_packages_checked": len(packages),
        "summary": _summarise_package_run(all_results),
        "source": used_source,
        "billing_units_actual": len(all_results),
    }


def run(payload: dict) -> dict:
    """Look up CVE details from NIST NVD with OSV.dev as fallback / primary for package mode.

    Two mutually-exclusive modes:
    - ``cve_id`` (str) or ``cve_ids`` (list[str]) — direct id lookup.
    - ``packages`` (list[str like "express@4.17.1"]) — package + version search.

    Why: NVD's keyword search produces high false-positive rates (querying
    "express" returns Outlook Express); OSV is ecosystem-aware and precise.
    NVD is preferred only for direct CVE-id details where its enrichment
    wins.
    """
    # NEW-5 (sweep 2026-05-20): structured envelope, not bare TypeError —
    # the dispatch layer surfaces uncaught raises as HTTP 500 with a
    # stack trace. Other agents (secret_scanner, dockerfile_analyzer, …)
    # return a structured error here; do the same for consistency.
    if not isinstance(payload, dict):
        return _err(
            "cve_lookup.invalid_payload",
            f"payload must be dict, got {type(payload).__name__}",
        )
    cve_id_single = payload.get("cve_id")
    cve_ids_list = payload.get("cve_ids")
    if cve_id_single is not None or cve_ids_list is not None:
        validated = _validate_cve_ids(cve_id_single, cve_ids_list)
        if isinstance(validated, dict):  # error envelope
            return validated
        ids, mode = validated
        return _run_cve_id_mode(ids, mode=mode)
    return _run_package_mode(payload)
