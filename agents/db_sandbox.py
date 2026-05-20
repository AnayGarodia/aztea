"""Ephemeral SQLite sandbox for safe, isolated SQL execution.

Each ``run()`` call creates a fresh SQLite database in a temporary directory,
executes the requested SQL, and returns results — all within a single call
lifetime. The database is deleted when the call returns, so there is no state
between invocations.

Safety limits
-------------
- ``_MAX_QUERY_CHARS`` (40 000) — maximum total characters per SQL statement.
- ``_MAX_STATEMENTS`` (25)     — maximum statements per single call.
- ``_MAX_ROWS`` (500)          — rows returned per statement; excess are truncated.
- ``_TIMEOUT_SECONDS`` (5)     — wall-clock execution budget enforced via
  SQLite's progress handler; interrupted queries return ``db_sandbox.timeout``.
- ``_MAX_DB_BYTES`` (50 MB)    — storage cap via SQLite ``max_page_count`` PRAGMA.

Payload schema
--------------
Either:
  ``sql`` (str) + optional ``params`` (list)   — single statement
  ``queries`` (list of {sql, params?})          — multiple statements

Optional fields:
  ``schema_sql`` (str)   — DDL run before queries (CREATE TABLE, INSERT seeds, etc.)
  ``explain`` (bool)     — include EXPLAIN QUERY PLAN output per statement (default True)

All write operations are committed after each non-SELECT statement; PRAGMA
``foreign_keys=ON`` is set so FK constraints are honoured in the sandbox.
"""

from __future__ import annotations

import re as _re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any
from agents._contracts import agent_error as _err

_MAX_QUERY_CHARS = 40_000
_MAX_STATEMENTS = 25
_MAX_ROWS = 500
_TIMEOUT_SECONDS = 5.0
_MAX_DB_BYTES = 50 * 1024 * 1024
_PAGE_SIZE = 4096
_PROGRESS_HANDLER_STEPS = 5_000
_RESOURCE_LIMIT_MSG_CHARS = 200

_BLOCKED_SQL_RE = _re.compile(r"^\s*(ATTACH|DETACH)\s", _re.IGNORECASE)
_STATEMENT_SPLIT_RE = _re.compile(r";\s*")


def _check_sql_blocked(sql: str) -> "dict | None":
    if _BLOCKED_SQL_RE.match(sql):
        keyword = sql.strip().split()[0].upper()
        return _err(
            "db_sandbox.blocked_command", f"{keyword} is not permitted in the sandbox."
        )
    return None


def _looks_multi_statement(sql: str) -> bool:
    """Pure: True iff `sql` contains ≥2 non-empty top-level statements separated by `;`.

    Strips trailing/empty fragments so `"SELECT 1;"` and `"SELECT 1; ; "`
    both register as single statements. Comments and string literals are not
    parsed — false positives on `;` inside quoted strings are acceptable
    here since the caller can switch to the `queries: [...]` form anyway.
    """
    pieces = [p.strip() for p in _STATEMENT_SPLIT_RE.split(sql) if p.strip()]
    return len(pieces) > 1



def _err_envelope_in_list(envelope: dict) -> list[dict[str, Any]]:
    """Pure: wrap an error envelope in the shape ``_normalize_queries`` returns."""
    return [{"error": envelope}]


def _normalize_one_query(index: int, item: Any) -> dict[str, Any]:
    """Pure: validate one query item; returns ``{sql, params}`` or ``{error}``."""
    if isinstance(item, str):
        item = {"sql": item}
    if not isinstance(item, dict):
        return {
            "error": _err(
                "db_sandbox.invalid_query",
                f"queries[{index}] must be a SQL string or an object.",
            )
        }
    sql = str(item.get("sql") or "").strip()
    if not sql:
        return {
            "error": _err(
                "db_sandbox.invalid_query",
                f"queries[{index}].sql is required.",
            )
        }
    # SQLite's `cur.execute` rejects multi-statement input with an opaque
    # `sqlite3.Warning`. Detect it up-front and return a structured error
    # pointing the caller at `queries: [...]` so we never surface a raw 502.
    if _looks_multi_statement(sql):
        return {
            "error": _err(
                "db_sandbox.multi_statement_not_allowed",
                f"queries[{index}].sql contains multiple statements; pass them as a list under `queries`.",
            )
        }
    blocked = _check_sql_blocked(sql)
    if blocked:
        return {"error": blocked}
    params = item.get("params")
    if params is not None and not isinstance(params, list):
        return {
            "error": _err(
                "db_sandbox.invalid_query",
                f"queries[{index}].params must be a list.",
            )
        }
    return {"sql": sql, "params": params or []}


