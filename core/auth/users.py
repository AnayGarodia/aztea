# OWNS: user registration/login, API key lifecycle (create/verify/rotate/delete)
# NOT OWNS: DB schema + hashing (auth/schema.py), agent registration (registry/)
#
# INVARIANTS:
# - raw API key values are NEVER logged — only the prefix (redaction filter in part_000.py)
# - keys are stored as salted SHA-256 digests; the raw value is only returned on creation
# - agent-scoped worker keys (azac_...) cannot be used for caller-side operations — enforced by scope check
# - signup credit is credited by the auth route after registration using payments.SIGNUP_CREDIT_CENTS
#
# DECISIONS:
# - legal acceptance state (terms_version, privacy_version) is returned on every auth response
#   so the frontend can prompt re-acceptance when server constants bump — don't move it to a
#   separate endpoint or the frontend will miss the prompt

from __future__ import annotations

import hashlib
import json
import logging
import secrets

from core import db as _db
from core.functional import Err, Ok, Result
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

_LOG = logging.getLogger(__name__)

from .schema import (
    AGENT_CALLER_KEY_PREFIX,
    AGENT_KEY_PREFIX,
    DEFAULT_KEY_SCOPES,
    KEY_PREFIX,
    LEGAL_PRIVACY_VERSION,
    LEGAL_TERMS_VERSION,
    MAX_USER_AGENT_LEN,
    MAX_USERNAME_LEN,
    MIN_PASSWORD_LEN,
    MIN_USERNAME_LEN,
    PBKDF2_ITERATIONS,
    PBKDF2_LEGACY_ITERATIONS,
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


def _validate_register_params(username: str, email: str, password: str, role: str) -> "Result[tuple[str, str, str, str], str]":
    """Pure guard: normalises and validates registration inputs without DB access."""
    normalized_role = str(role or "").strip().lower()
    if normalized_role not in _VALID_ROLES:
        return Err(f"Invalid role '{role}'. Must be one of: builder, hirer, both.")
    normalized_username = str(username or "").strip()
    if len(normalized_username) < MIN_USERNAME_LEN or len(normalized_username) > MAX_USERNAME_LEN:
        return Err(
            f"Username must be between {MIN_USERNAME_LEN} and {MAX_USERNAME_LEN} characters."
        )
    normalized_email = str(email or "").strip().lower()
    if not normalized_email or "@" not in normalized_email:
        return Err("A valid email address is required.")
    if len(password) < MIN_PASSWORD_LEN:
        return Err(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    return Ok((normalized_username, normalized_email, password, normalized_role))


def _register_user_db(params: tuple) -> "Result[dict, str]":
    """DB step of registration — pure Result, no raising.

    Receives the validated (username, email, password, role) tuple from
    ``_validate_register_params`` via ``and_then``.  Returns Err on duplicate
    email so callers can handle it without a try/except.
    """
    normalized_username, normalized_email, password, role = params
    user_id = str(uuid.uuid4())
    salt = secrets.token_hex(32)
    pw_hash = _hash_password(password, salt, iterations=PBKDF2_ITERATIONS)
    raw_key, key_hash, key_prefix = _make_api_key()
    key_id = str(uuid.uuid4())
    session_scopes_json = json.dumps(list(DEFAULT_KEY_SCOPES))
    now = _now()

    with _conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, salt, pbkdf2_iterations, created_at, role)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (user_id, normalized_username, normalized_email, pw_hash, salt, PBKDF2_ITERATIONS, now, role),
            )
            conn.execute(
                "INSERT INTO api_keys (key_id, user_id, key_hash, key_prefix, name, scopes, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (key_id, user_id, key_hash, key_prefix, "Session key", session_scopes_json, now),
            )
        except _db.IntegrityError as exc:
            message = str(exc).lower()
            if (
                "users.email" in message
                or "unique constraint failed: users.email" in message
            ):
                return Err("An account with that email already exists.")
            raise  # unexpected — let it propagate as 500

    return Ok({
        "user_id": user_id,
        "username": normalized_username,
        "email": normalized_email,
        "role": role,
        "raw_api_key": raw_key,
        "key_id": key_id,
        "key_prefix": key_prefix,
        **_legal_state_from_row({}),
    })


