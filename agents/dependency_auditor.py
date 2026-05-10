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
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

import requests
from agents._contracts import agent_error as _err

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

# CVSS bucket boundaries (per FIRST.org). Used both to label numeric scores
# and to back-fill scores from upstream "severity": "HIGH" labels.
_CVSS_CRITICAL = 9.0
_CVSS_HIGH = 7.0
_CVSS_MEDIUM = 4.0
_CVSS_LABEL_TO_SCORE = {
    "LOW": 3.0,
    "MODERATE": 5.5,
    "MEDIUM": 5.5,
    "HIGH": 7.5,
    "CRITICAL": 9.5,
}
_OSV_SUMMARY_MAX_CHARS = 600

_COPYLEFT = {"gpl", "agpl", "lgpl", "eupl", "cddl", "mpl", "osl"}
_RESTRICTIVE_LICENSE_HINTS = ("unknown", "proprietary", "see license")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_VER_SPLIT_RE = re.compile(r"[.\-]")
_OSV_SCORE_TAIL_RE = re.compile(r"\b(\d+(?:\.\d+)?)$")
_PYPI_REQ_LINE_RE = re.compile(
    r"([A-Za-z0-9_\-\.]+)(?:\[[A-Za-z0-9_,\-\.]+\])?"
    r"\s*([>=<!~]=?\s*[\w\.\*]+(?:\s*,\s*[>=<!~]=?\s*[\w\.\*]+)*)?"
)
_PYPI_VER_OP_RE = re.compile(r"[>=<!~^]+\s*")
_NPM_VER_DIGITS_RE = re.compile(r"[^0-9\.]")


def _ver_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in _VER_SPLIT_RE.split(v.strip())[:3])
    except (ValueError, TypeError):
        return (0, 0, 0)


def _detect_ecosystem(manifest: str) -> str:
    return "npm" if manifest.strip().startswith("{") else "pypi"


def _parse_pypi_manifest(manifest: str) -> list[tuple[str, str]]:
    """Parse requirements.txt-style lines into ``(name, version)`` pairs.

    Why: the regex matches *whole* requirement lines (re.fullmatch) so a
    free-form sentence like "this is not a manifest" does not become a
    package named "this". Pure: no I/O, no globals.
    """
    packages: list[tuple[str, str]] = []
    for raw_line in manifest.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PYPI_REQ_LINE_RE.fullmatch(line)
        if not m:
            continue
        name = m.group(1).strip()
        ver_spec = (m.group(2) or "").strip()
        if ver_spec:
            ver = _PYPI_VER_OP_RE.sub("", ver_spec).split(",")[0].strip()
        else:
            ver = ""
        packages.append((name, ver))
    return packages


def _npm_extract_deps(data: Any) -> list[tuple[str, str]]:
    """Pure: pull ``(name, ver)`` pairs from a parsed package.json-shaped dict."""
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        for name, ver_spec in (data.get(key) or {}).items():
            ver = _NPM_VER_DIGITS_RE.sub("", str(ver_spec)).strip(".") if ver_spec else ""
            out.append((name, ver))
    return out


def _npm_candidate_payloads(text: str) -> list[str]:
    """Build progressively-wrapped JSON candidates from a manifest snippet.

    Why: callers paste several common shapes — full package.json, just a
    "dependencies": {...} fragment, or a bare deps dict. Trying the strictest
    form first preserves intent; widening only when the strict parse yields
    nothing avoids misinterpreting a real package.json with empty deps.
    """
    candidates = [text]
    if text and not text.startswith("{") and '"dependencies"' in text:
        candidates.append("{" + text.rstrip(",") + "}")
    if text.startswith("{") and '"dependencies"' not in text and '"name"' not in text:
        candidates.append('{"dependencies": ' + text + "}")
    return candidates


def _parse_npm_manifest(manifest: str) -> list[tuple[str, str]]:
    """Parse a package.json-shaped string into ``(name, version)`` pairs."""
    for candidate in _npm_candidate_payloads(manifest.strip()):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        packages = _npm_extract_deps(parsed)
        if packages:
            return packages
    return []


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


def _osv_score_from_label(vuln: dict) -> float:
    """Pure: back-fill CVSS from OSV's ``database_specific.severity`` label.

    Why: some advisories only ship a HIGH/CRITICAL label; without this
    fallback the agent reports cvss=0 even on plainly serious vulns.
    """
    label = (
        str((vuln.get("database_specific") or {}).get("severity") or "")
        .strip()
        .upper()
    )
    return _CVSS_LABEL_TO_SCORE.get(label, 0.0)


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
    """Pure: shape a single OSV vuln record into the agent's CVE schema."""
    cvss = _osv_score_from_severity_field(vuln.get("severity") or [])
    if cvss == 0.0:
        cvss = _osv_score_from_label(vuln)
    summary = (vuln.get("summary") or vuln.get("details") or "")[:_OSV_SUMMARY_MAX_CHARS]
    return {
        "id": _osv_canonical_cve_id(vuln),
        "cvss": cvss,
        "severity": _cvss_to_severity(cvss),
        "description": summary,
        "fixed_in": _osv_extract_fixed_in(vuln),
    }


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


