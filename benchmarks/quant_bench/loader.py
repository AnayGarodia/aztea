"""Filesystem loader for quant-bench entries.

# OWNS: walking `entries/`, validating the directory shape, and yielding
#        normalised `Entry` records.
# NOT OWNS: scoring (see score.py) or the agent itself.
# INVARIANTS:
#   - load_entry() raises ValueError on any directory that doesn't match
#     the documented layout. Silent skips would mask coverage gaps.
# DECISIONS:
#   - We read the candidate file contents up front. The bench is small
#     (~30 entries × ~3 candidates × ~1 kB) so eager I/O is fine and
#     gives a single point of failure if the corpus is malformed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_ALLOWED_LABELS = frozenset({"correct", "regression", "broken"})

# Directory containing one subdirectory per entry. The bench root is the
# parent of *this* file.
_BENCH_ROOT = Path(__file__).resolve().parent
_ENTRIES_DIR = _BENCH_ROOT / "entries"


@dataclass(frozen=True)
class Candidate:
    """One AI-generated patch plus its ground-truth label."""

    filename: str
    source: str
    label: str  # one of _ALLOWED_LABELS


@dataclass(frozen=True)
class Entry:
    """A single benchmark entry: reference + several candidates + metadata."""

    slug: str
    category: str
    reference_source: str
    gold_source: str
    candidates: tuple[Candidate, ...]
    notes: str

    @property
    def n_correct(self) -> int:
        return sum(1 for c in self.candidates if c.label == "correct")

    @property
    def n_regression(self) -> int:
        return sum(1 for c in self.candidates if c.label == "regression")

    @property
    def n_broken(self) -> int:
        return sum(1 for c in self.candidates if c.label == "broken")


def _read(path: Path) -> str:
    if not path.exists():
        raise ValueError(f"required file missing: {path}")
    return path.read_text(encoding="utf-8")


def load_entry(slug: str) -> Entry:
    """Load one entry by directory slug; raise ValueError if malformed."""
    entry_dir = _ENTRIES_DIR / slug
    if not entry_dir.is_dir():
        raise ValueError(f"no such entry directory: {entry_dir}")
    reference = _read(entry_dir / "pre.py")
    gold = _read(entry_dir / "gold.py")
    labels_doc = json.loads(_read(entry_dir / "labels.json"))
    candidate_dir = entry_dir / "candidates"
    if not candidate_dir.is_dir():
        raise ValueError(f"candidates/ missing in {entry_dir}")

    label_map: dict[str, str] = labels_doc.get("candidates") or {}
    # Validate label vocabulary
    for fname, label in label_map.items():
        if label not in _ALLOWED_LABELS:
            raise ValueError(f"{slug}: unknown label {label!r} for {fname}")
    # Validate label map is total over present files
    present = {p.name for p in candidate_dir.glob("*.py") if not p.name.startswith("__")}
    if set(label_map.keys()) != present:
        missing = present - label_map.keys()
        extra = label_map.keys() - present
        raise ValueError(
            f"{slug}: labels.json mismatch with candidate files. "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )

    candidates = tuple(
        Candidate(filename=fname, source=_read(candidate_dir / fname), label=label_map[fname])
        for fname in sorted(label_map.keys())
    )
    return Entry(
        slug=slug,
        category=str(labels_doc.get("category") or "uncategorised"),
        reference_source=reference,
        gold_source=gold,
        candidates=candidates,
        notes=str(labels_doc.get("notes") or ""),
    )


def iter_entries() -> Iterator[Entry]:
    """Yield every entry under entries/, in directory-name sorted order."""
    if not _ENTRIES_DIR.is_dir():
        return
    for entry_dir in sorted(_ENTRIES_DIR.iterdir()):
        if entry_dir.is_dir() and not entry_dir.name.startswith("_"):
            yield load_entry(entry_dir.name)
