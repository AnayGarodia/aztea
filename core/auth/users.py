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
import secrets
import sqlite3
import uuid

from .schema import (
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
    _make_api_key,
    _normalize_optional_non_negative_int,
    _normalize_optional_text,
    _normalize_scopes,
    _now,
)


def register_user(username: str, email: str, password: str) -> dict:
    """
    Create a new user and return their first API key (raw, shown once).
    Raises ValueError on duplicate email.
    """
    user_id = str(uuid.uuid4())
    salt = secrets.token_hex(32)
    pw_hash = _hash_password(password, salt)
    normalized_email = email.lower().strip()
    normalized_username = username.strip()

    raw_key, key_hash, key_prefix = _make_api_key()
    key_id = str(uuid.uuid4())
    scopes_json = json.dumps(list(DEFAULT_KEY_SCOPES))
    now = _now()

    with _conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, salt, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, normalized_username, normalized_email, pw_hash, salt, now),
            )
            conn.execute(
                "INSERT INTO api_keys (key_id, user_id, key_hash, key_prefix, name, scopes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, user_id, key_hash, key_prefix, "Default", scopes_json, now),
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
        "created_at": user["created_at"],
        "raw_api_key": result["raw_key"],
        "key_id": result["key_id"],
        "key_prefix": result["key_prefix"],
        **_legal_state_from_row(user),
    }


def get_user_by_id(user_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    d.pop("salt", None)
    return d


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


def verify_api_key(raw_key: str) -> dict | None:
    """
    Verify a raw API key against the DB. Returns user info dict or None.
    Side-effect: updates last_used_at.
    """
    if not raw_key.startswith(KEY_PREFIX):
        return None
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
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
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
            (_now(), row["key_id"]),
        )
    return {
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
    return {
        "user_id": str(row["user_id"]),
        **_legal_state_from_row(dict(row)),
    }


def create_agent_api_key(agent_id: str, name: str = "Agent key") -> dict:
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
            INSERT INTO agent_keys (key_id, agent_id, key_hash, key_prefix, name, created_at, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (key_id, normalized_agent_id, key_hash, key_prefix, normalized_name, created_at),
        )
    return {
        "key_id": key_id,
        "agent_id": normalized_agent_id,
        "raw_key": raw_key,
        "key_prefix": key_prefix,
        "name": normalized_name,
        "created_at": created_at,
    }


def verify_agent_api_key(raw_key: str) -> dict | None:
    if not raw_key.startswith(AGENT_KEY_PREFIX):
        return None
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    with _conn() as conn:
        try:
            row = conn.execute(
                """
                SELECT ak.key_id, ak.agent_id, a.owner_id, a.status AS agent_status
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
    return {
        "key_id": row["key_id"],
        "agent_id": row["agent_id"],
        "owner_id": row["owner_id"],
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
