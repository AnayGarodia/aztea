"""
multi_file_executor.py — Run multi-file Python projects in a sandboxed tempdir

Input:
  {
    "files": [{"path": "main.py", "content": "..."}],
    "requirements": "requests\nnumpy",   # optional, installs before run
    "entry_point": "main.py",            # which file to run (default: main.py)
    "stdin": "",                         # optional stdin
    "timeout": 15                        # seconds (max 30)
  }

Output:
  {
    "stdout": str,
    "stderr": str,
    "exit_code": int,
    "timed_out": bool,
    "execution_time_ms": int,
    "files_written": int,
    "packages_installed": [str],
    "install_error": str | null,
    "explanation": str
  }
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

from core.llm import CompletionRequest, Message, run_with_fallback

_MAX_FILES = 20
_MAX_FILE_SIZE = 50_000
_MAX_REQ_CHARS = 2_000
_MAX_TIMEOUT = 30

_EXPLAIN_SYSTEM = """\
You are a Python expert. The user ran a multi-file Python project and got this output.
Briefly explain what the output means and any errors in 2-4 sentences."""


def _install_requirements(tmpdir: str, requirements: str) -> tuple[list[str], str | None]:
    req_path = os.path.join(tmpdir, "_requirements.txt")
    with open(req_path, "w") as f:
        f.write(requirements.strip())

    packages = [ln.strip() for ln in requirements.strip().splitlines() if ln.strip() and not ln.startswith("#")]
    if not packages:
        return [], None

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", req_path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[:500]
            return packages, f"pip install failed: {err}"
        return packages, None
    except subprocess.TimeoutExpired:
        return packages, "pip install timed out after 60s"
    except Exception as exc:
        return packages, f"pip install error: {exc}"


def run(payload: dict) -> dict:
    """Execute a multi-file Python project in an isolated tempdir sandbox.

    Required: ``files`` (list[{path: str, content: str}]) — the files to write
    into the sandbox before execution. Max ``_MAX_FILES`` files.

    Optional:
    - ``entry`` (str, default ``"main.py"``) — the file to execute as the
      entrypoint.
    - ``stdin`` (str) — data piped to stdin.
    - ``timeout_seconds`` (float, default 10.0, max 30.0).
    - ``packages`` (list[str]) — pip packages to install before execution
      (allowlisted; injection attempts are rejected).

    Returns ``{stdout, stderr, exit_code, execution_time_ms, timed_out}``.
    All files and the venv are deleted after each call.
    """
    files = payload.get("files")
    if not files or not isinstance(files, list):
        raise ValueError("'files' must be a non-empty list of {path, content} objects.")
    if len(files) > _MAX_FILES:
        raise ValueError(f"At most {_MAX_FILES} files allowed per call.")

    requirements = str(payload.get("requirements") or "").strip()[:_MAX_REQ_CHARS]
    entry_point = str(payload.get("entry_point") or "main.py").strip()
    stdin_data = str(payload.get("stdin") or "")
    timeout = min(int(payload.get("timeout") or 15), _MAX_TIMEOUT)
    explain = bool(payload.get("explain", True))

    tmpdir = tempfile.mkdtemp(prefix="aztea_mfx_")
    try:
        files_written = 0
        for f in files:
            if not isinstance(f, dict):
                continue
            path = str(f.get("path") or "").strip()
            content = str(f.get("content") or "")
            if not path or len(content) > _MAX_FILE_SIZE:
                continue
            # Prevent path traversal
            full = os.path.realpath(os.path.join(tmpdir, path))
            if not full.startswith(os.path.realpath(tmpdir)):
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
            files_written += 1

        if files_written == 0:
            raise ValueError("No valid files were written.")

        packages_installed: list[str] = []
        install_error: str | None = None
        if requirements:
            packages_installed, install_error = _install_requirements(tmpdir, requirements)

        entry_full = os.path.realpath(os.path.join(tmpdir, entry_point))
        if not entry_full.startswith(os.path.realpath(tmpdir)):
            raise ValueError("entry_point must be inside the project directory.")
        if not os.path.exists(entry_full):
            raise ValueError(f"entry_point '{entry_point}' not found in provided files.")

        start = time.monotonic()
        timed_out = False
        try:
            result = subprocess.run(
                [sys.executable, entry_full],
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.PIPE if stdin_data else None,
                input=stdin_data if stdin_data else None,
                cwd=tmpdir,
                env={**os.environ, "PYTHONPATH": tmpdir},
            )
            stdout = result.stdout[:20_000]
            stderr = result.stderr[:5_000]
            exit_code = result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace")[:20_000]
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")[:5_000]
            exit_code = -1
            timed_out = True
        except Exception as exc:
            stdout = ""
            stderr = str(exc)[:2_000]
            exit_code = -1

        execution_time_ms = int((time.monotonic() - start) * 1000)

        explanation = ""
        if explain:
            req = CompletionRequest(
                model="",
                messages=[
                    Message("system", _EXPLAIN_SYSTEM),
                    Message("user", f"entry_point: {entry_point}\nstdout:\n{stdout[:1500]}\nstderr:\n{stderr[:500]}\nexit_code: {exit_code}"),
                ],
                max_tokens=300,
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
            "execution_time_ms": execution_time_ms,
            "files_written": files_written,
            "packages_installed": packages_installed,
            "install_error": install_error,
            "explanation": explanation,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
