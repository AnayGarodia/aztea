"""Sandbox-backed workspace routes reads/writes to core/sandbox/filesystem.

Booting a real Docker sandbox per test is too heavy for the unit suite.
Instead these tests mock ``core.sandbox.filesystem.{read_file,write_file}``
and assert the workspace module routes correctly when
``backing_type='sandbox'``. The shared CRUD tests in
``tests/test_workspaces_crud.py`` cover the bytea path.
"""

from __future__ import annotations

import base64
import uuid

import pytest


def _close_conn() -> None:
    from core import db as _db
    conn = getattr(_db._local, "conn", None)
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass
    try:
        delattr(_db._local, "conn")
    except AttributeError:
        pass


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    from core import db as _db
    from core import workspaces as _ws
    db_path = str(tmp_path / f"ws-{uuid.uuid4().hex}.db")
    _close_conn()
    monkeypatch.setattr(_db, "DB_PATH", db_path)
    monkeypatch.setattr(_ws, "DB_PATH", db_path)
    from core.migrate import apply_migrations
    apply_migrations(db_path)
    yield
    _close_conn()


def test_sandbox_backed_write_calls_filesystem_write_file(monkeypatch):
    from core import workspaces
    from core.sandbox import filesystem as _sb_fs

    calls = []

    def fake_write(payload):
        calls.append(payload)
        return {"bytes_written": len(payload.get("content") or payload.get("content_b64") or "")}

    monkeypatch.setattr(_sb_fs, "write_file", fake_write)

    ws = workspaces.create_workspace(
        owner_user_id="usr_test", backing_type="sandbox", backing_id="sbx_fake",
    )
    workspaces.write_artifact(ws, "Dockerfile", b"FROM alpine\n", "text/plain")

    assert len(calls) == 1
    assert calls[0]["sandbox_id"] == "sbx_fake"
    assert calls[0]["path"] == "artifacts/Dockerfile"
    # text/plain content goes through inline 'content' field, not b64.
    assert calls[0]["content"] == "FROM alpine\n"


def test_sandbox_backed_write_uses_b64_for_binary(monkeypatch):
    from core import workspaces
    from core.sandbox import filesystem as _sb_fs

    calls = []
    monkeypatch.setattr(_sb_fs, "write_file", lambda p: calls.append(p))

    ws = workspaces.create_workspace(
        owner_user_id="usr_test", backing_type="sandbox", backing_id="sbx_fake",
    )
    workspaces.write_artifact(
        ws, "blob.bin", b"\x00\x01\x02\xff", "application/octet-stream",
    )

    assert "content_b64" in calls[0]
    assert base64.b64decode(calls[0]["content_b64"]) == b"\x00\x01\x02\xff"


def test_sandbox_backed_read_routes_through_filesystem(monkeypatch):
    from core import workspaces
    from core.sandbox import filesystem as _sb_fs

    monkeypatch.setattr(_sb_fs, "write_file", lambda p: None)
    monkeypatch.setattr(
        _sb_fs, "read_file",
        lambda p: {"content": "ROUTED", "binary": False},
    )

    ws = workspaces.create_workspace(
        owner_user_id="usr_test", backing_type="sandbox", backing_id="sbx_fake",
    )
    workspaces.write_artifact(ws, "x", b"unused", "text/plain")
    content, ct = workspaces.read_artifact(ws, "x")
    assert content == b"ROUTED"
    assert ct == "text/plain"


def test_sandbox_eviction_sets_workspace_status(monkeypatch):
    from core import workspaces
    from core import workspaces_errors as wse
    from core.sandbox import filesystem as _sb_fs
    from core.sandbox import models as _sb_models

    def raise_evicted(_payload):
        raise _sb_models.SandboxNotFound("gone")

    monkeypatch.setattr(_sb_fs, "write_file", raise_evicted)

    ws = workspaces.create_workspace(
        owner_user_id="usr_test", backing_type="sandbox", backing_id="sbx_dead",
    )
    with pytest.raises(wse.BackingEvicted):
        workspaces.write_artifact(ws, "x", b"data", "text/plain")
    row = workspaces.get_workspace(ws)
    assert row["status"] == "sandbox_evicted"


def test_evicted_workspace_reads_raise_backing_evicted(monkeypatch):
    from core import workspaces
    from core import workspaces_errors as wse
    from core.sandbox import filesystem as _sb_fs
    from core.sandbox import models as _sb_models

    monkeypatch.setattr(_sb_fs, "write_file", lambda p: None)

    ws = workspaces.create_workspace(
        owner_user_id="usr_test", backing_type="sandbox", backing_id="sbx_x",
    )
    workspaces.write_artifact(ws, "x", b"first", "text/plain")
    # Force eviction directly.
    workspaces._mark_sandbox_evicted(ws)
    with pytest.raises(wse.BackingEvicted):
        workspaces.read_artifact(ws, "x")
