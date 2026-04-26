"""User registration, login, and API key lifecycle.

Paired with ``core.auth.schema`` (which owns the DB schema, password / key
hashing, and shared constants). This module implements the mutating
operations that the HTTP layer hits on every auth route:

- ``register_user`` / ``login_user`` — account creation and authentication
  with hashed passwords, legal acceptance tracking, and the "$1.00 free
  credit" wallet bootstrap.
- ``create_api_key`` / ``verify_api_key`` / ``rotate_api_key`` / ``delete_api_key``
  — scoped API key lifecycle. Keys are stored as salted SHA-256 digests; the
  raw key is only ever returned on creation and never logged (see the
  redaction filter in ``server.application_parts.part_000``).
- ``create_agent_api_key`` / ``verify_agent_api_key`` — agent-scoped worker
  keys (`azk_...`) that are pinned to a specific agent and cannot be used
  for caller-side operations.

Legal acceptance state (``terms_version_accepted``, ``privacy_version_accepted``)
flows through every auth response so the frontend can prompt for re-acceptance
whenever the server-side version constant is bumped.
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from .schema import (
    AGENT_CALLER_KEY_PREFIX,
    AGENT_KEY_PREFIX,
    DEFAULT_KEY_SCOPES,
    KEY_PREFIX,
    LEGAL_PRIVACY_VERSION,
    LEGAL_TERMS_VERSION,
    _conn,
    _decode_scopes_json,
    _hash_password,
    _legal_state_from_row,
    _make_agent_api_key,
    _make_agent_caller_api_key,
    _make_api_key,
    _normalize_optional_non_negative_int,
    _normalize_optional_text,
    _normalize_scopes,
    _now,
)


_VALID_ROLES = frozenset({"builder", "hirer", "both"})


def register_user(username: str, email: str, password: str, role: str = "both") -> dict:
    """
    Create a new user and mint a short-lived Session key so the frontend can
    authenticate immediately. Users create named API keys from /keys themselves.
    Raises ValueError on duplicate email or invalid role.
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: builder, hirer, both.")
    user_id = str(uuid.uuid4())
    salt = secrets.token_hex(32)
    pw_hash = _hash_password(password, salt)
    normalized_email = email.lower().strip()
    normalized_username = username.strip()

    raw_key, key_hash, key_prefix = _make_api_key()
    key_id = str(uuid.uuid4())
    session_scopes_json = json.dumps(list(DEFAULT_KEY_SCOPES))
    now = _now()

    with _conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, salt, created_at, role)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, normalized_username, normalized_email, pw_hash, salt, now, role),
            )
            conn.execute(
                "INSERT INTO api_keys (key_id, user_id, key_hash, key_prefix, name, scopes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, user_id, key_hash, key_prefix, "Session key", session_scopes_json, now),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "users.email" in message or "unique constraint failed: users.email" in message:
                raise ValueError("An account with that email already exists.")
            raise

    return {
        "user_id": user_id,
        "username": normalized_username,
        "email": normalized_email,
        "role": role,
        "raw_api_key": raw_key,
        "key_id": key_id,
        "key_prefix": key_prefix,
        **_legal_state_from_row({}),
    }


class AccountSuspendedError(Exception):
    """Raised when a suspended or banned account attempts to log in."""


def login_user(email: str, password: str) -> dict | None:
    """
    Verify credentials. Returns user dict, or None if wrong credentials.
    Raises AccountSuspendedError if the account is suspended or banned.
    Always mints a fresh API key so the caller always gets a usable key.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
    if row is None:
        return None
    user = dict(row)
    status = str(user.get("status") or "active").strip().lower()
    if status != "active":
        raise AccountSuspendedError(status)
    expected = _hash_password(password, user["salt"])
    if not secrets.compare_digest(user["password_hash"], expected):
        return None

    # Revoke any existing session key so logins don't accumulate unbounded rows.
    with _conn() as conn:
        conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE user_id = ? AND name = 'Session key' AND is_active = 1",
            (user["user_id"],),
        )
    result = _create_key_for_user(user["user_id"], "Session key")
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "email": user["email"],
        "role": user.get("role") or "both",
        "created_at": user["created_at"],
        "raw_api_key": result["raw_key"],
        "key_id": result["key_id"],
        "key_prefix": result["key_prefix"],
        **_legal_state_from_row(user),
    }


def login_or_register_via_google(email: str, name: str = "") -> tuple[dict, bool]:
    """Log in (or create) a user identified by a Google-verified email.

    Returns (user_dict, created). The dict has the same shape as the result
    from `login_user` / `register_user` so the HTTP layer can return a single
    AuthLoginResponse. Caller is responsible for crediting the starter
    balance and sending the welcome email when `created` is True.
    """
    normalized_email = email.lower().strip()
    if not normalized_email:
        raise ValueError("Email is required.")

    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (normalized_email,)
        ).fetchone()

    if row is not None:
        user = dict(row)
        status = str(user.get("status") or "active").strip().lower()
        if status != "active":
            raise AccountSuspendedError(status)
        # Revoke prior session keys, mint a fresh one — same pattern as login_user.
        with _conn() as conn:
            conn.execute(
                "UPDATE api_keys SET is_active = 0 WHERE user_id = ? AND name = 'Session key' AND is_active = 1",
                (user["user_id"],),
            )
        result = _create_key_for_user(user["user_id"], "Session key")
        return (
            {
                "user_id": user["user_id"],
                "username": user["username"],
                "email": user["email"],
                "role": user.get("role") or "both",
                "created_at": user["created_at"],
                "raw_api_key": result["raw_key"],
                "key_id": result["key_id"],
                "key_prefix": result["key_prefix"],
                **_legal_state_from_row(user),
            },
            False,
        )

    # No existing account — create one. Username derived from the email local
    # part (sanitised + uniqueness suffix). Password is randomized; the user
    # can use 'Forgot password' to set a real one if they ever want to log in
    # without Google.
    local_part = normalized_email.split("@", 1)[0]
    base_username = "".join(c for c in local_part if c.isalnum() or c in "-_") or "user"
    base_username = base_username[:24] or "user"
    candidate = base_username
    suffix = 0
    with _conn() as conn:
        while conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (candidate,)
        ).fetchone() is not None:
            suffix += 1
            candidate = f"{base_username}{suffix}"[:32]
            if suffix > 9999:
                candidate = f"user{secrets.token_hex(4)}"
                break
    random_password = secrets.token_urlsafe(24)
    return register_user(candidate, normalized_email, random_password, role="both"), True


def get_user_by_id(user_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    d.pop("salt", None)
    return d


def update_user_role(user_id: str, role: str) -> None:
    """Update the role for a user. Raises ValueError for invalid role."""
    if role not in _VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: builder, hirer, both.")
    with _conn() as conn:
        conn.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))


def _create_key_for_user(
    user_id: str,
    name: str,
    scopes: list[str] | None = None,
    *,
    max_spend_cents: int | None = None,
    per_job_cap_cents: int | None = None,
) -> dict:
    raw, key_hash, prefix = _make_api_key()
    key_id = str(uuid.uuid4())
    normalized_scopes = _normalize_scopes(scopes)
    normalized_max_spend = _normalize_optional_non_negative_int(
        max_spend_cents,
        field_name="max_spend_cents",
    )
    normalized_per_job_cap = _normalize_optional_non_negative_int(
        per_job_cap_cents,
        field_name="per_job_cap_cents",
    )
    with _conn() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_id, user_id, key_hash, key_prefix, name, scopes, max_spend_cents, per_job_cap_cents, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key_id,
                user_id,
                key_hash,
                prefix,
                name,
                json.dumps(normalized_scopes),
                normalized_max_spend,
                normalized_per_job_cap,
                _now(),
            ),
        )
    return {
        "raw_key": raw,
        "key_id": key_id,
        "key_prefix": prefix,
        "name": name,
        "scopes": normalized_scopes,
        "max_spend_cents": normalized_max_spend,
        "per_job_cap_cents": normalized_per_job_cap,
    }


_MAX_KEYS_PER_USER = 10


class KeyLimitExceededError(Exception):
    """Raised when a user tries to create more keys than the platform allows."""


def count_user_active_keys(user_id: str) -> int:
    """Return the number of active non-session API keys for a user."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM api_keys WHERE user_id = ? AND is_active = 1 AND name != 'Session key'",
            (user_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def create_api_key(
    user_id: str,
    name: str = "New key",
    scopes: list[str] | tuple[str, ...] | set[str] | None = None,
    max_spend_cents: int | None = None,
    per_job_cap_cents: int | None = None,
) -> dict:
    """Create a named API key for a user. Returns {"raw_key", "key_id", "key_prefix", "name"}."""
    active = count_user_active_keys(user_id)
    if active >= _MAX_KEYS_PER_USER:
        raise KeyLimitExceededError(
            f"You've reached the {_MAX_KEYS_PER_USER} active key limit. "
            "Revoke an unused key to create a new one."
        )
    return _create_key_for_user(
        user_id,
        name,
        scopes=list(scopes) if scopes is not None else None,
        max_spend_cents=max_spend_cents,
        per_job_cap_cents=per_job_cap_cents,
    )


# Cache verified key lookups for up to 60 seconds to avoid a DB read on every request.
# Entry: key_hash -> (result_dict, cached_at_monotonic)
_KEY_CACHE: dict[str, tuple[dict, float]] = {}
_KEY_CACHE_TTL = 60.0
_KEY_CACHE_LOCK = threading.Lock()

# Track which key_ids need a last_used_at write; flush at most once per minute per key.
_LAST_USED_PENDING: dict[str, float] = {}  # key_id -> last flushed monotonic
_LAST_USED_LOCK = threading.Lock()
_LAST_USED_FLUSH_INTERVAL = 60.0


def _flush_last_used(key_id: str) -> None:
    now_m = time.monotonic()
    with _LAST_USED_LOCK:
        last_flushed = _LAST_USED_PENDING.get(key_id, 0.0)
        if now_m - last_flushed < _LAST_USED_FLUSH_INTERVAL:
            return
        _LAST_USED_PENDING[key_id] = now_m
    try:
        with _conn() as conn:
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
                (_now(), key_id),
            )
    except Exception:
        pass


def invalidate_key_cache(key_hash: str) -> None:
    with _KEY_CACHE_LOCK:
        _KEY_CACHE.pop(key_hash, None)


def invalidate_key_cache_for_user(user_id: str) -> None:
    with _KEY_CACHE_LOCK:
        to_drop = [h for h, (r, _) in _KEY_CACHE.items() if r.get("user_id") == user_id]
        for h in to_drop:
            del _KEY_CACHE[h]


def verify_api_key(raw_key: str) -> dict | None:
    """
    Verify a raw API key against the DB. Returns user info dict or None.
    Caches positive results for 60s to reduce per-request DB reads.
    Writes last_used_at at most once per minute per key.
    """
    if not raw_key.startswith(KEY_PREFIX):
        return None
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    now_m = time.monotonic()
    with _KEY_CACHE_LOCK:
        cached = _KEY_CACHE.get(key_hash)
        if cached is not None:
            result, cached_at = cached
            if now_m - cached_at < _KEY_CACHE_TTL:
                _flush_last_used(result["key_id"])
                return dict(result)
            del _KEY_CACHE[key_hash]

    with _conn() as conn:
        row = conn.execute(
            """
            SELECT ak.key_id, ak.user_id, ak.name AS key_name,
                   ak.scopes, ak.max_spend_cents, ak.per_job_cap_cents, u.username, u.email,
                   u.terms_version_accepted, u.privacy_version_accepted, u.legal_accepted_at
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.user_id
            WHERE ak.key_hash = ? AND ak.is_active = 1 AND u.status = 'active'
            """,
            (key_hash,),
        ).fetchone()
        if row is None:
            return None

    result = {
        "key_id": row["key_id"],
        "user_id": row["user_id"],
        "username": row["username"],
        "email": row["email"],
        "key_name": row["key_name"],
        "scopes": _decode_scopes_json(row["scopes"]),
        "max_spend_cents": (
            int(row["max_spend_cents"])
            if row["max_spend_cents"] is not None
            else None
        ),
        "per_job_cap_cents": (
            int(row["per_job_cap_cents"])
            if row["per_job_cap_cents"] is not None
            else None
        ),
        **_legal_state_from_row(dict(row)),
    }

    with _KEY_CACHE_LOCK:
        _KEY_CACHE[key_hash] = (result, time.monotonic())

    _flush_last_used(result["key_id"])
    return dict(result)


def accept_legal_terms(
    user_id: str,
    *,
    terms_version: str,
    privacy_version: str,
    accepted_ip: str | None = None,
    accepted_user_agent: str | None = None,
) -> dict:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        raise ValueError("user_id is required.")

    normalized_terms = str(terms_version or "").strip()
    normalized_privacy = str(privacy_version or "").strip()
    if normalized_terms != LEGAL_TERMS_VERSION or normalized_privacy != LEGAL_PRIVACY_VERSION:
        raise ValueError("Legal version mismatch. Refresh terms and privacy and try again.")

    now = _now()
    normalized_ip = _normalize_optional_text(accepted_ip)
    normalized_ua = _normalize_optional_text(accepted_user_agent)
    if normalized_ua and len(normalized_ua) > 512:
        normalized_ua = normalized_ua[:512]

    with _conn() as conn:
        result = conn.execute(
            """
            UPDATE users
            SET terms_version_accepted = ?,
                privacy_version_accepted = ?,
                legal_accepted_at = ?,
                legal_accept_ip = ?,
                legal_accept_user_agent = ?
            WHERE user_id = ?
            """,
            (normalized_terms, normalized_privacy, now, normalized_ip, normalized_ua, normalized_user_id),
        )
        if result.rowcount < 1:
            raise ValueError("User not found.")
        row = conn.execute(
            """
            SELECT user_id, terms_version_accepted, privacy_version_accepted, legal_accepted_at
            FROM users
            WHERE user_id = ?
            """,
            (normalized_user_id,),
        ).fetchone()

    if row is None:
        raise ValueError("User not found.")
    invalidate_key_cache_for_user(normalized_user_id)
    return {
        "user_id": str(row["user_id"]),
        **_legal_state_from_row(dict(row)),
    }


def create_agent_api_key(agent_id: str, name: str = "Agent key") -> dict:
    """Create a *worker* key (`azk_...`) for the agent.

    Worker keys are valid only for claim/heartbeat/complete/release on jobs
    assigned to this agent. They cannot be used to hire other agents.
    """
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        raise ValueError("agent_id must be a non-empty string.")
    normalized_name = str(name or "").strip() or "Agent key"
    raw_key, key_hash, key_prefix = _make_agent_api_key()
    key_id = str(uuid.uuid4())
    created_at = _now()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agent_keys (key_id, agent_id, key_hash, key_prefix, key_type, name, created_at, revoked_at)
            VALUES (?, ?, ?, ?, 'worker', ?, ?, NULL)
            """,
            (key_id, normalized_agent_id, key_hash, key_prefix, normalized_name, created_at),
        )
    return {
        "key_id": key_id,
        "agent_id": normalized_agent_id,
        "raw_key": raw_key,
        "key_prefix": key_prefix,
        "key_type": "worker",
        "name": normalized_name,
        "created_at": created_at,
    }


def create_agent_caller_api_key(agent_id: str, name: str = "Caller key") -> dict:
    """Create a *caller* key (`azac_...`) that authenticates as the agent itself.

    When a request comes in with this key, ``verify_agent_api_key`` returns
    ``key_type='caller'`` and the auth layer sets ``owner_id='agent:<agent_id>'``
    so all wallet/billing logic naturally charges the agent's sub-wallet.
    """
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        raise ValueError("agent_id must be a non-empty string.")
    normalized_name = str(name or "").strip() or "Caller key"
    raw_key, key_hash, key_prefix = _make_agent_caller_api_key()
    key_id = str(uuid.uuid4())
    created_at = _now()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agent_keys (key_id, agent_id, key_hash, key_prefix, key_type, name, created_at, revoked_at)
            VALUES (?, ?, ?, ?, 'caller', ?, ?, NULL)
            """,
            (key_id, normalized_agent_id, key_hash, key_prefix, normalized_name, created_at),
        )
    return {
        "key_id": key_id,
        "agent_id": normalized_agent_id,
        "raw_key": raw_key,
        "key_prefix": key_prefix,
        "key_type": "caller",
        "name": normalized_name,
        "created_at": created_at,
    }


