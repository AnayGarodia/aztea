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

import re
import time

import requests

_NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_NVD_TIMEOUT = 10
_NVD_RATE_DELAY = 0.7  # NVD public API allows ~5 req/s without key
_CVE_ID_PATTERN = re.compile(r'^CVE-\d{4}-\d{4,}$', re.IGNORECASE)


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


def _query_nvd(keyword: str) -> list[dict] | str:
    """Returns a list of CVE dicts, or an error string on failure."""
    try:
        resp = requests.get(
            _NVD_API,
            params={"keywordSearch": keyword, "resultsPerPage": 20},
            timeout=_NVD_TIMEOUT,
            headers={"User-Agent": "aztea-cve-lookup/1.0"},
        )
        if resp.status_code == 429:
            return "NVD API rate limit reached. Try again in a minute."
        if resp.status_code != 200:
            return f"NVD API returned status {resp.status_code}. Try again later."
        data = resp.json()
    except requests.exceptions.Timeout:
        return "NVD API timed out. Try again in a moment."
    except Exception as e:
        return f"Could not reach NVD API: {type(e).__name__}."

    results = []
    for item in data.get("vulnerabilities", []):
        cve = item.get("cve", {})
        cve_id = cve.get("id", "")
        metrics = cve.get("metrics", {})
        cvss = _extract_cvss(metrics)
        severity = _cvss_to_severity(cvss)
        descriptions = cve.get("descriptions", [])
        desc = next((d["value"] for d in descriptions if d.get("lang") == "en"), "")
        results.append({
            "cve": cve_id,
            "cvss": cvss,
            "severity": severity,
            "description": desc,
            "published": cve.get("published", "")[:10],
            "last_modified": cve.get("lastModified", "")[:10],
        })

    return results


def _fetch_cve(cve_id: str) -> dict:
    """Fetch a single CVE by its ID from the NIST NVD API.

    Returns a dict with CVE fields on success, or {"cve_id": ..., "error": ...} on failure.
    """
    try:
        resp = requests.get(
            _NVD_API,
            params={"cveId": cve_id},
            timeout=_NVD_TIMEOUT,
            headers={"User-Agent": "aztea-cve-lookup/1.0"},
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
        "description": desc[:500],
        "published": cve.get("published", "")[:10],
        "last_modified": cve.get("lastModified", "")[:10],
        "cwe_ids": cwe_ids,
        "references": references,
        "source": "nvd",
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
            return {"error": "cve_id or cve_ids is required"}

        if not isinstance(ids_to_lookup, list):
            return {"error": "cve_ids must be a list of CVE ID strings"}

        if len(ids_to_lookup) > 10:
            return {"error": f"At most 10 CVE IDs can be looked up per call. You provided {len(ids_to_lookup)}."}

        normalized = []
        for raw_id in ids_to_lookup:
            if not isinstance(raw_id, str):
                return {"error": f"Each CVE ID must be a string, got: {type(raw_id).__name__}"}
            upper_id = raw_id.strip().upper()
            if not _CVE_ID_PATTERN.match(upper_id):
                return {"error": f"Invalid CVE ID format: {raw_id!r}. Expected pattern: CVE-YYYY-NNNNN"}
            normalized.append(upper_id)

        return _run_cve_id_mode(normalized, single_mode)

    # --- Package-based mode (original) ---
    packages = payload.get("packages") or []
    include_patched = bool(payload.get("include_patched", False))

    if not isinstance(packages, list):
        return {"error": "packages must be a list of strings (e.g. [\"express@4.17.1\"])"}

    if not packages:
        return {
            "results": [],
            "total_vulnerable": 0,
            "total_packages_checked": 0,
            "summary": "No packages provided. Pass a list like: [\"express@4.17.1\", \"lodash@4.17.20\"]",
            "source": "nvd",
            "billing_units_actual": 0,
        }

    if len(packages) > 10:
        return {"error": f"At most 10 packages can be checked per call. You provided {len(packages)}."}

    for raw_pkg in packages:
        if not isinstance(raw_pkg, str):
            return {"error": "Each package must be a string like \"express@4.17.1\"."}
        if len(str(raw_pkg)) > 200:
            return {"error": f"Package name is too long (max 200 characters): {str(raw_pkg)[:40]}..."}

    all_results: list[dict] = []
    seen_cves: set[str] = set()

    for raw_pkg in packages[:10]:  # cap at 10 packages per call
        pkg_name, pkg_version = _parse_package_version(str(raw_pkg))
        nvd_result = _query_nvd(pkg_name)
        time.sleep(_NVD_RATE_DELAY)
        if isinstance(nvd_result, str):
            return {"error": nvd_result}
        nvd_cves = nvd_result

        for item in nvd_cves:
            if item["cve"] in seen_cves:
                continue
            seen_cves.add(item["cve"])

            # Use LLM to extract affected range / fixed version from description
            affected_range = ""
            fixed_in = ""
            if pkg_version:
                # Quick heuristic from description text
                desc_lower = item["description"].lower()
                range_m = re.search(r"before\s+([\d.]+)", desc_lower)
                if range_m:
                    fixed_in = range_m.group(1)
                    affected_range = f"< {fixed_in}"
                if not include_patched and affected_range:
                    if not _version_in_range(pkg_version, affected_range):
                        continue

            exploit_available = any(
                kw in item["description"].lower()
                for kw in ("exploit", "poc", "proof-of-concept", "metasploit", "actively exploited")
            )

            all_results.append({
                "package": pkg_name,
                "version": pkg_version or "unknown",
                "cve": item["cve"],
                "cvss": item["cvss"],
                "severity": item["severity"],
                "description": item["description"][:400],
                "published": item["published"],
                "last_modified": item["last_modified"],
                "affected_range": affected_range or "see description",
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
        "source": "nvd",
        "billing_units_actual": len(all_results),
    }