def register_user_result(username: str, email: str, password: str, role: str = "both") -> "Result[dict, str]":
    """Register a new user, returning Result[dict, str] instead of raising.

    Composes validation and DB steps with ``and_then`` so the happy path is a
    single chain and every Err short-circuits automatically.  Route handlers
    that call this need no try/except for expected errors.
    """
    return (
        _validate_register_params(username, email, password, role)
        .and_then(_register_user_db)
    )


def register_user(username: str, email: str, password: str, role: str = "both") -> dict:
    """Register a new user, raising ValueError on invalid inputs or duplicate email.

    Thin raising wrapper around ``register_user_result`` for callers that prefer
    exceptions (e.g. admin scripts, tests that expect ValueError).
    """
    result = register_user_result(username, email, password, role)
    result.raise_on_err()
    return result.value  # type: ignore[union-attr]


class AccountSuspendedError(Exception):
    """Raised when a suspended or banned account attempts to log in."""


def login_user(
    email: str | None = None,
    password: str = "",
    *,
    username: str | None = None,
    rotate: bool = False,
) -> dict | None:
    """
    Verify credentials. Returns user dict, or None if wrong credentials.
    Raises AccountSuspendedError if the account is suspended or banned.

    By default the caller's existing active "Session key" is reused so SDK
    clients do not see their key change on every login (and so logins do not
    leave a long trail of revoked keys behind them). Pass ``rotate=True`` to
    force a fresh key — useful after a suspected credential leak. The
    ``username`` parameter is accepted as an alternative identifier; either
    ``email`` or ``username`` must be supplied.
    """
    if not (email or username):
        return None
    with _conn() as conn:
        if email:
            row = conn.execute(
                "SELECT * FROM users WHERE email = %s", (email.lower().strip(),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM users WHERE username = %s", (str(username).strip(),)
            ).fetchone()
    if row is None:
        return None
    user = dict(row)
    status = str(user.get("status") or "active").strip().lower()
    if status != "active":
        raise AccountSuspendedError(status)
    # Verify at the user's stored cost so legacy hashes (100k) still compare
    # correctly after the constant rises. Migration 0066 backfills the column
    # to PBKDF2_LEGACY_ITERATIONS for pre-existing rows; fall back to the
    # legacy floor here for test fixtures or partially-migrated databases
    # where the column is missing/null/0.
    stored_iterations = int(user.get("pbkdf2_iterations") or 0) or PBKDF2_LEGACY_ITERATIONS
    expected = _hash_password(password, user["salt"], iterations=stored_iterations)
    if not secrets.compare_digest(user["password_hash"], expected):
        return None
    # Opportunistic rehash: if the stored cost is below the current default,
    # the password is correct so we know its plaintext; rehash at the new cost.
    # No-op on the happy path where the user is already at the current default.
    if stored_iterations < PBKDF2_ITERATIONS:
        new_salt = secrets.token_hex(32)
        new_hash = _hash_password(password, new_salt, iterations=PBKDF2_ITERATIONS)
        with _conn() as conn:
            conn.execute(
                "UPDATE users SET password_hash = %s, salt = %s, pbkdf2_iterations = %s WHERE user_id = %s",
                (new_hash, new_salt, PBKDF2_ITERATIONS, user["user_id"]),
            )
        user["password_hash"] = new_hash
        user["salt"] = new_salt
        user["pbkdf2_iterations"] = PBKDF2_ITERATIONS

    raw_key: str | None = None
    key_id: str | None = None
    key_prefix: str | None = None
    if not rotate:
        # Return the most recent active Session key when one exists. We can't
        # return the *raw* key for an existing row (the raw value is never
        # stored), so we mint a new one only when no active session key
        # exists. This avoids the unbounded-key-rows complaint while still
        # giving cold callers a usable credential.
        with _conn() as conn:
            existing = conn.execute(
                """
                SELECT key_id, key_prefix
                FROM api_keys
                WHERE user_id = %s AND name = 'Session key' AND is_active = 1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user["user_id"],),
            ).fetchone()
        if existing is not None:
            key_id = existing["key_id"]
            key_prefix = existing["key_prefix"]
            # raw_key is intentionally None — caller already has it client-side.

    if rotate or raw_key is None and key_id is None:
        # Mint a fresh session key. We deliberately do NOT revoke the prior
        # sessions here — multiple concurrent session keys must coexist so
        # that signing in on the web doesn't invalidate the user's MCP /
        # CLI / SDK session (and vice versa). Callers who want a true
        # rotation (suspected key leak) should call /auth/keys/revoke for
        # the specific key id.
        result = _create_key_for_user(user["user_id"], "Session key")
        raw_key = result["raw_key"]
        key_id = result["key_id"]
        key_prefix = result["key_prefix"]

    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "email": user["email"],
        "role": user.get("role") or "both",
        "created_at": user["created_at"],
        # raw_api_key may be None when an existing session key was reused —
        # the SDK is expected to still have the cached value client-side.
        "raw_api_key": raw_key,
        "key_id": key_id,
        "key_prefix": key_prefix,
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
            "SELECT * FROM users WHERE email = %s", (normalized_email,)
        ).fetchone()

    if row is not None:
        user = dict(row)
        status = str(user.get("status") or "active").strip().lower()
        if status != "active":
            raise AccountSuspendedError(status)
        # Reuse the most recent active Session key when one exists, matching
        # login_user's behaviour. Pre-fix this branch unconditionally revoked
        # + minted on every Google sign-in, so repeated logins left a long
        # trail of revoked keys for the same account (and broke any client
        # — MCP, SDK, CLI — that was still holding a previously-issued key).
        # When no active session exists we mint one so brand-new Google
        # users still get a usable raw key back.
        with _conn() as conn:
            existing = conn.execute(
                """
                SELECT key_id, key_prefix
                FROM api_keys
                WHERE user_id = %s AND name = 'Session key' AND is_active = 1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user["user_id"],),
            ).fetchone()
        if existing is not None:
            raw_key = None
            key_id = existing["key_id"]
            key_prefix = existing["key_prefix"]
        else:
            minted = _create_key_for_user(user["user_id"], "Session key")
            raw_key = minted["raw_key"]
            key_id = minted["key_id"]
            key_prefix = minted["key_prefix"]
        return (
            {
                "user_id": user["user_id"],
                "username": user["username"],
                "email": user["email"],
                "role": user.get("role") or "both",
                "created_at": user["created_at"],
                "raw_api_key": raw_key,
                "key_id": key_id,
                "key_prefix": key_prefix,
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
        while (
            conn.execute(
                "SELECT 1 FROM users WHERE username = %s", (candidate,)
            ).fetchone()
            is not None
        ):
            suffix += 1
            candidate = f"{base_username}{suffix}"[:32]
            if suffix > 9999:
                candidate = f"user{secrets.token_hex(4)}"
                break
    random_password = secrets.token_urlsafe(24)
    return register_user(
        candidate, normalized_email, random_password, role="both"
    ), True


def get_user_by_id(user_id: str) -> dict | None:
    """Fetch a user record by ID. Returns None if not found. Password hash is stripped from the result."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    d.pop("salt", None)
    return d


def update_user_role(user_id: str, role: str) -> None:
    """Update the role for a user. Raises ValueError for invalid role."""
    if role not in _VALID_ROLES:
        raise ValueError(
            f"Invalid role '{role}'. Must be one of: builder, hirer, both."
        )
    with _conn() as conn:
        conn.execute("UPDATE users SET role = %s WHERE user_id = %s", (role, user_id))


def update_user_profile(
    user_id: str,
    *,
    full_name: str | None = None,
    phone: str | None = None,
) -> dict | None:
    """B23, 2026-05-19: minimal profile-update surface for /users/me.

    Updates full_name and/or phone on the user row. Either field accepts
    None as a no-op (no SQL touch); pass an empty string to clear. Email
    changes require a verification flow and are intentionally NOT
    supported here. Returns the updated user dict; returns None if the
    user does not exist.

    Raises ValueError on overly-long inputs so the API boundary stays
    strict.
    """
    updates: list[tuple[str, str | None]] = []
    if full_name is not None:
        normalized_name = str(full_name).strip()
        if len(normalized_name) > 200:
            raise ValueError("full_name must be 200 characters or fewer.")
        updates.append(("full_name", normalized_name or None))
    if phone is not None:
        normalized_phone = str(phone).strip()
        if len(normalized_phone) > 32:
            raise ValueError("phone must be 32 characters or fewer.")
        updates.append(("phone", normalized_phone or None))
    if not updates:
        return get_user_by_id(user_id)
    set_clauses = ", ".join(f"{field} = %s" for field, _ in updates)
    params = tuple(value for _, value in updates) + (user_id,)
    with _conn() as conn:
        conn.execute(
            f"UPDATE users SET {set_clauses} WHERE user_id = %s",
            params,
        )
    return get_user_by_id(user_id)


def record_legal_acceptance(
    user_id: str,
    *,
    terms_version: str | None = None,
    privacy_version: str | None = None,
) -> dict | None:
    """Record that a user has accepted the current Terms of Service + Privacy Policy.

    Stamps ``terms_version_accepted``, ``privacy_version_accepted``, and
    ``legal_accepted_at`` on the user row. The platform-current versions live
    in ``core.auth.schema`` (``LEGAL_TERMS_VERSION`` / ``LEGAL_PRIVACY_VERSION``);
    callers may override per-version for explicit historical acceptance, but the
    common path leaves them None and the current versions are recorded.

    Returns the updated legal-state dict (the same shape ``/auth/me`` exposes),
    or ``None`` when the user does not exist.
    """
    accepted_terms = (terms_version or LEGAL_TERMS_VERSION).strip()
    accepted_privacy = (privacy_version or LEGAL_PRIVACY_VERSION).strip()
    accepted_at = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id = %s", (str(user_id),)
        ).fetchone()
        if existing is None:
            return None
        conn.execute(
            """
            UPDATE users
            SET terms_version_accepted = %s,
                privacy_version_accepted = %s,
                legal_accepted_at = %s
            WHERE user_id = %s
            """,
            (accepted_terms, accepted_privacy, accepted_at, str(user_id)),
        )
        row = conn.execute(
            """
            SELECT terms_version_accepted, privacy_version_accepted, legal_accepted_at
            FROM users WHERE user_id = %s
            """,
            (str(user_id),),
        ).fetchone()
    # Drop the in-memory api-key cache for this user so the *next* authenticated
    # request reads the freshly-stamped legal columns. Without this the gate
    # keeps firing for up to ``_KEY_CACHE_TTL`` seconds even though the user
    # has accepted.
    invalidate_key_cache_for_user(str(user_id))
    return _legal_state_from_row(dict(row) if row else {})


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
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
            "SELECT COUNT(*) AS n FROM api_keys WHERE user_id = %s AND is_active = 1 AND name != 'Session key'",
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
                "UPDATE api_keys SET last_used_at = %s WHERE key_id = %s",
                (_now(), key_id),
            )
    except Exception:
        # Non-fatal: last_used_at is best-effort; don't fail the auth path
        _LOG.warning("Failed to flush last_used_at for key %s", key_id, exc_info=True)


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
            WHERE ak.key_hash = %s AND ak.is_active = 1 AND u.status = 'active'
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
            int(row["max_spend_cents"]) if row["max_spend_cents"] is not None else None
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


def api_key_is_revoked(raw_key: str) -> bool:
    """Return True when a syntactically valid user API key exists but is inactive."""
    if not raw_key.startswith(KEY_PREFIX):
        return False
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    with _conn() as conn:
        row = conn.execute(
            "SELECT is_active FROM api_keys WHERE key_hash = %s",
            (key_hash,),
        ).fetchone()
    return row is not None and int(row["is_active"] or 0) == 0


def accept_legal_terms(
    user_id: str,
    *,
    terms_version: str,
    privacy_version: str,
    accepted_ip: str | None = None,
    accepted_user_agent: str | None = None,
) -> dict:
    """Record that a user has accepted the current platform ToS and privacy policy.

    Raises ``ValueError`` if ``terms_version`` or ``privacy_version`` do not match the
    current canonical versions. Returns the updated user dict.
    """
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        raise ValueError("user_id is required.")

    normalized_terms = str(terms_version or "").strip()
    normalized_privacy = str(privacy_version or "").strip()
    if (
        normalized_terms != LEGAL_TERMS_VERSION
        or normalized_privacy != LEGAL_PRIVACY_VERSION
    ):
        raise ValueError(
            "Legal version mismatch. Refresh terms and privacy and try again."
        )

    now = _now()
    normalized_ip = _normalize_optional_text(accepted_ip)
    normalized_ua = _normalize_optional_text(accepted_user_agent)
    if normalized_ua and len(normalized_ua) > MAX_USER_AGENT_LEN:
        normalized_ua = normalized_ua[:MAX_USER_AGENT_LEN]

    with _conn() as conn:
        result = conn.execute(
            """
            UPDATE users
            SET terms_version_accepted = %s,
                privacy_version_accepted = %s,
                legal_accepted_at = %s,
                legal_accept_ip = %s,
                legal_accept_user_agent = %s
            WHERE user_id = %s
            """,
            (
                normalized_terms,
                normalized_privacy,
                now,
                normalized_ip,
                normalized_ua,
                normalized_user_id,
            ),
        )
        if result.rowcount < 1:
            raise ValueError("User not found.")
        row = conn.execute(
            """
            SELECT user_id, terms_version_accepted, privacy_version_accepted, legal_accepted_at
            FROM users
            WHERE user_id = %s
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
            VALUES (%s, %s, %s, %s, 'worker', %s, %s, NULL)
            """,
            (
                key_id,
                normalized_agent_id,
                key_hash,
                key_prefix,
                normalized_name,
                created_at,
            ),
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
            VALUES (%s, %s, %s, %s, 'caller', %s, %s, NULL)
            """,
            (
                key_id,
                normalized_agent_id,
                key_hash,
                key_prefix,
                normalized_name,
                created_at,
            ),
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
    if not (
        raw_key.startswith(AGENT_KEY_PREFIX)
        or raw_key.startswith(AGENT_CALLER_KEY_PREFIX)
    ):
        return None
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    with _conn() as conn:
        try:
            row = conn.execute(
                """
                SELECT ak.key_id, ak.agent_id, ak.key_type, a.owner_id, a.status AS agent_status
                FROM agent_keys ak
                JOIN agents a ON a.agent_id = ak.agent_id
                WHERE ak.key_hash = %s AND ak.revoked_at IS NULL
                """,
                (key_hash,),
            ).fetchone()
        except _db.OperationalError:
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
    """Return all non-revoked API keys for a user, ordered newest-first. Never returns raw key values."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT key_id, key_prefix, name, scopes, max_spend_cents, per_job_cap_cents, created_at, last_used_at, is_active"
            " FROM api_keys WHERE user_id = %s ORDER BY created_at DESC",
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
    """Return agent-scoped worker keys for a given ``agent_id``, ordered newest-first."""
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        raise ValueError("agent_id must be a non-empty string.")
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT key_id, agent_id, key_prefix, name, created_at, revoked_at
            FROM agent_keys
            WHERE agent_id = %s
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
            "UPDATE api_keys SET is_active = 0 WHERE key_id = %s AND user_id = %s AND is_active = 1",
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
    """Revoke an existing API key and issue a replacement with the same (or new) scope/limits.

    Returns the new key dict (including the raw secret, only time it is returned), or None
    if the old key was not found or already revoked.
    """
    normalized_scopes = _normalize_scopes(scopes) if scopes is not None else None
    normalized_max_spend = (
        _normalize_optional_non_negative_int(
            max_spend_cents, field_name="max_spend_cents"
        )
        if max_spend_cents_provided
        else None
    )
    normalized_per_job_cap = (
        _normalize_optional_non_negative_int(
            per_job_cap_cents, field_name="per_job_cap_cents"
        )
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
            WHERE key_id = %s AND user_id = %s AND is_active = 1
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
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
            WHERE key_id = %s AND user_id = %s
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
            "SELECT user_id FROM users WHERE LOWER(email) = %s AND status = 'active'",
            (normalized,),
        ).fetchone()
    if row is None:
        return None

    user_id = str(row["user_id"])
    otp = "".join(str(secrets.randbelow(10)) for _ in range(_OTP_LENGTH))
    token_hash = _otp_hash(otp)
    token_id = str(uuid.uuid4())
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=_OTP_EXPIRY_MINUTES)
    ).isoformat()

    with _conn() as conn:
        # Invalidate any previous unused tokens for this user
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = %s WHERE user_id = %s AND used_at IS NULL",
            (_now(), user_id),
        )
        conn.execute(
            "INSERT INTO password_reset_tokens (token_id, user_id, token_hash, expires_at) VALUES (%s, %s, %s, %s)",
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

    if (
        len(new_password) < MIN_PASSWORD_LEN
        or not any(c.isalpha() for c in new_password)
        or not any(c.isdigit() for c in new_password)
    ):
        raise PasswordResetError(
            f"Password must be at least {MIN_PASSWORD_LEN} characters and include letters and numbers."
        )

    token_hash = _otp_hash(normalized_otp)
    now_iso = _now()

    with _conn() as conn:
        row = conn.execute(
            """
            SELECT prt.token_id, prt.user_id, prt.expires_at, prt.used_at
            FROM password_reset_tokens prt
            JOIN users u ON prt.user_id = u.user_id
            WHERE prt.token_hash = %s
              AND LOWER(u.email) = %s
              AND u.status = 'active'
            """,
            (token_hash, normalized_email),
        ).fetchone()

        if row is None:
            raise PasswordResetError("Invalid or expired code. Request a new one.")
        if row["used_at"] is not None:
            raise PasswordResetError(
                "This code has already been used. Request a new one."
            )
        if row["expires_at"] < now_iso:
            raise PasswordResetError("This code has expired. Request a new one.")

        user_id = str(row["user_id"])
        token_id = str(row["token_id"])
        salt = secrets.token_hex(16)
        new_hash = _hash_password(new_password, salt, iterations=PBKDF2_ITERATIONS)

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE users SET password_hash = %s, salt = %s, pbkdf2_iterations = %s WHERE user_id = %s",
            (new_hash, salt, PBKDF2_ITERATIONS, user_id),
        )
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = %s WHERE token_id = %s",
            (now_iso, token_id),
        )
        # Revoke all existing API keys so old sessions are invalidated
        conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE user_id = %s",
            (user_id,),
        )

    invalidate_key_cache_for_user(user_id)


# ─── Signup email verification ──────────────────────────────────────────────


class SignupVerificationError(Exception):
    """Raised when a pending signup token cannot be issued or consumed."""


def _validate_signup_inputs(
    username: str, email: str, password: str, role: str
) -> tuple[str, str]:
    if role not in _VALID_ROLES:
        raise ValueError(
            f"Invalid role '{role}'. Must be one of: builder, hirer, both."
        )
    normalized_email = str(email or "").strip().lower()
    normalized_username = str(username or "").strip()
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("Enter a valid email address.")
    if len(normalized_username) < MIN_USERNAME_LEN or len(normalized_username) > MAX_USERNAME_LEN:
        raise ValueError(f"Username must be between {MIN_USERNAME_LEN} and {MAX_USERNAME_LEN} characters.")
    if (
        len(password) < MIN_PASSWORD_LEN
        or not any(c.isalpha() for c in password)
        or not any(c.isdigit() for c in password)
    ):
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LEN} characters and include letters and numbers."
        )
    return normalized_email, normalized_username


def issue_signup_verification(
    username: str, email: str, password: str, role: str = "both"
) -> str:
    """
    Validate registration input, ensure the email is free, and store a pending
    signup row keyed by a 6-digit OTP. Returns the raw OTP so the caller can
    email it. Does NOT create the user row — that only happens on consume.
    """
    normalized_email, normalized_username = _validate_signup_inputs(
        username, email, password, role
    )

    with _conn() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE LOWER(email) = %s",
            (normalized_email,),
        ).fetchone()
    if existing is not None:
        raise ValueError("An account with that email already exists.")

    salt = secrets.token_hex(32)
    pw_hash = _hash_password(password, salt, iterations=PBKDF2_ITERATIONS)
    otp = "".join(str(secrets.randbelow(10)) for _ in range(_OTP_LENGTH))
    code_hash = _otp_hash(otp)
    token_id = str(uuid.uuid4())
    now = _now()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=_OTP_EXPIRY_MINUTES)
    ).isoformat()

    with _conn() as conn:
        # Invalidate prior unconsumed tokens for this email so only the latest works.
        conn.execute(
            "UPDATE signup_verification_tokens SET consumed_at = %s "
            "WHERE LOWER(email) = %s AND consumed_at IS NULL",
            (now, normalized_email),
        )
        conn.execute(
            "INSERT INTO signup_verification_tokens "
            "(token_id, email, username, password_hash, salt, pbkdf2_iterations, role, code_hash, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                token_id,
                normalized_email,
                normalized_username,
                pw_hash,
                salt,
                PBKDF2_ITERATIONS,
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
            "WHERE LOWER(email) = %s AND consumed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (normalized_email,),
        ).fetchone()
    if row is None:
        return None

    otp = "".join(str(secrets.randbelow(10)) for _ in range(_OTP_LENGTH))
    code_hash = _otp_hash(otp)
    now = _now()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=_OTP_EXPIRY_MINUTES)
    ).isoformat()

    with _conn() as conn:
        conn.execute(
            "UPDATE signup_verification_tokens SET code_hash = %s, expires_at = %s, created_at = %s "
            "WHERE token_id = %s",
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
    if (
        not normalized_email
        or len(normalized_otp) != _OTP_LENGTH
        or not normalized_otp.isdigit()
    ):
        raise SignupVerificationError("Enter the 6-digit code from your email.")

    code_hash = _otp_hash(normalized_otp)
    now_iso = _now()

    with _conn() as conn:
        row = conn.execute(
            "SELECT token_id, email, username, password_hash, salt, pbkdf2_iterations, role, expires_at, consumed_at "
            "FROM signup_verification_tokens "
            "WHERE LOWER(email) = %s AND code_hash = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (normalized_email, code_hash),
        ).fetchone()

        if row is None:
            raise SignupVerificationError("Invalid or expired code. Request a new one.")
        if row["consumed_at"] is not None:
            raise SignupVerificationError(
                "This code has already been used. Request a new one."
            )
        if row["expires_at"] < now_iso:
            raise SignupVerificationError("This code has expired. Request a new one.")

        # Re-check email is still free (someone might have registered concurrently).
        existing = conn.execute(
            "SELECT user_id FROM users WHERE LOWER(email) = %s",
            (normalized_email,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE signup_verification_tokens SET consumed_at = %s WHERE token_id = %s",
                (now_iso, row["token_id"]),
            )
            raise SignupVerificationError("An account with that email already exists.")

        user_id = str(uuid.uuid4())
        raw_key, key_hash, key_prefix = _make_api_key()
        key_id = str(uuid.uuid4())
        session_scopes_json = json.dumps(list(DEFAULT_KEY_SCOPES))

        # Tolerate legacy token rows written before migration 0066 (pre-bump
        # column did not exist). Fall back to the legacy cost so the consumed
        # row stays self-consistent — the hash on the token was produced at
        # that cost.
        token_iterations = int(row["pbkdf2_iterations"] or 0) or PBKDF2_LEGACY_ITERATIONS

        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, salt, pbkdf2_iterations, created_at, role)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    user_id,
                    row["username"],
                    row["email"],
                    row["password_hash"],
                    row["salt"],
                    token_iterations,
                    now_iso,
                    row["role"],
                ),
            )
            conn.execute(
                "INSERT INTO api_keys (key_id, user_id, key_hash, key_prefix, name, scopes, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    key_id,
                    user_id,
                    key_hash,
                    key_prefix,
                    "Session key",
                    session_scopes_json,
                    now_iso,
                ),
            )
            conn.execute(
                "UPDATE signup_verification_tokens SET consumed_at = %s WHERE token_id = %s",
                (now_iso, row["token_id"]),
            )
        except _db.IntegrityError as exc:
            message = str(exc).lower()
            if (
                "users.email" in message
                or "unique constraint failed: users.email" in message
            ):
                raise SignupVerificationError(
                    "An account with that email already exists."
                ) from exc
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
