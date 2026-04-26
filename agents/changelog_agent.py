"""
changelog_agent.py — Fetch real changelogs between package versions

Input:
  {
    "package": "requests",            # package name
    "ecosystem": "pypi|npm|auto",    # default: auto (detected from package format)
    "from_version": "2.28.0",         # optional, earliest version
    "to_version": "2.32.0"            # optional, latest version (defaults to latest)
  }

Output:
  {
    "package": str,
    "ecosystem": str,
    "from_version": str | null,
    "to_version": str,
    "latest_version": str,
    "changelog_url": str | null,
    "changelog_text": str,
    "breaking_changes": [str],
    "highlights": [str],
    "summary": str
  }
"""
from __future__ import annotations

import re

import requests

from core.llm import CompletionRequest, Message, run_with_fallback

_TIMEOUT = 10
_MAX_CHANGELOG_CHARS = 12_000

_SYSTEM = """\
You are a technical writer specializing in software release notes. Given raw changelog text \
for a package, extract the most important information.

Return JSON only — no prose outside the object."""

_USER = """\
Package: {package} ({ecosystem})
Versions: {from_version} → {to_version}

Raw changelog / release notes:
{changelog_text}

Return JSON:
{{
  "breaking_changes": ["list of breaking changes, empty if none"],
  "highlights": ["3-6 most important changes across all versions in range"],
  "summary": "2-3 sentence plain-English summary of what changed"
}}"""


def _detect_ecosystem(package: str) -> str:
    # npm packages use @scope/name or single-word names; PyPI uses underscore/hyphen convention
    # Default to pypi; if caller specified npm-style scope, use npm
    if package.startswith("@"):
        return "npm"
    return "pypi"


def _fetch_pypi_info(package: str) -> dict:
    resp = requests.get(
        f"https://pypi.org/pypi/{package}/json",
        timeout=_TIMEOUT,
        headers={"User-Agent": "aztea-changelog/1.0"},
    )
    if resp.status_code == 404:
        raise ValueError(f"Package '{package}' not found on PyPI.")
    if resp.status_code != 200:
        raise RuntimeError(f"PyPI API returned {resp.status_code}.")
    return resp.json()


def _fetch_npm_info(package: str) -> dict:
    encoded = package.replace("/", "%2F")
    resp = requests.get(
        f"https://registry.npmjs.org/{encoded}",
        timeout=_TIMEOUT,
        headers={"User-Agent": "aztea-changelog/1.0"},
    )
    if resp.status_code == 404:
        raise ValueError(f"Package '{package}' not found on npm.")
    if resp.status_code != 200:
        raise RuntimeError(f"npm registry returned {resp.status_code}.")
    return resp.json()


def _fetch_github_changelog(repo_url: str, from_v: str | None, to_v: str | None) -> str | None:
    """Try to fetch CHANGELOG.md or releases from a GitHub URL."""
    m = re.search(r"github\.com/([^/]+/[^/]+)", repo_url)
    if not m:
        return None
    repo = m.group(1).rstrip(".git")

    # Try releases API
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/releases",
            params={"per_page": 20},
            timeout=_TIMEOUT,
            headers={"User-Agent": "aztea-changelog/1.0", "Accept": "application/vnd.github+json"},
        )
        if resp.status_code == 200:
            releases = resp.json()
            if not releases:
                pass
            else:
                parts = []
                for r in releases:
                    tag = r.get("tag_name", "")
                    body = r.get("body") or ""
                    if body.strip():
                        parts.append(f"## {tag}\n{body}")
                if parts:
                    return "\n\n".join(parts)[:_MAX_CHANGELOG_CHARS]
    except Exception:
        pass

    # Try raw CHANGELOG.md
    for branch in ("main", "master"):
        for fname in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md"):
            try:
                resp = requests.get(
                    f"https://raw.githubusercontent.com/{repo}/{branch}/{fname}",
                    timeout=_TIMEOUT,
                    headers={"User-Agent": "aztea-changelog/1.0"},
                )
                if resp.status_code == 200 and len(resp.text) > 100:
                    return resp.text[:_MAX_CHANGELOG_CHARS]
            except Exception:
                continue

    return None


def _semver_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.split(r"[.\-]", v.lstrip("v"))[:3])
    except (ValueError, TypeError):
        return (0, 0, 0)


def _filter_changelog_by_versions(text: str, from_v: str | None, to_v: str | None) -> str:
    if not from_v:
        return text[:_MAX_CHANGELOG_CHARS]
    from_t = _semver_tuple(from_v)
    to_t = _semver_tuple(to_v) if to_v else (999, 999, 999)
    lines = text.splitlines()
    in_range = False
    keep: list[str] = []
    for line in lines:
        # heading lines that look like version headers
        m = re.match(r"^#+\s+[vV]?(\d+\.\d+[\.\d]*)", line)
        if m:
            vt = _semver_tuple(m.group(1))
            in_range = from_t <= vt <= to_t
        if in_range:
            keep.append(line)
    filtered = "\n".join(keep).strip()
    return filtered[:_MAX_CHANGELOG_CHARS] if filtered else text[:_MAX_CHANGELOG_CHARS]


