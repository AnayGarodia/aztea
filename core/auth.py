"""
auth.py — User accounts and API key management for the agentmarket platform.

Tables (share registry.db):

  users:
    user_id TEXT PRIMARY KEY, username TEXT NOT NULL, email TEXT UNIQUE,
    password_hash TEXT, salt TEXT, created_at TEXT

  api_keys:
    key_id TEXT PRIMARY KEY, user_id TEXT, key_hash TEXT UNIQUE,
    key_prefix TEXT, name TEXT, created_at TEXT, last_used_at TEXT,
    is_active INTEGER DEFAULT 1

Design:
  - Passwords: PBKDF2-HMAC-SHA256, 260k iterations, 32-byte random salt
  - API keys: "am_" + secrets.token_hex(32)  (67 chars total)
  - Only the SHA-256 hash is stored; the raw key is returned once on creation
  - key_prefix = first 12 chars for display (am_ + 9 chars)
"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "registry.db")
KEY_PREFIX = "am_"
PBKDF2_ITERATIONS = 260_000
VALID_KEY_SCOPES = {"caller", "worker", "admin"}
DEFAULT_KEY_SCOPES = ("caller", "worker")


# ── Connection ────────────────────────────────────────────────────────────────

_local = threading.local()


def _conn() -> sqlite3.Connection:
    """Thread-local connection with WAL mode to match registry/payments."""
    if not getattr(_local, "conn", None):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Schema ────────────────────────────────────────────────────────────────────

def init_auth_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       TEXT PRIMARY KEY,
                username      TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id        TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                key_hash      TEXT NOT NULL UNIQUE,
                key_prefix    TEXT NOT NULL,
                name          TEXT NOT NULL DEFAULT 'Default',
                scopes        TEXT NOT NULL DEFAULT '["caller","worker"]',
                created_at    TEXT NOT NULL,
                last_used_at  TEXT,
                is_active     INTEGER NOT NULL DEFAULT 1
            )
        """)
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()
        }
        if "scopes" not in cols:
            conn.execute(
                """ALTER TABLE api_keys
                   ADD COLUMN scopes TEXT NOT NULL DEFAULT '["caller","worker"]'"""
            )
            conn.execute(
                """
                UPDATE api_keys
                SET scopes = '["caller","worker"]'
                WHERE scopes IS NULL OR TRIM(scopes) = ''
                """
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_user_active ON api_keys(user_id, is_active)"
        )


# ── Hashing ───────────────────────────────────────────────────────────────────

def _hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return dk.hex()


def _make_api_key() -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix)."""
    raw = KEY_PREFIX + secrets.token_hex(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest(), raw[:12]


def _decode_scopes_json(raw_scopes: str | None) -> list[str]:
    try:
        parsed = json.loads(raw_scopes or "[]")
    except json.JSONDecodeError:
        return list(DEFAULT_KEY_SCOPES)
    if not isinstance(parsed, list):
        return list(DEFAULT_KEY_SCOPES)
    normalized: list[str] = []
    for scope in parsed:
        value = str(scope).strip().lower()
        if value in VALID_KEY_SCOPES and value not in normalized:
            normalized.append(value)
    return normalized or list(DEFAULT_KEY_SCOPES)


def _normalize_scopes(scopes: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    if scopes is None:
        return list(DEFAULT_KEY_SCOPES)
    if isinstance(scopes, (set, tuple)):
        candidate_scopes = list(scopes)
    else:
        candidate_scopes = scopes
    if not isinstance(candidate_scopes, list):
        raise ValueError("scopes must be a list of strings.")

    normalized: list[str] = []
    for scope in candidate_scopes:
        value = str(scope).strip().lower()
        if not value:
            continue
        if value not in VALID_KEY_SCOPES:
            valid = ", ".join(sorted(VALID_KEY_SCOPES))
            raise ValueError(f"Invalid key scope '{value}'. Valid scopes: {valid}.")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise ValueError("At least one key scope is required.")
    return normalized


# ── User management ───────────────────────────────────────────────────────────

def register_user(username: str, email: str, password: str) -> dict:
    """
    Create a new user and return their first API key (raw, shown once).
    Raises ValueError on duplicate email.
    """
    user_id = str(uuid.uuid4())
    salt = secrets.token_hex(32)
    pw_hash = _hash_password(password, salt)

    with _conn() as conn:
        try:
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, salt, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username.strip(), email.lower().strip(), pw_hash, salt, _now()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("An account with that email already exists.")

    result = _create_key_for_user(user_id, "Default")
    return {
        "user_id": user_id,
        "username": username.strip(),
        "email": email.lower().strip(),
        "raw_api_key": result["raw_key"],
        "key_id": result["key_id"],
        "key_prefix": result["key_prefix"],
    }


def login_user(email: str, password: str) -> dict | None:
    """
    Verify credentials. Returns user dict, or None if wrong.
    Always mints a fresh API key so the caller always gets a usable key.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
    if row is None:
        return None
    user = dict(row)
    expected = _hash_password(password, user["salt"])
    if not secrets.compare_digest(user["password_hash"], expected):
        return None

    result = _create_key_for_user(user["user_id"], "Session key")
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "email": user["email"],
        "created_at": user["created_at"],
        "raw_api_key": result["raw_key"],
        "key_id": result["key_id"],
        "key_prefix": result["key_prefix"],
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


# ── API key management ────────────────────────────────────────────────────────

def _create_key_for_user(user_id: str, name: str, scopes: list[str] | None = None) -> dict:
    raw, key_hash, prefix = _make_api_key()
    key_id = str(uuid.uuid4())
    normalized_scopes = _normalize_scopes(scopes)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_id, user_id, key_hash, key_prefix, name, scopes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key_id, user_id, key_hash, prefix, name, json.dumps(normalized_scopes), _now()),
        )
    return {
        "raw_key": raw,
        "key_id": key_id,
        "key_prefix": prefix,
        "name": name,
        "scopes": normalized_scopes,
    }


def create_api_key(
    user_id: str,
    name: str = "New key",
    scopes: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict:
    """Create a named API key for a user. Returns {"raw_key", "key_id", "key_prefix", "name"}."""
    return _create_key_for_user(user_id, name, scopes=list(scopes) if scopes is not None else None)


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
                   ak.scopes, u.username, u.email
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.user_id
            WHERE ak.key_hash = ? AND ak.is_active = 1
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
    }


def list_api_keys(user_id: str) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT key_id, key_prefix, name, scopes, created_at, last_used_at, is_active"
            " FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    keys: list[dict] = []
    for row in rows:
        item = dict(row)
        item["scopes"] = _decode_scopes_json(item.get("scopes"))
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
) -> dict | None:
    normalized_scopes = (
        _normalize_scopes(scopes)
        if scopes is not None
        else None
    )
    replacement_name = (name or "").strip() or None

    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT key_id, name, scopes
            FROM api_keys
            WHERE key_id = ? AND user_id = ? AND is_active = 1
            """,
            (key_id, user_id),
        ).fetchone()
        if row is None:
            return None

        final_name = replacement_name or row["name"]
        final_scopes = normalized_scopes or _decode_scopes_json(row["scopes"])

        raw, key_hash, prefix = _make_api_key()
        new_key_id = str(uuid.uuid4())
        now = _now()

        conn.execute(
            """
            INSERT INTO api_keys (key_id, user_id, key_hash, key_prefix, name, scopes, created_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                new_key_id,
                user_id,
                key_hash,
                prefix,
                final_name,
                json.dumps(final_scopes),
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
    }
