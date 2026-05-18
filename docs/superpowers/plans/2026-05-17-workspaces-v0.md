# Workspaces v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a server-side shared-state primitive (`workspace_id` → named blob store) so multi-agent workflows can pass artifacts by reference instead of duplicating payloads, and so the entire workflow can be sealed under a single Ed25519-signed manifest for verifiable audit.

**Architecture:** New `core/workspaces.py` module owns lifecycle, CRUD, and seal/verify. Two new tables (`workspaces`, `workspace_artifacts`) follow the existing dual-backend (SQLite/Postgres) pattern via `core/db.py`. HTTP surface added as `part_013.py` extensions. Sealing reuses `core/crypto.py` Ed25519 primitives with a new per-server signing keypair stored at `data/workspace_signing_key.pem`. Dispatch layer (`part_007.py`, `part_008.py`) gets `_artifact_ref` resolution + auto-write of agent outputs. Pipeline executor opts in via `auto_workspace=True` on the recipe definition. Optional sandbox backing routes reads/writes through `core/sandbox/filesystem.py`.

**Tech Stack:** Python 3.12, FastAPI, SQLite/Postgres via `core/db.py`, `cryptography` (Ed25519 already used), pytest. No new dependencies.

**Naming decision (locked):** `workspaces` (plural) for the table, `core/workspaces.py` for the module, `/workspaces/...` for the HTTP route. This does **not** collide with existing `core/workspace_bundle*.py` / `core/workspace_consent.py` files (singular, `_bundle`/`_consent` suffix). The existing sandbox-receipt `workspace_id` field (`core/sandbox/receipts.py:74-123`, currently always `null`) is the same concept and gets populated by Task 11.

**Out of scope (deferred):** versioning, cross-user sharing, S3 backing, scoped tokens, UI surface, streaming I/O, per-workspace billing budget, automatic GC of `expired`-state content (manual delete + sweeper marks `expired` only).

---

## File Structure

| Path | Responsibility | New / Modified |
|---|---|---|
| `migrations/0053_workspaces.sql` | `workspaces` + `workspace_artifacts` schema | New |
| `core/workspaces.py` | Lifecycle, CRUD, seal/verify, sweeper | New (~600 lines, split if it crosses 900) |
| `core/workspaces_errors.py` | Typed exception hierarchy | New (~50 lines) |
| `core/error_codes.py` | Append workspace error code constants | Modify |
| `server/application_parts/part_013.py` | HTTP routes: POST/GET/DELETE workspace, PUT/GET/DELETE artifact, seal/manifest/verify, DID doc | Modify (~+350 lines) |
| `server/application_parts/part_007.py` | `_resolve_artifact_refs()` helper, wired into sync-call dispatch | Modify (~+60 lines) |
| `server/application_parts/part_008.py` | `_write_output_to_workspace()` helper, called after settlement | Modify (~+40 lines) |
| `core/pipelines/executor.py` | Optional auto-workspace per run, seal on completion | Modify (~+40 lines) |
| `core/pipelines/db.py` | Add `workspace_id` column to `pipeline_runs` | Modify (~+5 lines) |
| `migrations/0049_pipeline_runs_workspace_id.sql` | Adds `workspace_id` column | New (1 line) |
| `server/application_parts/part_006.py` | Wire workspace sweeper into the existing background sweeper | Modify (~+10 lines) |
| `sdks/python-sdk/aztea/mcp/meta_tools.py` | `aztea_workspace_inspect` meta-tool | Modify (~+50 lines) |
| `sdks/python-sdk/aztea/mcp/server.py` | Register the new meta-tool | Modify (~+5 lines) |
| `tests/test_workspaces_crud.py` | Unit tests for `core/workspaces.py` CRUD | New |
| `tests/test_workspaces_seal.py` | Unit tests for seal + verify | New |
| `tests/integration/test_workspaces_http.py` | HTTP CRUD + auth | New |
| `tests/integration/test_workspaces_dispatch.py` | `_artifact_ref` + auto-write integration | New |
| `tests/integration/test_workspaces_sandbox_backing.py` | Sandbox-backed workspace integration | New |
| `tests/integration/test_workspaces_pipeline_e2e.py` | Recipe-with-auto-workspace end-to-end | New |
| `tests/test_workspaces_sweeper.py` | TTL sweeper behaviour | New |

**Why split this way:** `core/workspaces.py` keeps all business logic in one file; errors live in a sibling so HTTP handlers can import the exception types without pulling in DB code. HTTP routes go in `part_013.py` (currently 366 lines, room to grow; `part_012.py` is already over the 1000-line cap). Dispatch integration is touch-three-shards because the dispatch path itself spans three shards — better to keep changes local to each than to invent a wrapper. Tests split into unit + integration matches existing layout under `tests/` and `tests/integration/`.

---

## Conventions used throughout this plan

- **TDD:** every new function gets a failing test written first. Steps are: write test → run (FAIL) → implement minimal code → run (PASS) → commit.
- **Backend coverage:** all DB-touching tests must pass under both SQLite (default) and Postgres. Run Postgres locally by setting `DATABASE_URL=postgresql://localhost/aztea_test` and `pytest` will use it. CI runs both.
- **Commit cadence:** one commit per task minimum; finer-grained commits encouraged inside a task (each green test is commit-worthy).
- **Commit message format:** `feat(workspaces): <summary>` for new functionality, `fix(workspaces): <summary>` for fixes, `test(workspaces): <summary>` for test-only changes.
- **Pre-commit gates:** before every commit, run `python scripts/check_file_line_budget.py` to enforce the 1000-line file budget.
- **Style:** every new module needs the four-field header (OWNS / NOT OWNS / INVARIANTS / DECISIONS / KNOWN DEBT) per CLAUDE.md.

---

## Task 1: Migration — `workspaces` and `workspace_artifacts` tables

**Files:**
- Create: `migrations/0053_workspaces.sql`
- Test: `tests/test_migrations_apply.py` (existing; will pick up the new file)

- [ ] **Step 1.1: Write the migration SQL**

Create `migrations/0053_workspaces.sql`:

```sql
-- 0053_workspaces.sql (originally drafted as 0048; renumbered at merge time)
-- Server-side shared-state primitive for multi-agent workflows.
--
-- A workspace is a named collection of artifacts (named blobs) that
-- multiple agents in one workflow read from and write to, instead of
-- threading payloads through the calling agent's context. Workspaces
-- can be sealed: a signed Ed25519 manifest over all artifact hashes
-- becomes verifiable evidence of the whole workflow.
--
-- Storage: bytea inline (v0). The schema reserves external_store_uri
-- so a future S3-backed mode is an additive migration, not a rewrite.
-- Backing: 'bytea' (default) stores content inline; 'sandbox' routes
-- reads/writes through core/sandbox/filesystem.py against backing_id.
--
-- Lifecycle: active -> sealed -> expired (content nulled by sweeper),
-- or active -> sandbox_evicted (terminal; sandbox died mid-workflow).

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id        TEXT PRIMARY KEY,
    owner_user_id       TEXT NOT NULL,
    run_id              TEXT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'sealed', 'expired', 'sandbox_evicted')),
    backing_type        TEXT NOT NULL DEFAULT 'bytea'
        CHECK (backing_type IN ('bytea', 'sandbox')),
    backing_id          TEXT NULL,
    external_store_uri  TEXT NULL,
    total_bytes         INTEGER NOT NULL DEFAULT 0,
    artifact_count      INTEGER NOT NULL DEFAULT 0,
    quota_bytes         INTEGER NOT NULL DEFAULT 67108864,
    seal_manifest       TEXT NULL,
    seal_signature      TEXT NULL,
    seal_public_key_did TEXT NULL,
    created_at          TEXT NOT NULL,
    sealed_at           TEXT NULL,
    expires_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS workspaces_owner_idx
    ON workspaces(owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS workspaces_run_idx
    ON workspaces(run_id);

CREATE INDEX IF NOT EXISTS workspaces_sweeper_idx
    ON workspaces(status, expires_at);

CREATE TABLE IF NOT EXISTS workspace_artifacts (
    artifact_id          TEXT PRIMARY KEY,
    workspace_id         TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,
    content_type         TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes           INTEGER NOT NULL,
    sha256               TEXT NOT NULL,
    content              BLOB NULL,
    created_by_agent_id  TEXT NULL,
    created_by_job_id    TEXT NULL,
    created_at           TEXT NOT NULL,
    UNIQUE (workspace_id, name)
);

CREATE INDEX IF NOT EXISTS workspace_artifacts_workspace_idx
    ON workspace_artifacts(workspace_id, created_at);
```

- [ ] **Step 1.2: Run migrations against a fresh SQLite DB**

```bash
rm -f /tmp/test_workspaces.db
DB_PATH=/tmp/test_workspaces.db python -c "from core.migrate import apply_migrations; apply_migrations()"
sqlite3 /tmp/test_workspaces.db ".schema workspaces"
sqlite3 /tmp/test_workspaces.db ".schema workspace_artifacts"
```

Expected: both `CREATE TABLE` statements printed; no errors.

- [ ] **Step 1.3: Run migrations a second time (idempotency check)**

```bash
DB_PATH=/tmp/test_workspaces.db python -c "from core.migrate import apply_migrations; apply_migrations()"
```

Expected: no error; the `IF NOT EXISTS` clauses + the `schema_migrations` row guard prevent re-application.

- [ ] **Step 1.4: Run the existing migrations test**

```bash
pytest tests/test_migrations_apply.py -v
```

Expected: PASS. If this test does not exist under that exact name, find it via `grep -r "schema_migrations" tests/`.

- [ ] **Step 1.5: Postgres smoke test (skip if no local Postgres)**

```bash
DATABASE_URL=postgresql://localhost/aztea_test python -c "from core.migrate import apply_migrations; apply_migrations()"
```

Expected: success. If you don't have a local Postgres, leave this step for CI.

- [ ] **Step 1.6: Commit**

```bash
git add migrations/0053_workspaces.sql
git commit -m "feat(workspaces): add 0048 migration for workspaces + workspace_artifacts tables"
```

---

## Task 2: Error taxonomy + typed exceptions

**Files:**
- Create: `core/workspaces_errors.py`
- Modify: `core/error_codes.py`

- [ ] **Step 2.1: Append workspace error codes**

Edit `core/error_codes.py`, add after the last constant (before `DEFAULT_BY_STATUS`):

```python
# Workspace lifecycle errors
WORKSPACE_NOT_FOUND = "workspace.not_found"
WORKSPACE_FORBIDDEN = "workspace.forbidden"
WORKSPACE_SEALED = "workspace.sealed"
WORKSPACE_QUOTA_EXCEEDED = "workspace.quota_exceeded"
WORKSPACE_ARTIFACT_NOT_FOUND = "workspace.artifact.not_found"
WORKSPACE_ARTIFACT_TOO_LARGE = "workspace.artifact.too_large"
WORKSPACE_ARTIFACT_NAME_INVALID = "workspace.artifact.name_invalid"
WORKSPACE_ARTIFACT_CONFLICT = "workspace.artifact.conflict"
WORKSPACE_BACKING_EVICTED = "workspace.backing.evicted"
WORKSPACE_SEAL_SIGNING_FAILED = "workspace.seal.signing_failed"
```

- [ ] **Step 2.2: Create the exception hierarchy**

Create `core/workspaces_errors.py`:

```python
"""Typed exceptions for the workspaces subsystem.

HTTP routes catch these and translate to the matching error code from
core/error_codes.py. Module-internal callers use the exception types so
they don't have to thread error-code strings through the call stack.

Mirrors the pattern in core/sandbox/models.py.
"""

from __future__ import annotations


class WorkspaceError(Exception):
    """Base class — every workspace error inherits from this."""


class WorkspaceNotFound(WorkspaceError):
    """No workspace row matches the given workspace_id."""


class WorkspaceForbidden(WorkspaceError):
    """Caller does not own this workspace and is not a worker-in-run."""


class WorkspaceSealed(WorkspaceError):
    """Workspace is sealed; mutating operations are not permitted."""


class WorkspaceQuotaExceeded(WorkspaceError):
    """Adding this artifact would exceed quota_bytes for the workspace."""


class ArtifactNotFound(WorkspaceError):
    """No artifact with that name in this workspace."""


class ArtifactTooLarge(WorkspaceError):
    """Single artifact exceeds the 8 MiB per-artifact cap."""


class ArtifactNameInvalid(WorkspaceError):
    """Artifact name fails validation (regex, length, path traversal)."""


class ArtifactConflict(WorkspaceError):
    """If-Match header sha256 does not match current artifact sha256."""


class BackingEvicted(WorkspaceError):
    """Sandbox-backed workspace's sandbox is no longer available."""


class SealSigningFailed(WorkspaceError):
    """Ed25519 signing of the seal manifest failed."""
```

- [ ] **Step 2.3: Commit**

