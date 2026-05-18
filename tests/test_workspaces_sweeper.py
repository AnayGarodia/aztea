"""TTL sweeper for active workspaces past expires_at."""

from __future__ import annotations

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
    monkeypatch.setenv("AZTEA_WORKSPACE_SIGNING_KEY_PATH",
                       str(tmp_path / "key.pem"))
    from core.migrate import apply_migrations
    apply_migrations(db_path)
    yield
    _close_conn()


def _owner() -> str:
    return f"usr_{uuid.uuid4().hex[:12]}"


def _force_expire(ws_id: str, when: str = "2000-01-01T00:00:00+00:00") -> None:
    from core import workspaces
    with workspaces._connect() as conn:
        conn.execute(
            "UPDATE workspaces SET expires_at = %s WHERE workspace_id = %s",
            (when, ws_id),
        )


def test_sweeper_marks_expired_active_workspace():
    from core import workspaces
    ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
    workspaces.write_artifact(ws, "a", b"x", "text/plain")
    _force_expire(ws)
    counts = workspaces.run_sweeper(now_iso="2030-01-01T00:00:00+00:00")
    assert counts["expired_marked"] >= 1
    row = workspaces.get_workspace(ws)
    assert row["status"] == "expired"


def test_sweeper_does_not_touch_sealed():
    from core import workspaces
    ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
    workspaces.write_artifact(ws, "a", b"x", "text/plain")
    workspaces.seal_workspace(ws)
    _force_expire(ws)
    workspaces.run_sweeper(now_iso="2030-01-01T00:00:00+00:00")
    row = workspaces.get_workspace(ws)
    assert row["status"] == "sealed"


def test_sweeper_does_not_touch_active_within_ttl():
    from core import workspaces
    ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
    # expires_at is ~1h from now; sweeper at "now" should not touch it.
    workspaces.run_sweeper()
    row = workspaces.get_workspace(ws)
    assert row["status"] == "active"


def test_expired_workspace_reads_404():
    from core import workspaces
    from core import workspaces_errors as wse
    ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
    workspaces.write_artifact(ws, "a", b"x", "text/plain")
    _force_expire(ws)
    workspaces.run_sweeper(now_iso="2030-01-01T00:00:00+00:00")
    with pytest.raises(wse.WorkspaceNotFound):
        workspaces.read_artifact(ws, "a")


# ---------------------------------------------------------------------------
# v0.1 — auto-content-deletion (second sweeper pass)
# ---------------------------------------------------------------------------


def _content_for(ws_id: str, name: str) -> bytes | None:
    """Read the raw content column for an artifact; bypasses the public API
    because the public API would refuse a purged workspace."""
    from core import workspaces
    conn = workspaces._connect()
    row = conn.execute(
        "SELECT content FROM workspace_artifacts "
        " WHERE workspace_id = %s AND name = %s",
        (ws_id, name),
    ).fetchone()
    return row["content"] if row else None


def test_purge_nulls_content_for_long_expired_workspace():
    from core import workspaces
    ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
    workspaces.write_artifact(ws, "a", b"keep-or-purge", "text/plain")
    _force_expire(ws)
    # Sweep with a far-future cutoff — both expires_at and the 7-day
    # retention buffer should be exceeded.
    counts = workspaces.run_sweeper(now_iso="2030-01-01T00:00:00+00:00")
    assert counts["expired_marked"] >= 1
    assert counts["content_purged"] >= 1

    # Content is gone.
    assert _content_for(ws, "a") is None
    row = workspaces.get_workspace(ws)
    assert row["content_purged_at"] is not None
    # Metadata survives.
    listing = workspaces.list_artifacts(ws)
    assert len(listing) == 1
    assert listing[0]["name"] == "a"
    assert listing[0]["sha256"]


