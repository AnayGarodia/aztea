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
import re as _re
import shlex
import subprocess
import time

from core.executor_sandbox import build_subprocess_env

_TIMEOUT_MIN = 1
_TIMEOUT_MAX = 60
_TIMEOUT_DEFAULT = 15
_OUTPUT_MAX = 20_000

_ALLOWLIST_PREFIXES = (
    "npm ",
    "npm\t",
    "npx ",
    "node ",
    "python ",
    "python3 ",
    "pip ",
    "pip3 ",
    "ruff ",
    "mypy ",
    "tsc ",
    "tsx ",
    "git log",
    "git diff",
    "git status",
    "git show",
    "make ",
    "cargo ",
    "go ",
    "pytest ",
    "uv ",
)

_BLOCKLIST_PATTERNS = (
    "rm ",
    "rm\t",
    "rmdir",
    "curl ",
    "wget ",
    "chmod ",
    "chown ",
    "sudo ",
    "su ",
    "bash ",
    "sh ",
    "zsh ",
    "eval ",
    "exec ",
    "dd ",
    "mkfs",
    "mount ",
    "umount",
    "iptables",
    "nc ",
    "ncat",
    # Command substitution and process substitution.
    "$(",
    "`",
    "<(",
    ">(",
)

# Patterns for inline code passed via `python3 -c` — blocks network + subprocess access.
_PYTHON_INLINE_BLOCKED = (
    r"\bsubprocess\b",
    r"import\s+socket",
    r"import\s+requests",
    r"import\s+urllib",
    r"import\s+http\.client",
    r"\bos\.sy" + r"stem\b",
    r"from\s+(socket|requests|urllib|http)\s+import",
    r"from\s+subprocess\s+import",
)


def _extract_python_inline(command: str) -> "str | None":
    """Return the inline code from `python3 -c '...'` / `python -c "..."`, else None."""
    m = _re.match(r"""(?:python3?)\s+-c\s+(['"])(.*?)\1""", command.strip(), _re.DOTALL)
    if m:
        return m.group(2)
    m2 = _re.match(r"""(?:python3?)\s+-c\s+(.+)""", command.strip())
    if m2:
        return m2.group(1)
    return None


def _has_unquoted_shell_operator(command: str) -> bool:
    """Return True when the command uses shell syntax outside quoted args."""
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char in {";", "|", ">", "&"}:
            return True
    return False


def _is_allowed(command: str) -> bool:
    stripped = command.strip()
    # Block patterns take priority
    if _has_unquoted_shell_operator(stripped):
        return False
    for pat in _BLOCKLIST_PATTERNS:
        if pat in stripped:
            return False
    # Must start with an allowed prefix
    lower = stripped.lower()
    # Reject python3/python commands with multiple -c flags — can hide blocked code in later flags.
    if lower.startswith(("python3 ", "python ")):
        if len(_re.findall(r"\s-c[\s]", lower)) > 1:
            return False
    # For python3 -c INLINE, apply static analysis to the inline code.
    inline = _extract_python_inline(stripped)
    if inline is not None:
        for pattern in _PYTHON_INLINE_BLOCKED:
            if _re.search(pattern, inline):
                return False
    return any(lower.startswith(p) for p in _ALLOWLIST_PREFIXES)


def run(payload: dict) -> dict:
    """Execute a bounded shell command and return stdout/stderr/exit code.

    Required: ``command`` (str, ≤1000 chars).
    Optional: ``timeout_seconds`` (float, default 10.0, max 60.0),
              ``stdin`` (str), ``env`` (dict[str, str]).

    The command must begin with an allowlisted binary (see ``_ALLOWED_PREFIXES``
    in the module). Commands not on the allowlist are rejected immediately with
    a structured error — no subprocess is spawned.

    Returns ``{stdout, stderr, exit_code, execution_time_ms, timed_out}``.
    """
    command = str(payload.get("command") or "").strip()
    if not command:
        raise ValueError("'command' is required.")
    if len(command) > 1000:
        raise ValueError("command must be <= 1000 characters.")

    if not _is_allowed(command):
        allowed = [p.strip() for p in _ALLOWLIST_PREFIXES]
        # Identify the *specific* reason for the rejection so the caller
        # doesn't have to guess. Pipe / redirect / chained-command rejection
        # is qualitatively different from "binary not allowlisted" and the
        # caller can act on the distinction (e.g. split into two calls).
        chain_chars = ("&&", "||", "|", ";", "`", "$(", ">", "<")
        chain_match = next((ch for ch in chain_chars if ch in command), None)
        first_token = command.split()[0] if command.split() else "(empty)"
        if chain_match:
            why = (
                f"Command not permitted — shell metacharacter {chain_match!r} is not allowed. "
                "Each call must run a single command. Split into multiple aztea_call invocations."
            )
        else:
            why = (
                f"Command not permitted — binary {first_token!r} is not in the allowlist. "
                f"Allowed prefixes: {', '.join(sorted(set(allowed)))}."
            )
        raise ValueError(why)

    timeout = int(payload.get("timeout") or _TIMEOUT_DEFAULT)
    timeout = max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, timeout))

    working_dir = str(payload.get("working_dir") or "/tmp").strip()
    if not os.path.isdir(working_dir):
        working_dir = "/tmp"

    extra_env = payload.get("env") or {}
    if not isinstance(extra_env, dict):
        extra_env = {}
    env = build_subprocess_env({str(k): str(v) for k, v in extra_env.items()})

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
        stdout = (
            (e.stdout or b"").decode("utf-8", errors="replace")[:_OUTPUT_MAX]
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")[:_OUTPUT_MAX]
        )
        stderr = (
            (e.stderr or b"").decode("utf-8", errors="replace")[:_OUTPUT_MAX]
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")[:_OUTPUT_MAX]
        )
        exit_code = -1
    except FileNotFoundError:
        raise ValueError(
            f"Command not found: {shlex.split(command)[0]!r}. Make sure the tool is installed."
        )
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
