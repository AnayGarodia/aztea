"""
multi_language_executor.py — Sandboxed code execution for Node/Deno/Bun/Go/Rust

Input:
  {
    "language": "javascript|typescript|go|rust",  # required
    "code": "console.log('hello')",               # required
    "stdin": "",                                   # optional stdin
    "timeout_seconds": 15                          # default 15, max 30
  }

Output:
  {
    "language": str,
    "runtime": str,            # e.g. "node v20.0.0"
    "stdout": str,
    "stderr": str,
    "exit_code": int,
    "passed": bool,
    "execution_time_ms": int,
    "error": ...               # only on tool failure (not code failure)
  }
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from typing import Any

_TIMEOUT_MAX = 30
_OUTPUT_TRUNCATE = 20_000

_SUPPORTED = ("javascript", "typescript", "go", "rust")


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run_subprocess(
    cmd: list[str],
    cwd: str,
    stdin: str,
    timeout: float,
) -> dict[str, Any]:
    t_start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timed out after {timeout}s.", "exit_code": 124, "timed_out": True}
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    stdout = proc.stdout[:_OUTPUT_TRUNCATE]
    stderr = proc.stderr[:_OUTPUT_TRUNCATE]
    return {"stdout": stdout, "stderr": stderr, "exit_code": proc.returncode, "elapsed_ms": elapsed_ms}


def _run_javascript(code: str, stdin: str, timeout: float) -> dict[str, Any]:
    # Prefer bun > deno > node for JS
    for runtime_name, cmd_template in [
        ("bun", ["bun", "run", "--smol"]),
        ("deno", ["deno", "run", "--allow-read", "--allow-env"]),
        ("node", ["node"]),
    ]:
        bin_path = _which(runtime_name)
        if bin_path is None:
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "main.js")
            with open(fpath, "w") as f:
                f.write(code)
            result = _run_subprocess(cmd_template + [fpath], tmpdir, stdin, timeout)
        version_proc = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=5)
        runtime_ver = f"{runtime_name} {version_proc.stdout.strip()[:30]}"
        return {**result, "runtime": runtime_ver}
    return _err("multi_language_executor.tool_unavailable", "No JavaScript runtime found (tried bun, deno, node).")


def _run_typescript(code: str, stdin: str, timeout: float) -> dict[str, Any]:
    # Prefer bun (native TS) > deno (native TS) > ts-node
    for runtime_name, cmd_template, ext in [
        ("bun", ["bun", "run", "--smol"], "ts"),
        ("deno", ["deno", "run", "--allow-read", "--allow-env"], "ts"),
    ]:
        if _which(runtime_name) is None:
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, f"main.{ext}")
            with open(fpath, "w") as f:
                f.write(code)
            result = _run_subprocess(cmd_template + [fpath], tmpdir, stdin, timeout)
        return {**result, "runtime": runtime_name}

    # ts-node fallback
    tsnode = _which("ts-node") or _which("npx")
    if tsnode:
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "main.ts")
            with open(fpath, "w") as f:
                f.write(code)
            cmd = [tsnode, "--yes", "ts-node", fpath] if "npx" in tsnode else [tsnode, fpath]
            result = _run_subprocess(cmd, tmpdir, stdin, timeout)
        return {**result, "runtime": "ts-node"}
    return _err("multi_language_executor.tool_unavailable", "No TypeScript runtime found (tried bun, deno, ts-node).")


def _run_go(code: str, stdin: str, timeout: float) -> dict[str, Any]:
    go_bin = _which("go")
    if go_bin is None:
        return _err("multi_language_executor.tool_unavailable", "Go is not installed on this executor.")
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "main.go")
        with open(fpath, "w") as f:
            f.write(code)
        result = _run_subprocess([go_bin, "run", fpath], tmpdir, stdin, timeout)
    version_proc = subprocess.run([go_bin, "version"], capture_output=True, text=True, timeout=5)
    runtime_ver = version_proc.stdout.strip()[:50] or "go"
    return {**result, "runtime": runtime_ver}


def _run_rust(code: str, stdin: str, timeout: float) -> dict[str, Any]:
    # cargo-script / rustscript approach using `rust-script` or plain rustc
    rust_script = _which("rust-script") or _which("cargo-script")
    if rust_script:
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "main.rs")
            with open(fpath, "w") as f:
                f.write(code)
            result = _run_subprocess([rust_script, fpath], tmpdir, stdin, timeout)
        return {**result, "runtime": "rust-script"}

    rustc = _which("rustc")
    if rustc is None:
        return _err("multi_language_executor.tool_unavailable", "Rust is not installed on this executor (tried rust-script, rustc).")

    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "main.rs")
        out = os.path.join(tmpdir, "main")
        with open(src, "w") as f:
            f.write(code)
        # Compile
        compile_result = subprocess.run(
            [rustc, src, "-o", out], capture_output=True, text=True, timeout=60, cwd=tmpdir
        )
        if compile_result.returncode != 0:
            return {
                "stdout": "",
                "stderr": compile_result.stderr[:_OUTPUT_TRUNCATE],
                "exit_code": compile_result.returncode,
                "elapsed_ms": 0,
                "runtime": "rustc",
            }
        result = _run_subprocess([out], tmpdir, stdin, timeout)
    version_proc = subprocess.run([rustc, "--version"], capture_output=True, text=True, timeout=5)
    return {**result, "runtime": version_proc.stdout.strip()[:50] or "rustc"}


_RUNNERS = {
    "javascript": _run_javascript,
    "typescript": _run_typescript,
    "go": _run_go,
    "rust": _run_rust,
}


def run(payload: dict[str, Any]) -> dict[str, Any]:
    language = str(payload.get("language") or "").strip().lower()
    if not language:
        return _err("multi_language_executor.missing_language", f"language is required. Supported: {', '.join(_SUPPORTED)}")
    if language not in _RUNNERS:
        return _err("multi_language_executor.unsupported_language", f"Unsupported language '{language}'. Supported: {', '.join(_SUPPORTED)}")

    code = str(payload.get("code") or "").strip()
    if not code:
        return _err("multi_language_executor.missing_code", "code is required.")
    if len(code) > 100_000:
        return _err("multi_language_executor.code_too_long", "code must be <= 100 000 characters.")

    stdin = str(payload.get("stdin") or "")
    timeout = float(min(float(payload.get("timeout_seconds") or 15), _TIMEOUT_MAX))

    result = _RUNNERS[language](code, stdin, timeout)

    # If runner returned an error envelope, pass it through
    if "error" in result and isinstance(result.get("error"), dict):
        return result

    return {
        "language": language,
        "runtime": result.get("runtime", language),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "exit_code": result.get("exit_code", -1),
        "passed": result.get("exit_code", -1) == 0,
        "execution_time_ms": result.get("elapsed_ms", 0),
    }
