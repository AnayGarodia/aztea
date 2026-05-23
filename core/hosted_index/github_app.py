"""
github_app.py — GitHub App installation-token issuer for hosted_index clone.

# OWNS: JWT minting from the Aztea App's private key + JWT-to-installation-token
#       exchange via GitHub's REST API; in-process token cache.
# NOT OWNS: cloning (ingest.py owns the actual git clone call once it has a
#           token); App management UI (out of scope).
#
# INVARIANTS:
#   * JWT lifetime never exceeds 10 minutes (GitHub's hard ceiling).
#   * Installation tokens expire at 1h; we cache for 50min to leave buffer.
#   * Private key path comes from GITHUB_APP_PRIVATE_KEY_PATH env var.
#   * App ID comes from GITHUB_APP_ID env var.
#   * On any configuration miss, callers get GitHubAppNotConfigured — never
#     a silent local clone path.
#
# DECISIONS:
#   * RS256 signing because GitHub mandates it; PyJWT supports it natively.
#   * Cache keyed on installation_id so a process serving multiple customers
#     doesn't trip over each other.
#   * Time math uses utcnow + timedelta to stay backend-agnostic; no
#     requirement that the host clock be monotonic (which it isn't on most
#     deployments).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import requests

_LOG = logging.getLogger(__name__)

# GitHub hard-codes 10min as the max JWT lifetime; we use a slightly shorter
# window so a clock skew of a few seconds doesn't bounce the request.
_JWT_LIFETIME_SECONDS: int = 9 * 60

# Installation tokens expire at 1h. Cache for 50min to leave a 10min buffer
# for in-flight requests at expiry.
_TOKEN_CACHE_TTL_SECONDS: int = 50 * 60

# Outbound requests should not hang forever; the GitHub App endpoint is normally
# sub-second. 15s is generous without being a footgun.
_GITHUB_API_TIMEOUT_SECONDS: float = 15.0

_GITHUB_API_BASE = "https://api.github.com"


class GitHubAppNotConfigured(RuntimeError):
    """Raised when the GitHub App env vars are missing.

    Why distinct: callers (the ingest pipeline) need to differentiate
    'configuration missing' from 'GitHub rejected the JWT' so the agent can
    surface the right error envelope.
    """


class GitHubAppAuthError(RuntimeError):
    """Raised when GitHub rejects the JWT or installation lookup."""


@dataclass(frozen=True)
class InstallationToken:
    """Cached installation token + UTC expiry."""

    token: str
    expires_at: float  # unix epoch seconds


class _TokenCache:
    """In-process cache keyed by installation_id. Thread-safe.

    Why in-process and not Redis: token issuance is cheap (~200ms p50); the
    cache is purely a latency reducer for the common case. Cross-worker
    consistency isn't required because GitHub allows multiple live tokens
    per installation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, InstallationToken] = {}

    def get(self, installation_id: int) -> InstallationToken | None:
        with self._lock:
            entry = self._entries.get(installation_id)
            if entry is None:
                return None
            if entry.expires_at <= time.time():
                # Expired — evict so the next caller refreshes.
                del self._entries[installation_id]
                return None
            return entry

    def put(self, installation_id: int, token: InstallationToken) -> None:
        with self._lock:
            self._entries[installation_id] = token

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _TokenCache()


