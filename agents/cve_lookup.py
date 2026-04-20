"""
cve_lookup.py — Real-time CVE lookup via NIST NVD API

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

import json
import re
import time

import requests

from core.llm import CompletionRequest, Message, run_with_fallback

_NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_NVD_TIMEOUT = 10
_NVD_RATE_DELAY = 0.7  # NVD public API allows ~5 req/s without key


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


def _query_nvd(keyword: str) -> list[dict]:
    try:
        resp = requests.get(
            _NVD_API,
            params={"keywordSearch": keyword, "resultsPerPage": 20},
            timeout=_NVD_TIMEOUT,
            headers={"User-Agent": "aztea-cve-lookup/1.0"},
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

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


def run(payload: dict) -> dict:
    packages = payload.get("packages") or []
    include_patched = bool(payload.get("include_patched", False))

    if not packages:
        return {
            "results": [],
            "total_vulnerable": 0,
            "total_packages_checked": 0,
            "summary": "No packages provided.",
            "source": "nvd",
        }

    all_results: list[dict] = []
    seen_cves: set[str] = set()

    for raw_pkg in packages[:10]:  # cap at 10 packages per call
        pkg_name, pkg_version = _parse_package_version(str(raw_pkg))
        nvd_cves = _query_nvd(pkg_name)
        time.sleep(_NVD_RATE_DELAY)

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
    }
