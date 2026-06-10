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
      "cves": [{"id": str, "severity": str, "cvss": float | null,
                "description": str, "fixed_in": str | null}],
      "license": str | null,
      "license_risk": "none|low|medium|high",
      "action": "upgrade|replace|review|ok|not_found|version_unreachable",
      "notes": str | null
    }],
    "top_priorities": [str],
    "parse_warnings": [{"line"|"package": str, "reason": str, ...}],
    "summary": str
  }

INVARIANTS:
  - cvss is null when the upstream advisory only ships a severity *label*
    (HIGH / CRITICAL etc.). Never fabricate a numeric score from a bucket.
  - parse_warnings is always present (may be empty) so callers can rely on
    its shape without a hasattr/get-with-default dance.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

import requests
from agents._contracts import agent_error as _err
from agents._manifest_parsing import (
    detect_ecosystem as _detect_ecosystem,
    parse_npm_manifest as _parse_npm_manifest,
    parse_pypi_manifest as _parse_pypi_manifest,
)

_LOG = logging.getLogger(__name__)

_OSV_API = "https://api.osv.dev/v1/query"
_PYPI_API = "https://pypi.org/pypi/{name}/json"
_NPM_API = "https://registry.npmjs.org/{name}"
_USER_AGENT = "aztea-dependency-auditor/1.0"
_TIMEOUT = 10
_MAX_PACKAGES = 20
_MAX_MANIFEST_CHARS = 10_000
_AUDIT_WORKERS = 8

_SUPPORTED_ECOSYSTEMS = frozenset({"npm", "pypi", "auto"})
_DEFAULT_CHECKS = ("cve", "outdated", "license")
_TOP_PRIORITIES_LIMIT = 10

# CVSS bucket boundaries (per FIRST.org). Used only to label numeric scores;
# the reverse (label → numeric midpoint) is deliberately NOT done — see
# _osv_severity_from_label.
_CVSS_CRITICAL = 9.0
_CVSS_HIGH = 7.0
_CVSS_MEDIUM = 4.0
# Map OSV/GHSA severity labels to our severity bucket *string*. We used to
# fabricate a numeric CVSS midpoint (5.5 / 7.5 / 9.5) from these labels and
# sign them with a receipt as if they were real NVD scores — that was a
# trust violation. Now we report cvss=None and let severity carry the label.
_LABEL_TO_SEVERITY = {
    "LOW": "low",
    "MODERATE": "medium",
    "MEDIUM": "medium",
    "HIGH": "high",
    "CRITICAL": "critical",
}
_OSV_SUMMARY_MAX_CHARS = 600
_PYPI_LICENSE_CLASSIFIER_PREFIX = "License ::"
_PYPI_LICENSE_CLASSIFIER_OSI = "License :: OSI Approved ::"

# Weak copyleft is checked BEFORE strong: "lgpl" contains "gpl" as a
# substring, so order is what keeps LGPL out of the "high" bucket.
_WEAK_COPYLEFT = ("lgpl", "mpl", "epl", "cddl")
_STRONG_COPYLEFT = ("agpl", "gpl", "sspl", "eupl", "osl")
_RESTRICTIVE_LICENSE_HINTS = ("unknown", "proprietary", "see license")
# Risk levels ordered for SPDX-expression combination (index = severity).
_RISK_ORDER = ("none", "low", "medium", "high")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_VER_SPLIT_RE = re.compile(r"[.\-]")
_OSV_SCORE_TAIL_RE = re.compile(r"\b(\d+(?:\.\d+)?)$")


def _ver_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in _VER_SPLIT_RE.split(v.strip())[:3])
    except (ValueError, TypeError):
        return (0, 0, 0)


def _cvss_to_severity(score: float) -> str:
    """Pure: map CVSS base score to its FIRST.org severity bucket."""
    if score >= _CVSS_CRITICAL:
        return "critical"
    if score >= _CVSS_HIGH:
        return "high"
    if score >= _CVSS_MEDIUM:
        return "medium"
    if score > 0.0:
        return "low"
    return "none"


def _extract_cvss(metrics: dict) -> float:
    """Pure: pull a numeric CVSS score from NVD's metrics block, preferring v3.1."""
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        entries = metrics.get(key, [])
        if entries:
            try:
                return float(entries[0]["cvssData"]["baseScore"])
            except (KeyError, IndexError, TypeError, ValueError):
                pass
    return 0.0