```bash
git add core/workspaces_errors.py core/error_codes.py
git commit -m "feat(workspaces): add error code taxonomy + typed exceptions"
```

---

## Task 3: `core/workspaces.py` — lifecycle (create, get, expire)

**Files:**
- Create: `core/workspaces.py`
- Create: `tests/test_workspaces_crud.py`

- [ ] **Step 3.1: Write the failing test for `create_workspace`**

Create `tests/test_workspaces_crud.py`:

```python
"""Unit tests for core/workspaces.py CRUD operations.

Runs against whichever backend core/db.py is configured for. Set
DATABASE_URL=postgresql://... before pytest to exercise Postgres.
"""

from __future__ import annotations

import re
import time
import uuid

import pytest

from core import db as core_db
from core import workspaces
from core import workspaces_errors as wse


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Point core.db at a fresh SQLite file per test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(core_db, "DB_PATH", str(db_path))
    # Force re-open on the thread-local pool.
    import threading
    if hasattr(core_db._local, "conn"):
        delattr(core_db._local, "conn")
    from core.migrate import apply_migrations
    apply_migrations()
    yield
    if hasattr(core_db._local, "conn"):
        try:
            core_db._local.conn.close()
        except Exception:
            pass
        delattr(core_db._local, "conn")


def _owner() -> str:
    return f"usr_{uuid.uuid4().hex[:12]}"


def test_create_workspace_returns_prefixed_id():
    ws_id = workspaces.create_workspace(owner_user_id=_owner())
    assert ws_id.startswith("ws_")
    assert len(ws_id) == 3 + 22  # 'ws_' + 22-char base62


def test_create_workspace_persists_row_with_defaults():
    owner = _owner()
    ws_id = workspaces.create_workspace(owner_user_id=owner)
    row = workspaces.get_workspace(ws_id)
    assert row["workspace_id"] == ws_id
    assert row["owner_user_id"] == owner
    assert row["status"] == "active"
    assert row["backing_type"] == "bytea"
    assert row["total_bytes"] == 0
    assert row["artifact_count"] == 0
    assert row["quota_bytes"] == 64 * 1024 * 1024
    assert row["run_id"] is None
```

- [ ] **Step 3.2: Run the test to verify it fails**

```bash
pytest tests/test_workspaces_crud.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'core.workspaces'`.

- [ ] **Step 3.3: Create `core/workspaces.py` with module header + ID generation + create/get**

```python
"""Workspace lifecycle, CRUD, sealing, and sweeper.

# OWNS: workspaces + workspace_artifacts tables; ID generation; CRUD;
#       seal manifest building + Ed25519 signing; sweeper for expired
#       workspaces.
# NOT OWNS: pipeline execution (core/pipelines/), sandbox lifecycle
#       (core/sandbox/), billing (core/payments/), HTTP routing (server/).
#
# INVARIANTS:
# - Sealed workspaces are immutable. write_artifact / delete_artifact
#   raise WorkspaceSealed.
# - Artifact name must match _ARTIFACT_NAME_RE (no '/', no path
#   traversal, max 256 bytes).
# - sha256 is computed server-side over the bytes we received. The
#   value sent by the client (if any) is ignored.
# - Sandbox-backed reads MUST route through core/sandbox/filesystem
#   even when a stale bytea row exists.
# - quota_bytes is enforced atomically: write_artifact reads the
#   current total inside the same transaction that inserts/updates.
#
# DECISIONS:
# - bytea inline storage in v0 (no S3). 8 MiB per-artifact cap matches
#   the sandbox write cap.
# - Last-write-wins on concurrent PUT to the same name. Callers that
#   need CAS pass If-Match: <sha256> at the HTTP layer.
# - Workspace IDs are 'ws_' + 22-char base62 (~131 bits of entropy),
#   matching the job-ID format. Unguessable; no per-workspace ACL needed
#   beyond owner + worker-in-run.
#
# KNOWN DEBT:
# - No auto-GC of 'expired' content yet; sweeper marks status only.
#   Add a second sweeper pass that nulls content + frees disk in v0.1.
"""

from __future__ import annotations

import base64
import hashlib
import json as _json
import os
import re
import secrets
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from core import db as core_db
from core import workspaces_errors as wse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE62 = string.ascii_letters + string.digits
_ID_LENGTH = 22
_WORKSPACE_ID_PREFIX = "ws_"
_ARTIFACT_ID_PREFIX = "art_"

_DEFAULT_TTL_SECONDS = 86_400          # 24 hours
_MAX_TTL_SECONDS = 7 * 86_400          # 7 days
_DEFAULT_QUOTA_BYTES = 64 * 1024 * 1024  # 64 MiB
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024    # 8 MiB matches sandbox write cap

_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,256}$")
# Path-traversal guard: even though '/' is allowed for subdirectory-style
# names ("outputs/scanner/result.json"), we reject anything that decodes
# to a parent reference.
_ARTIFACT_NAME_DENY = re.compile(r"(^|/)\.\.($|/)")


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    body = "".join(secrets.choice(_BASE62) for _ in range(_ID_LENGTH))
    return f"{prefix}{body}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_iso(seconds_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def create_workspace(
    *,
    owner_user_id: str,
    backing_type: str = "bytea",
    backing_id: str | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    run_id: str | None = None,
    quota_bytes: int = _DEFAULT_QUOTA_BYTES,
) -> str:
    """Create a new workspace and return its workspace_id.

    Why we validate inputs here: bad TTL or backing_type only surfaces
    later as a confusing query failure if we let it through.
    """
    if backing_type not in ("bytea", "sandbox"):
        raise ValueError(f"backing_type must be 'bytea' or 'sandbox', got {backing_type!r}")
    if backing_type == "sandbox" and not backing_id:
        raise ValueError("backing_id is required when backing_type='sandbox'")
    if not (1 <= ttl_seconds <= _MAX_TTL_SECONDS):
        raise ValueError(f"ttl_seconds must be 1..{_MAX_TTL_SECONDS}, got {ttl_seconds}")

    workspace_id = _new_id(_WORKSPACE_ID_PREFIX)
    now = _utcnow_iso()
    expires = _expires_iso(ttl_seconds)

    with core_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO workspaces (
                workspace_id, owner_user_id, run_id, status,
                backing_type, backing_id, quota_bytes,
                created_at, expires_at
            ) VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s)
            """,
            (workspace_id, owner_user_id, run_id, backing_type, backing_id,
             quota_bytes, now, expires),
        )
    return workspace_id


def get_workspace(workspace_id: str) -> dict[str, Any]:
    """Fetch the workspace row as a dict. Raises WorkspaceNotFound."""
    with core_db.connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = %s",
            (workspace_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise wse.WorkspaceNotFound(workspace_id)
    return row
```

NOTE on `core_db.connection()`: this is the standard context-manager helper exported by `core/db.py`. If the function is actually named differently in your repo (e.g. `get_connection()` or `transaction()`), grep `core/db.py` for `@contextmanager` and use the canonical name. The plan assumes `connection()`.

- [ ] **Step 3.4: Check the actual core/db.py context-manager name and fix imports if needed**

```bash
grep -n "@contextmanager\|^def\|def connection\|def transaction" core/db.py | head -20
```

If the helper is named something other than `connection`, replace every `core_db.connection()` in `core/workspaces.py` with the correct name before continuing. The plan will continue to use `connection` as a placeholder; substitute one name globally.

- [ ] **Step 3.5: Run the failing test again, fix any signature mismatches**

```bash
pytest tests/test_workspaces_crud.py -v
```

Expected: the two tests written in Step 3.1 PASS.

- [ ] **Step 3.6: Commit**

```bash
git add core/workspaces.py tests/test_workspaces_crud.py
git commit -m "feat(workspaces): create_workspace + get_workspace + module skeleton"
```

---

## Task 4: `core/workspaces.py` — artifact CRUD (write, read, list, delete)

**Files:**
- Modify: `core/workspaces.py`
- Modify: `tests/test_workspaces_crud.py`

- [ ] **Step 4.1: Add failing tests for write/read/list/delete**

Append to `tests/test_workspaces_crud.py`:

```python
def test_write_artifact_persists_content_and_metadata():
    ws_id = workspaces.create_workspace(owner_user_id=_owner())
    meta = workspaces.write_artifact(
        ws_id, "hello.txt", b"hello world", "text/plain",
        created_by_agent_id="agt_test", created_by_job_id="job_test",
    )
    assert meta["name"] == "hello.txt"
    assert meta["size_bytes"] == 11
    assert meta["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert meta["content_type"] == "text/plain"


def test_read_artifact_returns_content_and_content_type():
    ws_id = workspaces.create_workspace(owner_user_id=_owner())
    workspaces.write_artifact(ws_id, "data.json", b'{"x":1}', "application/json")
    content, content_type = workspaces.read_artifact(ws_id, "data.json")
    assert content == b'{"x":1}'
    assert content_type == "application/json"


def test_list_artifacts_returns_metadata_for_all():
    ws_id = workspaces.create_workspace(owner_user_id=_owner())
    workspaces.write_artifact(ws_id, "a.bin", b"AA", "application/octet-stream")
    workspaces.write_artifact(ws_id, "b.bin", b"BBB", "application/octet-stream")
    listing = workspaces.list_artifacts(ws_id)
    assert {a["name"] for a in listing} == {"a.bin", "b.bin"}
    assert all("sha256" in a and "size_bytes" in a for a in listing)


def test_write_artifact_overwrites_existing_name_last_write_wins():
    ws_id = workspaces.create_workspace(owner_user_id=_owner())
    workspaces.write_artifact(ws_id, "x", b"v1", "text/plain")
    workspaces.write_artifact(ws_id, "x", b"v2", "text/plain")
    content, _ = workspaces.read_artifact(ws_id, "x")
    assert content == b"v2"
    listing = workspaces.list_artifacts(ws_id)
    assert len(listing) == 1


def test_delete_artifact_removes_row_and_decrements_counters():
    ws_id = workspaces.create_workspace(owner_user_id=_owner())
    workspaces.write_artifact(ws_id, "drop_me", b"bytes", "text/plain")
    workspaces.delete_artifact(ws_id, "drop_me")
    with pytest.raises(wse.ArtifactNotFound):
        workspaces.read_artifact(ws_id, "drop_me")
    ws = workspaces.get_workspace(ws_id)
    assert ws["artifact_count"] == 0
    assert ws["total_bytes"] == 0


def test_read_artifact_missing_raises_not_found():
    ws_id = workspaces.create_workspace(owner_user_id=_owner())
    with pytest.raises(wse.ArtifactNotFound):
        workspaces.read_artifact(ws_id, "ghost")


def test_write_artifact_rejects_invalid_name():
    ws_id = workspaces.create_workspace(owner_user_id=_owner())
    for bad in ["", "../escape", "foo/../bar", " ", "x" * 257]:
        with pytest.raises(wse.ArtifactNameInvalid):
            workspaces.write_artifact(ws_id, bad, b"x", "text/plain")


def test_write_artifact_rejects_oversized_blob():
    ws_id = workspaces.create_workspace(owner_user_id=_owner())
    too_big = b"\x00" * (8 * 1024 * 1024 + 1)
    with pytest.raises(wse.ArtifactTooLarge):
        workspaces.write_artifact(ws_id, "big.bin", too_big, "application/octet-stream")


def test_write_artifact_enforces_workspace_quota():
    ws_id = workspaces.create_workspace(
        owner_user_id=_owner(),
        quota_bytes=1024,  # tight quota for test
    )
    workspaces.write_artifact(ws_id, "a", b"x" * 600, "application/octet-stream")
    with pytest.raises(wse.WorkspaceQuotaExceeded):
        workspaces.write_artifact(ws_id, "b", b"y" * 500, "application/octet-stream")


import hashlib  # used by sha256 assertion above; keep at module level in real file
```

(Move the `import hashlib` to the top of the test file before committing.)

- [ ] **Step 4.2: Run to verify all eight new tests FAIL**

```bash
pytest tests/test_workspaces_crud.py -v
```

Expected: 8 FAIL (AttributeError: module has no attribute 'write_artifact' / 'read_artifact' / etc.).

- [ ] **Step 4.3: Implement validation helpers + write/read/list/delete**

Append to `core/workspaces.py`:

```python
# ---------------------------------------------------------------------------
# Artifact name validation
# ---------------------------------------------------------------------------


def _validate_artifact_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise wse.ArtifactNameInvalid("name must be a non-empty string")
    if len(name.encode("utf-8")) > 256:
        raise wse.ArtifactNameInvalid("name exceeds 256 bytes")
    if not _ARTIFACT_NAME_RE.match(name):
        raise wse.ArtifactNameInvalid(f"name contains invalid characters: {name!r}")
    if _ARTIFACT_NAME_DENY.search(name):
        raise wse.ArtifactNameInvalid(f"name contains path traversal: {name!r}")


def _validate_active(ws_row: dict[str, Any]) -> None:
    if ws_row["status"] == "sealed":
        raise wse.WorkspaceSealed(ws_row["workspace_id"])
    if ws_row["status"] == "sandbox_evicted":
        raise wse.BackingEvicted(ws_row["workspace_id"])


# ---------------------------------------------------------------------------
# Artifact CRUD
# ---------------------------------------------------------------------------


def write_artifact(
    workspace_id: str,
    name: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    *,
    created_by_agent_id: str | None = None,
    created_by_job_id: str | None = None,
    if_match_sha256: str | None = None,
) -> dict[str, Any]:
    """Write or overwrite an artifact. Returns its metadata.

    if_match_sha256: optional CAS token. If provided and the current
    artifact's sha256 doesn't match, raises ArtifactConflict and writes
    nothing. Caller can use it to avoid clobbering concurrent updates.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError("content must be bytes")
    _validate_artifact_name(name)
    size = len(content)
    if size > _MAX_ARTIFACT_BYTES:
        raise wse.ArtifactTooLarge(f"{size} > {_MAX_ARTIFACT_BYTES}")

    sha = hashlib.sha256(content).hexdigest()

    with core_db.connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = %s",
            (workspace_id,),
        )
        ws_row = cursor.fetchone()
        if ws_row is None:
            raise wse.WorkspaceNotFound(workspace_id)
        _validate_active(ws_row)

        # CAS check + size delta in the same transaction as the row update.
        cursor = conn.execute(
            "SELECT size_bytes, sha256 FROM workspace_artifacts "
            "WHERE workspace_id = %s AND name = %s",
            (workspace_id, name),
        )
        existing = cursor.fetchone()
        if if_match_sha256 is not None:
            current_sha = existing["sha256"] if existing else None
            if current_sha != if_match_sha256:
                raise wse.ArtifactConflict(
                    f"If-Match mismatch: have {current_sha!r}, expected {if_match_sha256!r}"
                )

        old_size = existing["size_bytes"] if existing else 0
        new_total = ws_row["total_bytes"] - old_size + size
        if new_total > ws_row["quota_bytes"]:
            raise wse.WorkspaceQuotaExceeded(
                f"{new_total} > {ws_row['quota_bytes']}"
            )

        now = _utcnow_iso()
        if existing:
            conn.execute(
                """
                UPDATE workspace_artifacts
                   SET content = %s, content_type = %s, size_bytes = %s,
                       sha256 = %s, created_by_agent_id = %s,
                       created_by_job_id = %s, created_at = %s
                 WHERE workspace_id = %s AND name = %s
                """,
                (content, content_type, size, sha, created_by_agent_id,
                 created_by_job_id, now, workspace_id, name),
            )
            artifact_count_delta = 0
        else:
            conn.execute(
                """
                INSERT INTO workspace_artifacts (
                    artifact_id, workspace_id, name, content_type,
                    size_bytes, sha256, content,
                    created_by_agent_id, created_by_job_id, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (_new_id(_ARTIFACT_ID_PREFIX), workspace_id, name, content_type,
                 size, sha, content, created_by_agent_id, created_by_job_id, now),
            )
            artifact_count_delta = 1

        conn.execute(
            "UPDATE workspaces SET total_bytes = %s, artifact_count = artifact_count + %s "
            "WHERE workspace_id = %s",
            (new_total, artifact_count_delta, workspace_id),
        )

    return {
        "name": name,
        "content_type": content_type,
        "size_bytes": size,
        "sha256": sha,
        "created_at": now,
    }


def read_artifact(workspace_id: str, name: str) -> tuple[bytes, str]:
    """Return (content_bytes, content_type). Raises ArtifactNotFound."""
    _validate_artifact_name(name)
    with core_db.connection() as conn:
        ws_cursor = conn.execute(
            "SELECT status FROM workspaces WHERE workspace_id = %s",
            (workspace_id,),
        )
        ws = ws_cursor.fetchone()
        if ws is None:
            raise wse.WorkspaceNotFound(workspace_id)
        if ws["status"] == "sandbox_evicted":
            raise wse.BackingEvicted(workspace_id)

        cursor = conn.execute(
            "SELECT content, content_type FROM workspace_artifacts "
            "WHERE workspace_id = %s AND name = %s",
            (workspace_id, name),
        )
        row = cursor.fetchone()
    if row is None:
        raise wse.ArtifactNotFound(f"{workspace_id}/{name}")
    return bytes(row["content"]), row["content_type"]


def list_artifacts(workspace_id: str) -> list[dict[str, Any]]:
    """Return artifact metadata (without content) for a workspace."""
    with core_db.connection() as conn:
        # Confirm workspace exists; bare list on missing workspace is misleading.
        ws_cursor = conn.execute(
            "SELECT 1 FROM workspaces WHERE workspace_id = %s",
            (workspace_id,),
        )
        if ws_cursor.fetchone() is None:
            raise wse.WorkspaceNotFound(workspace_id)
        cursor = conn.execute(
            """
            SELECT name, content_type, size_bytes, sha256,
                   created_by_agent_id, created_by_job_id, created_at
              FROM workspace_artifacts
             WHERE workspace_id = %s
             ORDER BY created_at
            """,
            (workspace_id,),
        )
        rows = cursor.fetchall()
    return rows


def delete_artifact(workspace_id: str, name: str) -> None:
    """Remove an artifact. Raises WorkspaceSealed if workspace sealed."""
    _validate_artifact_name(name)
    with core_db.connection() as conn:
        ws_cursor = conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = %s",
            (workspace_id,),
        )
        ws_row = ws_cursor.fetchone()
        if ws_row is None:
            raise wse.WorkspaceNotFound(workspace_id)
        _validate_active(ws_row)

        cursor = conn.execute(
            "SELECT size_bytes FROM workspace_artifacts "
            "WHERE workspace_id = %s AND name = %s",
            (workspace_id, name),
        )
        existing = cursor.fetchone()
        if existing is None:
            raise wse.ArtifactNotFound(f"{workspace_id}/{name}")

        conn.execute(
            "DELETE FROM workspace_artifacts WHERE workspace_id = %s AND name = %s",
            (workspace_id, name),
        )
        conn.execute(
            "UPDATE workspaces "
            "   SET total_bytes = total_bytes - %s, artifact_count = artifact_count - 1 "
            " WHERE workspace_id = %s",
            (existing["size_bytes"], workspace_id),
        )
```

- [ ] **Step 4.4: Run all tests; expect PASS**

```bash
pytest tests/test_workspaces_crud.py -v
```

Expected: all tests PASS. If any fail with `KeyError` on row fields, double-check the column names in the migration match the SELECTs.

- [ ] **Step 4.5: Check file line count is under budget**

```bash
python scripts/check_file_line_budget.py
```

Expected: PASS. `core/workspaces.py` should be ~350 lines at this point, well under the cap.

- [ ] **Step 4.6: Commit**

```bash
git add core/workspaces.py tests/test_workspaces_crud.py
git commit -m "feat(workspaces): write/read/list/delete artifact + quota + CAS"
```

---

## Task 5: HTTP routes — workspace lifecycle

**Files:**
- Modify: `server/application_parts/part_013.py`
- Create: `tests/integration/test_workspaces_http.py`

- [ ] **Step 5.1: Write failing integration tests for workspace lifecycle routes**

Create `tests/integration/test_workspaces_http.py`:

```python
"""HTTP-layer tests for workspace endpoints."""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Fresh DB + caller API key per test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("API_KEY", "master_test_key")
    # Re-import application so it picks up the new DB_PATH.
    import importlib
    import core.db
    importlib.reload(core.db)
    from core.migrate import apply_migrations
    apply_migrations()
    import server.application as app_mod
    importlib.reload(app_mod)
    return TestClient(app_mod.app)


def _caller_headers() -> dict[str, str]:
    # Replace with whatever the project uses to create a caller-scoped key
    # in tests; many existing tests in tests/integration/ use the master key
    # via X-API-Key: master_test_key. Match the existing pattern.
    return {"X-API-Key": "master_test_key"}


def test_post_workspaces_creates_active_workspace(client):
    r = client.post("/workspaces", json={"ttl_seconds": 3600}, headers=_caller_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace_id"].startswith("ws_")
    assert "expires_at" in body


def test_get_workspaces_returns_metadata(client):
    create = client.post("/workspaces", json={}, headers=_caller_headers()).json()
    ws_id = create["workspace_id"]
    r = client.get(f"/workspaces/{ws_id}", headers=_caller_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["backing_type"] == "bytea"
    assert body["artifact_count"] == 0


def test_get_unknown_workspace_returns_404(client):
    r = client.get("/workspaces/ws_doesnotexist", headers=_caller_headers())
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "workspace.not_found"


def test_delete_workspace_removes_it(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    r = client.delete(f"/workspaces/{ws_id}", headers=_caller_headers())
    assert r.status_code == 204
    r2 = client.get(f"/workspaces/{ws_id}", headers=_caller_headers())
    assert r2.status_code == 404
```

NOTE: existing tests in `tests/integration/` already use a specific TestClient fixture pattern. Open one (e.g. `tests/integration/test_hooks_builtin_mcp.py`) and adapt your fixture to match — the snippet above is a sketch, not necessarily the exact pattern the project uses.

- [ ] **Step 5.2: Run to verify all fail (no routes defined)**

```bash
pytest tests/integration/test_workspaces_http.py -v
```

Expected: 4 FAIL (404 from FastAPI for unknown route, OR import error).

- [ ] **Step 5.3: Add the routes to `part_013.py`**

Append to `server/application_parts/part_013.py` (preserve all existing content):

```python
# ---------------------------------------------------------------------------
# Workspace lifecycle routes (added 2026-05-17 for workspaces v0).
# ---------------------------------------------------------------------------

from core import workspaces as _workspaces
from core import workspaces_errors as _wse


def _workspace_not_found_response(workspace_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=error_codes.make_error(
            error_codes.WORKSPACE_NOT_FOUND,
            "Workspace not found.",
            {"workspace_id": workspace_id},
        ),
    )


def _require_workspace_owner(workspace_id: str, caller) -> dict:
    """Return the workspace row if caller owns it; else raise 403/404."""
    try:
        ws = _workspaces.get_workspace(workspace_id)
    except _wse.WorkspaceNotFound:
        raise _workspace_not_found_response(workspace_id)
    if ws["owner_user_id"] != caller["owner_id"]:
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_FORBIDDEN,
                "Caller does not own this workspace.",
                {"workspace_id": workspace_id},
            ),
        )
    return ws


@app.post("/workspaces", responses=_error_responses(400, 401, 403, 422, 429))
@limiter.limit("60/minute")
def workspaces_create(
    request: Request,
    body: dict = Body(default={}),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    ttl = int(body.get("ttl_seconds", 86400))
    backing_type = str(body.get("backing_type", "bytea"))
    backing_id = body.get("backing_id")
    run_id = body.get("run_id")
    try:
        ws_id = _workspaces.create_workspace(
            owner_user_id=caller["owner_id"],
            backing_type=backing_type,
            backing_id=backing_id,
            ttl_seconds=ttl,
            run_id=run_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT, str(exc), {},
            ),
        )
    ws = _workspaces.get_workspace(ws_id)
    return {"workspace_id": ws_id, "expires_at": ws["expires_at"]}


@app.get("/workspaces/{workspace_id}", responses=_error_responses(401, 403, 404))
def workspaces_get(
    workspace_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    ws = _require_workspace_owner(workspace_id, caller)
    return {
        "workspace_id": ws["workspace_id"],
        "status": ws["status"],
        "backing_type": ws["backing_type"],
        "total_bytes": ws["total_bytes"],
        "artifact_count": ws["artifact_count"],
        "quota_bytes": ws["quota_bytes"],
        "created_at": ws["created_at"],
        "expires_at": ws["expires_at"],
        "sealed_at": ws["sealed_at"],
        "run_id": ws["run_id"],
    }


@app.delete("/workspaces/{workspace_id}", responses=_error_responses(401, 403, 404))
def workspaces_delete(
    workspace_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    with _db.connection() as conn:
        conn.execute("DELETE FROM workspaces WHERE workspace_id = %s", (workspace_id,))
    return Response(status_code=204)
```

NOTE: `_require_api_key`, `_require_scope`, `core_models.CallerContext`, `error_codes`, `_error_responses`, `Body`, `Depends`, `Request`, `Response`, `HTTPException`, `app`, `limiter`, `_db` are all in scope already — `part_013.py` runs in the same exec namespace as the rest. If the helper for `caller["owner_id"]` is actually `caller["user_id"]` in your codebase, grep an existing route (`grep -n "caller\[" server/application_parts/part_012.py` for example) and use the right key.

- [ ] **Step 5.4: Run the integration tests; iterate on field names if any fail**

```bash
pytest tests/integration/test_workspaces_http.py -v
```

