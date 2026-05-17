#!/usr/bin/env python3
"""
backfill_observability.py — one-shot fill of jobs.origin for rows created
before migration 0049.

Inference rules (highest signal first):

  1. compare  — job_id appears in compare_sessions.job_ids_json
  2. watcher  — job_id == watcher_runs.fired_job_id
  3. pipeline — job_id is reachable from pipeline_runs.step_results.
                Recipes go through the same executor, so rows attributed to
                'pipeline' here MAY actually be recipe runs. We cannot
                distinguish reliably without a `recipe_id` column on
                pipeline_runs, so the backfill labels both as 'pipeline'
                and the report makes the ambiguity explicit. Going-forward
                writes use the contextvar to set 'recipe' precisely.
  4. direct   — fallback for any row still NULL after the above passes.

Auto-hire backfill is intentionally NOT attempted. The pre-migration
delegate path leaves no breadcrumb, so we'd be guessing — guessing here
would poison the very signal the new column is meant to provide.

Usage:
    python scripts/backfill_observability.py            # apply, full report
    python scripts/backfill_observability.py --dry-run  # report only

Idempotent: running twice on the same DB is a no-op (only rows with NULL
origin are touched).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from typing import Iterable

from core import db as _db

logger = logging.getLogger("backfill_observability")


def _job_ids_in_compare_sessions(conn) -> set[str]:
    rows = conn.execute(
        "SELECT job_ids_json FROM compare_sessions WHERE job_ids_json IS NOT NULL"
    ).fetchall()
    out: set[str] = set()
    for r in rows:
        try:
            ids = json.loads(r["job_ids_json"] if isinstance(r, dict) else r[0])
        except (TypeError, ValueError):
            continue
        for jid in ids or []:
            if isinstance(jid, str) and jid:
                out.add(jid)
    return out


def _job_ids_in_watcher_runs(conn) -> set[str]:
    rows = conn.execute(
        "SELECT fired_job_id FROM watcher_runs WHERE fired_job_id IS NOT NULL"
    ).fetchall()
    out: set[str] = set()
    for r in rows:
        val = r["fired_job_id"] if isinstance(r, dict) else r[0]
        if isinstance(val, str) and val:
            out.add(val)
    return out


def _extract_job_ids_from_step_results(blob: str) -> Iterable[str]:
    """Yield every job_id embedded in a pipeline_runs.step_results JSON blob.

    The shape is ``{node_id: {..., "job_id": "...", ...}}`` for each step
    that hired an agent. Some node outputs may not carry a job_id (pure
    transforms); we silently skip those.
    """
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return
    if not isinstance(parsed, dict):
        return
    for value in parsed.values():
        if isinstance(value, dict):
            jid = value.get("job_id")
            if isinstance(jid, str) and jid:
                yield jid


def _job_ids_in_pipeline_runs(conn) -> set[str]:
    rows = conn.execute(
        "SELECT step_results FROM pipeline_runs WHERE step_results IS NOT NULL"
    ).fetchall()
    out: set[str] = set()
    for r in rows:
        blob = r["step_results"] if isinstance(r, dict) else r[0]
        for jid in _extract_job_ids_from_step_results(blob):
            out.add(jid)
    return out


def _all_jobs_missing_origin(conn) -> list[str]:
    rows = conn.execute(
        "SELECT job_id FROM jobs WHERE origin IS NULL"
    ).fetchall()
    return [r["job_id"] if isinstance(r, dict) else r[0] for r in rows]


def _classify(
    job_ids: list[str],
    compare_ids: set[str],
    watcher_ids: set[str],
    pipeline_ids: set[str],
) -> dict[str, str]:
    """Pure: produce {job_id: origin} for every NULL row, using the inference order."""
    out: dict[str, str] = {}
    for jid in job_ids:
        if jid in compare_ids:
            out[jid] = "compare"
        elif jid in watcher_ids:
            out[jid] = "watcher"
        elif jid in pipeline_ids:
            out[jid] = "pipeline"
        else:
            out[jid] = "direct"
    return out


def _apply(conn, assignments: dict[str, str]) -> None:
    """Side-effect: UPDATE jobs.origin in batches of 500."""
    items = list(assignments.items())
    batch = 500
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        for jid, origin in chunk:
            conn.execute(
                "UPDATE jobs SET origin = %s WHERE job_id = %s AND origin IS NULL",
                (origin, jid),
            )
        conn.commit()


def run(*, dry_run: bool) -> dict[str, int]:
    conn = _db.get_raw_connection(_db.DB_PATH)
    compare_ids  = _job_ids_in_compare_sessions(conn)
    watcher_ids  = _job_ids_in_watcher_runs(conn)
    pipeline_ids = _job_ids_in_pipeline_runs(conn)
    missing = _all_jobs_missing_origin(conn)
    assignments = _classify(missing, compare_ids, watcher_ids, pipeline_ids)
    counts = Counter(assignments.values())

    print("--- Backfill report ---")
    print(f"  rows with NULL origin     : {len(missing)}")
    print(f"  compare-session job_ids   : {len(compare_ids)} (known signal)")
    print(f"  watcher-run job_ids       : {len(watcher_ids)} (known signal)")
    print(f"  pipeline-step job_ids     : {len(pipeline_ids)} (known signal, recipes inferred as 'pipeline')")
    print("  classification:")
    for origin in ("compare", "watcher", "pipeline", "direct"):
        print(f"    {origin:9s}: {counts.get(origin, 0)}")
    if dry_run:
        print("\nDRY RUN — no rows updated.")
    else:
        _apply(conn, assignments)
        print(f"\nUpdated {len(assignments)} rows.")
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not UPDATE.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
