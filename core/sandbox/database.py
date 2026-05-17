"""DB ops against the detected Postgres service in a sandbox.

# OWNS: db_query / db_snapshot / db_restore / db_introspect / db_seed.
# NOT OWNS: Postgres detection (lives in boot.py).
# INVARIANTS:
#   * Every action operates on the detected_postgres_service from BootInfo.
#   * Snapshots use pg_dump (custom format) and live under the per-sandbox
#     state dir so they survive sandbox stop/restart for snapshot use.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from core.sandbox.docker_cli import run_docker
from core.sandbox.models import (
    SandboxInvalidInput,
    SandboxServiceMissing,
)
from core.sandbox.state import SandboxState, get, sandbox_dir

_LOG = logging.getLogger("aztea.sandbox.database")
_MAX_ROWS = 1000
_QUERY_TIMEOUT_S = 60


def db_query(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require_with_pg(payload)
    sql = str(payload.get("sql") or "").strip()
    if not sql:
        raise SandboxInvalidInput("sql is required for sandbox_db_query")
    explain = bool(payload.get("explain", False))
    rows, columns, status = _run_psql_select(state, sql)
    response: dict[str, Any] = {
        "sandbox_id": state.sandbox_id,
        "rows": rows[:_MAX_ROWS],
        "row_count": len(rows),
        "columns": columns,
        "truncated": len(rows) > _MAX_ROWS,
        "status": status,
    }
    if explain and rows is not None:
        plan, plan_err = _run_psql_explain(state, sql)
        response["explain_analyze"] = plan
        if plan_err:
            response["explain_analyze_error"] = plan_err
    state.touch()
    return response


def db_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require_with_pg(payload)
    label = str(payload.get("label") or "").strip() or _ts_label()
    snap_dir = sandbox_dir(state.sandbox_id) / "db_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = snap_dir / f"{label}.pgdump"
    container = state.boot.services[state.boot.detected_postgres_service]["container"]
    db_name = state.boot.detected_postgres_db or "postgres"
    user = state.boot.detected_postgres_user or "postgres"
    proc = run_docker(
        [
            "exec",
            "-e",
            "PGPASSWORD",
            container,
            "pg_dump",
            "-U",
            user,
            "-d",
            db_name,
            "-Fc",
        ],
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise SandboxInvalidInput(
            f"pg_dump failed (rc={proc.returncode}): {(proc.stderr or '')[:512]}"
        )
    target.write_bytes(proc.stdout.encode("latin-1", "replace"))
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "label": label,
        "size_bytes": target.stat().st_size,
        "path": str(target),
    }


def db_restore(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require_with_pg(payload)
    label = str(payload.get("label") or "").strip()
    if not label:
        raise SandboxInvalidInput("label is required for sandbox_db_restore")
    target = sandbox_dir(state.sandbox_id) / "db_snapshots" / f"{label}.pgdump"
    if not target.is_file():
        raise SandboxInvalidInput(f"db snapshot not found: {label!r}")
    container = state.boot.services[state.boot.detected_postgres_service]["container"]
    db_name = state.boot.detected_postgres_db or "postgres"
    user = state.boot.detected_postgres_user or "postgres"
    # Drop + recreate the target DB, then pg_restore over stdin.
    _terminate_connections(container, user, db_name)
    run_docker(
        ["exec", container, "psql", "-U", user, "-d", "postgres", "-c", f"DROP DATABASE IF EXISTS {db_name};"],
        timeout=30,
        check=False,
    )
    run_docker(
        ["exec", container, "psql", "-U", user, "-d", "postgres", "-c", f"CREATE DATABASE {db_name};"],
        timeout=30,
    )
    raw = target.read_bytes()
    run_docker(
        [
            "exec",
            "-i",
            container,
            "pg_restore",
            "--no-owner",
            "--clean",
            "--if-exists",
            "-U",
            user,
            "-d",
            db_name,
        ],
        stdin=raw.decode("latin-1"),
        timeout=300,
    )
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "restored_label": label,
        "database": db_name,
    }


def db_introspect(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require_with_pg(payload)
    schema_sql = """