def _normalize_queries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: shape payload into ``[{sql, params}, ...]`` or a single ``[{error}]`` envelope."""
    queries = payload.get("queries")
    if isinstance(queries, list) and queries:
        if len(queries) > _MAX_STATEMENTS:
            return _err_envelope_in_list(_err(
                "db_sandbox.too_many_queries",
                f"queries may contain at most {_MAX_STATEMENTS} statements.",
            ))
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(queries[:_MAX_STATEMENTS]):
            normalized_item = _normalize_one_query(index, item)
            if "error" in normalized_item:
                return [normalized_item]
            normalized.append(normalized_item)
        return normalized
    sql = str(payload.get("sql") or "").strip()
    if not sql:
        return _err_envelope_in_list(_err("db_sandbox.missing_sql", "Provide sql or queries."))
    blocked = _check_sql_blocked(sql)
    if blocked:
        return _err_envelope_in_list(blocked)
    # L-2 (audit 2026-05-19): pre-fix, callers passing a multi-statement
    # string under `sql=` got an opaque 502 because sqlite3.cur.execute
    # raises sqlite3.Warning on multi-statement input. The `queries=[]`
    # path already preflights this — mirror the check here so `sql=`
    # surfaces a structured 422 with a clear migration hint instead.
    if _looks_multi_statement(sql):
        return _err_envelope_in_list(
            _err(
                "db_sandbox.multi_statement_not_allowed",
                "sql contains multiple statements; pass them as a list "
                "under `queries: [{sql: ...}, ...]` instead.",
            )
        )
    params = payload.get("params")
    if params is not None and not isinstance(params, list):
        return _err_envelope_in_list(_err("db_sandbox.invalid_params", "params must be a list."))
    return [{"sql": sql, "params": params or []}]


def _query_plan(
    cur: sqlite3.Cursor, sql: str, params: list[Any]
) -> list[dict[str, Any]]:
    try:
        rows = cur.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [
        {"select_id": row[0], "order": row[1], "from": row[2], "detail": row[3]}
        for row in rows
    ]


def _validate_schema_sql(schema_sql: str) -> dict | None:
    """Pure: enforce schema-sql size and blocked-keyword rules. None if OK."""
    if len(schema_sql) > _MAX_QUERY_CHARS:
        return _err(
            "db_sandbox.schema_too_large",
            f"schema_sql exceeds {_MAX_QUERY_CHARS} characters.",
        )
    if not schema_sql:
        return None
    for stmt in _STATEMENT_SPLIT_RE.split(schema_sql):
        cleaned = stmt.strip()
        if not cleaned:
            continue
        blocked = _check_sql_blocked(cleaned)
        if blocked:
            return blocked
    return None


def _validate_query_sizes(queries: list[dict[str, Any]]) -> dict | None:
    """Pure: each query.sql must fit within ``_MAX_QUERY_CHARS``."""
    for query in queries:
        if len(query["sql"]) > _MAX_QUERY_CHARS:
            return _err(
                "db_sandbox.sql_too_large",
                f"Each sql statement must be <= {_MAX_QUERY_CHARS} characters.",
            )
    return None


def _configure_sandbox_pragmas(conn: sqlite3.Connection) -> None:
    """Side-effect: apply the sandbox PRAGMAs that bound resource use."""
    page_cap = max(1, _MAX_DB_BYTES // _PAGE_SIZE)
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute(f"PRAGMA page_size={_PAGE_SIZE}")
    conn.execute(f"PRAGMA max_page_count={page_cap}")


def _execute_one_statement(
    cur: sqlite3.Cursor, conn: sqlite3.Connection,
    sql: str, params: list[Any], *, explain: bool, statement_started: float,
) -> dict[str, Any]:
    """Side-effect: execute one SQL statement and shape the result row."""
    plan = _query_plan(cur, sql, params) if explain else []
    cursor = cur.execute(sql, params)
    description = cursor.description or []
    columns = [col[0] for col in description]
    rows: list[dict[str, Any]] = []
    truncated = False
    if description:
        for row in cursor.fetchmany(_MAX_ROWS + 1):
            if len(rows) >= _MAX_ROWS:
                truncated = True
                break
            rows.append(dict(row))
    else:
        conn.commit()
    return {
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "rows_affected": cursor.rowcount if cursor.rowcount >= 0 else None,
        "query_plan": plan,
        "execution_time_ms": int((time.monotonic() - statement_started) * 1000),
    }


def _statement_error_row(
    sql: str, code: str, message: str, statement_started: float
) -> dict[str, Any]:
    """Pure: per-statement error row, matching the success-row shape."""
    return {
        "sql": sql,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "rows_affected": None,
        "query_plan": [],
        "error": {"code": code, "message": message},
        "execution_time_ms": int((time.monotonic() - statement_started) * 1000),
    }


def _run_statements(
    cur: sqlite3.Cursor, conn: sqlite3.Connection,
    queries: list[dict[str, Any]], *, explain: bool, timeout_seconds: float,
) -> list[dict[str, Any]] | dict:
    """Side-effect: execute each query; returns the list of result rows or a fatal error envelope.

    Why: ``MemoryError`` and ``OverflowError`` from things like ``randomblob(100MB)``
    bubble up as opaque 500s otherwise; catching them here gives the caller
    a structured envelope that the platform can refund against.
    """
    results: list[dict[str, Any]] = []
    for query in queries:
        sql = query["sql"]
        params = query["params"]
        statement_started = time.monotonic()
        try:
            results.append(_execute_one_statement(
                cur, conn, sql, params, explain=explain, statement_started=statement_started,
            ))
        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "interrupted" in message.lower():
                return _err(
                    "db_sandbox.timeout",
                    f"Query exceeded {timeout_seconds:.0f}s execution limit.",
                )
            results.append(_statement_error_row(sql, "db_sandbox.sql_error", message, statement_started))
        except sqlite3.DatabaseError as exc:
            results.append(_statement_error_row(
                sql, "db_sandbox.database_error", str(exc), statement_started,
            ))
        except (MemoryError, OverflowError, ValueError) as exc:
            return _err(
                "db_sandbox.resource_limit",
                f"Statement exceeded sandbox resource limits: {type(exc).__name__}: "
                f"{str(exc)[:_RESOURCE_LIMIT_MSG_CHARS]}",
            )
    return results


def _all_statements_errored(results: list[dict[str, Any]]) -> dict | None:
    """Pure: when every statement failed, return a single rolled-up error envelope so the call refunds."""
    error_count = sum(1 for item in results if item.get("error"))
    if not results or error_count != len(results):
        return None
    first_error = next(
        (item.get("error") for item in results if item.get("error")), None,
    ) or {}
    return {
        "error": {
            "code": "db_sandbox.sql_error",
            "message": str(first_error.get("message") or "All SQL statements failed."),
            "details": {
                "statements_executed": len(results),
                "statement_error_count": error_count,
            },
        }
    }


def _shape_run_response(
    results: list[dict[str, Any]], db_path: Path, start: float
) -> dict[str, Any]:
    """Pure: shape a successful run's results into the response envelope."""
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    return {
        "engine": "sqlite",
        "results": results,
        "statements_executed": len(results),
        "statement_error_count": sum(1 for item in results if item.get("error")),
        "db_size_bytes": size_bytes,
        "execution_time_ms": int((time.monotonic() - start) * 1000),
    }


