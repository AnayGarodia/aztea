# OWNS: sequential execution of the builtin-frontier matrix — one harness
#   invocation per (task, harness), built-in tools only, NO Aztea.
# NOT OWNS: scoring (scorer.py), measurement internals (capture.py).
# INVARIANTS: built-in only — OpenClaw runs against an MCP/plugin-free config,
#   Hermes against an mcp_servers/hooks-free HERMES_HOME. No local Aztea
#   server is started; no deference plugin is loaded. Runs are SEQUENTIAL and
#   append-only (resumable: cells already in runs.jsonl are skipped).
"""Run the builtin-frontier experiment. Usage:

    python experiments/builtin-frontier/runner.py [--only id] [--harness openclaw|hermes] [--dry-list]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture  # noqa: E402

_HERE = Path(__file__).resolve().parent
CORPUS_PATH = _HERE / "corpus.json"
RUNS_PATH = _HERE / "results" / "runs.jsonl"
REPO_ROOT = _HERE.parents[1]

# Built-in-only configs (no MCP, no plugin, no hooks) — reuse the sibling
# experiment's Aztea-free configs. Verified MCP-free in Phase 0.
OPENCLAW_BUILTIN_CONFIG = _HERE.parent / "deference" / "configs" / "openclaw.builtin.json"
HERMES_BUILTIN_HOME = Path.home() / ".hermes-aztea-builtin"
FIXTURES_DIR = _HERE / "fixtures"
RUN_TIMEOUT_S = 280
HARNESSES = ("openclaw", "hermes")


def _prompt_for(task: dict[str, Any]) -> str:
    """Resolve the {FIXTURES} placeholder to an absolute path so the harness
    file/exec tools find the document regardless of their working dir."""
    return task["prompt"].replace("{FIXTURES}", str(FIXTURES_DIR))


def _openclaw_cmd(task: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    env["OPENCLAW_CONFIG_PATH"] = str(OPENCLAW_BUILTIN_CONFIG)
    cmd = [
        "openclaw", "agent", "--local", "--json",
        "--timeout", str(RUN_TIMEOUT_S - 20),
        "--session-id", f"frontier-{task['id']}",
        "--message", _prompt_for(task),
    ]
    return cmd, env


def _hermes_cmd(task: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HERMES_BUILTIN_HOME)
    env["HERMES_ACCEPT_HOOKS"] = "1"
    return ["hermes", "-z", _prompt_for(task)], env


def run_cell(task: dict[str, Any], harness: str) -> dict[str, Any]:
    cmd, env = (_openclaw_cmd if harness == "openclaw" else _hermes_cmd)(task)
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            timeout=RUN_TIMEOUT_S, cwd=str(REPO_ROOT),
        )
        rc, stdout = proc.returncode, proc.stdout
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        rc, timed_out = -1, True
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    wall_s = round(time.time() - started, 1)

    if harness == "openclaw":
        answer = capture.openclaw_answer(stdout)
        usage = capture.openclaw_usage(stdout)
        tools = capture.openclaw_tools(stdout)
    else:
        answer = stdout.strip() or None
        usage = capture.hermes_usage(HERMES_BUILTIN_HOME, started)
        tools = capture.hermes_tools(HERMES_BUILTIN_HOME, started)

    return {
        "task_id": task["id"],
        "harness": harness,
        "ts": started,
        "wall_s": wall_s,
        "rc": rc,
        "timed_out": timed_out,
        "answer": answer,
        "tools": tools,
        "harness_usage": usage,
    }


def _existing_cells(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    cells = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            cells.add((row["task_id"], row["harness"]))
    return cells


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def main(argv: list[str]) -> int:
    tasks = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["tasks"]
    only, harness_filter = _flag(argv, "--only"), _flag(argv, "--harness")
    matrix = [
        (t, h) for t in tasks for h in HARNESSES
        if (not only or t["id"] == only) and (not harness_filter or h == harness_filter)
    ]
    done = _existing_cells(RUNS_PATH)
    pending = [(t, h) for (t, h) in matrix if (t["id"], h) not in done]
    if "--dry-list" in argv:
        for t, h in pending:
            print(f"{t['id']}/{h}")
        print(f"{len(pending)} pending ({len(done)} done)")
        return 0
    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    for i, (task, harness) in enumerate(pending, 1):
        print(f"[{i}/{len(pending)}] {task['id']}/{harness} ...", flush=True)
        row = run_cell(task, harness)
        with RUNS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        ans = (row["answer"] or "")[:60].replace("\n", " ")
        print(f"    -> rc={row['rc']} {row['wall_s']}s tools={row['tools']}  {ans!r}", flush=True)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