def _osv_score_from_severity_field(severity_entries: list) -> float:
    """Pure: extract a CVSS score from OSV's ``severity`` array.

    Why: the array can hold either a raw numeric base score or a full CVSS3
    vector string; the trailing-number regex misses the score on a vector,
    so we try a direct float() first and fall back to the regex.
    """
    for sev in severity_entries or []:
        score_field = str(sev.get("score") or "").strip()
        if not score_field:
            continue
        try:
            return float(score_field)
        except ValueError:
            pass
        m = _OSV_SCORE_TAIL_RE.search(score_field)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return 0.0


def _osv_severity_from_label(vuln: dict) -> str | None:
    """Pure: extract the OSV ``database_specific.severity`` label as a severity bucket.

    Why: some advisories only ship a HIGH/CRITICAL label with no numeric CVSS.
    Returning the label as severity (instead of fabricating a midpoint
    numeric score) preserves the upstream signal without claiming precision
    we don't have. cvss stays None — the receipt now attests only to what
    NVD/OSV actually provided.
    """
    label = (
        str((vuln.get("database_specific") or {}).get("severity") or "")
        .strip()
        .upper()
    )
    return _LABEL_TO_SEVERITY.get(label)


def _osv_extract_fixed_in(vuln: dict) -> str | None:
    """Pure: walk OSV's affected/ranges/events tree to find the first ``fixed`` event."""
    for affected in vuln.get("affected") or []:
        for r in affected.get("ranges") or []:
            for ev in r.get("events") or []:
                if "fixed" in ev:
                    return ev["fixed"]
    return None


def _osv_canonical_cve_id(vuln: dict) -> str:
    """Pure: prefer the CVE alias over OSV's internal id when both are present."""
    vuln_id = vuln.get("id", "")
    aliases = vuln.get("aliases") or []
    return next((a for a in aliases if a.startswith("CVE-")), vuln_id)


def _osv_vuln_to_cve(vuln: dict) -> dict:
    """Pure: shape a single OSV vuln record into the agent's CVE schema.

    cvss is None when the upstream advisory only provides a severity label
    (HIGH / CRITICAL etc.) — see _osv_severity_from_label. Callers that
    need a comparable number should use ``_cvss_for_sort``.
    """
    cvss = _osv_score_from_severity_field(vuln.get("severity") or [])
    if cvss > 0.0:
        severity = _cvss_to_severity(cvss)
        cvss_out: float | None = cvss
    else:
        label_severity = _osv_severity_from_label(vuln)
        severity = label_severity or "none"
        cvss_out = None
    summary = (vuln.get("summary") or vuln.get("details") or "")[:_OSV_SUMMARY_MAX_CHARS]
    return {
        "id": _osv_canonical_cve_id(vuln),
        "cvss": cvss_out,
        "severity": severity,
        "description": summary,
        "fixed_in": _osv_extract_fixed_in(vuln),
    }


# Severity → comparable numeric used only for sorting/threshold checks. Kept
# private to this module: the public API surfaces cvss=None when unknown.
_SEVERITY_SORT_VALUE = {
    "critical": 9.5,
    "high": 7.5,
    "medium": 5.5,
    "low": 3.0,
    "none": 0.0,
}


def _cvss_for_sort(cve: dict) -> float:
    """Pure: return a comparable float for a CVE record whose ``cvss`` may be None.

    Why: aggregation/sort code wants a number; the *output* still preserves
    the honest ``cvss: null`` for label-only advisories.
    """
    raw = cve.get("cvss")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return _SEVERITY_SORT_VALUE.get(str(cve.get("severity") or "none").lower(), 0.0)


