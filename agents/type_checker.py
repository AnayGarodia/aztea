"""
type_checker.py — Run mypy (Python) or tsc (TypeScript) on submitted code

Input:
  {
    "code": "def add(a, b): return a + b",
    "language": "python | typescript",
    "stubs": {"helper.py": "def helper() -> int: ..."},  # optional extra files
    "strict": false                                        # default false
  }

Output:
  {
    "language": str,
    "passed": bool,
    "error_count": int,
    "errors": [{"file": str, "line": int | null, "col": int | null, "code": str, "message": str}],
    "raw_output": str,
    "tool_version": str
  }
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

_TIMEOUT = 30
_CODE_MAX = 100_000


def _run_mypy(code: str, stubs: dict[str, str], strict: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        main_path = os.path.join(tmpdir, "main.py")
        with open(main_path, "w") as f:
            f.write(code)
        for fname, content in (stubs or {}).items():
            safe = os.path.basename(fname)
            if safe and safe.endswith(".py"):
                with open(os.path.join(tmpdir, safe), "w") as f:
                    f.write(content)

        cmd = ["python3", "-m", "mypy", "--no-error-summary", "--show-column-numbers"]
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
        version_m = re.search(r"mypy\s+([\d.]+)", raw)
        tool_version = f"mypy {version_m.group(1)}" if version_m else "mypy"

        errors = []
        for line in result.stdout.splitlines():
            m = re.match(r"^(.+?):(\d+):(\d+): error:\s+(.+?)\s+\[(.+?)\]$", line)
            if m:
                errors.append({
                    "file": os.path.basename(m.group(1)),
                    "line": int(m.group(2)),
                    "col": int(m.group(3)),
                    "code": m.group(5),
                    "message": m.group(4),
                })
                continue
            m2 = re.match(r"^(.+?):(\d+): error:\s+(.+)$", line)
            if m2:
                errors.append({
                    "file": os.path.basename(m2.group(1)),
                    "line": int(m2.group(2)),
                    "col": None,
                    "code": "error",
                    "message": m2.group(3),
                })

        return {
            "language": "python",
            "passed": result.returncode == 0,
            "error_count": len(errors),
            "errors": errors,
            "raw_output": raw[:5000],
            "tool_version": tool_version,
        }


def _run_tsc(code: str, stubs: dict[str, str], strict: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        main_path = os.path.join(tmpdir, "main.ts")
        with open(main_path, "w") as f:
            f.write(code)
        for fname, content in (stubs or {}).items():
            safe = os.path.basename(fname)
            if safe and (safe.endswith(".ts") or safe.endswith(".d.ts")):
                with open(os.path.join(tmpdir, safe), "w") as f:
                    f.write(content)

        tsconfig = {
            "strict": strict,
            "noEmit": True,
            "target": "ES2020",
            "module": "commonjs",
        }
        import json
        with open(os.path.join(tmpdir, "tsconfig.json"), "w") as f:
            json.dump({"compilerOptions": tsconfig}, f)

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
            [tsc_bin, "--version"], capture_output=True, text=True, timeout=5
        )
        tool_version = version_result.stdout.strip() or "tsc"

        errors = []
        for line in result.stdout.splitlines():
            m = re.match(r"^(.+?)\((\d+),(\d+)\): error (TS\d+): (.+)$", line)
            if m:
                errors.append({
                    "file": os.path.basename(m.group(1)),
                    "line": int(m.group(2)),
                    "col": int(m.group(3)),
                    "code": m.group(4),
                    "message": m.group(5),
                })

        return {
            "language": "typescript",
            "passed": result.returncode == 0,
            "error_count": len(errors),
            "errors": errors,
            "raw_output": raw[:5000],
            "tool_version": tool_version,
        }


def run(payload: dict) -> dict:
    code = str(payload.get("code") or "").strip()
    if not code:
        raise ValueError("'code' is required.")
    if len(code) > _CODE_MAX:
        raise ValueError(f"'code' must be <= {_CODE_MAX} characters.")

    language = str(payload.get("language") or "python").strip().lower()
    if language not in ("python", "typescript"):
        raise ValueError("'language' must be 'python' or 'typescript'.")

    stubs = payload.get("stubs") or {}
    if not isinstance(stubs, dict):
        stubs = {}

    strict = bool(payload.get("strict", False))

    if language == "python":
        return _run_mypy(code, stubs, strict)
    return _run_tsc(code, stubs, strict)
