#!/usr/bin/env python3
"""Fail if any source file exceeds a line budget (default 1000).

Excludes vendor, virtualenv, build output, and generated SDK artifacts by default.
Run from repo root: python3 scripts/check_file_line_budget.py
"""

from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path


DEFAULT_EXCLUDES = [
    ".git/*",
    ".venv/*",
    "node_modules/*",
    "**/node_modules/*",
    "frontend/.venv/*",
    "frontend/dist/*",
    "frontend/node_modules/*",
    "sdks/typescript/node_modules/*",
    "sdks/typescript/src/generated/*",
    "sdks/typescript/dist/*",
    "package-lock.json",
    ".claude/plugins/cache/*",
]


def _matches_any(path_str: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path_str, pat) for pat in patterns)


def _is_vendor_path(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    return "node_modules" in parts or ".venv" in parts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-lines", type=int, default=1000)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument(
        "--extra-exclude",
        action="append",
        default=[],
        help="Glob relative to root (may be repeated).",
    )
    args = parser.parse_args()
    root: Path = args.root
    max_lines: int = args.max_lines
    patterns = [*(p.replace("\\", "/") for p in DEFAULT_EXCLUDES), *args.extra_exclude]

    exts = {".py", ".ts", ".tsx", ".sql", ".md"}
    violations: list[tuple[int, str]] = []

    for dirpath, _dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        for name in filenames:
            if Path(name).suffix.lower() not in exts:
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            rel = rel.replace("\\", "/")
            if _is_vendor_path(rel) or _matches_any(rel, patterns):
                continue
            full = root / rel
            try:
                with open(full, encoding="utf-8", errors="replace") as handle:
                    n = sum(1 for _ in handle)
            except OSError:
                continue
            if n > max_lines:
                violations.append((n, rel))

    violations.sort(key=lambda x: (-x[0], x[1]))
    if not violations:
        print(f"OK: no files over {max_lines} lines (after excludes).")
        return 0

    print(f"FAIL: {len(violations)} file(s) exceed {max_lines} lines:\n")
    for n, rel in violations[:80]:
        print(f"  {n:5d}  {rel}")
    if len(violations) > 80:
        print(f"  ... and {len(violations) - 80} more")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
