"""Workspace lifecycle, CRUD, sealing, and sweeper.

# OWNS: workspaces + workspace_artifacts tables; ID generation; CRUD;
#       seal manifest building + Ed25519 signing; sweeper for expired
#       workspaces; artifact-ref resolution helper used by the dispatch
#       layer.
# NOT OWNS: pipeline execution (core/pipelines/), sandbox lifecycle
#       (core/sandbox/), billing (core/payments/), HTTP routing (server/).
#
# INVARIANTS:
# - Sealed workspaces are immutable. write_artifact / delete_artifact
#   raise WorkspaceSealed.
# - Artifact name must match _ARTIFACT_NAME_RE (no path traversal, max
#   256 bytes). '/' is allowed for subdirectory-style names like
#   "outputs/scanner/result.json".
# - sha256 is computed server-side over the bytes we received. The value
#   sent by the client (if any) is ignored.
# - Sandbox-backed reads MUST route through core/sandbox/filesystem even
#   when a stale bytea row exists (sandbox is the source of truth).
# - quota_bytes is enforced atomically: write_artifact reads the current
#   total inside the same transaction that inserts/updates.
#
# DECISIONS:
# - bytea inline storage in v0 (no S3). 8 MiB per-artifact cap matches
#   the sandbox write cap (core/sandbox/filesystem.py:_MAX_WRITE_BYTES).
# - Last-write-wins on concurrent PUT to the same name. Callers that
#   need CAS pass If-Match: <sha256> at the HTTP layer.
# - Workspace IDs are 'ws_' + 22-char base62 (~131 bits of entropy),
#   matching the existing ID format. Unguessable; no per-workspace ACL
#   needed beyond owner + worker-in-run.
# - The signing keypair is per-server (not per-workspace) and mirrors
#   the sandbox per-host key pattern at core/sandbox/receipts.py.
#
# KNOWN DEBT:
# - No auto-GC of 'expired' content yet; sweeper only marks status. Add
#   a second pass that nulls content + frees disk in v0.1.
# - No rotation tooling for the workspace signing key. Manual rotation
#   only — delete data/workspace_signing_key.pem and the next call to
#   _load_or_create_signing_keypair regenerates.
"""

from __future__ import annotations

import base64
import hashlib
import json as _json
import logging
import os
import re
import secrets
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from core import crypto as _crypto
from core import db as _db
from core import identity as _identity
from core import workspaces_errors as wse

_LOG = logging.getLogger(__name__)

# Module-level DB_PATH binding mirrors the pattern in core/payments/base.py so
# tests can isolate by monkeypatching ``core.workspaces.DB_PATH``. See
# tests/test_workspaces_crud.py for the fixture.
DB_PATH = _db.DB_PATH

# Re-export the dual-backend thread-local so the integration-test helper
# ``tests/integration/helpers._close_module_conn`` (which closes the
# cached connection per fixture teardown) finds it on this module.
_local = _db._local


def _resolved_db_path() -> str:
    """Re-read ``DB_PATH`` from this module each call so tests can patch it."""
    return DB_PATH


def _connect():
    """Return the thread-local DbConnection for this module's resolved path.

    The DbConnection itself supports ``with conn:`` for commit-on-success
    semantics, mirroring the pattern in core/agent_generator/persistence.py.
    """
    return _db.get_raw_connection(_resolved_db_path())

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE62 = string.ascii_letters + string.digits
_ID_LENGTH = 22
_WORKSPACE_ID_PREFIX = "ws_"
_ARTIFACT_ID_PREFIX = "art_"

_DEFAULT_TTL_SECONDS = 86_400  # 24 hours
_MAX_TTL_SECONDS = 7 * 86_400  # 7 days
_DEFAULT_QUOTA_BYTES = 64 * 1024 * 1024  # 64 MiB
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024  # matches sandbox write cap

# Name regex: alphanumerics, dot, underscore, dash, slash. 1..256 bytes.
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,256}$")
# Path-traversal guard even though '/' is allowed for nested names.
_ARTIFACT_NAME_DENY = re.compile(r"(^|/)\.\.($|/)")

