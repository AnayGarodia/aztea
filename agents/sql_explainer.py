"""
sql_explainer.py — Run EXPLAIN QUERY PLAN against a real SQLite database
populated from the caller's schema. Returns the raw plan plus
heuristic-derived index suggestions.

Owns:
  - Spinning an isolated, in-memory SQLite database per call.
  - Executing schema_sql (DDL + INSERTs) before EXPLAIN.
  - Running EXPLAIN QUERY PLAN for one or more queries.
  - Heuristic suggestions for SCAN nodes that could become SEARCH.

Does NOT own:
  - Other SQL dialects. SQLite only — close enough for plan-shape work,
    but caller should know we're not running their PG/MySQL planner.
  - Persisting any data anywhere.

Hard limits: schema_sql ≤ 30 KB, ≤ 10 queries per call, query length
≤ 4 KB each, total wall clock ≤ 5 s, in-memory DB only (no disk file
written, no network).

Input:
  {
    "schema_sql": str,          # required, DDL + seed data
    "queries": [str],           # required, 1..10 SELECTs (parametric SELECT-only)
    "params": [[...] | {...}]   # optional, parallel to queries
  }

Output:
  {
    "queries": [
      {
        "sql": str,
        "plan": [{"id": int, "parent": int, "detail": str}],
        "issues": [str],           # heuristic findings
        "suggestions": [str],      # human-readable index/restructure hints
        "elapsed_ms": float
      }
    ],
    "total_issues": int,
    "summary": str
  }
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

_MAX_SCHEMA_CHARS = 30_000
_MAX_QUERIES = 10
_MAX_QUERY_CHARS = 4096

_DML_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER)\b", re.IGNORECASE)
_SELECT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_SCHEMA_SQL_RE = re.compile(
    r"(?ix)\b("
    r"attach|detach|vacuum|pragma|load_extension|"
    r"create\s+virtual\s+table|alter\s+table|drop\s+table|drop\s+index|drop\s+view|drop\s+trigger"
    r")\b"
)
_MAX_WALL_CLOCK_SECONDS = 5.0

_DENIED_ACTION_CODES = {
    getattr(sqlite3, "SQLITE_ATTACH", -1),
    getattr(sqlite3, "SQLITE_DETACH", -1),
    getattr(sqlite3, "SQLITE_ALTER_TABLE", -1),
    getattr(sqlite3, "SQLITE_DROP_TABLE", -1),
    getattr(sqlite3, "SQLITE_DROP_INDEX", -1),
    getattr(sqlite3, "SQLITE_DROP_VIEW", -1),
    getattr(sqlite3, "SQLITE_DROP_TRIGGER", -1),
    getattr(sqlite3, "SQLITE_DROP_VTABLE", -1),
    getattr(sqlite3, "SQLITE_CREATE_VTABLE", -1),
}


def _err(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **details}}


def _analyze_plan(plan_rows: list[tuple[int, int, int, str]]) -> tuple[list[str], list[str]]:
    """Return (issues, suggestions) from plan rows."""
    issues: list[str] = []
    suggestions: list[str] = []
    for _id, _parent, _notused, detail in plan_rows:
        upper = detail.upper()
        if "SCAN CONSTANT ROW" in upper:
            continue
        if "SCAN" in upper and "USING" not in upper:
            tbl_match = re.search(r"SCAN\s+(?:TABLE\s+)?(\w+)", detail, re.IGNORECASE)
            if tbl_match:
                tbl = tbl_match.group(1)
                issues.append(f"Full scan on `{tbl}`")
                suggestions.append(
                    f"Consider an index on the WHERE/JOIN columns used against `{tbl}`."
                )
            else:
                issues.append(f"Full scan: {detail}")
        if "USE TEMP B-TREE" in upper:
            issues.append("Plan uses a temporary B-tree (likely ORDER BY/GROUP BY without an index).")
            suggestions.append(
                "Consider an index covering the ORDER BY/GROUP BY columns to remove the temp B-tree sort."
            )
        if "MATERIALIZE" in upper:
            issues.append("Subquery materialized as a temp table.")
        if "CORRELATED" in upper:
            issues.append("Correlated subquery detected — may execute per outer row.")
            suggestions.append("Consider rewriting the correlated subquery as a JOIN or CTE.")
    # de-dup while preserving order
    seen: set[str] = set()
    issues = [x for x in issues if not (x in seen or seen.add(x))]
    seen.clear()
    suggestions = [x for x in suggestions if not (x in seen or seen.add(x))]
    return issues, suggestions


def _authorizer(action_code: int, param1: str | None, param2: str | None, db_name: str | None, source: str | None) -> int:
    if action_code in _DENIED_ACTION_CODES:
        return sqlite3.SQLITE_DENY
    if action_code == getattr(sqlite3, "SQLITE_FUNCTION", -1):
        function_name = str(param2 or param1 or "").lower()
        if function_name in {"load_extension", "writefile", "readfile"}:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def run(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return _err("sql_explainer.invalid_payload", "payload must be an object")

    schema_sql = payload.get("schema_sql")
    if not isinstance(schema_sql, str) or not schema_sql.strip():
        return _err("sql_explainer.missing_schema", "'schema_sql' is required and must contain DDL/seed SQL")
    if len(schema_sql) > _MAX_SCHEMA_CHARS:
        return _err("sql_explainer.schema_too_large", f"schema_sql exceeds {_MAX_SCHEMA_CHARS} chars")
    forbidden = _FORBIDDEN_SCHEMA_SQL_RE.search(schema_sql)
    if forbidden:
        return _err(
            "sql_explainer.unsafe_schema_sql",
            f"schema_sql contains forbidden statement or pragma: {forbidden.group(0)!r}",
        )

    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        return _err("sql_explainer.missing_queries", "'queries' is required and must be a non-empty list")
    if len(queries) > _MAX_QUERIES:
        return _err("sql_explainer.too_many_queries", f"max {_MAX_QUERIES} queries per call")
    for i, q in enumerate(queries):
        if not isinstance(q, str) or not q.strip():
            return _err("sql_explainer.invalid_query", f"queries[{i}] must be a non-empty string")
        if len(q) > _MAX_QUERY_CHARS:
            return _err("sql_explainer.query_too_large", f"queries[{i}] exceeds {_MAX_QUERY_CHARS} chars")
        if _DML_RE.match(q):
            return _err(
                "sql_explainer.dml_not_supported",
                f"queries[{i}] is DML; this agent only EXPLAINs SELECT/WITH statements (run DML via db_sandbox).",
            )
        if not _SELECT_RE.match(q):
            return _err(
                "sql_explainer.non_select",
                f"queries[{i}] does not begin with SELECT or WITH",
            )

    params_input = payload.get("params") or []
    if not isinstance(params_input, list):
        return _err("sql_explainer.invalid_params", "params must be a list")
    if params_input and len(params_input) != len(queries):
        return _err(
            "sql_explainer.params_mismatch",
            f"params length ({len(params_input)}) must match queries length ({len(queries)}) when provided",
        )

    started = time.monotonic()
    deadline = started + _MAX_WALL_CLOCK_SECONDS
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = OFF")
    try:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass
        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1_000)
        try:
            conn.executescript(schema_sql)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                return _err("sql_explainer.timeout", f"schema_sql exceeded {_MAX_WALL_CLOCK_SECONDS:.0f}s execution limit")
            return _err(
                "sql_explainer.schema_failed",
                f"schema_sql failed to execute: {exc}",
            )
        except sqlite3.Error as exc:
            return _err(
                "sql_explainer.schema_failed",
                f"schema_sql failed to execute: {exc}",
            )

        # Lock the DB read-only for the EXPLAIN phase.
        conn.execute("PRAGMA query_only = ON")

        results: list[dict[str, Any]] = []
        total_issues = 0
        for i, query in enumerate(queries):
            params: Any = []
            if params_input:
                params = params_input[i]
                if not isinstance(params, (list, dict)):
                    return _err(
                        "sql_explainer.invalid_params_entry",
                        f"params[{i}] must be a list or dict",
                    )

            start = time.monotonic()
            try:
                cursor = conn.execute(f"EXPLAIN QUERY PLAN {query}", params)
                rows = cursor.fetchall()
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).lower():
                    results.append(
                        {
                            "sql": query,
                            "plan": [],
                            "issues": [f"EXPLAIN timed out after {_MAX_WALL_CLOCK_SECONDS:.0f}s wall-clock budget."],
                            "suggestions": [],
                            "elapsed_ms": round((time.monotonic() - start) * 1000.0, 2),
                            "error": str(exc),
                        }
                    )
                    continue
                results.append(
                    {
                        "sql": query,
                        "plan": [],
                        "issues": [f"EXPLAIN failed: {exc}"],
                        "suggestions": [],
                        "elapsed_ms": round((time.monotonic() - start) * 1000.0, 2),
                        "error": str(exc),
                    }
                )
                continue
            except sqlite3.Error as exc:
                results.append(
                    {
                        "sql": query,
                        "plan": [],
                        "issues": [f"EXPLAIN failed: {exc}"],
                        "suggestions": [],
                        "elapsed_ms": round((time.monotonic() - start) * 1000.0, 2),
                        "error": str(exc),
                    }
                )
                continue
            elapsed_ms = round((time.monotonic() - start) * 1000.0, 2)

            plan = [
                {"id": int(r[0]), "parent": int(r[1]), "detail": str(r[3])}
                for r in rows
            ]
            issues, suggestions = _analyze_plan(rows)
            total_issues += len(issues)
            results.append(
                {
                    "sql": query,
                    "plan": plan,
                    "issues": issues,
                    "suggestions": suggestions,
                    "elapsed_ms": elapsed_ms,
                }
            )
    finally:
        conn.close()

    if total_issues == 0:
        summary = f"All {len(queries)} query plan(s) look clean — no full scans or temp B-trees flagged."
    else:
        summary = f"Found {total_issues} potential plan issue(s) across {len(queries)} query/queries."

    return {
        "queries": results,
        "total_issues": total_issues,
        "summary": summary,
    }
