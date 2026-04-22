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
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

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


def _is_safe(code: str) -> bool:
    for pattern in _BLOCKED_PATTERNS:
        if re.search(pattern, code):
            return False
    return True


def run(payload: dict) -> dict:
    code = str(payload.get("code", "")).strip()
    if not code:
        return {"error": "code is required"}

    if len(code) > _MAX_CODE_CHARS:
        return {"error": f"code too long (max {_MAX_CODE_CHARS} chars)"}

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
        return {"error": "stdin must be 65536 characters or fewer"}

    try:
        timeout = max(1, min(int(payload.get("timeout", 10)), 30))
    except (TypeError, ValueError):
        return {"error": "timeout must be a number between 1 and 30"}

    explain = bool(payload.get("explain", True))

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
    stderr = "\n".join(stderr_lines)

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
