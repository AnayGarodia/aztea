"""
pypi_metadata.py — Fetch PyPI metadata for a package in a single call.

Input:
  {
    "package_name": "pydantic",                # required
    "version": "2.5.0"                          # optional; latest if omitted
  }

Output:
  {
    "name": str,
    "version": str,
    "latest_version": str,
    "summary": str | null,
    "license": str | null,
    "classifiers": [str],
    "maintainers": [str],
    "requires_python": str | null,
    "release_date": str | null,
    "project_urls": dict | null,
    "homepage": str | null,
    "not_found": bool
  }

OWNS: single-package PyPI metadata round-trip, license fallback to
      classifiers, latest-version determination.
NOT OWNS: vulnerability lookup (dependency_auditor / cve_lookup),
          dependency-tree resolution.
INVARIANTS:
  * 404 from PyPI returns ``not_found: true`` — never silently treated
    as "no info available."
"""

from __future__ import annotations

import logging

import requests

from agents._contracts import agent_error as _err


_LOG = logging.getLogger(__name__)

_PYPI_API = "https://pypi.org/pypi/{name}/json"
_PYPI_VERSION_API = "https://pypi.org/pypi/{name}/{version}/json"
_USER_AGENT = "Aztea-PyPI-Metadata/1.0"
_TIMEOUT_S = 10
_LICENSE_CLASSIFIER_PREFIX = "License ::"
_LICENSE_CLASSIFIER_OSI = "License :: OSI Approved ::"
_MAX_NAME_CHARS = 128


def _license_from_classifiers(classifiers: list) -> str | None:
    """Pure: pull an SPDX-ish license name from PyPI's classifiers list.

    Why: modern packages (pydantic, fastapi) leave ``info["license"]``
    empty and publish only the Trove classifier. Without this fallback
    the auditor reports ``license: null`` for clearly-MIT packages.
    """
    if not isinstance(classifiers, list):
        return None
    for entry in classifiers:
        if not isinstance(entry, str) or not entry.startswith(_LICENSE_CLASSIFIER_PREFIX):
            continue
        if entry.startswith(_LICENSE_CLASSIFIER_OSI):
            tail = entry[len(_LICENSE_CLASSIFIER_OSI):].strip()
            if tail:
                return tail
        parts = [p.strip() for p in entry.split("::") if p.strip()]
        if len(parts) >= 2 and parts[-1].lower() != "license":
            return parts[-1]
    return None


def _release_date(data: dict, version: str) -> str | None:
    """Pure: pull the first upload time for ``version`` from PyPI's releases blob."""
    releases = data.get("releases") or {}
    if not isinstance(releases, dict):
        return None
    files = releases.get(version) or []
    if not isinstance(files, list) or not files:
        return None
    first = files[0]
    if not isinstance(first, dict):
        return None
    upload_time = first.get("upload_time_iso_8601") or first.get("upload_time")
    if isinstance(upload_time, str):
        return upload_time
    return None


def _maintainers(info: dict) -> list[str]:
    """Pure: collect author/maintainer entries from a PyPI info dict."""
    out: list[str] = []
    for key in ("author", "maintainer"):
        val = info.get(key)
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
    return out


def _fetch_pypi(name: str, version: str | None) -> tuple[dict | None, bool, str | None]:
    """Side-effect: fetch PyPI metadata. Returns (data, not_found, error_message)."""
    url = (
        _PYPI_VERSION_API.format(name=name, version=version) if version
        else _PYPI_API.format(name=name)
    )
    try:
        resp = requests.get(
            url, timeout=_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
    except requests.exceptions.Timeout:
        return None, False, "PyPI API timed out"
    except Exception as exc:  # noqa: BLE001
        return None, False, f"Could not reach PyPI: {type(exc).__name__}"
    if resp.status_code == 404:
        return None, True, None
    if resp.status_code != 200:
        return None, False, f"PyPI returned status {resp.status_code}"
    try:
        return resp.json(), False, None
    except ValueError:
        return None, False, "PyPI returned non-JSON response"


def run(payload: dict) -> dict:
    """Fetch PyPI metadata for a package in a single HTTP round-trip."""
    if not isinstance(payload, dict):
        return _err("pypi_metadata.bad_input",
                    f"payload must be dict, got {type(payload).__name__}")
    name = str(payload.get("package_name") or "").strip()
    if not name:
        return _err("pypi_metadata.missing_package", "'package_name' is required.")
    if len(name) > _MAX_NAME_CHARS:
        return _err(
            "pypi_metadata.invalid_package_name",
            f"package_name exceeds {_MAX_NAME_CHARS} chars",
        )
    version_arg = payload.get("version")
    version = str(version_arg).strip() if version_arg is not None else None
    if version == "":
        version = None
    data, not_found, fetch_error = _fetch_pypi(name, version)
    if not_found:
        return {
            "name": name,
            "version": None,
            "latest_version": None,
            "summary": None,
            "license": None,
            "classifiers": [],
            "maintainers": [],
            "requires_python": None,
            "release_date": None,
            "project_urls": None,
            "homepage": None,
            "not_found": True,
        }
    if data is None:
        return _err(
            "pypi_metadata.fetch_failed",
            fetch_error or "Failed to fetch PyPI metadata",
            details={"package_name": name},
        )
    info = data.get("info") or {}
    classifiers = info.get("classifiers") or []
    license_value = info.get("license") or _license_from_classifiers(classifiers)
    latest = info.get("version") or None
    requested_version = version or latest
    return {
        "name": info.get("name") or name,
        "version": requested_version,
        "latest_version": latest,
        "summary": info.get("summary") or None,
        "license": license_value,
        "classifiers": list(classifiers) if isinstance(classifiers, list) else [],
        "maintainers": _maintainers(info),
        "requires_python": info.get("requires_python") or None,
        "release_date": _release_date(data, requested_version) if requested_version else None,
        "project_urls": info.get("project_urls") or None,
        "homepage": info.get("home_page") or None,
        "not_found": False,
    }
