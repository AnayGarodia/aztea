"""Target fingerprinting strategies.

Each strategy returns ``(fingerprint, error)`` where exactly one is non-None.
Strategies never raise — errors are reported as the second tuple element so
the sweeper can record them on the watcher row without crashing the loop.

# OWNS: HTTP / git / package-manifest fingerprinting
# NOT OWNS: storage of fingerprints (crud.py), sweeper scheduling (sweeper.py)
# INVARIANTS:
# - Every outbound URL MUST pass through core.url_security before any I/O.
# - HTTP fetch capped at HTTP_BODY_BYTE_CAP to avoid runaway downloads.
# - subprocess git ls-remote runs with timeout and clean env (no inherited creds).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
from typing import Any

import requests

from core import url_security as _url_security

_LOG = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 10
HTTP_BODY_BYTE_CAP = 5 * 1024 * 1024  # 5 MB
GIT_TIMEOUT_SECONDS = 10

# Conservative UA so registry maintainers can identify us.
_USER_AGENT = "aztea-watcher/1.0"


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def fingerprint_target(
    target_kind: str,
    target_url: str,
    target_meta: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(fingerprint, error)``.

    Both elements are str-or-None. When ``error`` is set, ``fingerprint`` is
    None and the caller should NOT compare against the previously stored
    fingerprint (i.e. an error is never treated as "changed").
    """
    meta = dict(target_meta or {})
    if target_kind == "http":
        return _fingerprint_http(target_url)
    if target_kind == "git":
        ref = str(meta.get("ref") or "HEAD").strip() or "HEAD"
        return _fingerprint_git(target_url, ref)
    if target_kind == "manifest":
        return _fingerprint_manifest(meta)
    return None, f"unknown target_kind: {target_kind!r}"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _fingerprint_http(url: str) -> tuple[str | None, str | None]:
    try:
        safe_url = _url_security.validate_outbound_url(url, "target_url")
    except ValueError as exc:
        return None, f"url_security: {exc}"
    try:
        resp = requests.get(
            safe_url,
            timeout=HTTP_TIMEOUT_SECONDS,
            stream=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
            allow_redirects=True,
        )
    except requests.exceptions.Timeout:
        return None, "http: timeout"
    except requests.exceptions.RequestException as exc:
        return None, f"http: {type(exc).__name__}"

    try:
        # SSRF defense in depth: revalidate the final resolved URL after the
        # redirect chain. The initial url_security check above guards the
        # registered target; without this second check, a public URL that
        # 30x's to 127.0.0.1 (or to a private/loopback host via a public
        # DNS record) would silently bypass the gate.
        final_url = str(getattr(resp, "url", "") or safe_url)
        if final_url and final_url != safe_url:
            try:
                _url_security.validate_outbound_url(final_url, "target_url")
            except ValueError as exc:
                return None, f"http: redirect to private host: {exc}"
        if resp.status_code >= 400:
            return None, f"http: HTTP {resp.status_code}"

        # Prefer ETag / Last-Modified when present — saves bandwidth and
        # avoids hashing dynamic but semantically-stable bodies (e.g.
        # timestamps in HTML comments). The header value is suffixed with
        # the URL host so two URLs sharing identical ETags don't collide.
        etag = (resp.headers.get("ETag") or "").strip()
        last_modified = (resp.headers.get("Last-Modified") or "").strip()
        cheap_header = etag or last_modified
        if cheap_header:
            return _hash_value(f"hdr|{cheap_header}|{safe_url}"), None

        body = b""
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body += chunk
            if len(body) > HTTP_BODY_BYTE_CAP:
                return None, f"http: body exceeds {HTTP_BODY_BYTE_CAP} bytes"
        if not body:
            return _hash_value(f"empty|{safe_url}"), None
        return _hash_bytes(_normalize_http_body(body)), None
    finally:
        resp.close()


def _normalize_http_body(body: bytes) -> bytes:
    """Strip trailing whitespace per line and collapse line endings.

    Defends against single-byte mutations in HTTP bodies that are
    semantically equivalent (\\r\\n vs \\n, trailing spaces from
    server-side rendering jitter) being counted as fingerprint changes.
    """
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return body
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Git ls-remote
# ---------------------------------------------------------------------------