Expected: 4 PASS. Most likely failure mode: `KeyError` on caller dict access — adjust to the actual key name (probably `caller["owner_id"]` but verify against an existing route).

- [ ] **Step 5.5: Commit**

```bash
git add server/application_parts/part_013.py tests/integration/test_workspaces_http.py
git commit -m "feat(workspaces): POST/GET/DELETE /workspaces HTTP routes"
```

---

## Task 6: HTTP routes — artifact CRUD

**Files:**
- Modify: `server/application_parts/part_013.py`
- Modify: `tests/integration/test_workspaces_http.py`

- [ ] **Step 6.1: Add failing tests for artifact endpoints**

Append to `tests/integration/test_workspaces_http.py`:

```python
def test_put_artifact_stores_bytes_and_returns_sha256(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    r = client.put(
        f"/workspaces/{ws_id}/artifacts/hello.txt",
        content=b"hello world",
        headers={**_caller_headers(), "Content-Type": "text/plain"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"] == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert body["size_bytes"] == 11


def test_get_artifact_returns_bytes_with_content_type(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    client.put(
        f"/workspaces/{ws_id}/artifacts/data.json",
        content=b'{"x":1}',
        headers={**_caller_headers(), "Content-Type": "application/json"},
    )
    r = client.get(f"/workspaces/{ws_id}/artifacts/data.json", headers=_caller_headers())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.content == b'{"x":1}'


def test_list_artifacts_returns_metadata(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/a", content=b"AA",
               headers={**_caller_headers(), "Content-Type": "application/octet-stream"})
    client.put(f"/workspaces/{ws_id}/artifacts/b", content=b"BBB",
               headers={**_caller_headers(), "Content-Type": "application/octet-stream"})
    r = client.get(f"/workspaces/{ws_id}/artifacts", headers=_caller_headers())
    assert r.status_code == 200
    listing = r.json()["artifacts"]
    assert {a["name"] for a in listing} == {"a", "b"}


def test_delete_artifact_removes_it(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/drop",
               content=b"x", headers={**_caller_headers(), "Content-Type": "text/plain"})
    r = client.delete(f"/workspaces/{ws_id}/artifacts/drop", headers=_caller_headers())
    assert r.status_code == 204
    r2 = client.get(f"/workspaces/{ws_id}/artifacts/drop", headers=_caller_headers())
    assert r2.status_code == 404


def test_put_artifact_rejects_oversized(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    too_big = b"\x00" * (8 * 1024 * 1024 + 1)
    r = client.put(
        f"/workspaces/{ws_id}/artifacts/big.bin",
        content=too_big,
        headers={**_caller_headers(), "Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 413
    assert r.json()["detail"]["error"] == "workspace.artifact.too_large"


def test_put_artifact_if_match_conflict_returns_409(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    first = client.put(f"/workspaces/{ws_id}/artifacts/cas",
                       content=b"v1", headers={**_caller_headers(),
                                               "Content-Type": "text/plain"}).json()
    r = client.put(
        f"/workspaces/{ws_id}/artifacts/cas",
        content=b"v2",
        headers={**_caller_headers(), "Content-Type": "text/plain",
                 "If-Match": "wrong_sha"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "workspace.artifact.conflict"
    # Correct If-Match wins.
    r2 = client.put(
        f"/workspaces/{ws_id}/artifacts/cas",
        content=b"v3",
        headers={**_caller_headers(), "Content-Type": "text/plain",
                 "If-Match": first["sha256"]},
    )
    assert r2.status_code == 200
```

- [ ] **Step 6.2: Run to verify failures**

```bash
pytest tests/integration/test_workspaces_http.py -v
```

Expected: the six new tests FAIL.

- [ ] **Step 6.3: Add artifact routes to `part_013.py`**

Append to `server/application_parts/part_013.py`:

```python
@app.put(
    "/workspaces/{workspace_id}/artifacts/{name:path}",
    responses=_error_responses(400, 401, 403, 404, 409, 413, 422, 429),
)
@limiter.limit("300/minute")
async def workspaces_put_artifact(
    workspace_id: str,
    name: str,
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    body = await request.body()
    if len(body) > 8 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_TOO_LARGE,
                f"Artifact exceeds 8 MiB cap.",
                {"size_bytes": len(body)},
            ),
        )
    content_type = request.headers.get("content-type", "application/octet-stream")
    if_match = request.headers.get("if-match")
    try:
        meta = _workspaces.write_artifact(
            workspace_id, name, body, content_type,
            if_match_sha256=if_match,
        )
    except _wse.ArtifactNameInvalid as exc:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_NAME_INVALID, str(exc), {"name": name},
            ),
        )
    except _wse.ArtifactTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_TOO_LARGE, str(exc), {},
            ),
        )
    except _wse.WorkspaceQuotaExceeded as exc:
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_QUOTA_EXCEEDED, str(exc), {},
            ),
        )
    except _wse.ArtifactConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_CONFLICT, str(exc), {},
            ),
        )
    except _wse.WorkspaceSealed:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_SEALED,
                "Workspace is sealed; writes are not permitted.",
                {"workspace_id": workspace_id},
            ),
        )
    return meta


@app.get(
    "/workspaces/{workspace_id}/artifacts/{name:path}",
    responses=_error_responses(401, 403, 404),
)
def workspaces_get_artifact(
    workspace_id: str,
    name: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    try:
        content, content_type = _workspaces.read_artifact(workspace_id, name)
    except _wse.ArtifactNotFound:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_NOT_FOUND,
                "Artifact not found.",
                {"workspace_id": workspace_id, "name": name},
            ),
        )
    return Response(content=content, media_type=content_type)


@app.get(
    "/workspaces/{workspace_id}/artifacts",
    responses=_error_responses(401, 403, 404),
)
def workspaces_list_artifacts(
    workspace_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    return {"artifacts": _workspaces.list_artifacts(workspace_id)}


@app.delete(
    "/workspaces/{workspace_id}/artifacts/{name:path}",
    responses=_error_responses(401, 403, 404, 409),
)
def workspaces_delete_artifact(
    workspace_id: str,
    name: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    try:
        _workspaces.delete_artifact(workspace_id, name)
    except _wse.ArtifactNotFound:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_NOT_FOUND,
                "Artifact not found.",
                {"workspace_id": workspace_id, "name": name},
            ),
        )
    except _wse.WorkspaceSealed:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_SEALED,
                "Workspace is sealed.",
                {"workspace_id": workspace_id},
            ),
        )
    return Response(status_code=204)
```

NOTE on `{name:path}`: FastAPI's `:path` converter allows `/` in the name, so `outputs/scanner/result.json` resolves cleanly. The artifact-name validator in `core/workspaces.py` decides what's actually accepted.

- [ ] **Step 6.4: Run all tests; expect PASS**

```bash
pytest tests/integration/test_workspaces_http.py -v
```

Expected: all PASS.

- [ ] **Step 6.5: Verify the file is under the 1000-line budget**

```bash
python scripts/check_file_line_budget.py
```

If `part_013.py` is approaching 800+ lines, split workspace routes into a new shard by renaming `part_014.py` → `part_015.py` (since 014 must remain last per its docstring) and creating a new `part_014.py` for workspace routes. For now expect comfortable headroom (part_013 was 366 lines pre-change).

- [ ] **Step 6.6: Commit**

```bash
git add server/application_parts/part_013.py tests/integration/test_workspaces_http.py
git commit -m "feat(workspaces): PUT/GET/DELETE artifact + list HTTP routes"
```

---

## Task 7: Seal manifest + Ed25519 signing key

**Files:**
- Modify: `core/workspaces.py`
- Create: `tests/test_workspaces_seal.py`

- [ ] **Step 7.1: Write failing tests for seal + verify**

Create `tests/test_workspaces_seal.py`:

```python
"""Seal manifest generation + Ed25519 signature verification."""

from __future__ import annotations

import json

import pytest

from core import db as core_db
from core import workspaces


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(core_db, "DB_PATH", str(db_path))
    monkeypatch.setenv("AZTEA_WORKSPACE_SIGNING_KEY_PATH", str(tmp_path / "key.pem"))
    if hasattr(core_db._local, "conn"):
        delattr(core_db._local, "conn")
    from core.migrate import apply_migrations
    apply_migrations()
    yield
    if hasattr(core_db._local, "conn"):
        try:
            core_db._local.conn.close()
        except Exception:
            pass
        delattr(core_db._local, "conn")


def test_seal_workspace_returns_manifest_signature_did():
    ws = workspaces.create_workspace(owner_user_id="usr_owner")
    workspaces.write_artifact(ws, "a", b"AA", "text/plain")
    workspaces.write_artifact(ws, "b", b"BBB", "text/plain")
    result = workspaces.seal_workspace(ws)
    assert "manifest" in result
    assert "signature" in result
    assert "public_key_did" in result
    manifest = result["manifest"]
    assert manifest["schema"] == "aztea/workspace-seal/1"
    assert manifest["workspace_id"] == ws
    assert manifest["artifact_count"] == 2
    assert {a["name"] for a in manifest["artifacts"]} == {"a", "b"}


def test_seal_marks_workspace_sealed():
    ws = workspaces.create_workspace(owner_user_id="usr_owner")
    workspaces.write_artifact(ws, "a", b"x", "text/plain")
    workspaces.seal_workspace(ws)
    row = workspaces.get_workspace(ws)
    assert row["status"] == "sealed"
    assert row["sealed_at"] is not None
    assert row["seal_manifest"] is not None
    assert row["seal_signature"] is not None


def test_sealed_workspace_rejects_writes():
    from core import workspaces_errors as wse
    ws = workspaces.create_workspace(owner_user_id="usr_owner")
    workspaces.write_artifact(ws, "a", b"x", "text/plain")
    workspaces.seal_workspace(ws)
    with pytest.raises(wse.WorkspaceSealed):
        workspaces.write_artifact(ws, "b", b"y", "text/plain")


def test_verify_seal_returns_true_for_intact_workspace():
    ws = workspaces.create_workspace(owner_user_id="usr_owner")
    workspaces.write_artifact(ws, "a", b"x", "text/plain")
    workspaces.seal_workspace(ws)
    assert workspaces.verify_seal(ws) is True


def test_verify_seal_detects_artifact_tampering():
    ws = workspaces.create_workspace(owner_user_id="usr_owner")
    workspaces.write_artifact(ws, "a", b"x", "text/plain")
    workspaces.seal_workspace(ws)
    # Tamper directly with the bytea row.
    with core_db.connection() as conn:
        conn.execute(
            "UPDATE workspace_artifacts SET content = %s, sha256 = %s "
            "WHERE workspace_id = %s AND name = %s",
            (b"y", "deadbeef" * 8, ws, "a"),
        )
    assert workspaces.verify_seal(ws) is False
```

- [ ] **Step 7.2: Run to verify failures**

```bash
pytest tests/test_workspaces_seal.py -v
```

Expected: 5 FAIL (AttributeError: 'seal_workspace' not defined).

- [ ] **Step 7.3: Implement seal + verify**

Append to `core/workspaces.py`:

