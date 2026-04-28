"""
type_checker.py — Run mypy (Python) or tsc (TypeScript) on submitted code.

This agent is intentionally tool-first. It never falls back to an LLM summary
because the value proposition is deterministic diagnostics from a real checker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

_TIMEOUT = 30
_CODE_MAX = 100_000


def _normalize_diagnostic(item: dict, *, default_file: str) -> dict:
    line = item.get("line")
    column = item.get("column")
    return {
        "file": os.path.basename(str(item.get("file") or default_file)),
        "line": int(line) if isinstance(line, int) or str(line).isdigit() else None,
        "col": int(column) if isinstance(column, int) or str(column).isdigit() else None,
        "code": str(item.get("code") or item.get("rule") or "error"),
        "message": str(item.get("message") or item.get("text") or "").strip(),
        "severity": str(item.get("severity") or "error").lower(),
    }


def _parse_mypy_text_diagnostics(raw: str) -> list[dict]:
    diagnostics: list[dict] = []
    for line in raw.splitlines():
        match = re.match(r"^(.+?):(\d+):(\d+): error:\s+(.+?)\s+\[(.+?)\]$", line)
        if match:
            diagnostics.append(
                {
                    "file": os.path.basename(match.group(1)),
                    "line": int(match.group(2)),
                    "col": int(match.group(3)),
                    "code": match.group(5),
                    "message": match.group(4),
                    "severity": "error",
                }
            )
            continue
        match = re.match(r"^(.+?):(\d+): error:\s+(.+)$", line)
        if match:
            diagnostics.append(
                {
                    "file": os.path.basename(match.group(1)),
                    "line": int(match.group(2)),
                    "col": None,
                    "code": "error",
                    "message": match.group(3),
                    "severity": "error",
                }
            )
    return diagnostics


def _run_mypy(code: str, stubs: dict[str, str], strict: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        main_path = os.path.join(tmpdir, "main.py")
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(code)
        for fname, content in (stubs or {}).items():
            safe = os.path.basename(fname)
            if safe and safe.endswith(".py"):
                with open(os.path.join(tmpdir, safe), "w", encoding="utf-8") as f:
                    f.write(content)

        config_path = os.path.join(tmpdir, "mypy.ini")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("[mypy]\n")
            f.write("python_version = 3.11\n")
            f.write("show_error_codes = True\n")

        cmd = [
            "python3",
            "-m",
            "mypy",
            "--no-error-summary",
            "--show-column-numbers",
            "--output=json",
            "--config-file",
            config_path,
        ]
        if strict:
            cmd.append("--strict")
        cmd.append(main_path)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("mypy timed out after 30 seconds.")
        except FileNotFoundError:
            raise RuntimeError("mypy is not installed on this executor. Install it with: pip install mypy")

        raw = result.stdout + result.stderr
        diagnostics: list[dict] = []
        # mypy --output=json emits JSON Lines (one diagnostic dict per line),
        # not a JSON array. The previous version's ``json.loads`` call on the
        # whole stdout therefore returned a single dict — failed the
        # ``isinstance(..., list)`` check — and silently dropped every
        # diagnostic. Parse line-by-line first; fall back to the legacy
        # full-document and text parsers so older mypy outputs still work.
        stdout = result.stdout.strip() or "[]"
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                diagnostics.append(_normalize_diagnostic(obj, default_file="main.py"))
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        diagnostics.append(_normalize_diagnostic(item, default_file="main.py"))
        if not diagnostics and result.returncode != 0:
            diagnostics = _parse_mypy_text_diagnostics(raw)

        try:
            version_result = subprocess.run(
                ["python3", "-m", "mypy", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=tmpdir,
            )
            version_blob = f"{version_result.stdout} {version_result.stderr}".strip()
        except Exception:
            version_blob = raw
        version_match = re.search(r"mypy\s+([\d.]+)", version_blob)
        tool_version = f"mypy {version_match.group(1)}" if version_match else "mypy"

        return {
            "language": "python",
            "ok": result.returncode == 0,
            "passed": result.returncode == 0,
            "error_count": len(diagnostics),
            "diagnostics": diagnostics,
            "errors": diagnostics,
            "raw_output": raw[:5000],
            "tool_version": tool_version,
        }


def _parse_tsc_diagnostics(raw: str) -> list[dict]:
    diagnostics: list[dict] = []
    for line in raw.splitlines():
        match = re.match(r"^(.+?)\((\d+),(\d+)\): error (TS\d+): (.+)$", line)
        if match:
            diagnostics.append(
                {
                    "file": os.path.basename(match.group(1)),
                    "line": int(match.group(2)),
                    "col": int(match.group(3)),
                    "code": match.group(4),
                    "message": match.group(5),
                    "severity": "error",
                }
            )
    return diagnostics


def _run_tsc(code: str, stubs: dict[str, str], strict: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        main_path = os.path.join(tmpdir, "main.ts")
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(code)
        for fname, content in (stubs or {}).items():
            safe = os.path.basename(fname)
            if safe and (safe.endswith(".ts") or safe.endswith(".d.ts")):
                with open(os.path.join(tmpdir, safe), "w", encoding="utf-8") as f:
                    f.write(content)

        with open(os.path.join(tmpdir, "tsconfig.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "compilerOptions": {
                        "strict": strict,
                        "noEmit": True,
                        "target": "ES2020",
                        "module": "commonjs",
                    }
                },
                f,
            )

        tsc_bin = shutil.which("tsc") or "tsc"
        cmd = [tsc_bin, "--noEmit", "--project", os.path.join(tmpdir, "tsconfig.json")]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("tsc timed out after 30 seconds.")
        except FileNotFoundError:
            raise RuntimeError("tsc is not installed. Install TypeScript globally: npm install -g typescript")

        raw = result.stdout + result.stderr
        version_result = subprocess.run(
            [tsc_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        tool_version = version_result.stdout.strip() or "tsc"
        diagnostics = _parse_tsc_diagnostics(raw)

        return {
            "language": "typescript",
            "ok": result.returncode == 0,
            "passed": result.returncode == 0,
            "error_count": len(diagnostics),
            "diagnostics": diagnostics,
            "errors": diagnostics,
            "raw_output": raw[:5000],
            "tool_version": tool_version,
        }


def run(payload: dict) -> dict:
    """Run static type checking via mypy (Python) or tsc (TypeScript).

    Required: ``code`` (str, ≤ ``_CODE_MAX`` chars).
    Optional:
    - ``language`` (str, default ``"auto"``) — ``"python"`` | ``"typescript"``
      | ``"auto"`` (detect from content).
    - ``strict`` (bool, default False) — enable strict mode (``--strict`` for
      both mypy and tsc).
    - ``config`` (str) — raw mypy.ini or tsconfig.json content to use instead
      of defaults.

    Runtime requirements:
    - Python: ``mypy`` must be on PATH.
    - TypeScript: ``tsc`` (from ``typescript`` npm package) must be on PATH.
      Returns ``tool_unavailable`` if absent.

    Returns ``{errors: [{file, line, col, message, severity}], total, passed}``.
    No LLM involved — output is deterministic.
    """
    code = str(payload.get("code") or "").strip()
    if not code:
        raise ValueError("'code' is required.")
    if len(code) > _CODE_MAX:
        raise ValueError(f"'code' must be <= {_CODE_MAX} characters.")

    language = str(payload.get("language") or "python").strip().lower()
    if language not in {"python", "typescript"}:
        raise ValueError("'language' must be 'python' or 'typescript'.")

    stubs = payload.get("stubs") or {}
    if not isinstance(stubs, dict):
        stubs = {}

    strict = bool(payload.get("strict", False))
    if language == "python":
        return _run_mypy(code, stubs, strict)
    return _run_tsc(code, stubs, strict)
