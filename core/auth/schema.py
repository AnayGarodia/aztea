"""
auth — schema, connection, hashing, and legal-state helpers.

User/session/key operations live in ``core.auth.users``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

from core import db as _db

DB_PATH = _db.DB_PATH

KEY_PREFIX = "am_"
AGENT_KEY_PREFIX = "amk_"
PBKDF2_ITERATIONS = 260_000
VALID_KEY_SCOPES = {"caller", "worker", "admin"}
DEFAULT_KEY_SCOPES = ("caller", "worker")
_CANONICAL_TIMESTAMP = "1970-01-01T00:00:00+00:00"
VALID_SUBJECT_STATUSES = {"active", "suspended", "banned"}
LEGAL_TERMS_VERSION = "2026-04-19"
LEGAL_PRIVACY_VERSION = "2026-04-19"

_local = _db._local


def _resolved_db_path() -> str:
    """Prefer ``core.auth.DB_PATH`` so isolated tests can monkeypatch the package."""
    pkg = sys.modules.get("core.auth")
    if pkg is not None:
        candidate = getattr(pkg, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH


def _conn() -> sqlite3.Connection:
    """Thread-local connection with WAL mode to match registry/payments."""
    conn = _db.get_raw_connection(_resolved_db_path())
    _local.conn = conn
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


_ALLOWED_PRAGMA_TABLES = frozenset({"users", "api_keys", "agent_keys"})


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if table_name not in _ALLOWED_PRAGMA_TABLES:
        raise ValueError(f"Disallowed table name for schema introspection: {table_name!r}")
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _create_users_table(conn: sqlite3.Connection, table_name: str = "users") -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            user_id       TEXT PRIMARY KEY,
            username      TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            salt          TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','banned')),
            terms_version_accepted TEXT,
            privacy_version_accepted TEXT,
            legal_accepted_at TEXT,
            legal_accept_ip TEXT,
            legal_accept_user_agent TEXT
        )
    """)


def _create_api_keys_table(conn: sqlite3.Connection, table_name: str = "api_keys") -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            key_id        TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL,
            key_hash      TEXT NOT NULL UNIQUE,
            key_prefix    TEXT NOT NULL,
            name          TEXT NOT NULL DEFAULT 'Default',
            scopes        TEXT NOT NULL DEFAULT '["caller","worker"]',
            max_spend_cents INTEGER CHECK(max_spend_cents >= 0),
            per_job_cap_cents INTEGER CHECK(per_job_cap_cents >= 0),
            created_at    TEXT NOT NULL,
            last_used_at  TEXT,
            is_active     INTEGER NOT NULL DEFAULT 1
        )
    """)


def _create_agent_keys_table(conn: sqlite3.Connection, table_name: str = "agent_keys") -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            key_id      TEXT PRIMARY KEY,
            agent_id    TEXT NOT NULL,
            key_hash    TEXT NOT NULL UNIQUE,
            key_prefix  TEXT NOT NULL,
            name        TEXT NOT NULL DEFAULT 'Agent key',
            created_at  TEXT NOT NULL,
            revoked_at  TEXT
        )
    """)


def _ensure_users_schema(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "users"):
        _create_users_table(conn)
        return

    cols = _table_columns(conn, "users")
    if "username" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN username TEXT NOT NULL DEFAULT 'unknown-user'")
    if "email" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    if "password_hash" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
    if "salt" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN salt TEXT NOT NULL DEFAULT ''")
    if "created_at" not in cols:
        conn.execute(f"ALTER TABLE users ADD COLUMN created_at TEXT NOT NULL DEFAULT '{_CANONICAL_TIMESTAMP}'")
    if "status" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    if "terms_version_accepted" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN terms_version_accepted TEXT")
    if "privacy_version_accepted" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN privacy_version_accepted TEXT")
    if "legal_accepted_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN legal_accepted_at TEXT")
    if "legal_accept_ip" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN legal_accept_ip TEXT")
    if "legal_accept_user_agent" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN legal_accept_user_agent TEXT")

    conn.execute(
        f"""
        UPDATE users
        SET created_at = '{_CANONICAL_TIMESTAMP}'
        WHERE created_at IS NULL OR TRIM(created_at) = ''
        """
    )
    conn.execute(
        """
        UPDATE users
        SET status = 'active'
        WHERE status IS NULL OR TRIM(status) = '' OR LOWER(TRIM(status)) NOT IN ('active','suspended','banned')
        """
    )


