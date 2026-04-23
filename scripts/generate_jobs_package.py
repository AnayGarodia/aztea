#!/usr/bin/env python3
"""Generate core/jobs/ package from monolithic core/jobs.py (run from repo root)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "core" / "jobs.py"
OUT = ROOT / "core" / "jobs"


def top_level_defs(src: str) -> set[str]:
    tree = ast.parse(src)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
    return names


def chunk_imports(chunk: str, db_names: set[str]) -> str:
    """Heuristic: names in chunk that look like identifiers and exist in db_names."""
    tree = ast.parse(chunk)
    local = top_level_defs(chunk)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in db_names and node.id not in local:
                used.add(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if isinstance(node.value, ast.Name) and node.value.id == "_models":
                used.add("_models")
    # Always need db connection stack
    used |= {"_conn", "_now", "_now_dt", "_parse_ts", "_iso_after_seconds", "_row_to_dict", "_msg_to_dict", "_decode_json"}
    used &= db_names | {"_models"}
    ordered = sorted(used)
    lines = [
        '"""Auto-generated jobs submodule — do not edit by hand; regenerate via scripts/generate_jobs_package.py."""',
        "from __future__ import annotations",
        "",
    ]
    if "_models" in used:
        lines.append("from core import models as _models")
        lines.append("")
    lines.append("from .db import (")
    for name in ordered:
        lines.append(f"    {name},")
    lines.append(")")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    db_lines = lines[:772] + lines[2255:2310]  # include _decode_json block through _msg_to_dict
    # Fix _conn in db: insert sys + _resolved after _local line
    db_text = "".join(db_lines)
    db_text = db_text.replace(
        "import uuid\n",
        "import sys\nimport uuid\n",
        1,
    )
    db_text = db_text.replace(
        "DB_PATH = _db.DB_PATH\n_local = _db._local\n\n",
        "DB_PATH = _db.DB_PATH\n_local = _db._local\n\n\n"
        "def _resolved_db_path() -> str:\n"
        '    """Prefer ``core.jobs.DB_PATH`` for isolated tests."""\n'
        '    pkg = sys.modules.get("core.jobs")\n'
        "    if pkg is not None:\n"
        '        c = getattr(pkg, "DB_PATH", None)\n'
        "        if isinstance(c, str) and c:\n"
        "            return c\n"
        "    return DB_PATH\n\n",
        1,
    )
    db_text = db_text.replace(
        "return _db.get_raw_connection(DB_PATH)",
        "return _db.get_raw_connection(_resolved_db_path())",
        1,
    )

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "db.py").write_text(db_text, encoding="utf-8")

    db_names = top_level_defs(db_text)
    # chunks: crud 773-1046, leases 1047-1908, messages 1909-2254 (0-based slice end exclusive for last uses line 2304)
    ranges = [
        ("crud.py", 773, 1046),
        ("leases.py", 1047, 1908),
        ("messaging.py", 1909, 2304),
    ]
    for fname, a, b in ranges:
        chunk = "".join(lines[a:b])
        header = chunk_imports(chunk, db_names)
        (OUT / fname).write_text(header + chunk, encoding="utf-8")

    init = '''"""Async jobs package (replaces monolithic core/jobs.py)."""
from __future__ import annotations

from . import crud
from . import db
from . import leases
from . import messaging

for _mod in (db, crud, leases, messaging):
    for _n in dir(_mod):
        if _n.startswith("__"):
            continue
        globals()[_n] = getattr(_mod, _n)
'''
    (OUT / "__init__.py").write_text(init, encoding="utf-8")
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
