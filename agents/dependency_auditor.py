"""
dependency_auditor.py — Dependency audit agent using live NVD data

Input:
  {
    "manifest": "contents of package.json or requirements.txt",
    "ecosystem": "npm|pypi|auto",
    "checks": ["cve", "outdated", "license"]   # default: all three
  }

Output:
  {
    "ecosystem": str,
    "total_packages": int,
    "vulnerable_count": int,
    "outdated_count": int,
    "critical_count": int,
    "packages": [{
      "name": str,
      "current_version": str,
      "latest_version": str | null,
      "cves": [{"id": str, "severity": str, "cvss": float, "description": str, "fixed_in": str | null}],
      "license": str | null,
      "license_risk": "none|low|medium|high",
      "action": "upgrade|replace|review|ok"
    }],
    "top_priorities": [str],
    "summary": str
  }
"""
from __future__ import annotations

import json
import re
import time

import requests

_NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_PYPI_API = "https://pypi.org/pypi/{name}/json"
_NPM_API = "https://registry.npmjs.org/{name}"
_TIMEOUT = 10
_NVD_RATE_DELAY = 0.7
_MAX_PACKAGES = 20
_MAX_MANIFEST_CHARS = 10_000

_COPYLEFT = {"gpl", "agpl", "lgpl", "eupl", "cddl", "mpl", "osl", "eupl"}


def _detect_ecosystem(manifest: str) -> str:
    return "npm" if manifest.strip().startswith("{") else "pypi"


def _parse_pypi_manifest(manifest: str) -> list[tuple[str, str]]:
    packages = []
    for line in manifest.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_\-\.]+)\s*([>=<!~^]+\s*[\d\.\*]+)?", line)
        if m:
            name = m.group(1).strip()
            ver_spec = (m.group(2) or "").strip()
            ver = re.sub(r"[>=<!~^]+\s*", "", ver_spec).split(",")[0].strip() if ver_spec else ""
            packages.append((name, ver))
    return packages


