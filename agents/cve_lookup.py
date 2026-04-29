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

import os
import re
import time

import requests

_NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_OSV_API = "https://api.osv.dev/v1/query"
_NVD_TIMEOUT = 10
_NVD_RATE_DELAY = 0.7  # NVD public API allows ~5 req/s without key
_CVE_ID_PATTERN = re.compile(r'^CVE-\d{4}-\d{4,}$', re.IGNORECASE)


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _cvss_to_severity(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0.0:
        return "low"
    return "none"


def _extract_cvss(metrics: dict) -> float:
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key, [])
        if entries:
            try:
                return float(entries[0]["cvssData"]["baseScore"])
            except (KeyError, IndexError, TypeError, ValueError):
                pass
    return 0.0


def _pkg_ecosystems(pkg_name: str, hint: str | None = None) -> list[str]:
    """Return OSV ecosystem candidates for a package name.

    ``hint`` may be ``"npm"`` / ``"pypi"`` / ``None`` (or ``"auto"``). When the
    caller is explicit we honour it and skip the multi-ecosystem fan-out;
    otherwise we use the package-name shape to guess (scoped or path-style
    names are npm-only) and fall back to trying both ecosystems.
    """
    h = (hint or "").strip().lower()
    if h == "npm":
        return ["npm"]
    if h in {"pypi", "python", "pip"}:
        return ["PyPI"]
    if pkg_name.startswith("@") or "/" in pkg_name:
        return ["npm"]
    return ["PyPI", "npm"]


