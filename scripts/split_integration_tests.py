#!/usr/bin/env python3
"""Split tests/test_server_api_integration.py at test function boundaries (max ~800 lines each)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tests" / "test_server_api_integration.py"
OUT_DIR = ROOT / "tests" / "integration"
MAX_LINES = 800


def main() -> None:
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    test_starts: list[int] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            test_starts.append(node.lineno)

    # First test starts after conftest block — keep lines 1..(first_test-1) as conftest
    first = min(test_starts)
    header_end = first - 1
    header = "".join(lines[:header_end])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "conftest.py").write_text(header, encoding="utf-8")

    # Build chunks of tests by line count
    test_starts.append(len(lines) + 1)
    chunks: list[tuple[int, int, int]] = []  # (start_line, end_line inclusive, file_idx)
    i = 0
    file_idx = 0
    while i < len(test_starts) - 1:
        start_ln = test_starts[i]
        j = i + 1
        while j < len(test_starts) - 1:
            end_ln = test_starts[j + 1] - 1
            if end_ln - start_ln + 1 > MAX_LINES:
                break
            j += 1
        end_ln = test_starts[j] - 1
        chunks.append((start_ln, end_ln, file_idx))
        file_idx += 1
        i = j

    names = [
        "test_workers_jobs_core",
        "test_onboarding_registry",
        "test_wallets_stripe_auth",
        "test_hooks_builtin_mcp",
        "test_agents_orchestration",
        "test_disputes_admin_public",
    ]
    for idx, (a, b, fi) in enumerate(chunks):
        name = names[fi] if fi < len(names) else f"test_integration_extra_{fi}"
        body = "".join(lines[a - 1 : b])
        (OUT_DIR / f"{name}.py").write_text(
            f'"""Server integration tests (auto-split fragment {idx + 1}/{len(chunks)})."""\n\n' + body,
            encoding="utf-8",
        )
    print(f"Wrote {len(chunks)} test modules + conftest to {OUT_DIR}")


if __name__ == "__main__":
    main()
