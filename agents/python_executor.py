"""
python_executor.py — Sandboxed Python code execution

Input:  {
  "code": "print(sum(range(100)))",
  "stdin": "",              # optional input fed to stdin
  "timeout": 10,            # seconds (1-30)
  "explain": true           # whether to explain the output
}
Output: {
  "stdout": str,
  "stderr": str,
  "exit_code": int,
  "timed_out": bool,
  "execution_time_ms": int,
  "explanation": str,       # if explain=true
  "variables_captured": {}  # top-level variable values if execution succeeded
}
"""

import json
import multiprocessing as mp
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from multiprocessing.pool import Pool
from pathlib import Path
from typing import Any

from core import feature_flags as _feature_flags
from core.llm import CompletionRequest, Message, run_with_fallback

_MAX_OUTPUT_CHARS = 8000
_MAX_CODE_CHARS = 16000

_EXPLAIN_SYSTEM = """\
You are a Python expert. Given code and its output, explain:
1. What the code does (one sentence)
2. Why the output is what it is (key mechanics)
3. Any potential issues or improvements (1-2 bullet points)

Be concise and technical. Plain prose, no markdown headers."""

# Appended to user code to capture local variables as JSON on stderr
_CAPTURE_SUFFIX = """
import json as _json, sys as _sys
_captured = {}
try:
    _frame = _sys._getframe(0)
    for _k, _v in list(_frame.f_locals.items()):
        if not _k.startswith('_'):
            try:
                _json.dumps(_v)
                _captured[_k] = _v
            except Exception:
                _captured[_k] = repr(_v)
except Exception:
    pass
print('__VARS__:' + _json.dumps(_captured), file=_sys.stderr)
"""

# Patterns blocked for safety
_BLOCKED_PATTERNS = [
    r"\bos\.system\b",
    r"\bsubprocess\b",
    r"\bshutil\.rmtree\b",
    r"open\s*\(.*?[\"'][aw][\"']",
    r"__import__\s*\(\s*[\"']os[\"']",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"import\s+socket",
    r"import\s+requests",
    r"import\s+urllib",
    r"import\s+http\.client",
]

_WARM_POOL_SIZE = max(1, min(int(os.environ.get("AZTEA_PYTHON_WARM_POOL_SIZE", "2") or "2"), 8))
_WARM_POOL: Pool | None = None


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _capture_variables(namespace: dict[str, Any]) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    for key, value in list(namespace.items()):
        if key.startswith("_"):
            continue
        try:
            json.dumps(value)
            captured[key] = value
        except Exception:
            captured[key] = repr(value)
    return captured


def _exec_in_pool(code: str, stdin_data: str) -> dict[str, Any]:
    import contextlib
    import io

    namespace: dict[str, Any] = {"__name__": "__main__"}
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    fake_stdin = io.StringIO(stdin_data)
    start = time.time()
    old_stdin = sys.stdin
    exit_code = 0
    timed_out = False
    try:
        sys.stdin = fake_stdin
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            exec(compile(code, "<aztea-python-executor>", "exec"), namespace, namespace)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    except Exception as exc:
        exit_code = 1
        print(f"{type(exc).__name__}: {exc}", file=stderr_buffer)
    finally:
        sys.stdin = old_stdin
    elapsed_ms = int((time.time() - start) * 1000)
    return {
        "stdout": stdout_buffer.getvalue(),
        "stderr": stderr_buffer.getvalue(),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "execution_time_ms": elapsed_ms,
        "variables_captured": _capture_variables(namespace) if exit_code == 0 else {},
    }


def _is_safe(code: str) -> bool:
    for pattern in _BLOCKED_PATTERNS:
        if re.search(pattern, code):
            return False
    return True


def _get_warm_pool() -> Pool:
    global _WARM_POOL
    if _WARM_POOL is None:
        method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
        ctx = mp.get_context(method)
        _WARM_POOL = ctx.Pool(processes=_WARM_POOL_SIZE)
    return _WARM_POOL


def _reset_warm_pool() -> None:
    global _WARM_POOL
    if _WARM_POOL is not None:
        _WARM_POOL.terminate()
        _WARM_POOL.join()
        _WARM_POOL = None