def _load_private_key() -> str:
    """Read the App's private key from GITHUB_APP_PRIVATE_KEY_PATH.

    Why a path and not the key body: PEM keys are multi-line, and env vars
    that contain newlines are a common ops footgun (some shells strip them).
    Mounting the key as a file is the standard pattern for k8s secrets,
    docker compose, and systemd EnvironmentFile alike.
    """
    path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    if not path:
        raise GitHubAppNotConfigured(
            "GITHUB_APP_PRIVATE_KEY_PATH is not set; cannot issue installation tokens."
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise GitHubAppNotConfigured(
            f"GITHUB_APP_PRIVATE_KEY_PATH={path!r} is unreadable: {exc}"
        ) from exc


def _load_app_id() -> str:
    """Read the App's numeric ID from GITHUB_APP_ID."""
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    if not app_id:
        raise GitHubAppNotConfigured(
            "GITHUB_APP_ID is not set; cannot mint App JWTs."
        )
    return app_id


def mint_app_jwt() -> str:
    """Sign a JWT as the GitHub App. Valid for ~9 minutes.

    Why a separate function: the JWT is needed BOTH for installation lookup
    (GET /app/installations) AND for installation-token exchange. Exposing it
    keeps both paths testable in isolation.
    """
    private_key = _load_private_key()
    app_id = _load_app_id()
    now = datetime.now(timezone.utc)
    # GitHub recommends iat slightly in the past to absorb minor clock skew.
    payload = {
        "iat": int((now - timedelta(seconds=30)).timestamp()),
        "exp": int((now + timedelta(seconds=_JWT_LIFETIME_SECONDS)).timestamp()),
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def get_installation_token(installation_id: int) -> InstallationToken:
    """Return a cached or freshly-minted installation token for installation_id.

    Why cached: hot ingest paths re-clone the same repo; one token serves
    every clone in the next 50 minutes. The first call pays the GitHub API
    round-trip; subsequent calls are an in-memory lookup.
    """
    if not isinstance(installation_id, int) or installation_id <= 0:
        raise ValueError(
            f"installation_id must be a positive int, got {installation_id!r}"
        )
    cached = _cache.get(installation_id)
    if cached is not None:
        return cached

    app_jwt = mint_app_jwt()
    url = f"{_GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aztea-hosted-index",
    }
    try:
        response = requests.post(url, headers=headers, timeout=_GITHUB_API_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise GitHubAppAuthError(
            f"GitHub installation-token exchange network error: {exc}"
        ) from exc
    if response.status_code != 201:
        raise GitHubAppAuthError(
            f"GitHub installation-token exchange failed: "
            f"HTTP {response.status_code} {response.text[:300]}"
        )
    body = _safe_json(response)
    token_str = body.get("token")
    if not isinstance(token_str, str) or not token_str:
        raise GitHubAppAuthError("GitHub response is missing 'token' field")

    token = InstallationToken(
        token=token_str,
        expires_at=time.time() + _TOKEN_CACHE_TTL_SECONDS,
    )
    _cache.put(installation_id, token)
    return token


def authenticated_clone_url(repo_full_name: str, installation_id: int) -> str:
    """Build a clone URL with the installation token embedded as basic auth.

    Pattern: https://x-access-token:<token>@github.com/<owner>/<repo>.git
    GitHub recognises the x-access-token user as the marker for App tokens.

    Why this function exists separately from get_installation_token: the
    full clone URL is what `git clone` needs, but tests prefer to assert on
    the token directly. Splitting keeps each concern independently mockable.
    """
    if "/" not in repo_full_name:
        raise ValueError(
            f"repo_full_name must be 'owner/repo', got {repo_full_name!r}"
        )
    token = get_installation_token(installation_id)
    return f"https://x-access-token:{token.token}@github.com/{repo_full_name}.git"


def is_configured() -> bool:
    """Return True iff both env vars are present and the key file is readable.

    Why: ingest.py checks this up front and returns 'requires_configuration'
    instead of crashing partway through a clone. Callers also use this to
    decide whether to even attempt the GitHub path vs. a local-fs fallback
    in dev.
    """
    try:
        _load_app_id()
        _load_private_key()
        return True
    except GitHubAppNotConfigured:
        return False


def reset_cache_for_tests() -> None:
    """Test-only: clear the in-process token cache."""
    _cache.clear()


def _safe_json(response: Any) -> dict:
    """Pure-ish: best-effort JSON parse; surfaces a typed error if malformed."""
    try:
        body = response.json()
    except ValueError as exc:
        raise GitHubAppAuthError(
            f"GitHub response was not JSON: {response.text[:300]}"
        ) from exc
    if not isinstance(body, dict):
        raise GitHubAppAuthError(
            f"GitHub response was not a JSON object: {type(body).__name__}"
        )
    return body