def test_purge_skips_recently_expired_within_retention():
    from core import workspaces
    ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
    workspaces.write_artifact(ws, "a", b"still-fresh", "text/plain")
    _force_expire(ws, when="2026-05-15T00:00:00+00:00")
    # Cutoff is 3 days past expires_at — well within the 7-day retention.
    counts = workspaces.run_sweeper(now_iso="2026-05-18T00:00:00+00:00")
    assert counts["content_purged"] == 0
    assert _content_for(ws, "a") == b"still-fresh"
    row = workspaces.get_workspace(ws)
    assert row["content_purged_at"] is None


def test_purge_handles_sealed_workspaces_after_long_retention():
    from core import workspaces
    ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
    workspaces.write_artifact(ws, "a", b"sealed-content", "text/plain")
    workspaces.seal_workspace(ws)
    # Force sealed_at into the deep past (>90d retention).
    with workspaces._connect() as conn:
        conn.execute(
            "UPDATE workspaces SET sealed_at = %s WHERE workspace_id = %s",
            ("2020-01-01T00:00:00+00:00", ws),
        )
    counts = workspaces.run_sweeper(now_iso="2030-01-01T00:00:00+00:00")
    assert counts["content_purged"] >= 1
    assert _content_for(ws, "a") is None
    # The seal manifest is preserved so external verifiers can still
    # confirm what the workspace originally contained.
    row = workspaces.get_workspace(ws)
    assert row["status"] == "sealed"
    assert row["seal_manifest"] is not None
    assert row["seal_signature"] is not None


def test_purge_does_not_touch_sealed_within_retention():
    from core import workspaces
    ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
    workspaces.write_artifact(ws, "a", b"fresh-seal", "text/plain")
    workspaces.seal_workspace(ws)
    # sealed_at defaults to now; cutoff = now means well within 90d retention.
    counts = workspaces.run_sweeper()
    assert counts["content_purged"] == 0
    assert _content_for(ws, "a") == b"fresh-seal"


def test_purge_is_idempotent():
    from core import workspaces
    ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
    workspaces.write_artifact(ws, "a", b"once", "text/plain")
    _force_expire(ws)
    first = workspaces.run_sweeper(now_iso="2030-01-01T00:00:00+00:00")
    second = workspaces.run_sweeper(now_iso="2030-01-01T00:00:00+00:00")
    assert first["content_purged"] >= 1
    assert second["content_purged"] == 0  # already purged; skip
    assert _content_for(ws, "a") is None


def test_purge_respects_env_override_for_expired_retention(monkeypatch):
    """Setting AZTEA_WORKSPACE_EXPIRED_RETENTION_SECONDS=0 means purge
    immediately on first sweep after expiration."""
    # Mutate the module constant directly rather than reloading the module,
    # because importlib.reload() leaves the bumped constant in place for
    # subsequent tests when the env var is later cleared by monkeypatch.
    # Discovered via main CI failure on the 0048->0053 renumber merge:
    # test_purge_skips_recently_expired_within_retention was matching rows
    # because this test had reloaded workspaces with retention=0 and the
    # reset never happened.
    from core import workspaces
    original = workspaces._EXPIRED_CONTENT_RETENTION_SECONDS
    monkeypatch.setattr(workspaces, "_EXPIRED_CONTENT_RETENTION_SECONDS", 0)
    try:
        ws = workspaces.create_workspace(owner_user_id=_owner(), ttl_seconds=3600)
        workspaces.write_artifact(ws, "a", b"vanishing", "text/plain")
        _force_expire(ws, when="2026-05-18T00:00:00+00:00")
        counts = workspaces.run_sweeper(now_iso="2026-05-18T00:00:01+00:00")
        assert counts["content_purged"] >= 1
        assert _content_for(ws, "a") is None
    finally:
        # Defensive: monkeypatch.setattr above already restores on teardown,
        # but make the restoration explicit so a future refactor doesn't
        # silently lose it.
        workspaces._EXPIRED_CONTENT_RETENTION_SECONDS = original