```python
# ---------------------------------------------------------------------------
# Seal manifest + Ed25519 signing
# ---------------------------------------------------------------------------

from core import crypto as _crypto
from core import identity as _identity

_WORKSPACE_SEAL_SCHEMA = "aztea/workspace-seal/1"
_DEFAULT_SIGNING_KEY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "workspace_signing_key.pem"
)


def _signing_key_path() -> str:
    return os.environ.get("AZTEA_WORKSPACE_SIGNING_KEY_PATH", _DEFAULT_SIGNING_KEY_PATH)


def _load_or_create_signing_keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem). Generates + persists on first call."""
    path = _signing_key_path()
    if os.path.exists(path):
        with open(path, "r", encoding="ascii") as f:
            content = f.read()
        # File format: private_pem + "\n---PUBLIC---\n" + public_pem
        marker = "\n---PUBLIC---\n"
        if marker in content:
            private_pem, public_pem = content.split(marker, 1)
            return private_pem, public_pem

    private_pem, public_pem = _crypto.generate_signing_keypair()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        f.write(private_pem + "\n---PUBLIC---\n" + public_pem)
    os.chmod(path, 0o600)
    return private_pem, public_pem


def _workspace_signer_did() -> str:
    """did:web:<host>:workspaces:sealer — mirrors the live-sandbox DID pattern."""
    return _identity.build_agent_did("workspaces:sealer")


def _build_manifest(workspace_id: str) -> dict[str, Any]:
    ws = get_workspace(workspace_id)
    listing = list_artifacts(workspace_id)
    return {
        "schema": _WORKSPACE_SEAL_SCHEMA,
        "workspace_id": workspace_id,
        "owner_user_id": ws["owner_user_id"],
        "run_id": ws["run_id"],
        "sealed_at": int(time.time()),
        "backing": {
            "type": ws["backing_type"],
            "id": ws["backing_id"],
        },
        "artifact_count": ws["artifact_count"],
        "total_bytes": ws["total_bytes"],
        "artifacts": [
            {
                "name": a["name"],
                "sha256": a["sha256"],
                "size_bytes": a["size_bytes"],
                "content_type": a["content_type"],
                "created_by_agent_id": a["created_by_agent_id"],
                "created_by_job_id": a["created_by_job_id"],
                "created_at": a["created_at"],
            }
            for a in listing
        ],
    }


def seal_workspace(workspace_id: str) -> dict[str, Any]:
    """Freeze the workspace: build manifest, sign, persist.

    Returns {manifest, signature, public_key_did}. Marks the workspace
    'sealed'; subsequent writes raise WorkspaceSealed.
    """
    ws = get_workspace(workspace_id)
    if ws["status"] == "sealed":
        # Idempotent: return the already-stored seal.
        return {
            "manifest": _json.loads(ws["seal_manifest"]),
            "signature": ws["seal_signature"],
            "public_key_did": ws["seal_public_key_did"],
        }
    if ws["status"] != "active":
        raise wse.WorkspaceError(
            f"cannot seal workspace in status {ws['status']!r}"
        )

    manifest = _build_manifest(workspace_id)
    private_pem, _public_pem = _load_or_create_signing_keypair()
    try:
        signature = _crypto.sign_payload(private_pem, manifest)
    except Exception as exc:
        raise wse.SealSigningFailed(str(exc)) from exc

    did = _workspace_signer_did()
    sealed_at_iso = _utcnow_iso()
    with core_db.connection() as conn:
        conn.execute(
            """
            UPDATE workspaces
               SET status = 'sealed',
                   sealed_at = %s,
                   seal_manifest = %s,
                   seal_signature = %s,
                   seal_public_key_did = %s
             WHERE workspace_id = %s
            """,
            (sealed_at_iso, _json.dumps(manifest, sort_keys=True),
             signature, did, workspace_id),
        )
    return {"manifest": manifest, "signature": signature, "public_key_did": did}


def verify_seal(workspace_id: str) -> bool:
    """Return True iff (a) signature is valid AND (b) every current
    artifact's sha256 matches what the manifest committed to.

    Returning False on any mismatch — never raising — lets callers
    branch on a single bool. Use get_workspace() if you need detail.
    """
    ws = get_workspace(workspace_id)
    if ws["status"] != "sealed":
        return False
    try:
        manifest = _json.loads(ws["seal_manifest"])
    except (TypeError, ValueError):
        return False

    _private_pem, public_pem = _load_or_create_signing_keypair()
    if not _crypto.verify_signature(public_pem, manifest, ws["seal_signature"]):
        return False

    # Re-hash live artifacts and compare against manifest entries.
    manifest_by_name = {a["name"]: a for a in manifest.get("artifacts", [])}
    current = list_artifacts(workspace_id)
    if len(current) != len(manifest_by_name):
        return False
    for art in current:
        committed = manifest_by_name.get(art["name"])
        if committed is None or committed["sha256"] != art["sha256"]:
            return False
    return True
```

NOTE on `_identity.build_agent_did`: it builds `did:web:<host>:agents:<suffix>`. If you want `did:web:<host>:workspaces:sealer` (not `agents:workspaces:sealer`), either pass a suffix that produces the right path, or open `core/identity.py` and add a sibling `build_workspace_did()` that swaps the path prefix. The plan calls `_identity.build_agent_did("workspaces:sealer")` — if that produces `did:web:<host>:agents:workspaces:sealer`, that's acceptable for v0 (still globally unique) but worth a one-line fix in `core/identity.py` to make it `did:web:<host>:workspaces:sealer`.

- [ ] **Step 7.4: Run the tests; expect PASS**

```bash
pytest tests/test_workspaces_seal.py -v
```

Expected: 5 PASS.

- [ ] **Step 7.5: Verify line budget; split module if needed**

```bash
python scripts/check_file_line_budget.py
wc -l core/workspaces.py
```

If `core/workspaces.py` is approaching 900 lines, split: keep CRUD in `core/workspaces.py`, move seal logic to `core/workspaces_seal.py`, re-export from `core/workspaces.py` so callers don't see the split.

- [ ] **Step 7.6: Commit**

```bash
git add core/workspaces.py tests/test_workspaces_seal.py
git commit -m "feat(workspaces): Ed25519 seal manifest + verify"
```

---

## Task 8: HTTP routes — seal, manifest, verify, DID document

**Files:**
- Modify: `server/application_parts/part_013.py`
- Modify: `tests/integration/test_workspaces_http.py`

- [ ] **Step 8.1: Write failing tests for seal + verify endpoints**

Append to `tests/integration/test_workspaces_http.py`:

```python
def test_post_seal_returns_signed_manifest(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/a",
               content=b"AA", headers={**_caller_headers(),
                                       "Content-Type": "text/plain"})
    r = client.post(f"/workspaces/{ws_id}/seal", headers=_caller_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["manifest"]["schema"] == "aztea/workspace-seal/1"
    assert "signature" in body
    assert body["public_key_did"].startswith("did:web:")


def test_get_manifest_after_seal(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/a",
               content=b"x", headers={**_caller_headers(),
                                      "Content-Type": "text/plain"})
    client.post(f"/workspaces/{ws_id}/seal", headers=_caller_headers())
    r = client.get(f"/workspaces/{ws_id}/manifest")  # public — no auth
    assert r.status_code == 200
    assert "manifest" in r.json()


def test_post_verify_returns_true_for_intact_workspace(client):
    ws_id = client.post("/workspaces", json={}, headers=_caller_headers()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/a",
               content=b"x", headers={**_caller_headers(),
                                      "Content-Type": "text/plain"})
    client.post(f"/workspaces/{ws_id}/seal", headers=_caller_headers())
    r = client.post(f"/workspaces/{ws_id}/verify")  # public
    assert r.status_code == 200
    assert r.json()["valid"] is True
```

- [ ] **Step 8.2: Run to verify failures**

```bash
pytest tests/integration/test_workspaces_http.py::test_post_seal_returns_signed_manifest -v
```

Expected: FAIL (404 from FastAPI).

- [ ] **Step 8.3: Add seal/manifest/verify routes**

Append to `server/application_parts/part_013.py`:

```python
@app.post(
    "/workspaces/{workspace_id}/seal",
    responses=_error_responses(401, 403, 404, 409, 500),
)
def workspaces_seal(
    workspace_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    try:
        return _workspaces.seal_workspace(workspace_id)
    except _wse.SealSigningFailed as exc:
        raise HTTPException(
            status_code=500,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_SEAL_SIGNING_FAILED,
                "Failed to sign workspace seal manifest.",
                {"reason": str(exc)},
            ),
        )


@app.get(
    "/workspaces/{workspace_id}/manifest",
    responses=_error_responses(404),
)
def workspaces_manifest(workspace_id: str) -> dict:
    # PUBLIC — sealed manifests are designed to be shareable evidence.
    # Returns 404 for unsealed workspaces (manifest doesn't exist yet).
    try:
        ws = _workspaces.get_workspace(workspace_id)
    except _wse.WorkspaceNotFound:
        raise _workspace_not_found_response(workspace_id)
    if ws["status"] != "sealed":
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_NOT_FOUND,
                "Workspace has no manifest (not sealed).",
                {"workspace_id": workspace_id, "status": ws["status"]},
            ),
        )
    import json as _json
    return {
        "manifest": _json.loads(ws["seal_manifest"]),
        "signature": ws["seal_signature"],
        "public_key_did": ws["seal_public_key_did"],
    }


@app.post(
    "/workspaces/{workspace_id}/verify",
    responses=_error_responses(404),
)
def workspaces_verify(workspace_id: str) -> dict:
    # PUBLIC. Returns a bool + the DID so an offline tool can re-verify
    # by fetching the DID document.
    try:
        ws = _workspaces.get_workspace(workspace_id)
    except _wse.WorkspaceNotFound:
        raise _workspace_not_found_response(workspace_id)
    valid = _workspaces.verify_seal(workspace_id)
    return {
        "valid": valid,
        "signer_did": ws["seal_public_key_did"],
        "sealed_at": ws["sealed_at"],
    }
```

- [ ] **Step 8.4: Add the DID document route**

```python
@app.get(
    "/workspaces/sealer/did.json",
    responses=_error_responses(500),
)
def workspaces_sealer_did_document() -> dict:
    """did:web resolution endpoint for the workspace signing key."""
    _private_pem, public_pem = _workspaces._load_or_create_signing_keypair()
    from core import crypto as _crypto
    did = _workspaces._workspace_signer_did()
    jwk = _crypto.public_key_to_jwk(public_pem)
    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": did,
        "verificationMethod": [
            {
                "id": f"{did}#key-1",
                "type": "JsonWebKey2020",
                "controller": did,
                "publicKeyJwk": jwk,
            }
        ],
        "assertionMethod": [f"{did}#key-1"],
    }
```

- [ ] **Step 8.5: Run the seal/verify tests**

```bash
pytest tests/integration/test_workspaces_http.py -v
```

Expected: all new tests PASS.

- [ ] **Step 8.6: Commit**

```bash
git add server/application_parts/part_013.py tests/integration/test_workspaces_http.py
git commit -m "feat(workspaces): seal/manifest/verify routes + DID document"
```

---

## Task 9: Worker-in-run auth extension

**Files:**
- Modify: `server/application_parts/part_013.py`
- Modify: `tests/integration/test_workspaces_http.py`

**Why this is its own task:** until now every workspace route was caller-only. Step 9 lets a worker holding a live job lease on `workspace.run_id` read/write artifacts on that workspace too. This is what enables agents dispatched into a recipe to use the workspace without inheriting the caller's key.

- [ ] **Step 9.1: Write a failing test**

Append to `tests/integration/test_workspaces_http.py`:

```python
def test_worker_holding_lease_on_run_can_write_artifact(client):
    """A worker key bound to an agent that holds an active job lease on
    workspace.run_id can write artifacts to that workspace."""
    # Build a workspace attached to a synthetic run_id.
    ws_id = client.post(
        "/workspaces",
        json={"run_id": "run_synthetic"},
        headers=_caller_headers(),
    ).json()["workspace_id"]

    # Create a synthetic job row that the worker key 'owns' and that
    # references run_synthetic. Use the project's existing job-creation
    # helper to keep this honest; if there isn't one, insert into the
    # jobs table directly with the minimum columns required by the
    # auth helper.
    #
    # (Sketch — the actual fixture path depends on test helpers in the
    # repo; check tests/integration/support.py for create_job_for_worker.)
    from tests.integration.support import create_worker_with_active_job
    worker_key, agent_id, job_id = create_worker_with_active_job(
        run_id="run_synthetic", workspace_id=ws_id,
    )

    r = client.put(
        f"/workspaces/{ws_id}/artifacts/from_worker",
        content=b"hello from worker",
        headers={"X-API-Key": worker_key, "Content-Type": "text/plain"},
    )
    assert r.status_code == 200, r.text
```

- [ ] **Step 9.2: Run to verify failure**

```bash
pytest tests/integration/test_workspaces_http.py::test_worker_holding_lease_on_run_can_write_artifact -v
```

