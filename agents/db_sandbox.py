from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any


_MAX_QUERY_CHARS = 40_000
_MAX_STATEMENTS = 25
_MAX_ROWS = 500
_TIMEOUT_SECONDS = 5.0
_MAX_DB_BYTES = 50 * 1024 * 1024


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _normalize_queries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    queries = payload.get("queries")
    if isinstance(queries, list) and queries:
        if len(queries) > _MAX_STATEMENTS:
            return [{"error": _err("db_sandbox.too_many_queries", f"queries may contain at most {_MAX_STATEMENTS} statements.")}]
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(queries[:_MAX_STATEMENTS]):
            if not isinstance(item, dict):
                return [{"error": _err("db_sandbox.invalid_query", f"queries[{index}] must be an object.")}]
            sql = str(item.get("sql") or "").strip()
            if not sql:
                return [{"error": _err("db_sandbox.invalid_query", f"queries[{index}].sql is required.")}]
            params = item.get("params")
            if params is not None and not isinstance(params, list):
                return [{"error": _err("db_sandbox.invalid_query", f"queries[{index}].params must be a list.")}]
            normalized.append({"sql": sql, "params": params or []})
        return normalized

    sql = str(payload.get("sql") or "").strip()
    if not sql:
        return [{"error": _err("db_sandbox.missing_sql", "Provide sql or queries.")}]
    params = payload.get("params")
    if params is not None and not isinstance(params, list):
        return [{"error": _err("db_sandbox.invalid_params", "params must be a list.")}]
    return [{"sql": sql, "params": params or []}]


def _query_plan(cur: sqlite3.Cursor, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    try:
        rows = cur.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [
        {"select_id": row[0], "order": row[1], "from": row[2], "detail": row[3]}
        for row in rows
    ]


def run(payload: dict[str, Any]) -> dict[str, Any]:
    schema_sql = str(payload.get("schema_sql") or "").strip()
    if len(schema_sql) > _MAX_QUERY_CHARS:
        return _err("db_sandbox.schema_too_large", f"schema_sql exceeds {_MAX_QUERY_CHARS} characters.")

    normalized_queries = _normalize_queries(payload)
    if normalized_queries and "error" in normalized_queries[0]:
        return normalized_queries[0]["error"]  # type: ignore[index]

    for query in normalized_queries:
        if len(query["sql"]) > _MAX_QUERY_CHARS:
            return _err("db_sandbox.sql_too_large", f"Each sql statement must be <= {_MAX_QUERY_CHARS} characters.")

    timeout_seconds = _TIMEOUT_SECONDS
    explain = bool(payload.get("explain", True))

    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="aztea-db-sandbox-") as tmpdir:
        db_path = Path(tmpdir) / "sandbox.sqlite3"
        conn = sqlite3.connect(str(db_path), timeout=1, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            page_cap = max(1, _MAX_DB_BYTES // 4096)
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA page_size=4096")
            conn.execute(f"PRAGMA max_page_count={page_cap}")

            def _progress() -> int:
                if time.monotonic() - start > timeout_seconds:
                    return 1
                return 0

            conn.set_progress_handler(_progress, 5_000)
            cur = conn.cursor()

            if schema_sql:
                cur.executescript(schema_sql)
                conn.commit()

            results: list[dict[str, Any]] = []
            for query in normalized_queries:
                sql = query["sql"]
                params = query["params"]
                statement_started = time.monotonic()
                try:
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
                    results.append(
                        {
                            "sql": sql,
                            "columns": columns,
                            "rows": rows,
                            "row_count": len(rows),
                            "truncated": truncated,
                            "rows_affected": cursor.rowcount if cursor.rowcount >= 0 else None,
                            "query_plan": plan,
                            "execution_time_ms": int((time.monotonic() - statement_started) * 1000),
                        }
                    )
                except sqlite3.OperationalError as exc:
                    message = str(exc)
                    if "interrupted" in message.lower():
                        return _err("db_sandbox.timeout", f"Query exceeded {timeout_seconds:.0f}s execution limit.")
                    return _err("db_sandbox.sql_error", message)
                except sqlite3.DatabaseError as exc:
                    return _err("db_sandbox.database_error", str(exc))

            size_bytes = db_path.stat().st_size if db_path.exists() else 0
            return {
                "engine": "sqlite",
                "results": results,
                "statements_executed": len(results),
                "db_size_bytes": size_bytes,
                "execution_time_ms": int((time.monotonic() - start) * 1000),
            }
        finally:
            conn.close()