def _normalize_legacy_key_hash(raw: str | None, fallback_seed: str) -> str:
    candidate = (raw or "").strip()
    if candidate:
        return candidate
    return hashlib.sha256(fallback_seed.encode("utf-8")).hexdigest()


def _normalize_legacy_api_key_row(
    row: dict,
    used_key_ids: set[str],
    used_key_hashes: set[str],
) -> tuple[str, str, str, str, str, str, int | None, int | None, str, str | None, int] | None:
    legacy_rowid = int(row.get("_legacy_rowid") or 0)

    key_id = str(row.get("key_id") or "").strip()
    if not key_id:
        key_id = f"legacy-key-{legacy_rowid}"
    if key_id in used_key_ids:
        suffix = 2
        candidate = f"{key_id}-{suffix}"
        while candidate in used_key_ids:
            suffix += 1
            candidate = f"{key_id}-{suffix}"
        key_id = candidate
    used_key_ids.add(key_id)

    user_id = str(row.get("user_id") or "").strip()
    if not user_id:
        return None

    key_hash = _normalize_legacy_key_hash(
        row.get("key_hash") or row.get("api_key_hash"),
        fallback_seed=f"legacy-key-hash:{legacy_rowid}:{key_id}:{user_id}",
    )
    if key_hash in used_key_hashes:
        key_hash = hashlib.sha256(
            f"{key_hash}:{legacy_rowid}:{key_id}".encode("utf-8")
        ).hexdigest()
    used_key_hashes.add(key_hash)

    key_prefix = str(row.get("key_prefix") or "").strip()
    if not key_prefix:
        key_prefix = f"{KEY_PREFIX}{key_hash[:9]}"

    name = str(row.get("name") or "").strip() or "Legacy key"
    scopes = json.dumps(_decode_scopes_json(row.get("scopes")))
    max_spend_cents = row.get("max_spend_cents")
    try:
        parsed_max_spend = int(max_spend_cents) if max_spend_cents is not None else None
    except (TypeError, ValueError):
        parsed_max_spend = None
    if parsed_max_spend is not None and parsed_max_spend < 0:
        parsed_max_spend = None
    per_job_cap_cents = row.get("per_job_cap_cents")
    try:
        parsed_per_job_cap = int(per_job_cap_cents) if per_job_cap_cents is not None else None
    except (TypeError, ValueError):
        parsed_per_job_cap = None
    if parsed_per_job_cap is not None and parsed_per_job_cap < 0:
        parsed_per_job_cap = None
    created_at = str(row.get("created_at") or "").strip() or _CANONICAL_TIMESTAMP
    last_used_at = str(row.get("last_used_at") or "").strip() or None

    try:
        is_active = int(row.get("is_active", 1))
    except (TypeError, ValueError):
        is_active = 1
    is_active = 1 if is_active else 0

    return (
        key_id,
        user_id,
        key_hash,
        key_prefix,
        name,
        scopes,
        parsed_max_spend,
        parsed_per_job_cap,
        created_at,
        last_used_at,
        is_active,
    )


