# OWNS: sequential orchestration of the deference experiment — one harness
#   invocation per (task, mode, harness) cell, with snapshot/capture around
#   each run and append-only JSONL results.
# NOT OWNS: scoring (scorer.py), measurement extraction internals (capture.py).
# INVARIANTS: runs are SEQUENTIAL (the local server's worker pool and the
#   experiment's rowid-window capture both assume no concurrency). Results are
#   append-only; re-running skips cells already present so the experiment is
#   resumable. Mode toggling is by env var ONLY (OPENCLAW_CONFIG_PATH /
#   HERMES_HOME) — the runner never edits a live harness config.
"""Run the deference experiment. Usage:

    python experiments/deference/runner.py [--only task-id] [--harness openclaw|hermes]
        [--mode aztea|builtin] [--dry-list]

Prereqs (see REPORT.md methodology): local Aztea server on :8013, `aztea` CLI
on PATH, OpenClaw configs under experiments/deference/configs/, Hermes homes
~/.hermes-aztea-exp (aztea mode) and ~/.hermes-aztea-builtin (builtin mode).
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
import capture  # noqa: E402  (script-style sibling import; needs the path above)

_HERE = Path(__file__).resolve().parent
CORPUS_PATH = _HERE / "corpus.json"
RUNS_PATH = _HERE / "results" / "runs.jsonl"
CONFIGS_DIR = _HERE / "configs"

DB_PATH = str(Path(os.environ.get("AZTEA_EXP_DB", str(_HERE.parents[1] / "registry.db"))))
RUN_TIMEOUT_S = 280  # per harness invocation; e2e runs took 30-90s
HARNESSES = ("openclaw", "hermes")
MODES = ("aztea", "builtin")
HERMES_HOMES = {
    "aztea": Path.home() / ".hermes-aztea-exp",
    "builtin": Path.home() / ".hermes-aztea-builtin",
}


def _openclaw_cmd(task: dict[str, Any], mode: str) -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    env["OPENCLAW_CONFIG_PATH"] = str(CONFIGS_DIR / f"openclaw.{mode}.json")
    session = f"aztea-exp-{task['id']}-{mode}"
    cmd = [
        "openclaw", "agent", "--local", "--json",
        "--timeout", str(RUN_TIMEOUT_S - 20),
        "--session-id", session,
        "--message", task["prompt"],
    ]
    return cmd, env


def _hermes_cmd(task: dict[str, Any], mode: str) -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HERMES_HOMES[mode])
    env["HERMES_ACCEPT_HOOKS"] = "1"
    if mode == "aztea":
        env["AZTEA_DEFERENCE_MODE"] = "block-all"
    return ["hermes", "-z", task["prompt"]], env


def run_cell(task: dict[str, Any], harness: str, mode: str) -> dict[str, Any]:
    """Execute one experiment cell and return its results row."""
    cmd, env = (_openclaw_cmd if harness == "openclaw" else _hermes_cmd)(task, mode)
    rowid_before = capture.jobs_max_rowid(DB_PATH)
    log_before = capture.deference_line_count()
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT_S
        )
        rc, stdout = proc.returncode, proc.stdout
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        rc, stdout = -1, (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        timed_out = True
    wall_s = round(time.time() - started, 1)

    if harness == "openclaw":
        answer = capture.openclaw_answer(stdout)
        usage = capture.openclaw_usage(stdout)
        tools = capture.openclaw_tools(stdout)
    else:
        answer = stdout.strip() or None
        usage = None  # hermes -z exposes no machine-readable usage; never fabricate
        tools = None

    jobs = capture.jobs_in_window(DB_PATH, rowid_before, client_id=harness)
    deference_rows = capture.deference_rows_after(log_before, client=harness)
    return {
        "task_id": task["id"],
        "harness": harness,
        "mode": mode,
        "ts": started,
        "wall_s": wall_s,
        "rc": rc,
        "infeasible": timed_out or rc != 0 or not (answer or "").strip(),
        "answer": answer,
        "harness_usage": usage,
        "tools": tools,
        "aztea_jobs": jobs,
        "deference": {
            "blocked": sum(1 for r in deference_rows if r.get("action") == "block"),
            "rows": deference_rows,
        },
    }


def _existing_cells(path: Path) -> set[tuple[str, str, str]]:
    if not path.exists():
        return set()
    cells = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cells.add((row["task_id"], row["harness"], row["mode"]))
    return cells


def _build_matrix(tasks: list[dict[str, Any]], args: list[str]) -> list[tuple[dict[str, Any], str, str]]:
    only = _flag_value(args, "--only")
    harness_filter = _flag_value(args, "--harness")
    mode_filter = _flag_value(args, "--mode")
    matrix = []
    for task in tasks:
        if only and task["id"] != only:
            continue
        for harness in HARNESSES:
            if harness_filter and harness != harness_filter:
                continue
            for mode in MODES:
                if mode_filter and mode != mode_filter:
                    continue
                matrix.append((task, harness, mode))
    return matrix


def _flag_value(args: list[str], flag: str) -> str | None:
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            return args[idx + 1]
    return None


def main(argv: list[str]) -> int:
    tasks = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["tasks"]
    matrix = _build_matrix(tasks, argv)
    done = _existing_cells(RUNS_PATH)
    pending = [(t, h, m) for (t, h, m) in matrix if (t["id"], h, m) not in done]
    if "--dry-list" in argv:
        for task, harness, mode in pending:
            print(f"{task['id']}/{harness}/{mode}")
        print(f"{len(pending)} pending ({len(done)} done)")
        return 0
    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    for i, (task, harness, mode) in enumerate(pending, 1):
        print(f"[{i}/{len(pending)}] {task['id']}/{harness}/{mode} ...", flush=True)
        row = run_cell(task, harness, mode)
        with RUNS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        status = "infeasible" if row["infeasible"] else f"ok in {row['wall_s']}s"
        jobs = len(row["aztea_jobs"])
        print(f"    -> {status}, {jobs} aztea job(s), {row['deference']['blocked']} blocked", flush=True)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