def _parse_npm_manifest(manifest: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(manifest)
    except json.JSONDecodeError:
        return []
    packages = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        for name, ver_spec in (data.get(key) or {}).items():
            ver = re.sub(r"[^0-9\.]", "", str(ver_spec)).strip(".") if ver_spec else ""
            packages.append((name, ver))
    return packages


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


def _query_nvd(pkg_name: str) -> list[dict]:
    try:
        resp = requests.get(
            _NVD_API,
            params={"keywordSearch": pkg_name, "resultsPerPage": 10},
            timeout=_TIMEOUT,
            headers={"User-Agent": "aztea-dependency-auditor/1.0"},
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
        if not cve_id:
            continue
        metrics = cve.get("metrics", {})
        cvss = _extract_cvss(metrics)
        descriptions = cve.get("descriptions", [])
        desc = next((d["value"] for d in descriptions if d.get("lang") == "en"), "")
        # Extract fixed version hint from description
        fixed_in = None
        range_m = re.search(r"before\s+([\d]+\.[\d\.]+)", desc.lower())
        if range_m:
            fixed_in = range_m.group(1)
        results.append({
            "id": cve_id,
            "cvss": cvss,
            "severity": _cvss_to_severity(cvss),
            "description": desc[:600],
            "fixed_in": fixed_in,
        })
    return results


def _fetch_pypi_latest(name: str) -> tuple[str | None, str | None]:
    """Returns (latest_version, license)."""
    try:
        resp = requests.get(_PYPI_API.format(name=name), timeout=_TIMEOUT,
                            headers={"User-Agent": "aztea-dependency-auditor/1.0"})
        if resp.status_code == 200:
            info = resp.json().get("info", {})
            return info.get("version"), info.get("license")
    except Exception:
        pass
    return None, None


def _fetch_npm_latest(name: str) -> tuple[str | None, str | None]:
    """Returns (latest_version, license)."""
    try:
        encoded = name.replace("/", "%2F")
        resp = requests.get(_NPM_API.format(name=encoded), timeout=_TIMEOUT,
                            headers={"User-Agent": "aztea-dependency-auditor/1.0",
                                     "Accept": "application/json"})
        if resp.status_code == 200:
            data = resp.json()
            latest = (data.get("dist-tags") or {}).get("latest")
            license_ = None
            if latest and latest in (data.get("versions") or {}):
                license_ = data["versions"][latest].get("license")
            return latest, license_
    except Exception:
        pass
    return None, None


def _license_risk(license_str: str | None) -> str:
    if not license_str:
        return "low"
    l = license_str.lower()
    if any(k in l for k in _COPYLEFT):
        return "high"
    if "unknown" in l or "proprietary" in l or "see license" in l:
        return "medium"
    return "none"


def run(payload: dict) -> dict:
    manifest = str(payload.get("manifest") or "").strip()
    if not manifest:
        raise ValueError("'manifest' is required (contents of package.json or requirements.txt).")

    ecosystem = str(payload.get("ecosystem") or "auto").strip().lower()
    if ecosystem == "auto":
        ecosystem = _detect_ecosystem(manifest)

    checks = payload.get("checks")
    if not checks or not isinstance(checks, list):
        checks = ["cve", "outdated", "license"]

    manifest = manifest[:_MAX_MANIFEST_CHARS]

    if ecosystem == "pypi":
        raw_packages = _parse_pypi_manifest(manifest)
    else:
        raw_packages = _parse_npm_manifest(manifest)

    raw_packages = raw_packages[:_MAX_PACKAGES]

    fetch_latest = ecosystem == "pypi" and "outdated" in checks
    fetch_license = "license" in checks

    packages_out = []
    vulnerable_count = 0
    outdated_count = 0
    critical_count = 0
    top_priorities: list[str] = []

    for name, current_ver in raw_packages:
        cves: list[dict] = []
        latest_version: str | None = None
        license_str: str | None = None

        # Fetch latest + license from registry
        if ecosystem == "pypi" and (fetch_latest or fetch_license):
            latest_version, license_str = _fetch_pypi_latest(name)
        elif ecosystem == "npm" and fetch_license:
            latest_version, license_str = _fetch_npm_latest(name)

        # CVE lookup via live NVD
        if "cve" in checks:
            cves = _query_nvd(name)
            time.sleep(_NVD_RATE_DELAY)

        is_outdated = False
        if "outdated" in checks and current_ver and latest_version:
            def _ver_tuple(v: str) -> tuple:
                try:
                    return tuple(int(x) for x in re.split(r"[.\-]", v.strip())[:3])
                except (ValueError, TypeError):
                    return (0, 0, 0)
            is_outdated = _ver_tuple(current_ver) < _ver_tuple(latest_version)

        l_risk = _license_risk(license_str) if fetch_license else "none"

        if cves:
            vulnerable_count += 1
            max_cvss = max(c["cvss"] for c in cves)
            if max_cvss >= 9.0:
                critical_count += 1
            action = "upgrade" if any(c["fixed_in"] for c in cves) else "replace"
            top_cve = max(cves, key=lambda c: c["cvss"])
            top_priorities.append(
                f"{'CRITICAL' if max_cvss >= 9.0 else 'HIGH' if max_cvss >= 7.0 else 'MEDIUM'}: "
                f"{name}@{current_ver or '?'} — {top_cve['id']} (CVSS {top_cve['cvss']})"
            )
        elif is_outdated:
            outdated_count += 1
            action = "upgrade"
        elif l_risk in ("high", "medium"):
            action = "review"
        else:
            action = "ok"

        if is_outdated and not cves:
            outdated_count += 1

        packages_out.append({
            "name": name,
            "current_version": current_ver or "unknown",
            "latest_version": latest_version,
            "cves": cves,
            "license": license_str,
            "license_risk": l_risk,
            "action": action,
        })

    # Sort: vulnerable first, then by severity
    def _sort_key(p: dict) -> tuple:
        max_cvss = max((c["cvss"] for c in p["cves"]), default=0.0)
        return (-max_cvss, 0 if p["action"] == "ok" else 1)

    packages_out.sort(key=_sort_key)

    total = len(packages_out)
    summary_parts = [f"Audited {total} package(s)."]
    if vulnerable_count:
        summary_parts.append(f"{vulnerable_count} with known CVEs ({critical_count} critical).")
    if outdated_count:
        summary_parts.append(f"{outdated_count} outdated.")
    if not vulnerable_count and not outdated_count:
        summary_parts.append("No known CVEs or obvious outdated packages found.")

    return {
        "ecosystem": ecosystem,
        "total_packages": total,
        "vulnerable_count": vulnerable_count,
        "outdated_count": outdated_count,
        "critical_count": critical_count,
        "packages": packages_out,
        "top_priorities": top_priorities[:10],
        "summary": " ".join(summary_parts),
    }
