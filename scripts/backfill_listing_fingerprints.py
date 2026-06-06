#!/usr/bin/env python3
"""
backfill_listing_fingerprints.py — one-shot fill of listing_fingerprints for
hosted skills published before migration 0077.

Why: the exact-copy duplicate hard-block (core/listing_dedup.find_verbatim_copy)
INNER-JOINs listing_fingerprints, which is only written in the post-publish
advisory pass. Every agent registered before 0077 therefore has no fingerprint
row and is invisible as a duplicate *source* — a copier could byte-clone a
pre-0077 skill and the block would not fire. This script computes the
fingerprint from each hosted skill's stored SKILL.md body and records it.

Scope: hosted skills only. Author-hosted external agents (kind self_hosted /
onboarding) store no body server-side, so there is nothing to fingerprint —
their duplicate protection is the advisory cosine pass alone, by design.

Usage:
    python scripts/backfill_listing_fingerprints.py            # apply
    python scripts/backfill_listing_fingerprints.py --dry-run  # report only

Idempotent: only agents without an existing fingerprint row are touched.
"""
from __future__ import annotations

import argparse
import sys

from core import db as _db
from core import listing_dedup
from core.registry.core_schema import _resolved_db_path


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(_resolved_db_path())


def backfill(*, dry_run: bool) -> tuple[int, int]:
    """Returns (candidates, written). ``written`` is 0 in dry-run mode."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT h.agent_id, h.raw_md FROM hosted_skills h "
            "LEFT JOIN listing_fingerprints f ON f.agent_id = h.agent_id "
            "WHERE f.agent_id IS NULL"
        ).fetchall()

    candidates = len(rows)
    written = 0
    for row in rows:
        agent_id = str(row["agent_id"])
        raw_md = row["raw_md"] or ""
        if not raw_md.strip():
            continue
        if dry_run:
            fp = listing_dedup.content_fingerprint(raw_md, "skill_md")
            print(f"  would fingerprint {agent_id} -> {fp[:16]}…")
            continue
        listing_dedup.record_fingerprint(agent_id, raw_md, "skill_md")
        written += 1
    return candidates, written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, no writes")
    args = parser.parse_args()

    candidates, written = backfill(dry_run=args.dry_run)
    if args.dry_run:
        print(f"\n{candidates} hosted skill(s) missing a fingerprint (dry-run, no writes).")
    else:
        print(f"\nBackfilled {written}/{candidates} hosted-skill fingerprint(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
