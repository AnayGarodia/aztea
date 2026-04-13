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
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "registry.db")
KEY_PREFIX = "am_"
PBKDF2_ITERATIONS = 260_000


# ── Connection ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
                created_at    TEXT NOT NULL,
                last_used_at  TEXT,
                is_active     INTEGER NOT NULL DEFAULT 1
            )
        """)


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

def _create_key_for_user(user_id: str, name: str) -> dict:
    raw, key_hash, prefix = _make_api_key()
    key_id = str(uuid.uuid4())
    with _conn() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_id, user_id, key_hash, key_prefix, name, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (key_id, user_id, key_hash, prefix, name, _now()),
        )
    return {"raw_key": raw, "key_id": key_id, "key_prefix": prefix, "name": name}


def create_api_key(user_id: str, name: str = "New key") -> dict:
    """Create a named API key for a user. Returns {"raw_key", "key_id", "key_prefix", "name"}."""
    return _create_key_for_user(user_id, name)


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
                   u.username, u.email
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
    }


def list_api_keys(user_id: str) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT key_id, key_prefix, name, created_at, last_used_at, is_active"
            " FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(key_id: str, user_id: str) -> bool:
    with _conn() as conn:
        result = conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_id = ? AND user_id = ?",
            (key_id, user_id),
        )
    return result.rowcount > 0