def _query_osv(pkg_name: str, version: str, ecosystem_hint: str | None = None) -> list[dict]:
    """Query OSV.dev for CVEs affecting a specific package (+ optional version).

    Pass ``ecosystem_hint`` (``"npm"`` / ``"pypi"``) to narrow the lookup to a
    single ecosystem. Without a hint the function tries both PyPI and npm in
    sequence and de-duplicates by CVE ID — that's the right default when the
    caller hasn't expressed a preference.
    """
    seen_ids: set[str] = set()
    results: list[dict] = []

    for ecosystem in _pkg_ecosystems(pkg_name, ecosystem_hint):
        try:
            body: dict = {"package": {"name": pkg_name, "ecosystem": ecosystem}}
            if version:
                body["version"] = version
            resp = requests.post(
                _OSV_API,
                json=body,
                timeout=_NVD_TIMEOUT,
                headers={"User-Agent": "aztea-cve-lookup/1.0"},
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue

        for vuln in data.get("vulns", []):
            vuln_id = vuln.get("id", "")
            aliases = vuln.get("aliases") or []
            cve_id = next((a for a in aliases if a.startswith("CVE-")), vuln_id)
            if cve_id in seen_ids:
                continue
            seen_ids.add(cve_id)

            cvss = 0.0
            for sev in vuln.get("severity") or []:
                score_str = sev.get("score", "")
                m = re.search(r"(\d+\.\d+)$", score_str)
                if m:
                    try:
                        cvss = float(m.group(1))
                        break
                    except ValueError:
                        pass

            fixed_in = ""
            for affected in vuln.get("affected") or []:
                for r in affected.get("ranges") or []:
                    for ev in r.get("events") or []:
                        if "fixed" in ev:
                            fixed_in = ev["fixed"]
                            break
                    if fixed_in:
                        break
                if fixed_in:
                    break

            summary = (vuln.get("summary") or vuln.get("details") or "")[:400]
            results.append({
                "cve": cve_id,
                "cvss": cvss,
                "severity": _cvss_to_severity(cvss),
                "description": summary,
                "published": (vuln.get("published") or "")[:10],
                "last_modified": (vuln.get("modified") or "")[:10],
                "fixed_in": fixed_in,
            })

    return results


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
        "description": str(detailed.get("description") or fallback.get("description") or "")[:400],
        "published": str(detailed.get("published") or fallback.get("published") or "")[:10],
        "last_modified": str(detailed.get("last_modified") or fallback.get("last_modified") or "")[:10],
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


def _search_nvd_packages(pkg_name: str) -> tuple[list[dict], bool]:
    """Search NVD for CVEs affecting a package by keyword.

    Returns (results, reached_nvd). reached_nvd=False means caller should fall back to OSV.
    """
    try:
        resp = requests.get(
            _NVD_API,
            params={"keywordSearch": pkg_name, "resultsPerPage": 20},
            timeout=_NVD_TIMEOUT,
            headers=_nvd_headers(),
        )
        # Treat 429/5xx as "NVD unreachable" so caller can fall back to OSV
        if resp.status_code == 429 or resp.status_code >= 500:
            return [], False
        if resp.status_code != 200:
            return [], False
        data = resp.json()
    except Exception:
        return [], False

    results = []
    for vuln_item in data.get("vulnerabilities", []):
        cve_obj = vuln_item.get("cve", {})
        cve_id = cve_obj.get("id", "")
        metrics = cve_obj.get("metrics", {})
        cvss = _extract_cvss(metrics)
        descriptions = cve_obj.get("descriptions", [])
        desc = next((d["value"] for d in descriptions if d.get("lang") == "en"), "")[:400]
        results.append({
            "cve": cve_id,
            "cvss": cvss,
            "severity": _cvss_to_severity(cvss),
            "description": desc,
            "published": cve_obj.get("published", "")[:10],
            "last_modified": cve_obj.get("lastModified", "")[:10],
            "fixed_in": "",
        })
    return results, True


def _fetch_cve(cve_id: str) -> dict:
    """Fetch a single CVE by its ID from the NIST NVD API.

    Returns a dict with CVE fields on success, or {"cve_id": ..., "error": ...} on failure.
    """
    try:
        resp = requests.get(
            _NVD_API,
            params={"cveId": cve_id},
            timeout=_NVD_TIMEOUT,
            headers=_nvd_headers(),
        )
        if resp.status_code == 404:
            return {"cve_id": cve_id, "error": "not found"}
        if resp.status_code == 429:
            return {"cve_id": cve_id, "error": "NVD API rate limit reached"}
        if resp.status_code != 200:
            return {"cve_id": cve_id, "error": f"NVD API returned status {resp.status_code}"}
        data = resp.json()
    except requests.exceptions.Timeout:
        return {"cve_id": cve_id, "error": "NVD API timed out"}
    except Exception as e:
        return {"cve_id": cve_id, "error": f"Could not reach NVD API: {type(e).__name__}"}

    vulns = data.get("vulnerabilities", [])
    if not vulns:
        return {"cve_id": cve_id, "error": "not found"}

    cve = vulns[0].get("cve", {})
    metrics = cve.get("metrics", {})
    cvss = _extract_cvss(metrics)
    severity = _cvss_to_severity(cvss)
    descriptions = cve.get("descriptions", [])
    desc = next((d["value"] for d in descriptions if d.get("lang") == "en"), "")

    weaknesses = cve.get("weaknesses", [])
    cwe_ids = []
    for w in weaknesses:
        for wd in w.get("description", []):
            if wd.get("lang") == "en" and wd.get("value", "").startswith("CWE-"):
                cwe_ids.append(wd["value"])

    references = [ref.get("url", "") for ref in cve.get("references", [])[:5]]

    return {
        "cve_id": cve.get("id", cve_id),
        "cvss": cvss,
        "severity": severity,
        "description": desc,
        "published": cve.get("published", "")[:10],
        "last_modified": cve.get("lastModified", "")[:10],
        "cwe_ids": cwe_ids,
        "references": references,
        "source": "nvd",
    }


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
            return {"cve_id": cve_id, "error": f"OSV returned status {resp.status_code}"}
        data = resp.json()
    except Exception as exc:
        return {"cve_id": cve_id, "error": f"Could not reach OSV API: {type(exc).__name__}"}

    aliases = data.get("aliases") or []
    severity_entries = data.get("severity") or []
    cvss = 0.0
    for sev in severity_entries:
        match = re.search(r"(\d+\.\d+)$", str(sev.get("score") or ""))
        if match:
            try:
                cvss = float(match.group(1))
                break
            except ValueError:
                pass
    return {
        "cve_id": next((alias for alias in aliases if str(alias).startswith("CVE-")), cve_id),
        "cvss": cvss,
        "severity": _cvss_to_severity(cvss),
        "description": (data.get("summary") or data.get("details") or "")[:1200],
        "published": str(data.get("published") or "")[:10],
        "last_modified": str(data.get("modified") or "")[:10],
        "cwe_ids": [],
        "references": [ref.get("url", "") for ref in (data.get("references") or [])[:5] if ref.get("url")],
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


def _run_cve_id_mode(cve_ids: list[str], single_mode: bool) -> dict:
    """Handle direct CVE ID lookup mode."""
    results = []
    for cve_id in cve_ids:
        result = _fetch_cve(cve_id)
        if result.get("error") and any(
            marker in str(result.get("error") or "").lower()
            for marker in ("timed out", "could not reach", "rate limit", "returned status 5")
        ):
            fallback = _fetch_cve_from_osv(cve_id)
            if "error" not in fallback:
                result = fallback
        results.append(result)
        time.sleep(_NVD_RATE_DELAY)

    successful = [r for r in results if "error" not in r]
    billing_units_actual = len(successful)

    output: dict = {
        "results": results,
        "billing_units_actual": billing_units_actual,
    }

    # Backward-compatibility: mirror first successful result's fields at top level
    # when a single cve_id (not cve_ids) was provided.
    if single_mode and successful:
        first = successful[0]
        for key, val in first.items():
            output[key] = val

    return output


def run(payload: dict) -> dict:
    """Look up CVE details from the NIST NVD live API.

    Three modes (mutually exclusive, checked in order):
    - ``cve_id`` (str) or ``cve_ids`` (list[str]) — fetch specific CVE records
      by ID (e.g. ``"CVE-2024-1234"``).
    - ``keyword`` (str) — full-text NVD keyword search; returns up to
      ``max_results`` (default 10, max 50) entries sorted by ``sort_by``
      (``"published"`` | ``"modified"`` | ``"score"``).

    Returns ``{cves: [...], total_results, query_type}``.  Each CVE entry
    includes ``id``, ``description``, ``cvss_score``, ``severity``,
    ``published``, ``references``, and ``affected_products``.
    """
    # --- Direct CVE ID mode ---
    cve_id_single = payload.get("cve_id")
    cve_ids_list = payload.get("cve_ids")

    if cve_id_single is not None or cve_ids_list is not None:
        single_mode = False

        if cve_ids_list is not None and len(cve_ids_list) > 0:
            ids_to_lookup = cve_ids_list
        elif cve_id_single is not None:
            ids_to_lookup = [cve_id_single]
            single_mode = True
        else:
            return _err("cve_lookup.missing_id", "cve_id or cve_ids is required")

        if not isinstance(ids_to_lookup, list):
            return _err("cve_lookup.invalid_input", "cve_ids must be a list of CVE ID strings")

        if len(ids_to_lookup) > 10:
            return _err("cve_lookup.too_many_ids", f"At most 10 CVE IDs can be looked up per call. You provided {len(ids_to_lookup)}.")

        normalized = []
        for raw_id in ids_to_lookup:
            if not isinstance(raw_id, str):
                return _err("cve_lookup.invalid_input", f"Each CVE ID must be a string, got: {type(raw_id).__name__}")
            upper_id = raw_id.strip().upper()
            if not _CVE_ID_PATTERN.match(upper_id):
                return _err("cve_lookup.invalid_id_format", f"Invalid CVE ID format: {raw_id!r}. Expected pattern: CVE-YYYY-NNNNN")
            normalized.append(upper_id)

        return _run_cve_id_mode(normalized, single_mode)

    # --- Package-based mode (original) ---
    packages = payload.get("packages") or []
    include_patched = bool(payload.get("include_patched", False))
    ecosystem_hint = str(payload.get("ecosystem") or "auto").strip().lower() or "auto"
    if ecosystem_hint in {"", "auto"}:
        ecosystem_hint_for_query = None
    else:
        ecosystem_hint_for_query = ecosystem_hint

    if not isinstance(packages, list):
        return _err("cve_lookup.invalid_input", "packages must be a list of strings (e.g. [\"express@4.17.1\"])")

    if not packages:
        return {
            "results": [],
            "total_vulnerable": 0,
            "total_packages_checked": 0,
            "summary": "No packages provided. Pass a list like: [\"express@4.17.1\", \"lodash@4.17.20\"]",
            "source": "osv",
            "billing_units_actual": 0,
        }

    if len(packages) > 10:
        return _err("cve_lookup.too_many_packages", f"At most 10 packages can be checked per call. You provided {len(packages)}.")

    for raw_pkg in packages:
        if not isinstance(raw_pkg, str):
            return _err("cve_lookup.invalid_input", "Each package must be a string like \"express@4.17.1\".")
        if len(str(raw_pkg)) > 200:
            return _err("cve_lookup.package_name_too_long", f"Package name is too long (max 200 characters): {str(raw_pkg)[:40]}...")

    all_results: list[dict] = []
    seen_cves: set[str] = set()
    # Track which data source was actually used (NVD preferred; OSV if NVD unreachable)
    used_source = "osv"

    for raw_pkg in packages[:10]:  # cap at 10 packages per call
        pkg_name, pkg_version = _parse_package_version(str(raw_pkg))

        # Always prefer OSV.dev for package lookups: it is ecosystem-aware
        # (npm vs PyPI), version-aware, and returns precise package matches
        # without false positives. The NVD ``keywordSearch`` endpoint matches
        # the package *string* against every CVE description, which catches
        # unrelated products — e.g. a query for ``express`` returns Microsoft
        # Outlook Express, Intel Express switches, and Disney Go Express.
        # NVD remains the source of truth for direct ``cve_id`` lookups (which
        # bypass this branch entirely via ``_run_cve_id_mode``).
        pkg_cves = _query_osv(pkg_name, pkg_version, ecosystem_hint_for_query)
        if pkg_cves:
            used_source = "osv+nvd"

        for item in pkg_cves:
            if item["cve"] in seen_cves:
                continue
            seen_cves.add(item["cve"])
            hydrated = _hydrate_cve_details(item["cve"], item)
            time.sleep(_NVD_RATE_DELAY)

            fixed_in = hydrated.get("fixed_in") or item.get("fixed_in") or ""
            affected_range = f"< {fixed_in}" if fixed_in else "see advisory"

            if not include_patched and fixed_in and pkg_version:
                if not _version_in_range(pkg_version, f"< {fixed_in}"):
                    continue

            exploit_available = any(
                kw in str(hydrated.get("description") or "").lower()
                for kw in ("exploit", "poc", "proof-of-concept", "metasploit", "actively exploited")
            )

            all_results.append({
                "package": pkg_name,
                "version": pkg_version or "unknown",
                "cve": hydrated["cve"],
                "cvss": hydrated["cvss"],
                "severity": hydrated["severity"],
                "description": hydrated["description"],
                "published": hydrated["published"],
                "last_modified": hydrated["last_modified"],
                "affected_range": affected_range,
                "fixed_in": fixed_in or "see advisory",
                "exploit_available": exploit_available,
            })

    # Sort by CVSS descending
    all_results.sort(key=lambda x: x["cvss"], reverse=True)

    critical = sum(1 for r in all_results if r["severity"] == "critical")
    high = sum(1 for r in all_results if r["severity"] == "high")
    medium = sum(1 for r in all_results if r["severity"] == "medium")

    pkg_names_with_vulns = {r["package"] for r in all_results}
    summary_parts = [f"Found {len(all_results)} CVE(s) across {len(pkg_names_with_vulns)} package(s)."]
    if critical:
        summary_parts.append(f"{critical} critical")
    if high:
        summary_parts.append(f"{high} high")
    if medium:
        summary_parts.append(f"{medium} medium")
    exploitable = [r["cve"] for r in all_results if r["exploit_available"]]
    if exploitable:
        summary_parts.append(f"Exploits known for: {', '.join(exploitable[:3])}.")

    return {
        "results": all_results[:50],
        "total_vulnerable": len(pkg_names_with_vulns),
        "total_packages_checked": len(packages),
        "summary": " ".join(summary_parts),
        "source": used_source,
        "billing_units_actual": len(all_results),
    }