def _migrate_api_keys_table(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT rowid AS _legacy_rowid, * FROM api_keys ORDER BY rowid").fetchall()
    conn.execute("DROP TABLE IF EXISTS api_keys__canonical")
    _create_api_keys_table(conn, table_name="api_keys__canonical")

    used_key_ids: set[str] = set()
    used_key_hashes: set[str] = set()
    for raw in rows:
        normalized = _normalize_legacy_api_key_row(dict(raw), used_key_ids, used_key_hashes)
        if normalized is None:
            continue
        conn.execute(
            """
            INSERT INTO api_keys__canonical
                (key_id, user_id, key_hash, key_prefix, name, scopes, max_spend_cents, per_job_cap_cents, created_at, last_used_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized,
        )

    conn.execute("DROP TABLE api_keys")
    conn.execute("ALTER TABLE api_keys__canonical RENAME TO api_keys")


def _ensure_api_keys_schema(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "api_keys"):
        _create_api_keys_table(conn)
        return

    cols = _table_columns(conn, "api_keys")
    required_core = {"key_id", "user_id", "key_hash"}
    if not required_core.issubset(cols):
        _migrate_api_keys_table(conn)
        cols = _table_columns(conn, "api_keys")

    if "key_prefix" not in cols:
        conn.execute(f"ALTER TABLE api_keys ADD COLUMN key_prefix TEXT NOT NULL DEFAULT '{KEY_PREFIX}legacy000'")
    if "name" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN name TEXT NOT NULL DEFAULT 'Default'")
    if "scopes" not in cols:
        conn.execute("""ALTER TABLE api_keys ADD COLUMN scopes TEXT NOT NULL DEFAULT '["caller","worker"]'""")
    if "max_spend_cents" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN max_spend_cents INTEGER")
    if "per_job_cap_cents" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN per_job_cap_cents INTEGER")
    if "created_at" not in cols:
        conn.execute(f"ALTER TABLE api_keys ADD COLUMN created_at TEXT NOT NULL DEFAULT '{_CANONICAL_TIMESTAMP}'")
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN last_used_at TEXT")
    if "is_active" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")

    conn.execute(
        """
        UPDATE api_keys
        SET key_prefix = ? || substr(key_hash, 1, 9)
        WHERE key_prefix IS NULL OR TRIM(key_prefix) = ''
        """,
        (KEY_PREFIX,),
    )
    conn.execute(
        """
        UPDATE api_keys
        SET name = 'Default'
        WHERE name IS NULL OR TRIM(name) = ''
        """
    )
    conn.execute(
        """
        UPDATE api_keys
        SET scopes = '["caller","worker"]'
        WHERE scopes IS NULL OR TRIM(scopes) = ''
        """
    )
    conn.execute(
        f"""
        UPDATE api_keys
        SET created_at = '{_CANONICAL_TIMESTAMP}'
        WHERE created_at IS NULL OR TRIM(created_at) = ''
        """
    )
    conn.execute(
        """
        UPDATE api_keys
        SET is_active = 1
        WHERE is_active IS NULL
        """
    )
    conn.execute(
        """
        UPDATE api_keys
        SET max_spend_cents = NULL
        WHERE max_spend_cents < 0
        """
    )
    conn.execute(
        """
        UPDATE api_keys
        SET per_job_cap_cents = NULL
        WHERE per_job_cap_cents < 0
        """
    )


def _ensure_agent_keys_schema(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "agent_keys"):
        _create_agent_keys_table(conn)
        return
    cols = _table_columns(conn, "agent_keys")
    if "key_id" not in cols or "agent_id" not in cols or "key_hash" not in cols:
        rows = conn.execute("SELECT rowid AS _legacy_rowid, * FROM agent_keys ORDER BY rowid").fetchall()
        conn.execute("DROP TABLE IF EXISTS agent_keys__canonical")
        _create_agent_keys_table(conn, table_name="agent_keys__canonical")
        used_ids: set[str] = set()
        used_hashes: set[str] = set()
        for row in rows:
            data = dict(row)
            key_id = str(data.get("key_id") or "").strip() or f"legacy-agent-key-{data.get('_legacy_rowid', 0)}"
            while key_id in used_ids:
                key_id = f"{key_id}-dup"
            used_ids.add(key_id)
            agent_id = str(data.get("agent_id") or "").strip()
            if not agent_id:
                continue
            key_hash = str(data.get("key_hash") or "").strip()
            if not key_hash:
                key_hash = hashlib.sha256(f"legacy-agent-key:{key_id}:{agent_id}".encode("utf-8")).hexdigest()
            while key_hash in used_hashes:
                key_hash = hashlib.sha256(f"{key_hash}:{key_id}".encode("utf-8")).hexdigest()
            used_hashes.add(key_hash)
            key_prefix = str(data.get("key_prefix") or "").strip() or f"{AGENT_KEY_PREFIX}{key_hash[:8]}"
            name = str(data.get("name") or "").strip() or "Agent key"
            created_at = str(data.get("created_at") or "").strip() or _CANONICAL_TIMESTAMP
            revoked_at = str(data.get("revoked_at") or "").strip() or None
            conn.execute(
                """
                INSERT INTO agent_keys__canonical
                    (key_id, agent_id, key_hash, key_prefix, name, created_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key_id, agent_id, key_hash, key_prefix, name, created_at, revoked_at),
            )
        conn.execute("DROP TABLE agent_keys")
        conn.execute("ALTER TABLE agent_keys__canonical RENAME TO agent_keys")
        cols = _table_columns(conn, "agent_keys")
    if "key_prefix" not in cols:
        conn.execute(f"ALTER TABLE agent_keys ADD COLUMN key_prefix TEXT NOT NULL DEFAULT '{AGENT_KEY_PREFIX}legacy'")
    if "name" not in cols:
        conn.execute("ALTER TABLE agent_keys ADD COLUMN name TEXT NOT NULL DEFAULT 'Agent key'")
    if "created_at" not in cols:
        conn.execute(f"ALTER TABLE agent_keys ADD COLUMN created_at TEXT NOT NULL DEFAULT '{_CANONICAL_TIMESTAMP}'")
    if "revoked_at" not in cols:
        conn.execute("ALTER TABLE agent_keys ADD COLUMN revoked_at TEXT")
    conn.execute(
        f"""
        UPDATE agent_keys
        SET key_prefix = '{AGENT_KEY_PREFIX}' || substr(key_hash, 1, 8)
        WHERE key_prefix IS NULL OR TRIM(key_prefix) = ''
        """
    )


def init_auth_db() -> None:
    with _conn() as conn:
        _ensure_users_schema(conn)
        _ensure_api_keys_schema(conn)
        _ensure_agent_keys_schema(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_user_active ON api_keys(user_id, is_active)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_keys_agent_active ON agent_keys(agent_id, revoked_at)"
        )


def _hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return dk.hex()


def _make_api_key() -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix)."""
    raw = KEY_PREFIX + secrets.token_hex(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest(), raw[:12]


def _make_agent_api_key() -> tuple[str, str, str]:
    raw = AGENT_KEY_PREFIX + secrets.token_hex(32)
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


def _normalize_optional_non_negative_int(value: int | str | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer >= 0.") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be >= 0.")
    return parsed


def _normalize_optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _legal_state_from_row(row: dict) -> dict:
    terms_version_accepted = _normalize_optional_text(row.get("terms_version_accepted"))
    privacy_version_accepted = _normalize_optional_text(row.get("privacy_version_accepted"))
    legal_accepted_at = _normalize_optional_text(row.get("legal_accepted_at"))
    has_required_acceptance = (
        terms_version_accepted == LEGAL_TERMS_VERSION
        and privacy_version_accepted == LEGAL_PRIVACY_VERSION
        and legal_accepted_at is not None
    )
    return {
        "legal_acceptance_required": not has_required_acceptance,
        "legal_accepted_at": legal_accepted_at,
        "terms_version_current": LEGAL_TERMS_VERSION,
        "privacy_version_current": LEGAL_PRIVACY_VERSION,
        "terms_version_accepted": terms_version_accepted,
        "privacy_version_accepted": privacy_version_accepted,
    }
