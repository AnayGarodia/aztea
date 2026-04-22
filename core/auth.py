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
import secrets
import sqlite3
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


# ── Connection ────────────────────────────────────────────────────────────────

_local = _db._local


def _conn() -> sqlite3.Connection:
    """Thread-local connection with WAL mode to match registry/payments."""
    conn = _db.get_raw_connection(DB_PATH)
    _local.conn = conn
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Schema ────────────────────────────────────────────────────────────────────

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


# ── User management ───────────────────────────────────────────────────────────

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


# ── API key management ────────────────────────────────────────────────────────

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