def _osv_fetch(pkg_name: str, version: str, ecosystem: str) -> list[dict]:
    """Side-effect: POST to OSV.dev and return the raw ``vulns`` list."""
    osv_ecosystem = "PyPI" if ecosystem == "pypi" else "npm"
    body: dict = {"package": {"name": pkg_name, "ecosystem": osv_ecosystem}}
    if version:
        body["version"] = version
    try:
        resp = requests.post(
            _OSV_API, json=body, timeout=_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
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
        _LOG.warning("OSV non-JSON response for %s", pkg_name, exc_info=True)
        return []


def _query_osv(pkg_name: str, version: str, ecosystem: str) -> list[dict]:
    """Side-effect: query OSV.dev and return CVE records, deduped by CVE id."""
    seen: set[str] = set()
    out: list[dict] = []
    for vuln in _osv_fetch(pkg_name, version, ecosystem):
        cve = _osv_vuln_to_cve(vuln)
        if cve["id"] in seen:
            continue
        seen.add(cve["id"])
        out.append(cve)
    return out


def _license_from_classifiers(classifiers: list) -> str | None:
    """Pure: pull a SPDX-ish license name from PyPI's ``classifiers`` list.

    Why: pydantic and many modern packages leave ``info["license"]`` empty
    and publish the license only as a Trove classifier
    (``"License :: OSI Approved :: MIT License"``). Without this fallback
    the auditor reported ``license: null`` and stamped the receipt with
    that null — making well-licensed packages look unaudited.
    """
    if not isinstance(classifiers, list):
        return None
    for entry in classifiers:
        if not isinstance(entry, str) or not entry.startswith(_PYPI_LICENSE_CLASSIFIER_PREFIX):
            continue
        # "License :: OSI Approved :: MIT License" → "MIT License"
        if entry.startswith(_PYPI_LICENSE_CLASSIFIER_OSI):
            tail = entry[len(_PYPI_LICENSE_CLASSIFIER_OSI):].strip()
            if tail:
                return tail
        # "License :: Other/Proprietary License" → "Other/Proprietary License"
        parts = [p.strip() for p in entry.split("::") if p.strip()]
        if len(parts) >= 2 and parts[-1].lower() != "license":
            return parts[-1]
    return None


def _fetch_pypi_latest(name: str) -> tuple[str | None, str | None, bool]:
    """Side-effect: fetch ``(latest_version, license, not_found)`` from PyPI.

    ``not_found=True`` is reserved for an explicit HTTP 404 (the package
    is not on PyPI). Network errors and other non-200s return
    ``(None, None, False)`` — distinguishing "doesn't exist" from
    "couldn't reach PyPI" matters: the first means the package is bogus,
    the second means we have no signal.
    """
    try:
        resp = requests.get(
            _PYPI_API.format(name=name),
            timeout=_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
    except Exception:
        _LOG.warning("PyPI version fetch failed for %s", name, exc_info=True)
        return None, None, False
    if resp.status_code == 404:
        return None, None, True
    if resp.status_code != 200:
        return None, None, False
    try:
        info = resp.json().get("info", {}) or {}
    except ValueError:
        _LOG.warning("PyPI non-JSON response for %s", name, exc_info=True)
        return None, None, False
    version = info.get("version") or None
    license_value = info.get("license") or None
    if not license_value:
        license_value = _license_from_classifiers(info.get("classifiers") or [])
    return version, license_value, False


def _best_npm_version(versions: dict[str, Any]) -> str | None:
    """Pure: highest stable semver published in the npm metadata, or None."""
    parsed: list[tuple[tuple[int, int, int], str]] = []
    for version in versions:
        if not _SEMVER_RE.match(str(version)):
            continue
        base = str(version).split("-", 1)[0].split("+", 1)[0]
        major, minor, patch = (int(part) for part in base.split("."))
        parsed.append(((major, minor, patch), str(version)))
    if not parsed:
        return None
    return max(parsed, key=lambda item: item[0])[1]


def _fetch_npm_latest(name: str) -> tuple[str | None, str | None, bool]:
    """Side-effect: fetch ``(latest_version, license, not_found)`` from npm.

    ``not_found=True`` is reserved for an explicit HTTP 404. Other non-200s
    and network failures return ``(None, None, False)``.
    """
    try:
        encoded = quote(name, safe="")
        resp = requests.get(
            _NPM_API.format(name=encoded),
            timeout=_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
    except Exception:
        _LOG.warning("npm version fetch failed for %s", name, exc_info=True)
        return None, None, False
    if resp.status_code == 404:
        return None, None, True
    if resp.status_code != 200:
        return None, None, False
    try:
        data = resp.json()
    except ValueError:
        return None, None, False
    versions = data.get("versions") or {}
    latest = (data.get("dist-tags") or {}).get("latest")
    if latest not in versions:
        latest = _best_npm_version(versions)
    license_ = versions[latest].get("license") if latest and latest in versions else None
    return latest, license_, False


def _single_license_risk(token: str) -> str:
    """Pure: risk bucket for ONE license identifier (no expression logic).

    Strong copyleft (GPL/AGPL/SSPL...) → high; weak/file-level copyleft
    (LGPL/MPL/EPL...) → medium; unknown/proprietary hints → medium;
    recognised-permissive or anything else → none.
    """
    lic = token.lower()
    if any(k in lic for k in _WEAK_COPYLEFT):
        return "medium"
    if any(k in lic for k in _STRONG_COPYLEFT):
        return "high"
    if any(hint in lic for hint in _RESTRICTIVE_LICENSE_HINTS):
        return "medium"
    return "none"


def _license_risk(license_str: str | None) -> str:
    """Pure: bucket a SPDX expression / free-text license into risk levels.

    SPDX semantics: ``OR`` lets the consumer pick the most permissive branch
    (min risk); ``AND`` binds every obligation (max risk). AND binds tighter
    than OR per the SPDX spec, which a flat OR-first split models correctly.
    Parentheses are dropped — a nested-grouping approximation we accept
    because real-world registry metadata almost never nests.
    """
    if not license_str:
        return "low"
    expression = license_str.replace("(", " ").replace(")", " ")
    or_risks = []
    for or_branch in re.split(r"\s+OR\s+", expression, flags=re.IGNORECASE):
        and_parts = re.split(r"\s+AND\s+", or_branch, flags=re.IGNORECASE)
        branch_risk = max(
            (_single_license_risk(p) for p in and_parts if p.strip()),
            key=_RISK_ORDER.index,
            default="none",
        )
        or_risks.append(branch_risk)
    return min(or_risks, key=_RISK_ORDER.index)


_NPM_FORMAT_HINTS = (
    "Full package.json: {\"dependencies\": {\"express\": \"4.17.1\"}, ...}",
    "Snippet: \"dependencies\": {\"express\": \"4.17.1\"}",
    "Bare deps dict: {\"express\": \"4.17.1\"}",
)
_PYPI_FORMAT_HINTS = (
    "requirements.txt: one package==version per line",
    "pyproject.toml [tool.poetry.dependencies] block (paste the full block)",
)


def _unsupported_ecosystem_error(ecosystem: str) -> dict:
    """Pure: structured-error envelope for an unsupported ecosystem string."""
    return _err(
        "dependency_auditor.unsupported_ecosystem",
        (
            f"Ecosystem {ecosystem!r} is not supported by dependency_auditor. "
            "Supported: npm (package.json), pypi (requirements.txt / "
            "pyproject.toml). For maven use OWASP Dependency-Check, for "
            "cargo use `cargo audit`, for go modules use `govulncheck`. "
            "Run those via shell_executor."
        ),
        details={
            "supported": sorted(_SUPPORTED_ECOSYSTEMS),
            "received": ecosystem,
            "next_step": (
                "call_specialist(slug='shell_executor', ...) with the "
                "ecosystem-native auditor"
            ),
        },
    )


def _invalid_manifest_error(ecosystem: str, manifest: str) -> dict:
    """Pure: structured-error envelope listing accepted formats per ecosystem."""
    hints = {"npm": _NPM_FORMAT_HINTS, "pypi": _PYPI_FORMAT_HINTS}.get(
        ecosystem, ("See npm/pypi formats above.",)
    )
    return _err(
        "dependency_auditor.invalid_manifest",
        "No dependencies found in manifest.",
        details={
            "ecosystem": ecosystem,
            "expected_formats": list(hints),
            "received_first_120_chars": manifest[:120],
        },
    )


_WORKSPACE_MANIFEST_PRIORITY = ("requirements.txt", "pyproject.toml", "package.json")


def _manifest_from_workspace(payload: dict) -> tuple[str, str]:
    """Look for a manifest in MCP-attached workspace_context. Returns (text, ecosystem)
    with empty strings if no usable manifest is present. Never raises.

    Why: when Claude Code runs in a project directory and the user has approved
    workspace sharing, the manifest is right there — making the agent demand
    a re-paste defeats the purpose of auto-context.
    """
    from core.workspace_helpers import extract_workspace_context

    bundle = extract_workspace_context(payload)
    if bundle is None:
        return "", ""
    for name in _WORKSPACE_MANIFEST_PRIORITY:
        body = bundle.manifests.get(name)
        if body and body.strip():
            ecosystem = "pypi" if name in ("requirements.txt", "pyproject.toml") else "npm"
            return body, ecosystem
    return "", ""


def _normalize_run_inputs(payload: dict) -> tuple[str, str, list[str]]:
    """Pure: validate + normalize ``run`` inputs. Raises ValueError for missing manifest.

    Why: rule 4 — fail loudly at the boundary instead of letting an empty
    manifest propagate into the parser.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    manifest = str(payload.get("manifest") or "").strip()
    ecosystem = str(payload.get("ecosystem") or "auto").strip().lower()
    if not manifest:
        ws_manifest, ws_ecosystem = _manifest_from_workspace(payload)
        if ws_manifest:
            manifest = ws_manifest
            if ecosystem == "auto":
                ecosystem = ws_ecosystem
    if not manifest:
        raise ValueError(
            "'manifest' is required (contents of package.json or requirements.txt)."
        )
    raw_checks = payload.get("checks")
    checks = list(raw_checks) if isinstance(raw_checks, list) and raw_checks else list(_DEFAULT_CHECKS)
    return manifest[:_MAX_MANIFEST_CHARS], ecosystem, checks


def _dedup_packages(
    packages: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[dict]]:
    """Pure: drop duplicate package entries, keeping the first occurrence.

    Why: two ``requests==2.28.0`` / ``requests==2.30.0`` lines in the same
    manifest used to be audited as separate independent entries, which
    inflated the package count and confused the priority list. We keep the
    first occurrence (matches what pip/npm actually install) and warn about
    any later duplicates so the user knows the manifest is inconsistent.
    """
    seen: dict[str, tuple[str, str]] = {}
    deduped: list[tuple[str, str]] = []
    warnings: list[dict] = []
    for name, ver in packages:
        key = name.lower()
        if key in seen:
            kept = seen[key]
            warnings.append({
                "package": name,
                "reason": "duplicate_entry",
                "kept": {"name": kept[0], "version": kept[1]},
                "ignored": {"name": name, "version": ver},
            })
            continue
        seen[key] = (name, ver)
        deduped.append((name, ver))
    return deduped, warnings


def _parse_manifest(
    ecosystem: str, manifest: str,
) -> tuple[list[tuple[str, str]], list[dict]]:
    """Pure dispatcher: route to the per-ecosystem parser and cap by ``_MAX_PACKAGES``.

    Returns ``(packages, parse_warnings)``. Warnings include unparseable
    lines (pypi only — npm manifests are JSON so any failure is whole-file)
    and duplicate-package entries.
    """
    parser = _parse_pypi_manifest if ecosystem == "pypi" else _parse_npm_manifest
    raw_packages, warnings = parser(manifest)
    deduped, dedup_warnings = _dedup_packages(raw_packages)
    warnings.extend(dedup_warnings)
    return deduped[:_MAX_PACKAGES], warnings


def _version_unreachable(current_ver: str | None, latest_version: str | None) -> bool:
    """Pure: True when the requested version exceeds the latest published one.

    Why: ``django>=99.0.0`` used to slip through silently — we'd parse the
    constraint, OSV would return no CVEs (there is no Django 99 to attack),
    and the audit would mark the package "ok". Now we flag it.
    """
    if not current_ver or not latest_version:
        return False
    cur = _ver_tuple(current_ver)
    latest = _ver_tuple(latest_version)
    return cur > latest and cur != (0, 0, 0) and latest != (0, 0, 0)


def _classify_package(
    cves: list[dict],
    is_outdated: bool,
    license_risk: str,
    *,
    not_found: bool = False,
    version_unreachable: bool = False,
) -> tuple[str, dict | None]:
    """Pure: derive ``(action, top_cve_or_None)`` from per-package signals.

    Why: keeps the action/priority taxonomy in one place — touching the
    rules later means editing here only, not the parallel auditor closure.
    """
    if not_found:
        return "not_found", None
    if version_unreachable:
        return "version_unreachable", None
    if cves:
        action = "upgrade" if any(c["fixed_in"] for c in cves) else "replace"
        top_cve = max(cves, key=_cvss_for_sort)
        return action, top_cve
    if is_outdated:
        return "upgrade", None
    if license_risk in ("high", "medium"):
        return "review", None
    return "ok", None


def _action_notes(
    action: str, current_ver: str | None, latest_version: str | None,
) -> str | None:
    """Pure: human-readable notes for non-ok actions that lack a top CVE."""
    if action == "not_found":
        return "Package is not present on PyPI / npm — likely typo or removed."
    if action == "version_unreachable":
        return (
            f"Requested version {current_ver or '?'} exceeds latest "
            f"published version {latest_version or '?'} — constraint is "
            "unsatisfiable; CVE/outdated checks were skipped."
        )
    return None


def _format_priority(name: str, current_ver: str | None, top_cve: dict) -> str:
    """Pure: human-readable priority line for the ``top_priorities`` summary list.

    When the upstream advisory only ships a severity label, ``cvss`` is None
    and we report the label instead of fabricating a number.
    """
    cvss = top_cve.get("cvss")
    severity = str(top_cve.get("severity") or "").lower()
    if isinstance(cvss, (int, float)) and cvss > 0:
        label = "CRITICAL" if cvss >= _CVSS_CRITICAL else "HIGH" if cvss >= _CVSS_HIGH else "MEDIUM"
        score_str = f"CVSS {cvss}"
    else:
        label = severity.upper() or "UNKNOWN"
        score_str = "CVSS unknown (label-only advisory)"
    return f"{label}: {name}@{current_ver or '?'} — {top_cve['id']} ({score_str})"


def _audit_one(
    item: tuple[str, str | None], *, ecosystem: str, checks: list[str]
) -> dict[str, Any]:
    """Side-effect: audit a single package via OSV + registry lookup.

    Why: side-effecting because it issues HTTP calls; isolating it lets
    callers parallelize over a thread pool while keeping classification
    pure in ``_classify_package``.

    Always fetches registry metadata (even when neither ``outdated`` nor
    ``license`` is in the requested checks) so ``not_found`` and
    ``version_unreachable`` signals are honest regardless of check flags.
    """
    name, current_ver = item
    fetcher = _fetch_pypi_latest if ecosystem == "pypi" else _fetch_npm_latest
    latest_version, license_str, not_found = fetcher(name)
    if not_found:
        # No point asking OSV about a package that doesn't exist.
        cves: list[dict] = []
    else:
        cves = _query_osv(name, current_ver, ecosystem) if "cve" in checks else []
    fetch_latest = "outdated" in checks
    fetch_license = "license" in checks
    is_outdated = bool(
        fetch_latest and current_ver and latest_version
        and _ver_tuple(current_ver) < _ver_tuple(latest_version)
    )
    version_unreachable = _version_unreachable(current_ver, latest_version)
    risk = _license_risk(license_str) if fetch_license else "none"
    action, top_cve = _classify_package(
        cves, is_outdated, risk,
        not_found=not_found,
        version_unreachable=version_unreachable,
    )
    priority = _format_priority(name, current_ver, top_cve) if top_cve else None
    package = {
        "name": name,
        "current_version": current_ver or "unknown",
        "latest_version": latest_version,
        "cves": cves,
        "license": license_str if fetch_license else None,
        "license_risk": risk,
        "action": action,
        "notes": _action_notes(action, current_ver, latest_version),
    }
    return {"package": package, "is_outdated": is_outdated, "priority": priority}


def _audit_all(
    raw_packages: list[tuple[str, str | None]],
    ecosystem: str,
    checks: list[str],
    emit_partial=None,
) -> list[dict[str, Any]]:
    """Side-effect: parallel-audit every package, preserving input order.

    When ``emit_partial`` is provided (co-pilot streaming jobs), each
    completed package fires a ``partial_output`` event so ``stop_when``
    predicates can abort the call mid-stream (e.g. ``stop on first
    critical``). Emission is best-effort: a callback that raises is logged
    and swallowed so a misbehaving emit channel can't sink the audit.
    """
    workers = min(_AUDIT_WORKERS, max(1, len(raw_packages)))

    def _audit_and_emit(item: tuple[str, str | None]) -> dict[str, Any]:
        audit = _audit_one(item, ecosystem=ecosystem, checks=checks)
        if emit_partial is not None:
            try:
                pkg = audit.get("package") or {}
                cves = list(pkg.get("cves") or [])
                severities = [str(c.get("severity") or "").lower() for c in cves]
                top_severity = "info"
                for s in ("critical", "high", "medium", "low"):
                    if s in severities:
                        top_severity = s
                        break
                # Shape: include the per-package finding plus a `findings`
                # array so jmespath predicates like
                # `output.findings[?severity=='critical']` match against
                # the streaming partial just like they would the final output.
                emit_partial(
                    {
                        "ecosystem": ecosystem,
                        "package": pkg,
                        "findings": [
                            {
                                "package": pkg.get("name"),
                                "current_version": pkg.get("current_version"),
                                "cve_id": c.get("id"),
                                "severity": c.get("severity"),
                                "cvss": c.get("cvss"),
                            }
                            for c in cves
                        ],
                        "severity": top_severity,
                    }
                )
            except Exception:  # noqa: BLE001 — best-effort emit, never crash audit
                _LOG.exception("dependency_auditor: emit_partial failed; continuing audit")
        return audit

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_audit_and_emit, raw_packages))


def _aggregate_audits(audits: list[dict]) -> dict[str, Any]:
    """Pure: derive vulnerable / outdated / critical counters and the priority list."""
    packages = [a["package"] for a in audits]
    vulnerable = sum(1 for p in packages if p["cves"])
    critical = sum(
        1 for p in packages
        if p["cves"] and max(_cvss_for_sort(c) for c in p["cves"]) >= _CVSS_CRITICAL
    )
    # WHY: a CVE-bearing package already counts in `vulnerable`; only count
    # it as outdated when it has no CVEs to avoid double-billing the user.
    outdated = sum(
        1 for a in audits if a["is_outdated"] and not a["package"]["cves"]
    )
    priorities = [a["priority"] for a in audits if a["priority"]]
    return {
        "packages": packages,
        "vulnerable_count": vulnerable,
        "critical_count": critical,
        "outdated_count": outdated,
        "top_priorities": priorities[:_TOP_PRIORITIES_LIMIT],
    }


def _sort_packages(packages: list[dict]) -> list[dict]:
    """Pure: sort by max CVSS desc, then by 'has issue' so 'ok' rows sink."""
    def key(p: dict) -> tuple[float, int]:
        max_cvss = max((_cvss_for_sort(c) for c in p["cves"]), default=0.0)
        return (-max_cvss, 0 if p["action"] == "ok" else 1)
    return sorted(packages, key=key)


def _summarise(total: int, vulnerable: int, critical: int, outdated: int) -> str:
    """Pure: one-line audit summary suitable for the result envelope."""
    parts = [f"Audited {total} package(s)."]
    if vulnerable:
        parts.append(f"{vulnerable} with known CVEs ({critical} critical).")
    if outdated:
        parts.append(f"{outdated} outdated.")
    if not vulnerable and not outdated:
        parts.append("No known CVEs or obvious outdated packages found.")
    return " ".join(parts)


def run(payload: dict, *, emit_partial=None) -> dict:
    """Audit a dependency manifest for known CVEs, outdated packages, and license risk.

    Why: the agent fans out OSV / PyPI / npm registry calls in parallel and
    returns a stable shape regardless of ecosystem so renderers can pretty-
    print without sniffing the input format.

    Co-pilot mode: when called from a streaming job, ``emit_partial`` is a
    callable that publishes a ``partial_output`` event per package as soon
    as that package's CVE check completes. Callers without streaming pass
    ``None`` (the default) and the agent behaves like before.
    """
    manifest, ecosystem, checks = _normalize_run_inputs(payload)
    if ecosystem not in _SUPPORTED_ECOSYSTEMS:
        return _unsupported_ecosystem_error(ecosystem)
    if ecosystem == "auto":
        ecosystem = _detect_ecosystem(manifest)
    raw_packages, parse_warnings = _parse_manifest(ecosystem, manifest)
    if not raw_packages:
        err = _invalid_manifest_error(ecosystem, manifest)
        # Even on failure, surface the warnings so the user knows what got dropped.
        err.setdefault("error", {}).setdefault("details", {})["parse_warnings"] = parse_warnings
        return err
    audits = _audit_all(raw_packages, ecosystem, checks, emit_partial=emit_partial)
    agg = _aggregate_audits(audits)
    packages = _sort_packages(agg["packages"])
    total = len(packages)
    return {
        "ecosystem": ecosystem,
        "total_packages": total,
        "vulnerable_count": agg["vulnerable_count"],
        "outdated_count": agg["outdated_count"],
        "critical_count": agg["critical_count"],
        "packages": packages,
        "top_priorities": agg["top_priorities"],
        "parse_warnings": parse_warnings,
        "summary": _summarise(
            total, agg["vulnerable_count"], agg["critical_count"], agg["outdated_count"]
        ),
    }
