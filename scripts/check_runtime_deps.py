#!/usr/bin/env python3
"""Verify every Python import in agents/ is satisfied by requirements.txt.

Scans `agents/*.py` and `agents/*/` packages, extracts top-level third-party
imports, and asserts each maps to a distribution declared in `requirements.txt`
(stdlib + first-party namespaces are excluded). Fail-fast — exit 1 on the
first missing dep, with file:line + offending import.

Run from repo root: python3 scripts/check_runtime_deps.py

Wired into Makefile (`make check-runtime-deps`) and CI.
"""

from __future__ import annotations

import ast
import re
import sys
import sysconfig
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"
REQUIREMENTS_TXT = REPO_ROOT / "requirements.txt"

# First-party top-level packages — never expected in requirements.txt.
_FIRST_PARTY = frozenset({
    "agents", "core", "server", "scripts", "tests", "benchmarks",
    "sdks", "elixir",
})

# Map import name → distribution name when they differ. PyPI distribution
# names rarely match the importable module name; we keep this list short
# and explicit rather than parsing PEP 621 metadata.
_IMPORT_TO_DIST = {
    "bs4": "beautifulsoup4",
    "yaml": "pyyaml",
    "fitz": "pymupdf",
    "PIL": "Pillow",
    "playwright": "playwright",
    "dotenv": "python-dotenv",
    "dns": "dnspython",
    "psycopg2": "psycopg2-binary",
    "sentence_transformers": "sentence-transformers",
    "jose": "python-jose",
    "jwt": "PyJWT",
    "magic": "python-magic",
    "git": "GitPython",
}


def _stdlib_modules() -> frozenset[str]:
    names = set(sys.stdlib_module_names)
    # `distutils` and a few legacy names — keep wide.
    names.update({"typing_extensions"})  # PEP 695 fallback
    return frozenset(names)


_STDLIB = _stdlib_modules()


def _parse_requirements() -> set[str]:
    """Return the lowercase set of distribution names in requirements.txt."""
    if not REQUIREMENTS_TXT.exists():
        return set()
    dists: set[str] = set()
    for raw in REQUIREMENTS_TXT.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # Strip extras and version specifiers: "uvicorn[standard]>=0.20" → "uvicorn"
        name = re.split(r"[<>=!~\[ ;]", line, maxsplit=1)[0].strip()
        if name:
            dists.add(name.lower())
    return dists


def _top_level_imports(py_path: Path) -> set[tuple[str, int]]:
    """Return (module_root, line) for every HARD import in `py_path`.

    "Hard" = imported at module load. Skipped:
      - imports wrapped in `try: import X / except ImportError` (gated optional)
      - imports inside function/method bodies (lazy — only fail if that code
        path runs; agents that legitimately defer heavy deps use this)
    Both patterns are the project's normal way to declare an optional dep.
    """
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    optional_lines: set[int] = set()

    # Mark imports inside try-blocks that catch ImportError as optional.
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            catches_importerror = any(
                (
                    h.type is None
                    or (isinstance(h.type, ast.Name) and h.type.id in {"ImportError", "ModuleNotFoundError", "Exception"})
                    or (
                        isinstance(h.type, ast.Tuple)
                        and any(
                            isinstance(elt, ast.Name)
                            and elt.id in {"ImportError", "ModuleNotFoundError", "Exception"}
                            for elt in h.type.elts
                        )
                    )
                )
                for h in node.handlers
            )
            if not catches_importerror:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    optional_lines.add(inner.lineno)

    # Mark imports nested under any FunctionDef/AsyncFunctionDef as lazy.
    for parent in ast.walk(tree):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(parent):
                if isinstance(inner, (ast.Import, ast.ImportFrom)) and inner is not parent:
                    optional_lines.add(inner.lineno)

    out: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if node.lineno in optional_lines:
                continue
            for alias in node.names:
                out.add((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.lineno in optional_lines:
                continue
            if node.level and node.level > 0:
                continue
            if node.module:
                out.add((node.module.split(".")[0], node.lineno))
    return out


def _agent_modules() -> list[Path]:
    out: list[Path] = []
    for p in sorted(AGENTS_DIR.glob("*.py")):
        if p.name.startswith("_"):
            continue
        out.append(p)
    for pkg in sorted(AGENTS_DIR.glob("*/")):
        if pkg.is_dir() and (pkg / "__init__.py").exists():
            for p in sorted(pkg.rglob("*.py")):
                out.append(p)
    return out


def main() -> int:
    declared = _parse_requirements()
    if not declared:
        print(f"ERROR: requirements.txt missing or empty at {REQUIREMENTS_TXT}", file=sys.stderr)
        return 2

    missing: list[tuple[Path, int, str, str]] = []
    for module_path in _agent_modules():
        for module_root, lineno in _top_level_imports(module_path):
            if module_root in _STDLIB:
                continue
            if module_root in _FIRST_PARTY:
                continue
            dist_name = _IMPORT_TO_DIST.get(module_root, module_root).lower()
            if dist_name in declared:
                continue
            # Allow underscore↔hyphen variants
            if dist_name.replace("_", "-") in declared:
                continue
            if dist_name.replace("-", "_") in declared:
                continue
            missing.append((module_path, lineno, module_root, dist_name))

    if not missing:
        print(f"ok — {len(_agent_modules())} agent modules; all third-party imports covered by requirements.txt")
        return 0

    print("FAIL: agent imports not satisfied by requirements.txt:\n", file=sys.stderr)
    rel = REPO_ROOT
    for path, line, mod, dist in missing:
        print(f"  {path.relative_to(rel)}:{line}: imports {mod!r} (expected dist {dist!r})", file=sys.stderr)
    print(
        f"\n{len(missing)} missing. Add the corresponding distribution(s) to requirements.txt "
        "or extend scripts/check_runtime_deps.py::_IMPORT_TO_DIST if the name mapping differs.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