_WORKSPACE_SEAL_SCHEMA = "aztea/workspace-seal/1"

_DEFAULT_SIGNING_KEY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "workspace_signing_key.pem"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    body = "".join(secrets.choice(_BASE62) for _ in range(_ID_LENGTH))
    return f"{prefix}{body}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_iso(seconds_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat()


def _signing_key_path() -> str:
    return os.environ.get("AZTEA_WORKSPACE_SIGNING_KEY_PATH", _DEFAULT_SIGNING_KEY_PATH)


def _is_textlike(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type == "application/json"


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
    if ws_row["status"] == "expired":
        raise wse.WorkspaceNotFound(ws_row["workspace_id"])


def _mark_sandbox_evicted(workspace_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE workspaces SET status = 'sandbox_evicted' "
            "WHERE workspace_id = %s AND status = 'active'",
            (workspace_id,),
        )


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

    Validates inputs up front: bad TTL or backing_type surfaces here
    rather than as a confusing query failure later.
    """
    if backing_type not in ("bytea", "sandbox"):
        raise ValueError(
            f"backing_type must be 'bytea' or 'sandbox', got {backing_type!r}"
        )
    if backing_type == "sandbox" and not backing_id:
        raise ValueError("backing_id is required when backing_type='sandbox'")
    if not (1 <= ttl_seconds <= _MAX_TTL_SECONDS):
        raise ValueError(
            f"ttl_seconds must be 1..{_MAX_TTL_SECONDS}, got {ttl_seconds}"
        )

    workspace_id = _new_id(_WORKSPACE_ID_PREFIX)
    now = _utcnow_iso()
    expires = _expires_iso(ttl_seconds)

    with _connect() as conn:
        conn.execute(
            "INSERT INTO workspaces ("
            "workspace_id, owner_user_id, run_id, status,"
            " backing_type, backing_id, quota_bytes,"
            " created_at, expires_at"
            ") VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s)",
            (workspace_id, owner_user_id, run_id, backing_type, backing_id,
             quota_bytes, now, expires),
        )
    return workspace_id


def get_workspace(workspace_id: str) -> dict[str, Any]:
    """Fetch the workspace row as a dict. Raises WorkspaceNotFound."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM workspaces WHERE workspace_id = %s",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise wse.WorkspaceNotFound(workspace_id)
    return row


def cleanup_workspace(workspace_id: str) -> None:
    """Delete a workspace and all its artifacts. Idempotent."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM workspaces WHERE workspace_id = %s",
            (workspace_id,),
        )


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
    nothing. Used to avoid clobbering a concurrent update.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError("content must be bytes")
    _validate_artifact_name(name)
    size = len(content)
    if size > _MAX_ARTIFACT_BYTES:
        raise wse.ArtifactTooLarge(f"{size} > {_MAX_ARTIFACT_BYTES}")

    sha = hashlib.sha256(content).hexdigest()
    now = _utcnow_iso()

    with _connect() as conn:
        ws_row = conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = %s",
            (workspace_id,),
        ).fetchone()
        if ws_row is None:
            raise wse.WorkspaceNotFound(workspace_id)
        _validate_active(ws_row)

        existing = conn.execute(
            "SELECT size_bytes, sha256 FROM workspace_artifacts "
            "WHERE workspace_id = %s AND name = %s",
            (workspace_id, name),
        ).fetchone()

        if if_match_sha256 is not None:
            current_sha = existing["sha256"] if existing else None
            if current_sha != if_match_sha256:
                raise wse.ArtifactConflict(
                    f"If-Match mismatch: have {current_sha!r}, "
                    f"expected {if_match_sha256!r}"
                )

        old_size = existing["size_bytes"] if existing else 0
        new_total = ws_row["total_bytes"] - old_size + size
        if new_total > ws_row["quota_bytes"]:
            raise wse.WorkspaceQuotaExceeded(
                f"{new_total} > {ws_row['quota_bytes']}"
            )

        if ws_row["backing_type"] == "sandbox":
            content_to_store = None
            _sandbox_write(ws_row["backing_id"], name, content, content_type,
                           workspace_id=workspace_id)
        else:
            content_to_store = bytes(content)

        if existing:
            conn.execute(
                "UPDATE workspace_artifacts"
                "   SET content = %s, content_type = %s, size_bytes = %s,"
                "       sha256 = %s, created_by_agent_id = %s,"
                "       created_by_job_id = %s, created_at = %s"
                " WHERE workspace_id = %s AND name = %s",
                (content_to_store, content_type, size, sha,
                 created_by_agent_id, created_by_job_id, now,
                 workspace_id, name),
            )
            artifact_delta = 0
        else:
            conn.execute(
                "INSERT INTO workspace_artifacts ("
                "artifact_id, workspace_id, name, content_type,"
                " size_bytes, sha256, content,"
                " created_by_agent_id, created_by_job_id, created_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (_new_id(_ARTIFACT_ID_PREFIX), workspace_id, name, content_type,
                 size, sha, content_to_store,
                 created_by_agent_id, created_by_job_id, now),
            )
            artifact_delta = 1

        conn.execute(
            "UPDATE workspaces "
            "   SET total_bytes = %s,"
            "       artifact_count = artifact_count + %s "
            " WHERE workspace_id = %s",
            (new_total, artifact_delta, workspace_id),
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
    conn = _connect()
    ws = conn.execute(
        "SELECT status, backing_type, backing_id "
        "FROM workspaces WHERE workspace_id = %s",
        (workspace_id,),
    ).fetchone()
    if ws is None:
        raise wse.WorkspaceNotFound(workspace_id)
    if ws["status"] == "sandbox_evicted":
        raise wse.BackingEvicted(workspace_id)
    if ws["status"] == "expired":
        raise wse.WorkspaceNotFound(workspace_id)

    row = conn.execute(
        "SELECT content, content_type FROM workspace_artifacts "
        "WHERE workspace_id = %s AND name = %s",
        (workspace_id, name),
    ).fetchone()
    if row is None:
        raise wse.ArtifactNotFound(f"{workspace_id}/{name}")

    if ws["backing_type"] == "sandbox":
        content_bytes = _sandbox_read(ws["backing_id"], name,
                                       workspace_id=workspace_id)
        return content_bytes, row["content_type"]

    return bytes(row["content"]) if row["content"] is not None else b"", row["content_type"]


def list_artifacts(workspace_id: str) -> list[dict[str, Any]]:
    """Return artifact metadata (without content) for a workspace."""
    conn = _connect()
    ws_exists = conn.execute(
        "SELECT 1 FROM workspaces WHERE workspace_id = %s",
        (workspace_id,),
    ).fetchone()
    if ws_exists is None:
        raise wse.WorkspaceNotFound(workspace_id)
    rows = conn.execute(
        "SELECT name, content_type, size_bytes, sha256,"
        " created_by_agent_id, created_by_job_id, created_at "
        "FROM workspace_artifacts "
        "WHERE workspace_id = %s "
        "ORDER BY created_at",
        (workspace_id,),
    ).fetchall()
    return rows


def delete_artifact(workspace_id: str, name: str) -> None:
    """Remove an artifact. Raises WorkspaceSealed if workspace sealed."""
    _validate_artifact_name(name)
    with _connect() as conn:
        ws_row = conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = %s",
            (workspace_id,),
        ).fetchone()
        if ws_row is None:
            raise wse.WorkspaceNotFound(workspace_id)
        _validate_active(ws_row)

        existing = conn.execute(
            "SELECT size_bytes FROM workspace_artifacts "
            "WHERE workspace_id = %s AND name = %s",
            (workspace_id, name),
        ).fetchone()
        if existing is None:
            raise wse.ArtifactNotFound(f"{workspace_id}/{name}")

        conn.execute(
            "DELETE FROM workspace_artifacts "
            "WHERE workspace_id = %s AND name = %s",
            (workspace_id, name),
        )
        conn.execute(
            "UPDATE workspaces "
            "   SET total_bytes = total_bytes - %s,"
            "       artifact_count = artifact_count - 1 "
            " WHERE workspace_id = %s",
            (existing["size_bytes"], workspace_id),
        )


# ---------------------------------------------------------------------------
# Sandbox backing
# ---------------------------------------------------------------------------


def _sandbox_write(sandbox_id: str, name: str, content: bytes,
                   content_type: str, *, workspace_id: str) -> None:
    """Route a write to the live sandbox's filesystem under artifacts/{name}.

    Marks the workspace 'sandbox_evicted' and re-raises BackingEvicted
    if the sandbox is gone. Any other failure propagates as-is so the
    caller sees the real reason.
    """
    from core.sandbox import filesystem as _sb_fs
    from core.sandbox import models as _sb_models

    path = f"artifacts/{name}"
    payload: dict[str, Any] = {"sandbox_id": sandbox_id, "path": path}
    if _is_textlike(content_type):
        try:
            payload["content"] = content.decode("utf-8")
        except UnicodeDecodeError:
            payload["content_b64"] = base64.b64encode(content).decode("ascii")
    else:
        payload["content_b64"] = base64.b64encode(content).decode("ascii")
    try:
        _sb_fs.write_file(payload)
    except _sb_models.SandboxNotFound as exc:
        _mark_sandbox_evicted(workspace_id)
        raise wse.BackingEvicted(str(exc)) from exc


def _sandbox_read(sandbox_id: str, name: str, *, workspace_id: str) -> bytes:
    """Route a read to the live sandbox's filesystem."""
    from core.sandbox import filesystem as _sb_fs
    from core.sandbox import models as _sb_models

    try:
        result = _sb_fs.read_file({
            "sandbox_id": sandbox_id,
            "path": f"artifacts/{name}",
        })
    except _sb_models.SandboxNotFound as exc:
        _mark_sandbox_evicted(workspace_id)
        raise wse.BackingEvicted(str(exc)) from exc

    if result.get("binary") or result.get("content_b64"):
        b64 = result.get("content_b64") or ""
        return base64.b64decode(b64)
    content_str = result.get("content") or ""
    return content_str.encode("utf-8")


# ---------------------------------------------------------------------------
# Seal manifest + Ed25519 signing
# ---------------------------------------------------------------------------


def _load_or_create_signing_keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem). Generates + persists on first call.

    File format: private PEM, then a marker line, then public PEM. One
    file keeps key management trivial; mode 0o600 limits exposure.
    """
    path = _signing_key_path()
    marker = "\n---PUBLIC---\n"
    if os.path.exists(path):
        with open(path, "r", encoding="ascii") as f:
            content = f.read()
        if marker in content:
            private_pem, public_pem = content.split(marker, 1)
            return private_pem, public_pem

    private_pem, public_pem = _crypto.generate_signing_keypair()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        f.write(private_pem + marker + public_pem)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows / sandboxed filesystems may reject chmod; the file is
        # still writeable and readable by the process. Worth a debug
        # breadcrumb but not fatal.
        _LOG.debug("chmod 0o600 failed on %s; continuing", path)
    return private_pem, public_pem


def workspace_signer_did() -> str:
    """did:web:<host>:workspaces:sealer — public, served by the DID route."""
    return _identity.build_workspace_did("sealer")


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
    'sealed'; subsequent writes raise WorkspaceSealed. Idempotent: a
    second call returns the same seal as the first.
    """
    ws = get_workspace(workspace_id)
    if ws["status"] == "sealed":
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

    did = workspace_signer_did()
    sealed_at_iso = _utcnow_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE workspaces"
            "   SET status = 'sealed',"
            "       sealed_at = %s,"
            "       seal_manifest = %s,"
            "       seal_signature = %s,"
            "       seal_public_key_did = %s"
            " WHERE workspace_id = %s",
            (sealed_at_iso, _json.dumps(manifest, sort_keys=True),
             signature, did, workspace_id),
        )
    return {"manifest": manifest, "signature": signature, "public_key_did": did}