def verify_agent_api_key(raw_key: str) -> dict | None:
    """Verify any agent key (worker `azk_` or caller `azac_`).

    Returns ``{key_id, agent_id, owner_id, key_type}`` where ``key_type`` is
    ``'worker'`` or ``'caller'``. The auth layer in ``server.application_parts``
    branches on ``key_type`` to build the right ``CallerContext``.
    """
    if not (raw_key.startswith(AGENT_KEY_PREFIX) or raw_key.startswith(AGENT_CALLER_KEY_PREFIX)):
        return None
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    with _conn() as conn:
        try:
            row = conn.execute(
                """
                SELECT ak.key_id, ak.agent_id, ak.key_type, a.owner_id, a.status AS agent_status
                FROM agent_keys ak
                JOIN agents a ON a.agent_id = ak.agent_id
                WHERE ak.key_hash = ? AND ak.revoked_at IS NULL
                """,
                (key_hash,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    if row is None:
        return None
    status = str(row["agent_status"] or "active").strip().lower()
    if status != "active":
        return None
    key_type = str(row["key_type"] or "worker").strip().lower()
    if key_type not in {"worker", "caller"}:
        key_type = "worker"
    return {
        "key_id": row["key_id"],
        "agent_id": row["agent_id"],
        "owner_id": row["owner_id"],
        "key_type": key_type,
    }


def list_api_keys(user_id: str) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT key_id, key_prefix, name, scopes, max_spend_cents, per_job_cap_cents, created_at, last_used_at, is_active"
            " FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    keys: list[dict] = []
    for row in rows:
        item = dict(row)
        item["scopes"] = _decode_scopes_json(item.get("scopes"))
        item["max_spend_cents"] = (
            int(item["max_spend_cents"])
            if item.get("max_spend_cents") is not None
            else None
        )
        item["per_job_cap_cents"] = (
            int(item["per_job_cap_cents"])
            if item.get("per_job_cap_cents") is not None
            else None
        )
        keys.append(item)
    return keys


def list_agent_api_keys(agent_id: str) -> list[dict]:
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        raise ValueError("agent_id must be a non-empty string.")
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT key_id, agent_id, key_prefix, name, created_at, revoked_at
            FROM agent_keys
            WHERE agent_id = ?
            ORDER BY created_at DESC
            """,
            (normalized_agent_id,),
        ).fetchall()
    keys: list[dict] = []
    for row in rows:
        item = dict(row)
        item["is_active"] = item.get("revoked_at") is None
        keys.append(item)
    return keys


def revoke_api_key(key_id: str, user_id: str) -> bool:
    with _conn() as conn:
        result = conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_id = ? AND user_id = ? AND is_active = 1",
            (key_id, user_id),
        )
    return result.rowcount > 0


def rotate_api_key(
    key_id: str,
    user_id: str,
    name: str | None = None,
    scopes: list[str] | tuple[str, ...] | set[str] | None = None,
    max_spend_cents: int | None = None,
    per_job_cap_cents: int | None = None,
    *,
    max_spend_cents_provided: bool = False,
    per_job_cap_cents_provided: bool = False,
) -> dict | None:
    normalized_scopes = (
        _normalize_scopes(scopes)
        if scopes is not None
        else None
    )
    normalized_max_spend = (
        _normalize_optional_non_negative_int(max_spend_cents, field_name="max_spend_cents")
        if max_spend_cents_provided
        else None
    )
    normalized_per_job_cap = (
        _normalize_optional_non_negative_int(per_job_cap_cents, field_name="per_job_cap_cents")
        if per_job_cap_cents_provided
        else None
    )
    replacement_name = (name or "").strip() or None

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT key_id, name, scopes, max_spend_cents, per_job_cap_cents
            FROM api_keys
            WHERE key_id = ? AND user_id = ? AND is_active = 1
            """,
            (key_id, user_id),
        ).fetchone()
        if row is None:
            return None

        final_name = replacement_name or row["name"]
        final_scopes = normalized_scopes or _decode_scopes_json(row["scopes"])
        final_max_spend = (
            normalized_max_spend
            if max_spend_cents_provided
            else (
                int(row["max_spend_cents"])
                if row["max_spend_cents"] is not None
                else None
            )
        )
        final_per_job_cap = (
            normalized_per_job_cap
            if per_job_cap_cents_provided
            else (
                int(row["per_job_cap_cents"])
                if row["per_job_cap_cents"] is not None
                else None
            )
        )

        raw, key_hash, prefix = _make_api_key()
        new_key_id = str(uuid.uuid4())
        now = _now()

        conn.execute(
            """
            INSERT INTO api_keys (
                key_id,
                user_id,
                key_hash,
                key_prefix,
                name,
                scopes,
                max_spend_cents,
                per_job_cap_cents,
                created_at,
                is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                new_key_id,
                user_id,
                key_hash,
                prefix,
                final_name,
                json.dumps(final_scopes),
                final_max_spend,
                final_per_job_cap,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE api_keys
            SET is_active = 0
            WHERE key_id = ? AND user_id = ?
            """,
            (key_id, user_id),
        )

    return {
        "rotated_key_id": key_id,
        "new_key_id": new_key_id,
        "raw_key": raw,
        "key_prefix": prefix,
        "name": final_name,
        "scopes": final_scopes,
        "max_spend_cents": final_max_spend,
        "per_job_cap_cents": final_per_job_cap,
    }


# ── Password reset (OTP via email) ────────────────────────────────────────────

_OTP_EXPIRY_MINUTES = 15
_OTP_LENGTH = 6


class PasswordResetError(Exception):
    pass


def _otp_hash(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def create_password_reset_token(email: str) -> str | None:
    """
    Generate a 6-digit OTP for the given email. Stores a hash in the DB.
    Returns the raw OTP (to be emailed), or None if no account found.
    Silently succeeds for unknown emails (don't leak account existence).
    """
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None

    with _conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE LOWER(email) = ? AND status = 'active'",
            (normalized,),
        ).fetchone()
    if row is None:
        return None

    user_id = str(row["user_id"])
    otp = "".join(str(random.randint(0, 9)) for _ in range(_OTP_LENGTH))
    token_hash = _otp_hash(otp)
    token_id = str(uuid.uuid4())
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=_OTP_EXPIRY_MINUTES)
    ).isoformat()

    with _conn() as conn:
        # Invalidate any previous unused tokens for this user
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (_now(), user_id),
        )
        conn.execute(
            "INSERT INTO password_reset_tokens (token_id, user_id, token_hash, expires_at) VALUES (?, ?, ?, ?)",
            (token_id, user_id, token_hash, expires_at),
        )

    return otp