def run(payload: dict) -> dict:
    package = str(payload.get("package") or "").strip()
    if not package:
        raise ValueError("'package' is required.")

    ecosystem = str(payload.get("ecosystem") or "auto").strip().lower()
    if ecosystem == "auto":
        ecosystem = _detect_ecosystem(package)
    if ecosystem not in ("pypi", "npm"):
        raise ValueError("ecosystem must be 'pypi', 'npm', or 'auto'.")

    from_version = str(payload.get("from_version") or "").strip() or None
    to_version = str(payload.get("to_version") or "").strip() or None

    changelog_text = ""
    changelog_url: str | None = None
    latest_version = ""

    if ecosystem == "pypi":
        data = _fetch_pypi_info(package)
        info = data.get("info", {})
        latest_version = info.get("version", "")
        if not to_version:
            to_version = latest_version
        # Try GitHub first
        for key in ("home_page", "project_url", "package_url"):
            val = info.get(key) or ""
            if "github.com" in val:
                text = _fetch_github_changelog(val, from_version, to_version)
                if text:
                    changelog_text = text
                    changelog_url = val
                    break
        # Fall back to PyPI release descriptions
        if not changelog_text:
            releases = data.get("releases", {})
            parts = []
            for ver, files in sorted(releases.items(), key=lambda kv: _semver_tuple(kv[0]), reverse=True):
                vt = _semver_tuple(ver)
                from_t = _semver_tuple(from_version) if from_version else (0, 0, 0)
                to_t = _semver_tuple(to_version) if to_version else (999, 999, 999)
                if from_t <= vt <= to_t and files:
                    desc = files[0].get("comment_text") or ""
                    if desc:
                        parts.append(f"## {ver}\n{desc}")
            if parts:
                changelog_text = "\n\n".join(parts)[:_MAX_CHANGELOG_CHARS]
        if not changelog_text:
            changelog_text = info.get("description") or ""
            if changelog_text:
                changelog_text = changelog_text[:_MAX_CHANGELOG_CHARS]
                changelog_url = f"https://pypi.org/project/{package}/"

    elif ecosystem == "npm":
        data = _fetch_npm_info(package)
        dist_tags = data.get("dist-tags", {})
        latest_version = dist_tags.get("latest", "")
        if not to_version:
            to_version = latest_version
        repo_info = data.get("repository", {})
        repo_url = repo_info.get("url", "") if isinstance(repo_info, dict) else str(repo_info)
        if "github.com" in repo_url:
            text = _fetch_github_changelog(repo_url, from_version, to_version)
            if text:
                changelog_text = text
                changelog_url = repo_url
        if not changelog_text:
            # Use npm versions list description
            versions = data.get("versions", {})
            parts = []
            for ver in sorted(versions.keys(), key=_semver_tuple, reverse=True):
                vt = _semver_tuple(ver)
                from_t = _semver_tuple(from_version) if from_version else (0, 0, 0)
                to_t = _semver_tuple(to_version) if to_version else (999, 999, 999)
                if from_t <= vt <= to_t:
                    desc = (versions[ver].get("description") or "").strip()
                    if desc:
                        parts.append(f"## {ver}\n{desc}")
            if parts:
                changelog_text = "\n\n".join(parts)[:_MAX_CHANGELOG_CHARS]
        if not changelog_text:
            changelog_text = data.get("description") or ""
            changelog_url = f"https://www.npmjs.com/package/{package}"

    # Filter to version range if we have full changelog
    if from_version and changelog_text:
        changelog_text = _filter_changelog_by_versions(changelog_text, from_version, to_version)

    # LLM synthesis
    breaking_changes: list[str] = []
    highlights: list[str] = []
    summary = ""

    if changelog_text:
        try:
            import json
            prompt = _USER.format(
                package=package,
                ecosystem=ecosystem,
                from_version=from_version or "beginning",
                to_version=to_version or latest_version,
                changelog_text=changelog_text[:8_000],
            )
            resp = run_with_fallback(CompletionRequest(
                model="",
                messages=[Message("system", _SYSTEM), Message("user", prompt)],
                max_tokens=600,
                json_mode=True,
            ))
            raw = resp.text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
            breaking_changes = parsed.get("breaking_changes", [])
            highlights = parsed.get("highlights", [])
            summary = parsed.get("summary", "")
        except Exception:
            summary = f"Changelog for {package} {from_version or ''} → {to_version or latest_version}."
    else:
        summary = f"No changelog text found for {package} {ecosystem}."

    return {
        "package": package,
        "ecosystem": ecosystem,
        "from_version": from_version,
        "to_version": to_version or latest_version,
        "latest_version": latest_version,
        "changelog_url": changelog_url,
        "changelog_text": changelog_text[:4_000] if changelog_text else "",
        "breaking_changes": breaking_changes,
        "highlights": highlights,
        "summary": summary,
    }