def _fingerprint_git(url: str, ref: str) -> tuple[str | None, str | None]:
    try:
        safe_url = _url_security.validate_outbound_url(url, "target_url")
    except ValueError as exc:
        return None, f"url_security: {exc}"
    if not safe_url.startswith(("https://", "http://")):
        return None, "git: only http(s) git URLs are supported"
    if not ref or any(ch.isspace() for ch in ref) or len(ref) > 200:
        return None, "git: invalid ref"

    # Block obvious shell metachars even though we're using argv form;
    # belt-and-suspenders.
    if any(ch in safe_url for ch in (";", "|", "&", "`", "$")):
        return None, "git: refusing url with shell metachars"

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
        "GCM_INTERACTIVE": "Never",
    }
    try:
        result = subprocess.run(
            ["git", "ls-remote", safe_url, ref],
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "git: timeout"
    except FileNotFoundError:
        return None, "git: 'git' executable not on PATH"
    except OSError as exc:
        return None, f"git: {type(exc).__name__}"

    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        return None, f"git: ls-remote rc={result.returncode} {stderr[:200]}"

    output = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    if not output:
        return None, "git: ref not found"
    first_line = output.splitlines()[0]
    sha = first_line.split()[0] if first_line else ""
    if len(sha) < 7:
        return None, "git: malformed ls-remote output"
    return sha, None


# ---------------------------------------------------------------------------
# Package manifest (pypi / npm)
# ---------------------------------------------------------------------------


_PYPI_URL_TEMPLATE = "https://pypi.org/pypi/{package}/json"
_NPM_URL_TEMPLATE = "https://registry.npmjs.org/{package}/latest"


def _fingerprint_manifest(meta: dict[str, Any]) -> tuple[str | None, str | None]:
    registry = str(meta.get("registry") or "").strip().lower()
    package = str(meta.get("package") or "").strip()
    if registry not in ("pypi", "npm"):
        return None, "manifest: registry must be 'pypi' or 'npm'"
    if not package or not _is_safe_package_name(package):
        return None, "manifest: invalid package name"

    if registry == "pypi":
        url = _PYPI_URL_TEMPLATE.format(package=_quote_path_segment(package))
    else:
        url = _NPM_URL_TEMPLATE.format(package=_quote_path_segment(package))

    try:
        safe_url = _url_security.validate_outbound_url(url, "manifest_url")
    except ValueError as exc:
        return None, f"manifest: url_security: {exc}"

    try:
        resp = requests.get(
            safe_url,
            timeout=HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
    except requests.exceptions.Timeout:
        return None, "manifest: timeout"
    except requests.exceptions.RequestException as exc:
        return None, f"manifest: {type(exc).__name__}"

    if resp.status_code == 404:
        return None, "manifest: package not found"
    if resp.status_code >= 400:
        return None, f"manifest: HTTP {resp.status_code}"

    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        return None, "manifest: invalid JSON"

    version = _extract_manifest_version(registry, data)
    if not version:
        return None, "manifest: version field missing"
    return _hash_value(f"{registry}|{package}|{version}"), None


def _extract_manifest_version(registry: str, data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    if registry == "pypi":
        info = data.get("info") or {}
        if isinstance(info, dict):
            v = info.get("version")
            if isinstance(v, str) and v.strip():
                return v.strip()
    elif registry == "npm":
        v = data.get("version")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _is_safe_package_name(name: str) -> bool:
    if len(name) > 214:  # npm cap; pypi is shorter but this is fine
        return False
    # Allow alnum, dot, dash, underscore, slash (npm scopes), @ (npm scope prefix)
    for ch in name:
        if ch.isalnum() or ch in (".", "-", "_", "/", "@"):
            continue
        return False
    return True


def _quote_path_segment(name: str) -> str:
    # Manual quote — avoid encoding @ and / which are valid in npm scopes
    # but call shlex.quote on it just to be safe in case anything sneaks
    # through.
    return shlex.quote(name).strip("'")


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_value(value: str) -> str:
    return _hash_bytes(value.encode("utf-8"))


__all__ = [
    "HTTP_BODY_BYTE_CAP",
    "HTTP_TIMEOUT_SECONDS",
    "GIT_TIMEOUT_SECONDS",
    "fingerprint_target",
]