def consume_password_reset_token(email: str, otp: str, new_password: str) -> None:
    """
    Verify OTP for email and set new password. Raises PasswordResetError on any failure.
    """
    normalized_email = str(email or "").strip().lower()
    normalized_otp = str(otp or "").strip()
    if not normalized_email or not normalized_otp:
        raise PasswordResetError("Email and code are required.")

    if len(new_password) < 8 or not any(c.isalpha() for c in new_password) or not any(c.isdigit() for c in new_password):
        raise PasswordResetError("Password must be at least 8 characters and include letters and numbers.")

    token_hash = _otp_hash(normalized_otp)
    now_iso = _now()

    with _conn() as conn:
        row = conn.execute(
            """
            SELECT prt.token_id, prt.user_id, prt.expires_at, prt.used_at
            FROM password_reset_tokens prt
            JOIN users u ON prt.user_id = u.user_id
            WHERE prt.token_hash = ?
              AND LOWER(u.email) = ?
              AND u.status = 'active'
            """,
            (token_hash, normalized_email),
        ).fetchone()

        if row is None:
            raise PasswordResetError("Invalid or expired code. Request a new one.")
        if row["used_at"] is not None:
            raise PasswordResetError("This code has already been used. Request a new one.")
        if row["expires_at"] < now_iso:
            raise PasswordResetError("This code has expired. Request a new one.")

        user_id = str(row["user_id"])
        token_id = str(row["token_id"])
        salt = secrets.token_hex(16)
        new_hash = _hash_password(new_password, salt)

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE user_id = ?",
            (new_hash, salt, user_id),
        )
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE token_id = ?",
            (now_iso, token_id),
        )
        # Revoke all existing API keys so old sessions are invalidated
        conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE user_id = ?",
            (user_id,),
        )

    invalidate_key_cache_for_user(user_id)