def verify_seal(workspace_id: str) -> bool:
    """Return True iff signature is valid AND every current artifact's
    sha256 matches what the manifest committed to.

    Never raises — branchable as a single bool. Use get_workspace() if
    you need detail.
    """
    try:
        ws = get_workspace(workspace_id)
    except wse.WorkspaceNotFound:
        return False
    if ws["status"] != "sealed":
        return False
    try:
        manifest = _json.loads(ws["seal_manifest"])
    except (TypeError, ValueError):
        return False

    _private_pem, public_pem = _load_or_create_signing_keypair()
    if not _crypto.verify_signature(public_pem, manifest, ws["seal_signature"]):
        return False

    manifest_by_name = {a["name"]: a for a in manifest.get("artifacts", [])}
    current = list_artifacts(workspace_id)
    if len(current) != len(manifest_by_name):
        return False
    for art in current:
        committed = manifest_by_name.get(art["name"])
        if committed is None or committed["sha256"] != art["sha256"]:
            return False
    return True


# ---------------------------------------------------------------------------
# Artifact-ref resolution (used by dispatch layer in PR 3)
# ---------------------------------------------------------------------------


_ARTIFACT_REF_KEY = "_artifact_ref"


def resolve_artifact_refs(
    payload: Any,
    *,
    caller_owner_id: str,
    allow_run_id: str | None = None,
) -> Any:
    """Recursively walk payload, replacing {_artifact_ref: 'ws_id/name'}
    with the artifact bytes (decoded as JSON if application/json, else
    UTF-8 text or base64).

    Auth: caller_owner_id must own the workspace, OR allow_run_id must
    match the workspace's run_id (worker-in-run path).

    Recurses into nested dicts and lists so callers can put refs inside
    hire_batch jobs[].input or pipeline-step input_map values.
    """
    if isinstance(payload, dict):
        if _ARTIFACT_REF_KEY in payload and len(payload) == 1:
            return _load_artifact_ref(
                payload[_ARTIFACT_REF_KEY],
                caller_owner_id=caller_owner_id,
                allow_run_id=allow_run_id,
            )
        return {
            k: resolve_artifact_refs(
                v,
                caller_owner_id=caller_owner_id,
                allow_run_id=allow_run_id,
            )
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [
            resolve_artifact_refs(
                item,
                caller_owner_id=caller_owner_id,
                allow_run_id=allow_run_id,
            )
            for item in payload
        ]
    return payload


def _load_artifact_ref(
    ref: str,
    *,
    caller_owner_id: str,
    allow_run_id: str | None,
) -> Any:
    if not isinstance(ref, str) or "/" not in ref:
        raise wse.ArtifactNameInvalid(f"bad artifact_ref {ref!r}")
    workspace_id, name = ref.split("/", 1)
    ws = get_workspace(workspace_id)
    if ws["owner_user_id"] != caller_owner_id:
        if not (allow_run_id and ws["run_id"] == allow_run_id):
            raise wse.WorkspaceForbidden(workspace_id)
    content, content_type = read_artifact(workspace_id, name)
    if content_type.startswith("application/json"):
        return _json.loads(content)
    if _is_textlike(content_type):
        return content.decode("utf-8")
    return base64.b64encode(content).decode("ascii")


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


def run_sweeper(*, now_iso: str | None = None) -> int:
    """Mark active workspaces past expires_at as 'expired'. Returns the
    number of rows updated.

    Sealed workspaces are intentionally left alone: their content is
    retained for the audit/dispute window. A v0.1 sweep can null content
    for sealed-and-past-retention rows separately.
    """
    cutoff = now_iso or _utcnow_iso()
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE workspaces SET status = 'expired' "
            " WHERE status = 'active' AND expires_at < %s",
            (cutoff,),
        )
        return cursor.rowcount or 0