Expected: FAIL (probably 403 forbidden because worker key isn't the workspace owner).

- [ ] **Step 9.3: Extend the auth helper to accept worker-in-run**

In `server/application_parts/part_013.py`, replace `_require_workspace_owner` with:

```python
def _caller_can_access_workspace(workspace_id: str, caller) -> dict:
    """Return the workspace row if caller can read/write it.

    Two auth paths:
      1. Caller owns the workspace (owner_user_id matches).
      2. Caller is a worker key on an agent with an active job lease
         on workspace.run_id.

    Raises 404 for unknown workspaces, 403 otherwise.
    """
    try:
        ws = _workspaces.get_workspace(workspace_id)
    except _wse.WorkspaceNotFound:
        raise _workspace_not_found_response(workspace_id)

    # Path 1: caller owns it.
    if ws["owner_user_id"] == caller.get("owner_id"):
        return ws

    # Path 2: worker-in-run. Requires a run_id and an active job lease
    # held by the caller's agent on that run.
    if ws["run_id"] and caller.get("type") == "worker":
        agent_id = caller.get("agent_id")
        if agent_id and _worker_has_active_job_on_run(agent_id, ws["run_id"]):
            return ws

    raise HTTPException(
        status_code=403,
        detail=error_codes.make_error(
            error_codes.WORKSPACE_FORBIDDEN,
            "Caller does not own this workspace and has no active job on its run.",
            {"workspace_id": workspace_id},
        ),
    )


def _worker_has_active_job_on_run(agent_id: str, run_id: str) -> bool:
    """Return True iff agent_id currently holds a non-terminal job lease
    on a step belonging to run_id."""
    with _db.connection() as conn:
        cursor = conn.execute(
            """
            SELECT 1
              FROM jobs
             WHERE agent_id = %s
               AND pipeline_run_id = %s
               AND status NOT IN ('complete', 'failed', 'cancelled')
             LIMIT 1
            """,
            (agent_id, run_id),
        )
        return cursor.fetchone() is not None
```

Replace every call to `_require_workspace_owner(...)` with `_caller_can_access_workspace(...)` (Tasks 5, 6, 8 each have several call sites — find them all).

Reserve seal + delete-workspace + delete-artifact for **owner only** — workers can read and write but not seal or delete. Add an explicit owner check after the access check:

```python
def _require_owner_only(ws: dict, caller) -> None:
    if ws["owner_user_id"] != caller.get("owner_id"):
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_FORBIDDEN,
                "Only the workspace owner can perform this action.",
                {"workspace_id": ws["workspace_id"]},
            ),
        )
```

Use it in `workspaces_seal`, `workspaces_delete`, `workspaces_delete_artifact`:

```python
# inside each of those:
ws = _caller_can_access_workspace(workspace_id, caller)
_require_owner_only(ws, caller)
```

- [ ] **Step 9.4: Add the test helper if missing**

If `tests/integration/support.py` doesn't have `create_worker_with_active_job`, add it:

```python
# tests/integration/support.py — append

def create_worker_with_active_job(*, run_id: str, workspace_id: str | None = None,
                                  caller_owner_id: str = "usr_test"):
    """Create an agent, a worker key for it, and an active job tied to run_id."""
    import secrets
    from core import db
    agent_id = f"agt_{secrets.token_hex(8)}"
    worker_key = f"azac_{secrets.token_hex(16)}"
    job_id = f"job_{secrets.token_hex(8)}"
    with db.connection() as conn:
        # Minimum agent row + api_key row + job row. Column names below
        # are illustrative — match the actual schema from migration 0001.
        conn.execute(
            "INSERT INTO agents (agent_id, owner_user_id, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (agent_id, caller_owner_id, f"slug-{agent_id}"),
        )
        conn.execute(
            "INSERT INTO api_keys (api_key, owner_user_id, scope, agent_id) "
            "VALUES (%s, %s, 'worker', %s)",
            (worker_key, caller_owner_id, agent_id),
        )
        conn.execute(
            "INSERT INTO jobs (job_id, agent_id, owner_user_id, pipeline_run_id, status) "
            "VALUES (%s, %s, %s, %s, 'running')",
            (job_id, agent_id, caller_owner_id, run_id),
        )
    return worker_key, agent_id, job_id
```

If a similar helper exists under a different name, use that instead and adjust the test import.

- [ ] **Step 9.5: Run all workspace HTTP tests; expect PASS**

```bash
pytest tests/integration/test_workspaces_http.py -v
```

Expected: all PASS (existing owner tests + new worker test).

- [ ] **Step 9.6: Commit**

```bash
git add server/application_parts/part_013.py tests/integration/test_workspaces_http.py tests/integration/support.py
git commit -m "feat(workspaces): worker-in-run auth — workers with active lease can read/write"
```

---

## Task 10: Dispatch integration — `_artifact_ref` resolution

**Files:**
- Modify: `server/application_parts/part_007.py`
- Create: `tests/integration/test_workspaces_dispatch.py`

**Why this matters:** without artifact-ref resolution, every agent call that needs workspace data has to call the workspace API explicitly. The user wants `{"_artifact_ref": "ws_xxx/name"}` in a payload to be substituted inline before the agent sees it.

- [ ] **Step 10.1: Write a failing test**

Create `tests/integration/test_workspaces_dispatch.py`:

```python
"""Dispatch integration: _artifact_ref substitution + auto-write."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("API_KEY", "master_test_key")
    monkeypatch.setenv("AZTEA_WORKSPACE_SIGNING_KEY_PATH", str(tmp_path / "key.pem"))
    import importlib, core.db
    importlib.reload(core.db)
    from core.migrate import apply_migrations
    apply_migrations()
    import server.application as app_mod
    importlib.reload(app_mod)
    return TestClient(app_mod.app)


def _caller():
    return {"X-API-Key": "master_test_key"}


def test_artifact_ref_in_payload_resolves_inline_before_agent_sees_it(client):
    # 1. Create a workspace with a "manifest" artifact.
    ws = client.post("/workspaces", json={}, headers=_caller()).json()["workspace_id"]
    client.put(
        f"/workspaces/{ws}/artifacts/manifest",
        content=b'{"deps": ["a", "b"]}',
        headers={**_caller(), "Content-Type": "application/json"},
    )

    # 2. Call a deterministic echo agent with a payload that contains
    #    an artifact_ref. The agent's response must include the resolved
    #    bytes — proving substitution happened before dispatch.
    #
    # The 'echo' built-in agent returns its input verbatim. If your
    # codebase doesn't have one, the test can use 'unicode_inspector'
    # or any agent that round-trips the input field.
    r = client.post(
        "/registry/agents/by-slug/echo/call",
        json={"input": {"_artifact_ref": f"{ws}/manifest"}},
        headers=_caller(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["output"]["input"] == {"deps": ["a", "b"]}
```

- [ ] **Step 10.2: Run to verify failure**

```bash
pytest tests/integration/test_workspaces_dispatch.py::test_artifact_ref_in_payload_resolves_inline_before_agent_sees_it -v
```

Expected: FAIL — agent sees the raw `{"_artifact_ref": "..."}` dict because no resolver is wired in.

- [ ] **Step 10.3: Add the resolver to `core/workspaces.py`**

Append to `core/workspaces.py`:

```python
# ---------------------------------------------------------------------------
# Artifact-ref resolution (used by dispatch layer)
# ---------------------------------------------------------------------------

_ARTIFACT_REF_KEY = "_artifact_ref"


def resolve_artifact_refs(
    payload: Any,
    *,
    caller_owner_id: str,
    allow_run_id: str | None = None,
) -> Any:
    """Recursively walk payload, replacing {_artifact_ref: 'ws/name'}
    with the artifact bytes (decoded as JSON if application/json, else
    base64).

    Auth: caller_owner_id must own the workspace, OR allow_run_id must
    match the workspace's run_id (worker-in-run path).

    Why we recurse: callers nest artifact_refs in lists and sub-dicts
    (e.g. hire_batch jobs: [{input: {_artifact_ref: ...}}]).
    """
    if isinstance(payload, dict):
        if _ARTIFACT_REF_KEY in payload and len(payload) == 1:
            return _load_artifact_ref(
                payload[_ARTIFACT_REF_KEY],
                caller_owner_id=caller_owner_id,
                allow_run_id=allow_run_id,
            )
        return {k: resolve_artifact_refs(v, caller_owner_id=caller_owner_id,
                                          allow_run_id=allow_run_id)
                for k, v in payload.items()}
    if isinstance(payload, list):
        return [resolve_artifact_refs(item, caller_owner_id=caller_owner_id,
                                       allow_run_id=allow_run_id)
                for item in payload]
    return payload


def _load_artifact_ref(ref: str, *, caller_owner_id: str,
                        allow_run_id: str | None) -> Any:
    if not isinstance(ref, str) or "/" not in ref:
        raise wse.ArtifactNameInvalid(f"bad artifact_ref {ref!r}")
    workspace_id, name = ref.split("/", 1)
    ws = get_workspace(workspace_id)  # raises WorkspaceNotFound
    if ws["owner_user_id"] != caller_owner_id:
        if not (allow_run_id and ws["run_id"] == allow_run_id):
            raise wse.WorkspaceForbidden(workspace_id)
    content, content_type = read_artifact(workspace_id, name)
    if content_type.startswith("application/json"):
        import json as _json
        return _json.loads(content)
    if content_type.startswith("text/"):
        return content.decode("utf-8")
    import base64
    return base64.b64encode(content).decode("ascii")
```

- [ ] **Step 10.4: Wire the resolver into the sync-call dispatch path**

In `server/application_parts/part_007.py`, find the function that handles `POST /registry/agents/{id}/call` (grep for `def call_agent` or similar). Locate the spot where the payload is finalised but before `_execute_builtin_agent()` or the HTTP proxy fires. Insert:

```python
# Resolve {_artifact_ref: "ws_id/name"} substitutions before the agent
# sees the payload. Owner can refer to their own workspaces; workers in
# a run can refer to that run's workspace.
from core import workspaces as _workspaces
from core import workspaces_errors as _wse

try:
    payload = _workspaces.resolve_artifact_refs(
        payload,
        caller_owner_id=caller["owner_id"],
        allow_run_id=payload.get("__run_id__") if isinstance(payload, dict) else None,
    )
except _wse.WorkspaceNotFound as exc:
    raise HTTPException(
        status_code=404,
        detail=error_codes.make_error(
            error_codes.WORKSPACE_NOT_FOUND,
            f"Artifact reference points to missing workspace.",
            {"reason": str(exc)},
        ),
    )
except _wse.WorkspaceForbidden as exc:
    raise HTTPException(
        status_code=403,
        detail=error_codes.make_error(
            error_codes.WORKSPACE_FORBIDDEN,
            "Caller cannot read artifact from this workspace.",
            {"reason": str(exc)},
        ),
    )
except _wse.ArtifactNotFound as exc:
    raise HTTPException(
        status_code=404,
        detail=error_codes.make_error(
            error_codes.WORKSPACE_ARTIFACT_NOT_FOUND,
            "Artifact referenced in payload is missing.",
            {"reason": str(exc)},
        ),
    )
```

NOTE: the exact placement depends on the structure of the call handler in `part_007.py`. Grep for `_execute_builtin_agent\|_proxy_agent_call\|hire_call` and put the resolver immediately before the first such dispatch call.

- [ ] **Step 10.5: Run the test; iterate until PASS**

```bash
pytest tests/integration/test_workspaces_dispatch.py -v
```

Expected: PASS. Likely failure modes: built-in agent doesn't exist with that slug — pick another from `server/builtin_agents/constants.py` that round-trips its input.

- [ ] **Step 10.6: Commit**

```bash
git add core/workspaces.py server/application_parts/part_007.py tests/integration/test_workspaces_dispatch.py
git commit -m "feat(workspaces): _artifact_ref payload substitution in sync-call dispatch"
```

---

## Task 11: Dispatch integration — auto-write agent output to workspace

**Files:**
- Modify: `server/application_parts/part_008.py`
- Modify: `tests/integration/test_workspaces_dispatch.py`

- [ ] **Step 11.1: Write a failing test**

Append to `tests/integration/test_workspaces_dispatch.py`:

```python
def test_call_with_workspace_id_envelope_auto_writes_output(client):
    ws = client.post("/workspaces", json={}, headers=_caller()).json()["workspace_id"]
    # Caller passes workspace_id alongside input in the call envelope.
    r = client.post(
        "/registry/agents/by-slug/echo/call",
        json={"input": {"x": 1}, "workspace_id": ws},
        headers=_caller(),
    )
    assert r.status_code == 200
    # Output should now be readable from the workspace under outputs/echo/{job_id}.json
    listing = client.get(f"/workspaces/{ws}/artifacts", headers=_caller()).json()["artifacts"]
    output_artifacts = [a for a in listing if a["name"].startswith("outputs/echo/")]
    assert len(output_artifacts) == 1
```

- [ ] **Step 11.2: Run to verify failure**

```bash
pytest tests/integration/test_workspaces_dispatch.py::test_call_with_workspace_id_envelope_auto_writes_output -v
```

Expected: FAIL (artifact list empty — no auto-write).

- [ ] **Step 11.3: Add the auto-write helper in `part_008.py`**

In `server/application_parts/part_008.py`, find the function that decorates the agent response after settlement (grep for `_decorate_with_rendered_output` or `_record_public_work_example`). Adjacent to those, add:

```python
def _write_output_to_workspace(
    *,
    workspace_id: str | None,
    agent_slug: str,
    job_id: str,
    output: dict,
    caller,
) -> None:
    """Auto-write the agent's output to workspace under outputs/{slug}/{job_id}.json
    when the call envelope included a workspace_id.

    Never raises; failures log and continue — auto-write is a convenience,
    not a correctness boundary. If a caller needs guaranteed writes, they
    can PUT to /workspaces/{id}/artifacts/{name} themselves.
    """
    if not workspace_id:
        return
    if isinstance(output, dict) and output.get("_no_workspace_write"):
        return
    import json as _json
    from core import workspaces as _workspaces
    from core import workspaces_errors as _wse

    try:
        body = _json.dumps(output, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _LOG.warning("workspace auto-write: output not JSON-serialisable job=%s err=%s",
                     job_id, exc)
        return
    if len(body) > 8 * 1024 * 1024:
        _LOG.warning("workspace auto-write: output exceeds 8 MiB cap job=%s size=%d",
                     job_id, len(body))
        return
    try:
        _workspaces.write_artifact(
            workspace_id,
            f"outputs/{agent_slug}/{job_id}.json",
            body,
            "application/json",
            created_by_agent_id=caller.get("agent_id"),
            created_by_job_id=job_id,
        )
    except (_wse.WorkspaceError, ValueError) as exc:
        _LOG.warning("workspace auto-write failed ws=%s job=%s err=%s",
                     workspace_id, job_id, exc)
```

Find the call site in the same file where successful settlement happens (grep for `settle_successful_job` or `_decorate_with_rendered_output`). Immediately after the settlement, call:

```python
_write_output_to_workspace(
    workspace_id=call_envelope.get("workspace_id"),
    agent_slug=agent_row["slug"],
    job_id=job_id,
    output=response_body,
    caller=caller,
)
```

`call_envelope` is the original request body; pass it through from wherever the dispatch handler decodes it. If the handler doesn't already preserve the envelope past the agent call, you need to capture `workspace_id` early and thread it down. ~5 line change.

- [ ] **Step 11.4: Run; iterate until PASS**

```bash
pytest tests/integration/test_workspaces_dispatch.py -v
```

Expected: all PASS.

- [ ] **Step 11.5: Commit**

```bash
git add server/application_parts/part_008.py tests/integration/test_workspaces_dispatch.py
git commit -m "feat(workspaces): auto-write agent output to workspace when workspace_id is set"
```

---

## Task 12: Sandbox backing

**Files:**
- Modify: `core/workspaces.py`
- Create: `tests/integration/test_workspaces_sandbox_backing.py`

- [ ] **Step 12.1: Write failing tests**

Create `tests/integration/test_workspaces_sandbox_backing.py`:

```python
"""Sandbox-backed workspace: reads/writes route to sandbox filesystem."""

from __future__ import annotations

import pytest

from core import workspaces


@pytest.fixture(autouse=True)
def _isolate_db_and_sandbox(tmp_path, monkeypatch):
    from core import db as core_db
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(core_db, "DB_PATH", str(db_path))
    monkeypatch.setenv("AZTEA_SANDBOX_STATE_ROOT", str(tmp_path / "sandbox"))
    if hasattr(core_db._local, "conn"):
        delattr(core_db._local, "conn")
    from core.migrate import apply_migrations
    apply_migrations()
    yield


def _boot_sandbox() -> str:
    """Helper: boot a real sandbox and return its sandbox_id."""
    from core.sandbox import lifecycle
    state = lifecycle.boot({})  # signature may differ; check core/sandbox/lifecycle.py
    return state.sandbox_id


def test_sandbox_backed_workspace_writes_to_sandbox_filesystem():
    sandbox_id = _boot_sandbox()
    ws = workspaces.create_workspace(
        owner_user_id="usr",
        backing_type="sandbox",
        backing_id=sandbox_id,
    )
    workspaces.write_artifact(ws, "Dockerfile", b"FROM alpine\n", "text/plain")

    # Verify the bytes actually live in the sandbox filesystem.
    from core.sandbox import filesystem
    result = filesystem.read_file({"sandbox_id": sandbox_id, "path": "artifacts/Dockerfile"})
    assert result["content"] == "FROM alpine\n"


def test_sandbox_backed_read_routes_to_sandbox_not_bytea():
    sandbox_id = _boot_sandbox()
    ws = workspaces.create_workspace(
        owner_user_id="usr",
        backing_type="sandbox",
        backing_id=sandbox_id,
    )
    workspaces.write_artifact(ws, "x.txt", b"hello", "text/plain")
    content, ct = workspaces.read_artifact(ws, "x.txt")
    assert content == b"hello"
    assert ct == "text/plain"


def test_sandbox_eviction_sets_workspace_status():
    from core.sandbox import lifecycle
    from core import workspaces_errors as wse
    sandbox_id = _boot_sandbox()
    ws = workspaces.create_workspace(
        owner_user_id="usr",
        backing_type="sandbox",
        backing_id=sandbox_id,
    )
    # Force eviction.
    lifecycle.stop({"sandbox_id": sandbox_id})

    # Subsequent reads should surface backing.evicted rather than a
    # cryptic SandboxNotFound from deep in the stack.
    with pytest.raises(wse.BackingEvicted):
        workspaces.read_artifact(ws, "anything")

    # Status row reflects the eviction after the first failure.
    row = workspaces.get_workspace(ws)
    assert row["status"] == "sandbox_evicted"
```

NOTE: `lifecycle.boot(...)` and `lifecycle.stop(...)` signatures may not match the placeholders above — open `core/sandbox/lifecycle.py` and use the real entry points.

- [ ] **Step 12.2: Run to verify failures**

```bash
pytest tests/integration/test_workspaces_sandbox_backing.py -v
```

Expected: FAIL (sandbox writes don't route through filesystem yet — content still in bytea).

- [ ] **Step 12.3: Add sandbox-routing logic to `core/workspaces.py`**

In `core/workspaces.py`, modify `write_artifact` to route to sandbox when backing_type='sandbox'. Replace the existing INSERT/UPDATE block with:

```python
        if ws_row["backing_type"] == "sandbox":
            # Write bytes to the sandbox filesystem under artifacts/{name}.
            # The bytea column stays NULL — the row is a metadata pointer.
            sandbox_id = ws_row["backing_id"]
            from core.sandbox import filesystem as _sb_fs
            from core.sandbox import lifecycle as _sb_lc
            try:
                _sb_fs.write_file({
                    "sandbox_id": sandbox_id,
                    "path": f"artifacts/{name}",
                    "content": content.decode("utf-8") if _is_textlike(content_type)
                               else None,
                    "content_b64": (
                        base64.b64encode(content).decode("ascii")
                        if not _is_textlike(content_type) else None
                    ),
                })
            except _sb_lc.SandboxNotFound as exc:
                _mark_sandbox_evicted(workspace_id)
                raise wse.BackingEvicted(str(exc)) from exc
            content_to_store = None  # don't duplicate bytes in bytea
        else:
            content_to_store = content
```

Then change the INSERT and UPDATE statements below to use `content_to_store` instead of `content`.

Update `read_artifact` similarly:

```python
def read_artifact(workspace_id: str, name: str) -> tuple[bytes, str]:
    _validate_artifact_name(name)
    with core_db.connection() as conn:
        ws_cursor = conn.execute(
            "SELECT status, backing_type, backing_id FROM workspaces WHERE workspace_id = %s",
            (workspace_id,),
        )
        ws = ws_cursor.fetchone()
        if ws is None:
            raise wse.WorkspaceNotFound(workspace_id)
        if ws["status"] == "sandbox_evicted":
            raise wse.BackingEvicted(workspace_id)
        cursor = conn.execute(
            "SELECT content, content_type FROM workspace_artifacts "
            "WHERE workspace_id = %s AND name = %s",
            (workspace_id, name),
        )
        row = cursor.fetchone()
    if row is None:
        raise wse.ArtifactNotFound(f"{workspace_id}/{name}")
    if ws["backing_type"] == "sandbox":
        from core.sandbox import filesystem as _sb_fs
        from core.sandbox import lifecycle as _sb_lc
        try:
            result = _sb_fs.read_file({
                "sandbox_id": ws["backing_id"],
                "path": f"artifacts/{name}",
            })
        except _sb_lc.SandboxNotFound as exc:
            _mark_sandbox_evicted(workspace_id)
            raise wse.BackingEvicted(str(exc)) from exc
        if result.get("binary"):
            content_bytes = base64.b64decode(result["content_b64"])
        else:
            content_bytes = result["content"].encode("utf-8")
        return content_bytes, row["content_type"]
    return bytes(row["content"]), row["content_type"]
```

And add the helpers (no new imports needed; `base64` is already at the top of the module):

```python
def _is_textlike(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type == "application/json"


def _mark_sandbox_evicted(workspace_id: str) -> None:
    with core_db.connection() as conn:
        conn.execute(
            "UPDATE workspaces SET status = 'sandbox_evicted' "
            "WHERE workspace_id = %s AND status = 'active'",
            (workspace_id,),
        )
```

- [ ] **Step 12.4: Run; iterate**

```bash
pytest tests/integration/test_workspaces_sandbox_backing.py -v
```

Expected: PASS. Common failure: sandbox boot signature mismatch — read `core/sandbox/lifecycle.py` and use the real API.

- [ ] **Step 12.5: Re-run all previous workspace tests to confirm no regression**

```bash
pytest tests/test_workspaces_crud.py tests/test_workspaces_seal.py tests/integration/test_workspaces_http.py tests/integration/test_workspaces_dispatch.py -v
```

Expected: all PASS — bytea-backed workspaces still work because the new code only branches when `backing_type == 'sandbox'`.

- [ ] **Step 12.6: Commit**

```bash
git add core/workspaces.py tests/integration/test_workspaces_sandbox_backing.py
git commit -m "feat(workspaces): sandbox backing routes read/write through sandbox filesystem"
```

---

## Task 13: Pipeline auto-workspace + seal on completion

**Files:**
- Modify: `core/pipelines/executor.py`
- Modify: `core/pipelines/db.py`
- Create: `migrations/0049_pipeline_runs_workspace_id.sql`
- Create: `tests/integration/test_workspaces_pipeline_e2e.py`

- [ ] **Step 13.1: Create migration for the new column**

```sql
-- migrations/0049_pipeline_runs_workspace_id.sql
-- Wire pipeline runs to their auto-created workspace (if the recipe opts in
-- via auto_workspace=true). Nullable: pre-existing runs and recipes that
-- don't opt in have workspace_id = NULL.
ALTER TABLE pipeline_runs ADD COLUMN workspace_id TEXT NULL;
```

- [ ] **Step 13.2: Run the migration**

```bash
DB_PATH=/tmp/test_workspaces_e2e.db python -c "from core.migrate import apply_migrations; apply_migrations()"
sqlite3 /tmp/test_workspaces_e2e.db ".schema pipeline_runs" | grep workspace_id
```

Expected: the `workspace_id` column appears in the schema.

- [ ] **Step 13.3: Write the failing e2e test**

Create `tests/integration/test_workspaces_pipeline_e2e.py`:

```python
"""End-to-end: recipe with auto_workspace=True creates, populates, seals."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    import importlib, core.db
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("API_KEY", "master_test_key")
    monkeypatch.setenv("AZTEA_WORKSPACE_SIGNING_KEY_PATH", str(tmp_path / "key.pem"))
    importlib.reload(core.db)
    from core.migrate import apply_migrations
    apply_migrations()
    import server.application as app_mod
    importlib.reload(app_mod)
    return TestClient(app_mod.app)


def _caller():
    return {"X-API-Key": "master_test_key"}


def test_recipe_with_auto_workspace_creates_seals_returns_curated_output(client):
    # Define a tiny 2-step pipeline that both steps write outputs into the
    # auto-created workspace. The exact pipeline-definition shape lives in
    # core/pipelines/resolver.py — use it as ground truth.
    definition = {
        "nodes": [
            {"id": "step1", "agent_slug": "echo", "input_map": {"value": "$input.value"}},
            {"id": "step2", "agent_slug": "echo",
             "input_map": {"value": "$step1.output.value"}},
        ],
        "auto_workspace": True,
    }
    create = client.post("/pipelines", json={"definition": definition}, headers=_caller())
    assert create.status_code == 200
    pipeline_id = create.json()["pipeline_id"]

    run = client.post(
        f"/pipelines/{pipeline_id}/runs",
        json={"input_payload": {"value": "hello"}},
        headers=_caller(),
    )
    assert run.status_code == 200
    run_id = run.json()["run_id"]

    # Poll for completion (pipelines run async in a daemon thread).
    import time
    for _ in range(50):
        status = client.get(f"/pipelines/runs/{run_id}", headers=_caller()).json()
        if status["status"] in ("complete", "failed"):
            break
        time.sleep(0.1)
    assert status["status"] == "complete"
    assert status.get("workspace_id", "").startswith("ws_")
    ws_id = status["workspace_id"]

    # Workspace exists, sealed, contains the expected output artifacts.
    ws = client.get(f"/workspaces/{ws_id}", headers=_caller()).json()
    assert ws["status"] == "sealed"

    listing = client.get(f"/workspaces/{ws_id}/artifacts", headers=_caller()).json()["artifacts"]
    names = {a["name"] for a in listing}
    assert any(n.startswith("outputs/echo/") for n in names)

    # Verify the seal manifest.
    verify = client.post(f"/workspaces/{ws_id}/verify").json()
    assert verify["valid"] is True
```

- [ ] **Step 13.4: Wire auto-workspace into `core/pipelines/executor.py`**

In `core/pipelines/executor.py:run_pipeline()` (around line 603-640), modify to:

```python
def run_pipeline(
    pipeline_id: str,
    input_payload: dict,
    caller_owner_id: str,
    caller_wallet_id: str,
    *,
    client_id: str | None = None,
    execute_builtin_agent: Callable[[str, dict[str, Any]], dict] | None = None,
) -> str:
    pipeline = db.get_pipeline(pipeline_id)
    if pipeline is None:
        raise ValueError(f"Pipeline '{pipeline_id}' not found.")
    validated = validate_definition(pipeline.get("definition") or {})
    del validated

    created = db.create_run(pipeline_id, caller_owner_id, input_payload)

    # Optional auto-workspace: when the recipe has auto_workspace=True,
    # create a workspace tied to this run. Step dispatch will pass the
    # workspace_id through to every agent call so outputs auto-write,
    # and run completion will seal the workspace.
    workspace_id: str | None = None
    definition = pipeline.get("definition") or {}
    if isinstance(definition, dict) and definition.get("auto_workspace"):
        from core import workspaces as _ws
        workspace_id = _ws.create_workspace(
            owner_user_id=caller_owner_id,
            run_id=created["run_id"],
        )
        db.set_run_workspace(created["run_id"], workspace_id)

    thread = threading.Thread(
        target=_execute_run,
        kwargs={
            "run_id": created["run_id"],
            "pipeline": pipeline,
            "input_payload": input_payload,
            "caller_owner_id": caller_owner_id,
            "caller_wallet_id": caller_wallet_id,
            "client_id": client_id,
            "execute_builtin_agent": execute_builtin_agent,
            "workspace_id": workspace_id,
        },
        name=f"aztea-pipeline-{created['run_id'][:8]}",
        daemon=True,
    )
    thread.start()
    return created["run_id"]
```

In the same file, modify `_execute_run` to accept `workspace_id` and pass it down to `_invoke_agent`. In `_invoke_agent`, add `workspace_id` to the call envelope sent to the agent dispatch handler. At the end of `_execute_run` (after the success path is settled), seal the workspace:

```python
    if workspace_id:
        try:
            from core import workspaces as _ws
            _ws.seal_workspace(workspace_id)
        except Exception:
            _LOG.exception("workspace seal failed run=%s ws=%s",
                           run_id, workspace_id)
```

NOTE: the exact `_execute_run` signature change cascades — find every call site and update. The plan can't enumerate them without reading the file in full; do this as part of step 13.4.

- [ ] **Step 13.5: Add `db.set_run_workspace` helper**

In `core/pipelines/db.py`:

```python
def set_run_workspace(run_id: str, workspace_id: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET workspace_id = %s WHERE run_id = %s",
            (workspace_id, run_id),
        )
```

Also update `get_run` (or whichever read function is used by `GET /pipelines/runs/{run_id}`) to include `workspace_id` in its SELECT and returned dict.

- [ ] **Step 13.6: Run the e2e test; iterate**

```bash
pytest tests/integration/test_workspaces_pipeline_e2e.py -v
```

Expected: PASS. Common failures: pipeline definition shape doesn't match what `validate_definition` accepts (read `core/pipelines/resolver.py`), or `get_run` doesn't expose `workspace_id`.

- [ ] **Step 13.7: Commit**

```bash
git add migrations/0049_pipeline_runs_workspace_id.sql core/pipelines/executor.py core/pipelines/db.py tests/integration/test_workspaces_pipeline_e2e.py
git commit -m "feat(workspaces): pipeline auto_workspace creates+seals workspace per run"
```

---

## Task 14: TTL sweeper

**Files:**
- Modify: `core/workspaces.py`
- Modify: `server/application_parts/part_006.py`
- Create: `tests/test_workspaces_sweeper.py`

- [ ] **Step 14.1: Write failing test**

Create `tests/test_workspaces_sweeper.py`:

```python
"""TTL sweeper: marks expired active workspaces and nulls their content."""

from __future__ import annotations

import time

import pytest

from core import db as core_db
from core import workspaces


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core_db, "DB_PATH", str(tmp_path / "db.sqlite"))
    if hasattr(core_db._local, "conn"):
        delattr(core_db._local, "conn")
    from core.migrate import apply_migrations
    apply_migrations()
    yield


def test_sweeper_marks_expired_active_workspace_as_expired():
    ws = workspaces.create_workspace(owner_user_id="usr", ttl_seconds=1)
    workspaces.write_artifact(ws, "a", b"x", "text/plain")
    # Force expires_at into the past.
    with core_db.connection() as conn:
        conn.execute(
            "UPDATE workspaces SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE workspace_id = %s", (ws,),
        )
    n = workspaces.run_sweeper(now_iso="2030-01-01T00:00:00+00:00")
    assert n >= 1
    row = workspaces.get_workspace(ws)
    assert row["status"] == "expired"


def test_sweeper_does_not_touch_sealed_workspaces_within_retention():
    ws = workspaces.create_workspace(owner_user_id="usr", ttl_seconds=3600)
    workspaces.write_artifact(ws, "a", b"x", "text/plain")
    workspaces.seal_workspace(ws)
    # Force expires_at past but status='sealed' protects content for the
    # 30-day retention window — sweeper should leave it alone.
    workspaces.run_sweeper(now_iso="2030-01-01T00:00:00+00:00")
    row = workspaces.get_workspace(ws)
    assert row["status"] == "sealed"
```

- [ ] **Step 14.2: Run to verify failure**

```bash
pytest tests/test_workspaces_sweeper.py -v
```

Expected: FAIL (`AttributeError: module has no attribute 'run_sweeper'`).

- [ ] **Step 14.3: Implement the sweeper**

Append to `core/workspaces.py`:

```python
# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


def run_sweeper(*, now_iso: str | None = None) -> int:
    """Mark active workspaces past expires_at as 'expired'. Returns the
    number of rows updated.

    Sealed workspaces are intentionally left alone: their content is
    retained for the dispute/audit window. A v0.1 sweep can null content
    for sealed-and-past-retention rows separately.
    """
    cutoff = now_iso or _utcnow_iso()
    with core_db.connection() as conn:
        cursor = conn.execute(
            "UPDATE workspaces SET status = 'expired' "
            " WHERE status = 'active' AND expires_at < %s",
            (cutoff,),
        )
        return cursor.rowcount or 0
```

- [ ] **Step 14.4: Wire the sweeper into the background loop in `part_006.py`**

Find the existing background sweeper in `server/application_parts/part_006.py` (grep for `def _background_sweeper` or `Thread.*sweeper`). Add a call to `workspaces.run_sweeper()` alongside the existing sweepers. Keep the interval at whatever the parent loop uses (probably 30-60s).

```python
# inside the sweeper loop, alongside existing sweeps:
try:
    from core import workspaces as _workspaces
    n = _workspaces.run_sweeper()
    if n:
        _LOG.info("workspace sweeper: marked %d expired", n)
except Exception:
    _LOG.exception("workspace sweeper raised")
```

- [ ] **Step 14.5: Run sweeper tests + smoke test the loop is wired**

```bash
pytest tests/test_workspaces_sweeper.py -v
```

Expected: PASS.

```bash
# Quick smoke: import the app, ensure no exceptions
python -c "import server.application as a; print('ok')"
```

- [ ] **Step 14.6: Commit**

```bash
git add core/workspaces.py server/application_parts/part_006.py tests/test_workspaces_sweeper.py
git commit -m "feat(workspaces): TTL sweeper for expired active workspaces"
```

---

## Task 15: MCP meta-tool `aztea_workspace_inspect`

**Files:**
- Modify: `sdks/python-sdk/aztea/mcp/meta_tools.py`
- Modify: `sdks/python-sdk/aztea/mcp/server.py`

- [ ] **Step 15.1: Add the inspect handler in `meta_tools.py`**

Append to `sdks/python-sdk/aztea/mcp/meta_tools.py` (follow the pattern of existing `_job_status` / `_pipeline_status` handlers):

```python
def _workspace_inspect(client, args: dict) -> dict:
    """Inspect a workspace: status, artifact list, manifest if sealed."""
    workspace_id = args.get("workspace_id")
    if not workspace_id:
        return {"error": "workspace_id is required"}
    ws = client.get(f"/workspaces/{workspace_id}")
    if ws.status_code == 404:
        return {"error": "workspace.not_found"}
    body = ws.json()
    listing = client.get(f"/workspaces/{workspace_id}/artifacts").json()
    manifest = None
    if body.get("status") == "sealed":
        m = client.get(f"/workspaces/{workspace_id}/manifest")
        if m.status_code == 200:
            manifest = m.json()
    return {
        "workspace": body,
        "artifacts": listing.get("artifacts", []),
        "manifest": manifest,
    }
```

- [ ] **Step 15.2: Register the meta-tool in `server.py`**

In `sdks/python-sdk/aztea/mcp/server.py`, find where existing meta-tools are registered (around lines 104-160). Add:

```python
{
    "name": "aztea_workspace_inspect",
    "description": (
        "Inspect a workspace: status, artifact list, and (if sealed) the "
        "signed manifest. workspace_id is required."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"workspace_id": {"type": "string"}},
        "required": ["workspace_id"],
    },
    "handler": _workspace_inspect,
},
```

The exact registration shape must match the file's existing pattern — it might be a list of dicts, a registration call per-tool, or a dispatch table. Match what's already there.

- [ ] **Step 15.3: Smoke test**

```bash
python -c "from sdks.python_sdk.aztea.mcp import server as s; print('ok')"
```

Or whichever import path matches the file layout. Pre-existing tests for meta-tools (likely `tests/test_mcp_lazy_tool_surface.py`) will catch tool-count regressions; update the expected count if needed (currently locked at 7).

- [ ] **Step 15.4: Update the lazy-tool-surface test if it counts tools**

Search for the test that asserts the tool count:

```bash
grep -rn "tool_count\|LAZY_TOOL_COUNT\|len.*tools" tests/test_mcp_lazy_tool_surface.py
```

If it asserts a hard count (e.g. `assert len(tools) == 7`), bump to 8.

- [ ] **Step 15.5: Commit**

```bash
git add sdks/python-sdk/aztea/mcp/meta_tools.py sdks/python-sdk/aztea/mcp/server.py tests/test_mcp_lazy_tool_surface.py
git commit -m "feat(workspaces): aztea_workspace_inspect MCP meta-tool"
```

---

## Final validation gate

After all tasks land:

- [ ] **Run the full test suite on SQLite**

```bash
pytest -q tests --ignore=tests/test_sdk_contract.py
```

Expected: PASS. Baseline is 2275 tests as of 2026-05-10; new tasks add ~30 tests.

- [ ] **Run on Postgres if a local instance is available**

```bash
DATABASE_URL=postgresql://localhost/aztea_test pytest -q tests --ignore=tests/test_sdk_contract.py
```

- [ ] **Line budget check**

```bash
python scripts/check_file_line_budget.py
```

- [ ] **OSS-mode test (no accidental hosted calls)**

```bash
pytest -q tests/test_oss_mode_isolation.py
```

- [ ] **Manual smoke: full audit_pr-style workflow**

```bash
# Boot the server locally, then:
curl -X POST http://localhost:8000/workspaces \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{}'
# -> {"workspace_id": "ws_...", "expires_at": "..."}

curl -X PUT http://localhost:8000/workspaces/ws_xxx/artifacts/manifest \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"deps": ["a", "b"]}'

curl -X POST http://localhost:8000/workspaces/ws_xxx/seal \
  -H "X-API-Key: $API_KEY"
# -> {"manifest": {...}, "signature": "...", "public_key_did": "did:web:..."}

curl http://localhost:8000/workspaces/ws_xxx/verify
# -> {"valid": true, ...}
```

---

## Stop conditions (where it's safe to halt and ship)

- **After Task 4** — `core/workspaces.py` CRUD is callable from internal code. No HTTP, no integration. Useful for tests and internal callers; ship if any of the next steps blow up.
- **After Task 8** — full HTTP surface for workspace CRUD + seal + verify. Callers can use workspaces as a standalone artifact store. No agent integration yet. Useful as a v0.5 release.
- **After Task 11** — dispatch integration done. Agents see resolved artifact refs; their outputs auto-write. This is the v0 the spec describes.
- **After Task 13** — pipelines opt in to auto-workspace. This is the value proposition the user described.
- **After Task 15** — fully integrated, observable, sealed. v0 complete.

If something fundamental breaks between tasks (e.g. Task 12 reveals the sandbox API is shaped wrong), stop at the previous task and revise the plan — don't push through with workarounds.

---

## Out-of-scope reminders (do not implement)

- Versioning of artifacts (schema is forward-compatible; defer)
- S3 / external-store backing (schema reserves `external_store_uri`; defer)
- Cross-user sharing or ACL (defer)
- Streaming I/O (8 MiB cap makes blocking fine; defer)
- Per-workspace billing budget (defer)
- Auto-content-deletion for `expired` workspaces (sweeper only marks status in v0)
- Workspace UI in the React frontend (defer)
- Reusing the existing `core/workspace_bundle*.py` files — they're a different concept (developer cwd context); leave them alone
