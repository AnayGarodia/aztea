"""Migration-hygiene guards (Phase -1 of the agent-readable-web build).

Prevents the drift the 2026-06-01 plan review caught: header comments whose
4-digit number disagreed with the filename (off by ~5 across 0072-0076), which
makes per-phase migration numbering unreliable across parallel branches. Also
guards filename format, version uniqueness, and sequence contiguity (the 0043
gap is now filled by a documented tombstone).
"""

from __future__ import annotations

import re
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")
# A header line like ``-- 0076_agent_domain_verification.sql`` — the number
# here MUST match the filename. Date-first headers (``-- 2026-…``) don't match
# (the digits aren't followed by ``_``) and are simply not checked.
_HEADER_NUM_RE = re.compile(r"^--\s*(\d{4})_")
_HEADER_SCAN_LINES = 5


def _migration_files() -> list[Path]:
    return sorted(p for p in _MIGRATIONS_DIR.glob("*.sql") if _FILENAME_RE.match(p.name))


def test_every_migration_filename_is_well_formed():
    bad = [p.name for p in _MIGRATIONS_DIR.glob("*.sql") if not _FILENAME_RE.match(p.name)]
    assert not bad, f"migration filenames must match NNNN_*.sql: {bad}"


def test_version_numbers_are_unique():
    versions = [int(_FILENAME_RE.match(p.name).group(1)) for p in _migration_files()]
    dupes = sorted({v for v in versions if versions.count(v) > 1})
    assert not dupes, f"duplicate migration version numbers: {dupes}"


def test_header_number_matches_filename():
    """The first ``-- NNNN_`` header line, if present, must equal the filename number."""
    mismatches: list[str] = []
    for path in _migration_files():
        file_num = _FILENAME_RE.match(path.name).group(1)
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = [next(handle, "") for _ in range(_HEADER_SCAN_LINES)]
        for line in head:
            m = _HEADER_NUM_RE.match(line.strip())
            if m:
                if m.group(1) != file_num:
                    mismatches.append(f"{path.name}: header says {m.group(1)}")
                break  # only the first numbered header line is authoritative
    assert not mismatches, "migration header/filename number drift: " + "; ".join(mismatches)


def test_sequence_is_contiguous():
    """No gaps from the first to the last version (intentional skips get a tombstone)."""
    versions = sorted(int(_FILENAME_RE.match(p.name).group(1)) for p in _migration_files())
    expected = list(range(versions[0], versions[-1] + 1))
    missing = sorted(set(expected) - set(versions))
    assert not missing, f"gaps in migration sequence (add a tombstone): {missing}"