SELECT json_build_object(
    'tables', (
        SELECT json_agg(json_build_object(
            'schema', table_schema,
            'name', table_name,
            'row_estimate', (
                SELECT reltuples FROM pg_class
                WHERE oid = (table_schema || '.' || table_name)::regclass
            ),
            'size_bytes', pg_total_relation_size((table_schema || '.' || table_name)::regclass)
        )) FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ),
    'indexes', (
        SELECT json_agg(json_build_object(
            'name', indexname, 'table', tablename, 'def', indexdef
        )) FROM pg_indexes
        WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
    ),
    'locks', (
        SELECT json_agg(json_build_object(
            'pid', pid, 'mode', mode, 'relation', relation::regclass::text,
            'granted', granted, 'query', LEFT(query, 200)
        )) FROM pg_locks l
        LEFT JOIN pg_stat_activity a USING (pid)
        WHERE relation IS NOT NULL
    )
) AS doc
"""
    rows, _columns, status = _run_psql_select(state, schema_sql)
    doc: Any = {}
    if rows:
        doc = rows[0].get("doc") or {}
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "introspection": doc,
        "status": status,
    }


def db_seed(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the project's seed command (passed in as ``cmd``) against the DB.

    Why: every project's seed flow is different. The agent doesn't try to
    guess — caller passes the same command they'd run locally, we run it
    inside the application service (not the DB service).
    """
    state = _require(payload)
    cmd = str(payload.get("cmd") or "").strip()
    if not cmd:
        raise SandboxInvalidInput("cmd is required for sandbox_db_seed")
    service = str(payload.get("service") or "").strip()
    if service and service in state.boot.services:
        container = state.boot.services[service]["container"]
    else:
        # default to app/web/api
        for hint in ("app", "web", "api"):
            if hint in state.boot.services:
                container = state.boot.services[hint]["container"]
                break
        else:
            raise SandboxServiceMissing("no app-like service for db_seed; pass 'service'")
    start = time.time()
    proc = run_docker(
        ["exec", container, "sh", "-lc", cmd],
        timeout=300,
        check=False,
    )
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[:8000],
        "stderr": (proc.stderr or "")[:4000],
        "duration_ms": int((time.time() - start) * 1000),
    }


def _run_psql_select(
    state: SandboxState, sql: str
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Run ``sql`` and return ``(rows, columns, status)``.

    ``psql --json`` isn't a thing, so we wrap with ``row_to_json`` for rows
    that are SELECT-shaped; non-SELECT statements get a status string.
    """
    container = state.boot.services[state.boot.detected_postgres_service]["container"]
    db_name = state.boot.detected_postgres_db or "postgres"
    user = state.boot.detected_postgres_user or "postgres"
    wrapped = f"SELECT json_agg(row_to_json(t)) AS _all FROM ({sql.rstrip(';')}) t;"
    proc = run_docker(
        ["exec", container, "psql", "-U", user, "-d", db_name, "-tAX", "-c", wrapped],
        timeout=_QUERY_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        # Probably a non-SELECT; retry as a direct call.
        direct = run_docker(
            ["exec", container, "psql", "-U", user, "-d", db_name, "-tAX", "-c", sql],
            timeout=_QUERY_TIMEOUT_S,
            check=False,
        )
        return [], [], direct.stdout.strip() or direct.stderr.strip() or "ok"
    body = (proc.stdout or "").strip()
    if not body or body == "":
        return [], [], "0 rows"
    try:
        rows = json.loads(body) or []
    except ValueError:
        return [], [], body[:256]
    columns: list[str] = []
    if rows and isinstance(rows[0], dict):
        columns = list(rows[0].keys())
    return rows, columns, f"{len(rows)} rows"


def _run_psql_explain(state: SandboxState, sql: str) -> tuple[Any, str | None]:
    container = state.boot.services[state.boot.detected_postgres_service]["container"]
    db_name = state.boot.detected_postgres_db or "postgres"
    user = state.boot.detected_postgres_user or "postgres"
    wrapped = f"EXPLAIN (ANALYZE, FORMAT JSON) {sql.rstrip(';')};"
    proc = run_docker(
        ["exec", container, "psql", "-U", user, "-d", db_name, "-tAX", "-c", wrapped],
        timeout=_QUERY_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        return None, (proc.stderr or "")[:512]
    body = (proc.stdout or "").strip()
    if not body:
        return None, None
    try:
        return json.loads(body), None
    except ValueError:
        return body, "unparseable EXPLAIN output"


def _terminate_connections(container: str, user: str, db_name: str) -> None:
    """Side-effect: kick existing connections so DROP DATABASE doesn't block."""
    run_docker(
        [
            "exec",
            container,
            "psql",
            "-U",
            user,
            "-d",
            "postgres",
            "-c",
            (
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{db_name}' AND pid <> pg_backend_pid();"
            ),
        ],
        timeout=30,
        check=False,
    )


def _ts_label() -> str:
    return time.strftime("snap-%Y%m%d-%H%M%S", time.gmtime())


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxInvalidInput(f"sandbox '{sandbox_id}' not active")
    return state


def _require_with_pg(payload: dict[str, Any]) -> SandboxState:
    state = _require(payload)
    if not state.boot.detected_postgres_service:
        raise SandboxServiceMissing(
            "no Postgres service detected in this sandbox; DB ops are only "
            "available when compose includes a postgres-like service"
        )
    return state
