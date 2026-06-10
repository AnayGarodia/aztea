# OWNS: per-run measurement capture — Aztea job rows in a rowid window,
#   deference-log rows in a line window, and harness LLM usage extraction.
# NOT OWNS: invoking the harnesses (runner.py) or scoring (scorer.py).
# INVARIANTS: capture never fabricates a number — anything unparseable is
#   recorded as None, explicitly. Reads the registry DB read-only (mode=ro);
#   this module must never write to it.
"""Measurement capture for the deference experiment."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DEFERENCE_LOG = Path.home() / ".aztea" / "deference.jsonl"


def jobs_max_rowid(db_path: str) -> int:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM jobs").fetchone()
    return int(row[0])


def jobs_in_window(db_path: str, after_rowid: int, client_id: str) -> list[dict[str, Any]]:
    """Jobs created after the snapshot, attributed to this harness. The rowid
    window makes the query race-free under sequential runs (this experiment
    never runs two harness invocations concurrently)."""
    query = (
        "SELECT job_id, agent_id, status, origin, price_cents, created_at "
        "FROM jobs WHERE rowid > ? AND client_id = ? ORDER BY rowid"
    )
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, (after_rowid, client_id)).fetchall()
    return [dict(r) for r in rows]


def deference_line_count(path: Path = DEFERENCE_LOG) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def deference_rows_after(count: int, client: str, path: Path = DEFERENCE_LOG) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[count:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("client") == client:
            rows.append(row)
    return rows


def openclaw_usage(stdout_text: str) -> dict[str, Any] | None:
    """Extract the usage/cost block from `openclaw agent --json` output.
    Returns None when absent or unparseable — never a fabricated zero."""
    try:
        payload = json.loads(stdout_text[stdout_text.find("{"):])
    except (ValueError, TypeError):
        return None
    found: dict[str, Any] | None = None

    def walk(node: Any) -> None:
        nonlocal found
        if found is not None or not isinstance(node, dict):
            return
        usage = node.get("usage")
        if isinstance(usage, dict) and ("input" in usage or "output" in usage):
            found = usage
            return
        for value in node.values():
            walk(value)

    walk(payload)
    return found


def openclaw_answer(stdout_text: str) -> str | None:
    """The final assistant text from `openclaw agent --json` output."""
    try:
        payload = json.loads(stdout_text[stdout_text.find("{"):])
    except (ValueError, TypeError):
        return None
    found: str | None = None

    def walk(node: Any) -> None:
        nonlocal found
        if found is not None or not isinstance(node, dict):
            return
        text = node.get("finalAssistantVisibleText")
        if isinstance(text, str) and text.strip():
            found = text
            return
        for value in node.values():
            walk(value)

    walk(payload)
    return found


def openclaw_tools(stdout_text: str) -> list[str] | None:
    try:
        payload = json.loads(stdout_text[stdout_text.find("{"):])
    except (ValueError, TypeError):
        return None
    found: list[str] | None = None

    def walk(node: Any) -> None:
        nonlocal found
        if found is not None or not isinstance(node, dict):
            return
        summary = node.get("toolSummary")
        if isinstance(summary, dict) and isinstance(summary.get("tools"), list):
            found = [str(t) for t in summary["tools"]]
            return
        for value in node.values():
            walk(value)

    walk(payload)
    return found
