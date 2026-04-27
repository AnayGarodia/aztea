"""
shell_executor.py — Run sandboxed shell commands with an allowlist

Input:
  {
    "command": "npm test",
    "working_dir": "/tmp",           # optional, defaults to /tmp
    "env": {"NODE_ENV": "test"},     # optional extra env vars
    "timeout": 15                    # 1–60, default 15
  }

Output:
  {
    "command": str,
    "exit_code": int,
    "stdout": str,
    "stderr": str,
    "timed_out": bool,
    "elapsed_seconds": float
  }
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time

_TIMEOUT_MIN = 1
_TIMEOUT_MAX = 60
_TIMEOUT_DEFAULT = 15
_OUTPUT_MAX = 20_000

_ALLOWLIST_PREFIXES = (
    "npm ", "npm\t", "npx ", "node ", "python ", "python3 ",
    "pip ", "pip3 ", "ruff ", "mypy ", "tsc ", "tsx ",
    "git log", "git diff", "git status", "git show",
    "make ", "cargo ", "go ", "pytest ", "uv ",
)

_BLOCKLIST_PATTERNS = (
    "rm ", "rm\t", "rmdir", "curl ", "wget ", "chmod ", "chown ",
    "sudo ", "su ", "bash ", "sh ", "zsh ", "eval ", "exec ",
    "dd ", "mkfs", "mount ", "umount", "iptables", "nc ", "ncat",
    ">", ">>", "|",
)


def _is_allowed(command: str) -> bool:
    stripped = command.strip()
    # Block patterns take priority
    for pat in _BLOCKLIST_PATTERNS:
        if pat in stripped:
            return False
    # Must start with an allowed prefix
    lower = stripped.lower()
    return any(lower.startswith(p) for p in _ALLOWLIST_PREFIXES)


def run(payload: dict) -> dict:
    command = str(payload.get("command") or "").strip()
    if not command:
        raise ValueError("'command' is required.")
    if len(command) > 1000:
        raise ValueError("command must be <= 1000 characters.")

    if not _is_allowed(command):
        allowed = [p.strip() for p in _ALLOWLIST_PREFIXES]
        raise ValueError(
            f"Command not permitted. Allowed prefixes: {', '.join(sorted(set(allowed)))}. "
            "Pipe operators, shell redirects, and destructive commands are blocked."
        )

    timeout = int(payload.get("timeout") or _TIMEOUT_DEFAULT)
    timeout = max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, timeout))

    working_dir = str(payload.get("working_dir") or "/tmp").strip()
    if not os.path.isdir(working_dir):
        working_dir = "/tmp"

    extra_env = payload.get("env") or {}
    if not isinstance(extra_env, dict):
        extra_env = {}
    env = {**os.environ, **{str(k): str(v) for k, v in extra_env.items()}}

    timed_out = False
    start = time.monotonic()
    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            env=env,
        )
        stdout = result.stdout[:_OUTPUT_MAX]
        stderr = result.stderr[:_OUTPUT_MAX]
        exit_code = result.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (e.stdout or b"").decode("utf-8", errors="replace")[:_OUTPUT_MAX] if isinstance(e.stdout, bytes) else (e.stdout or "")[:_OUTPUT_MAX]
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")[:_OUTPUT_MAX] if isinstance(e.stderr, bytes) else (e.stderr or "")[:_OUTPUT_MAX]
        exit_code = -1
    except FileNotFoundError:
        raise ValueError(f"Command not found: {shlex.split(command)[0]!r}. Make sure the tool is installed.")
    except Exception as exc:
        raise RuntimeError(f"Failed to execute command: {exc}")

    elapsed = round(time.monotonic() - start, 3)

    return {
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
    }