def _execute_in_sandbox(
    db_path: Path, schema_sql: str, queries: list[dict[str, Any]],
    *, explain: bool, timeout_seconds: float, start: float,
) -> dict[str, Any]:
    """Side-effect: open a sandbox connection and run schema + queries.

    Why: a raw sqlite3 connection is justified — the sandbox must NOT share
    ``core.db``'s pool with the registry; an ephemeral DB lives only for
    this call's lifetime.
    """
    conn = sqlite3.connect(str(db_path), timeout=1, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        _configure_sandbox_pragmas(conn)

        def _progress() -> int:
            return 1 if time.monotonic() - start > timeout_seconds else 0

        conn.set_progress_handler(_progress, _PROGRESS_HANDLER_STEPS)
        cur = conn.cursor()
        if schema_sql:
            cur.executescript(schema_sql)
            conn.commit()
        results = _run_statements(
            cur, conn, queries, explain=explain, timeout_seconds=timeout_seconds,
        )
        if isinstance(results, dict):
            return results  # fatal error envelope (timeout / resource_limit)
        rolled_up = _all_statements_errored(results)
        return rolled_up if rolled_up is not None else _shape_run_response(results, db_path, start)
    finally:
        conn.close()


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute SQL against an ephemeral SQLite sandbox and return results.

    Why: callers want to test queries against fresh schema without polluting
    any production DB; a per-call tempfile gives complete isolation.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    schema_sql = str(payload.get("schema_sql") or "").strip()
    schema_err = _validate_schema_sql(schema_sql)
    if schema_err is not None:
        return schema_err
    normalized_queries = _normalize_queries(payload)
    if normalized_queries and "error" in normalized_queries[0]:
        return normalized_queries[0]["error"]  # type: ignore[index]
    size_err = _validate_query_sizes(normalized_queries)
    if size_err is not None:
        return size_err
    explain = bool(payload.get("explain", True))
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="aztea-db-sandbox-") as tmpdir:
        db_path = Path(tmpdir) / "sandbox.sqlite3"
        return _execute_in_sandbox(
            db_path, schema_sql, normalized_queries,
            explain=explain, timeout_seconds=_TIMEOUT_SECONDS, start=start,
        )