# ─── Signup email verification ──────────────────────────────────────────────


class SignupVerificationError(Exception):
    """Raised when a pending signup token cannot be issued or consumed."""


def _validate_signup_inputs(username: str, email: str, password: str, role: str) -> tuple[str, str]:
    if role not in _VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: builder, hirer, both.")
    normalized_email = str(email or "").strip().lower()
    normalized_username = str(username or "").strip()
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("Enter a valid email address.")
    if len(normalized_username) < 3 or len(normalized_username) > 32:
        raise ValueError("Username must be between 3 and 32 characters.")
    if len(password) < 8 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise ValueError("Password must be at least 8 characters and include letters and numbers.")
    return normalized_email, normalized_username


def issue_signup_verification(username: str, email: str, password: str, role: str = "both") -> str:
    """
    Validate registration input, ensure the email is free, and store a pending
    signup row keyed by a 6-digit OTP. Returns the raw OTP so the caller can
    email it. Does NOT create the user row — that only happens on consume.
    """
    normalized_email, normalized_username = _validate_signup_inputs(username, email, password, role)

    with _conn() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE LOWER(email) = ?",
            (normalized_email,),
        ).fetchone()
    if existing is not None:
        raise ValueError("An account with that email already exists.")

    salt = secrets.token_hex(32)
    pw_hash = _hash_password(password, salt)
    otp = "".join(str(random.randint(0, 9)) for _ in range(_OTP_LENGTH))
    code_hash = _otp_hash(otp)
    token_id = str(uuid.uuid4())
    now = _now()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=_OTP_EXPIRY_MINUTES)
    ).isoformat()

    with _conn() as conn:
        # Invalidate prior unconsumed tokens for this email so only the latest works.
        conn.execute(
            "UPDATE signup_verification_tokens SET consumed_at = ? "
            "WHERE LOWER(email) = ? AND consumed_at IS NULL",
            (now, normalized_email),
        )
        conn.execute(
            "INSERT INTO signup_verification_tokens "
            "(token_id, email, username, password_hash, salt, role, code_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_id,
                normalized_email,
                normalized_username,
                pw_hash,
                salt,
                role,
                code_hash,
                now,
                expires_at,
            ),
        )

    return otp


