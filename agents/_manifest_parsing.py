"""Manifest parsing for the dependency auditor.

# OWNS: turning raw requirements.txt / package.json text into
#       ``(packages, warnings)`` where packages is ``[(name, version), ...]``
#       and warnings is a list of ``{"line": str, "reason": str}`` dicts.
# NOT OWNS: registry/OSV lookups, version-outdated comparison, license
#       risk — those stay in ``agents/dependency_auditor.py``.
# INVARIANTS:
#   * Pure functions only — no I/O, no network, no logging side effects.
#   * Every non-empty, non-comment input line either yields a package or a
#     classified warning. Silent drops are forbidden: the 2026-05 audit
#     found extras/markers/VCS lines vanishing without a trace.
#   * Warning ``reason`` values are a closed vocabulary documented on each
#     parser; callers and the agent spec rely on it.
# DECISIONS:
#   - PyPI lines parse via ``packaging.requirements.Requirement`` (already a
#     pinned direct dependency) instead of a hand-rolled regex. The old
#     regex mangled extras (``pkg[socks]``) and choked on env markers.
#   - npm versions keep their prerelease/build identifiers. The old
#     digit-stripping regex corrupted ``^1.2.3-beta.1`` into ``1.2.3.1``,
#     which then queried OSV for a version that never existed.
"""

from __future__ import annotations

import json
import re
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

# Lower bound of an npm range expression, prerelease identifiers included.
# First version in the spec string is the range's lower bound for every
# shape npm emits (^x.y.z, ~x.y, >=a <b, x.y.z - a.b.c, plain pins, "16").
# Prerelease/build identifiers only attach to a full x.y.z triple.
_NPM_SEMVER_RE = re.compile(r"\d+(?:\.\d+(?:\.\d+(?:-[0-9A-Za-z.-]+)?)?)?")
# Specs that point at code instead of a registry version — not auditable.
_NPM_URL_SPEC_RE = re.compile(r"^(?:git\+|github:|gitlab:|bitbucket:|https?:|file:|link:|workspace:)")
_VCS_PREFIXES = ("git+", "hg+", "svn+", "bzr+")


def detect_ecosystem(manifest: str) -> str:
    """Pure: cheap shape sniff — JSON object means npm, anything else pypi."""
    return "npm" if manifest.strip().startswith("{") else "pypi"


def _classify_pip_option(line: str) -> str | None:
    """Pure: classify a pip option/URL line into a warning reason, or None.

    Reasons: ``editable_not_audited`` | ``vcs_url_not_audited`` |
    ``nested_requirements_not_followed`` | ``pip_option_ignored``.
    """
    if line.startswith(("-e ", "-e\t", "--editable")):
        return "editable_not_audited"
    if line.startswith(("-r ", "-r\t", "-c ", "-c\t", "--requirement", "--constraint")):
        return "nested_requirements_not_followed"
    # Other pip options (--index-url, --hash, ...) before the URL check —
    # an option's URL argument is not a VCS requirement.
    if line.startswith("-"):
        return "pip_option_ignored"
    if any(p in line for p in _VCS_PREFIXES) or "://" in line:
        return "vcs_url_not_audited"
    return None


def _pin_from_specifier(req: Requirement) -> str:
    """Pure: best auditable version from a requirement's specifier set.

    Prefer an exact ``==`` pin; otherwise the lowest ``>=`` bound (the
    version the user is at least running); otherwise empty (unpinned).
    """
    eq_versions = [s.version for s in req.specifier if s.operator in ("==", "===")]
    if eq_versions:
        return eq_versions[0].rstrip(".*")
    ge_versions = sorted(s.version for s in req.specifier if s.operator == ">=")
    if ge_versions:
        return ge_versions[0]
    return ""


def parse_pypi_manifest(manifest: str) -> tuple[list[tuple[str, str]], list[dict]]:
    """Parse requirements.txt-style text into ``(packages, warnings)``.

    Handles extras (``pkg[socks]``), multi-specifiers, and environment
    markers (``; python_version >= "3.10"``) via packaging. Option/VCS/
    editable lines get classified warnings (see ``_classify_pip_option``);
    anything else unparseable warns ``unparseable``.
    """
    packages: list[tuple[str, str]] = []
    warnings: list[dict] = []
    for raw_line in manifest.splitlines():
        # Inline comments are legal in requirements.txt; strip before parsing.
        line = raw_line.split(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        option_reason = _classify_pip_option(line)
        if option_reason is not None:
            warnings.append({"line": raw_line.strip(), "reason": option_reason})
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            warnings.append({"line": raw_line.strip(), "reason": "unparseable"})
            continue
        packages.append((req.name, _pin_from_specifier(req)))
    return packages, warnings


def _npm_version_from_spec(ver_spec: Any) -> tuple[str, str | None]:
    """Pure: ``(version, warning_reason)`` from one package.json spec value.

    URL-ish specs (git/github/file/link/workspace) are not auditable against
    the npm registry → ``vcs_url_not_audited``. Tags like ``latest`` or
    ``*`` yield an empty version (audited as unpinned) with no warning.
    """
    spec = str(ver_spec or "").strip()
    if not spec:
        return "", None
    if _NPM_URL_SPEC_RE.match(spec):
        return "", "vcs_url_not_audited"
    m = _NPM_SEMVER_RE.search(spec)
    return (m.group(0) if m else "", None)


def _npm_extract_deps(data: Any) -> tuple[list[tuple[str, str]], list[dict]]:
    """Pure: pull ``(name, ver)`` pairs from a parsed package.json-shaped dict."""
    if not isinstance(data, dict):
        return [], []
    out: list[tuple[str, str]] = []
    warnings: list[dict] = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        for name, ver_spec in (data.get(key) or {}).items():
            ver, reason = _npm_version_from_spec(ver_spec)
            if reason is not None:
                warnings.append({"line": f"{name}: {ver_spec}", "reason": reason})
            out.append((name, ver))
    return out, warnings


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


def parse_npm_manifest(manifest: str) -> tuple[list[tuple[str, str]], list[dict]]:
    """Parse a package.json-shaped string into ``(packages, warnings)``."""
    for candidate in _npm_candidate_payloads(manifest.strip()):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        packages, warnings = _npm_extract_deps(parsed)
        if packages:
            return packages, warnings
    return [], []
