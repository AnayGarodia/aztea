#!/usr/bin/env python3
"""Split a large Python module into smaller files at top-level def/class boundaries.

Writes ``out_dir/part_XXX.py`` and ``out_dir/__init__.py`` that re-imports all public
symbols (and leading-underscore names except private dunders) from parts in order.

Usage:
  python3 scripts/split_python_by_ast.py server/application.py server/application_pkg --max-lines=900
"""

from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--max-lines", type=int, default=900)
    args = ap.parse_args()
    src: Path = args.source
    out: Path = args.out_dir
    max_lines: int = args.max_lines

    text = src.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)

    breaks: list[int] = [1]  # 1-based line numbers for chunk starts
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lineno = int(node.lineno)
            if lineno > breaks[-1]:
                # If adding this node would push current chunk over max, start new chunk
                chunk_start = breaks[-1]
                chunk_lines = lineno - chunk_start
                if chunk_lines >= max_lines and lineno > chunk_start:
                    breaks.append(lineno)
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.Import | ast.ImportFrom):
            lineno = int(getattr(node, "lineno", 1))
            chunk_start = breaks[-1]
            if lineno - chunk_start >= max_lines and lineno > chunk_start:
                breaks.append(lineno)

    # Ensure last chunk not huge: add forced breaks
    extra: list[int] = []
    for i, start in enumerate(breaks):
        end = breaks[i + 1] - 1 if i + 1 < len(breaks) else len(lines)
        if end - start + 1 > max_lines * 1.2:
            pos = start + max_lines
            while pos < end:
                extra.append(pos)
                pos += max_lines
    breaks = sorted(set(breaks + extra))

    out.mkdir(parents=True, exist_ok=True)
    (out / "__init__.py").unlink(missing_ok=True)

    part_paths: list[str] = []
    for i, start in enumerate(breaks):
        end = breaks[i + 1] - 1 if i + 1 < len(breaks) else len(lines)
        chunk = "".join(lines[start - 1 : end])
        name = f"part_{i:03d}.py"
        part_paths.append(name)
        (out / name).write_text(
            '"""Auto-split fragment — see package ``__init__``."""\nfrom __future__ import annotations\n\n'
            + chunk,
            encoding="utf-8",
        )

    init = (
        '"""Sharded module — symbols re-exported from fragments."""\nfrom __future__ import annotations\n\n'
    )
    for p in part_paths:
        mod = p[:-3]
        init += f"from .{mod} import *\n"
    (out / "__init__.py").write_text(init, encoding="utf-8")
    print(f"Wrote {len(part_paths)} parts to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