def reissue_signup_verification_otp(email: str) -> str | None:
    """Mint a new OTP for the most recent pending signup row, invalidating
    earlier ones. Returns None if no pending signup exists."""
    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        return None

    with _conn() as conn:
        row = conn.execute(
            "SELECT token_id FROM signup_verification_tokens "
            "WHERE LOWER(email) = ? AND consumed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (normalized_email,),
        ).fetchone()
    if row is None:
        return None

    otp = "".join(str(random.randint(0, 9)) for _ in range(_OTP_LENGTH))
    code_hash = _otp_hash(otp)
    now = _now()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=_OTP_EXPIRY_MINUTES)
    ).isoformat()

    with _conn() as conn:
        conn.execute(
            "UPDATE signup_verification_tokens SET code_hash = ?, expires_at = ?, created_at = ? "
            "WHERE token_id = ?",
            (code_hash, expires_at, now, row["token_id"]),
        )

    return otp


def consume_signup_verification(email: str, otp: str) -> dict:
    """
    Verify the OTP for a pending signup, create the user + initial Session API
    key, and return the same payload `register_user` produced. Raises
    SignupVerificationError on any failure.
    """
    normalized_email = str(email or "").strip().lower()
    normalized_otp = str(otp or "").strip()
    if not normalized_email or len(normalized_otp) != _OTP_LENGTH or not normalized_otp.isdigit():
        raise SignupVerificationError("Enter the 6-digit code from your email.")

    code_hash = _otp_hash(normalized_otp)
    now_iso = _now()

    with _conn() as conn:
        row = conn.execute(
            "SELECT token_id, email, username, password_hash, salt, role, expires_at, consumed_at "
            "FROM signup_verification_tokens "
            "WHERE LOWER(email) = ? AND code_hash = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (normalized_email, code_hash),
        ).fetchone()

        if row is None:
            raise SignupVerificationError("Invalid or expired code. Request a new one.")
        if row["consumed_at"] is not None:
            raise SignupVerificationError("This code has already been used. Request a new one.")
        if row["expires_at"] < now_iso:
            raise SignupVerificationError("This code has expired. Request a new one.")

        # Re-check email is still free (someone might have registered concurrently).
        existing = conn.execute(
            "SELECT user_id FROM users WHERE LOWER(email) = ?",
            (normalized_email,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE signup_verification_tokens SET consumed_at = ? WHERE token_id = ?",
                (now_iso, row["token_id"]),
            )
            raise SignupVerificationError("An account with that email already exists.")

        user_id = str(uuid.uuid4())
        raw_key, key_hash, key_prefix = _make_api_key()
        key_id = str(uuid.uuid4())
        session_scopes_json = json.dumps(list(DEFAULT_KEY_SCOPES))

        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, salt, created_at, role)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    row["username"],
                    row["email"],
                    row["password_hash"],
                    row["salt"],
                    now_iso,
                    row["role"],
                ),
            )
            conn.execute(
                "INSERT INTO api_keys (key_id, user_id, key_hash, key_prefix, name, scopes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, user_id, key_hash, key_prefix, "Session key", session_scopes_json, now_iso),
            )
            conn.execute(
                "UPDATE signup_verification_tokens SET consumed_at = ? WHERE token_id = ?",
                (now_iso, row["token_id"]),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "users.email" in message or "unique constraint failed: users.email" in message:
                raise SignupVerificationError("An account with that email already exists.") from exc
            raise

    return {
        "user_id": user_id,
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "raw_api_key": raw_key,
        "key_id": key_id,
        "key_prefix": key_prefix,
        **_legal_state_from_row({}),
    }