def _fetch_pypi_latest(name: str) -> tuple[str | None, str | None]:
    """Side-effect: fetch ``(latest_version, license)`` from PyPI; ``(None, None)`` on failure."""
    try:
        resp = requests.get(
            _PYPI_API.format(name=name),
            timeout=_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
        if resp.status_code == 200:
            info = resp.json().get("info", {})
            return info.get("version"), info.get("license")
    except Exception:
        _LOG.warning("PyPI version fetch failed for %s", name, exc_info=True)
    return None, None


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


def _fetch_npm_latest(name: str) -> tuple[str | None, str | None]:
    """Side-effect: fetch ``(latest_version, license)`` from npm; ``(None, None)`` on failure."""
    try:
        encoded = quote(name, safe="")
        resp = requests.get(
            _NPM_API.format(name=encoded),
            timeout=_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        versions = data.get("versions") or {}
        latest = (data.get("dist-tags") or {}).get("latest")
        if latest not in versions:
            latest = _best_npm_version(versions)
        license_ = versions[latest].get("license") if latest and latest in versions else None
        return latest, license_
    except Exception:
        _LOG.warning("npm version fetch failed for %s", name, exc_info=True)
        return None, None


def _license_risk(license_str: str | None) -> str:
    """Pure: bucket a SPDX/free-text license string into the auditor's risk levels."""
    if not license_str:
        return "low"
    lic = license_str.lower()
    if any(k in lic for k in _COPYLEFT):
        return "high"
    if any(hint in lic for hint in _RESTRICTIVE_LICENSE_HINTS):
        return "medium"
    return "none"


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


def _parse_manifest(ecosystem: str, manifest: str) -> list[tuple[str, str]]:
    """Pure dispatcher: route to the per-ecosystem parser and cap by ``_MAX_PACKAGES``."""
    parser = _parse_pypi_manifest if ecosystem == "pypi" else _parse_npm_manifest
    return parser(manifest)[:_MAX_PACKAGES]


def _classify_package(
    cves: list[dict], is_outdated: bool, license_risk: str
) -> tuple[str, dict | None]:
    """Pure: derive ``(action, top_cve_or_None)`` from per-package signals.

    Why: keeps the action/priority taxonomy in one place — touching the
    rules later means editing here only, not the parallel auditor closure.
    """
    if cves:
        action = "upgrade" if any(c["fixed_in"] for c in cves) else "replace"
        top_cve = max(cves, key=lambda c: c["cvss"])
        return action, top_cve
    if is_outdated:
        return "upgrade", None
    if license_risk in ("high", "medium"):
        return "review", None
    return "ok", None


def _format_priority(name: str, current_ver: str | None, top_cve: dict) -> str:
    """Pure: human-readable priority line for the ``top_priorities`` summary list."""
    cvss = top_cve["cvss"]
    label = "CRITICAL" if cvss >= _CVSS_CRITICAL else "HIGH" if cvss >= _CVSS_HIGH else "MEDIUM"
    return f"{label}: {name}@{current_ver or '?'} — {top_cve['id']} (CVSS {cvss})"


def _audit_one(
    item: tuple[str, str | None], *, ecosystem: str, checks: list[str]
) -> dict[str, Any]:
    """Side-effect: audit a single package via OSV + registry lookup.

    Why: side-effecting because it issues HTTP calls; isolating it lets
    callers parallelize over a thread pool while keeping classification
    pure in ``_classify_package``.
    """
    name, current_ver = item
    fetch_latest = "outdated" in checks
    fetch_license = "license" in checks
    latest_version: str | None = None
    license_str: str | None = None
    if fetch_latest or fetch_license:
        fetcher = _fetch_pypi_latest if ecosystem == "pypi" else _fetch_npm_latest
        latest_version, license_str = fetcher(name)
    cves = _query_osv(name, current_ver, ecosystem) if "cve" in checks else []
    is_outdated = bool(
        fetch_latest and current_ver and latest_version
        and _ver_tuple(current_ver) < _ver_tuple(latest_version)
    )
    risk = _license_risk(license_str) if fetch_license else "none"
    action, top_cve = _classify_package(cves, is_outdated, risk)
    priority = _format_priority(name, current_ver, top_cve) if top_cve else None
    package = {
        "name": name,
        "current_version": current_ver or "unknown",
        "latest_version": latest_version,
        "cves": cves,
        "license": license_str,
        "license_risk": risk,
        "action": action,
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
        if p["cves"] and max(c["cvss"] for c in p["cves"]) >= _CVSS_CRITICAL
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
        max_cvss = max((c["cvss"] for c in p["cves"]), default=0.0)
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
    raw_packages = _parse_manifest(ecosystem, manifest)
    if not raw_packages:
        return _invalid_manifest_error(ecosystem, manifest)
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
        "summary": _summarise(
            total, agg["vulnerable_count"], agg["critical_count"], agg["outdated_count"]
        ),
    }