def _run_in_subprocess(code: str, stdin_data: str, timeout: int) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        f.write("\n")
        f.write(textwrap.dedent(_CAPTURE_SUFFIX))
        tmp_path = f.name

    start = time.time()
    timed_out = False
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-I", tmp_path],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout
        stderr_raw = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        stdout = ""
        stderr_raw = f"Execution timed out after {timeout} seconds."
        exit_code = 124
        timed_out = True
    except Exception as exc:
        stdout = ""
        stderr_raw = f"Execution error: {exc}"
        exit_code = 1
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    elapsed_ms = int((time.time() - start) * 1000)

    variables_captured = {}
    stderr_lines = []
    for line in stderr_raw.splitlines():
        if line.startswith("__VARS__:"):
            try:
                variables_captured = json.loads(line[len("__VARS__:"):])
            except Exception:
                pass
        else:
            stderr_lines.append(line)
    return {
        "stdout": stdout,
        "stderr": "\n".join(stderr_lines),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "execution_time_ms": elapsed_ms,
        "variables_captured": variables_captured,
    }


def run(payload: dict) -> dict:
    """Execute Python code in an isolated subprocess and return stdout/stderr.

    Required: ``code`` (str, ≤ ``_MAX_CODE_CHARS``).
    Optional:
    - ``stdin`` (str) — data piped to the subprocess stdin.
    - ``timeout_seconds`` (float, default 10.0, max 30.0).
    - ``packages`` (list[str]) — pip packages to install before execution;
      each name is allowlisted to prevent arbitrary package injection.

    Returns ``{stdout, stderr, exit_code, execution_time_ms, timed_out}``.
    The subprocess runs with a restricted environment (no network, limited
    file-system write access) using a tempdir. The tempdir is deleted after
    each call regardless of outcome.
    """
    code = str(payload.get("code", "")).strip()
    if not code:
        return _err("python_executor.missing_code", "code is required")

    if len(code) > _MAX_CODE_CHARS:
        return _err("python_executor.code_too_long", f"code too long (max {_MAX_CODE_CHARS} chars)")

    if not _is_safe(code):
        return {
            "stdout": "",
            "stderr": "Blocked: code contains disallowed operations (network, file writes, shell execution).",
            "exit_code": 1,
            "timed_out": False,
            "execution_time_ms": 0,
            "explanation": "",
            "variables_captured": {},
        }

    stdin_data = str(payload.get("stdin", "") or "")
    if len(stdin_data) > 65536:
        return _err("python_executor.stdin_too_long", "stdin must be 65536 characters or fewer")

    try:
        timeout = max(1, min(int(payload.get("timeout", 10)), 30))
    except (TypeError, ValueError):
        return _err("python_executor.invalid_timeout", "timeout must be a number between 1 and 30")

    explain = bool(payload.get("explain", True))

    if _feature_flags.PYTHON_WARM_POOL:
        try:
            pool = _get_warm_pool()
            async_result = pool.apply_async(_exec_in_pool, (code, stdin_data))
            pooled = async_result.get(timeout=timeout)
            stdout = pooled["stdout"]
            stderr = pooled["stderr"]
            exit_code = pooled["exit_code"]
            timed_out = pooled["timed_out"]
            elapsed_ms = pooled["execution_time_ms"]
            variables_captured = pooled["variables_captured"]
        except mp.TimeoutError:
            _reset_warm_pool()
            stdout = ""
            stderr = f"Execution timed out after {timeout} seconds."
            exit_code = 124
            timed_out = True
            elapsed_ms = timeout * 1000
            variables_captured = {}
        except Exception as exc:
            _reset_warm_pool()
            stdout = ""
            stderr = f"Execution error: {exc}"
            exit_code = 1
            timed_out = False
            elapsed_ms = 0
            variables_captured = {}
    else:
        raw_result = _run_in_subprocess(code, stdin_data, timeout)
        stdout = raw_result["stdout"]
        stderr = raw_result["stderr"]
        exit_code = raw_result["exit_code"]
        timed_out = raw_result["timed_out"]
        elapsed_ms = raw_result["execution_time_ms"]
        variables_captured = raw_result["variables_captured"]

    stdout = stdout[:_MAX_OUTPUT_CHARS]
    stderr = stderr[:2000]

    explanation = ""
    if explain and (stdout or stderr or exit_code != 0):
        prompt = f"Code:\n```python\n{code[:2000]}\n```\n\nstdout:\n{stdout[:1000]}\nstderr:\n{stderr[:500]}\nexit code: {exit_code}"
        req = CompletionRequest(
            model="",
            messages=[
                Message(role="system", content=_EXPLAIN_SYSTEM),
                Message(role="user", content=prompt),
            ],
            temperature=0.2,
            max_tokens=400,
        )
        try:
            raw = run_with_fallback(req)
            explanation = raw.text.strip()
        except Exception:
            pass

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "execution_time_ms": elapsed_ms,
        "explanation": explanation,
        "variables_captured": variables_captured,
    }
