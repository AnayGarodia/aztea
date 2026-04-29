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

from core.executor_sandbox import build_subprocess_env

_TIMEOUT_MAX = 30
_OUTPUT_TRUNCATE = 20_000

_SUPPORTED = ("javascript", "typescript", "go", "rust")


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _which(name: str) -> str | None:
    return shutil.which(name)


def _version_string(bin_path: str, *args: str, fallback: str) -> str:
    try:
        proc = subprocess.run(
            [bin_path, *args],
            capture_output=True,
            text=True,
            timeout=5,
            env=build_subprocess_env(),
        )
        text = " ".join(part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()).strip()
        return text[:50] or fallback
    except Exception:
        return fallback


def _available_runtimes() -> dict[str, list[str]]:
    runtimes: dict[str, list[str]] = {}

    js_runtimes = [name for name in ("bun", "deno", "node") if _which(name)]
    if js_runtimes:
        runtimes["javascript"] = js_runtimes

    ts_runtimes: list[str] = []
    if _which("bun"):
        ts_runtimes.append("bun")
    if _which("deno"):
        ts_runtimes.append("deno")
    if _which("tsx"):
        ts_runtimes.append("tsx")
    if _which("node") and _which("tsc"):
        ts_runtimes.append("tsc+node")
    if _which("ts-node"):
        ts_runtimes.append("ts-node")
    if ts_runtimes:
        runtimes["typescript"] = ts_runtimes

    if _which("go"):
        runtimes["go"] = ["go"]

    rust_runtimes = [name for name in ("rust-script", "cargo-script", "rustc") if _which(name)]
    if rust_runtimes:
        runtimes["rust"] = rust_runtimes

    return runtimes


def available_languages() -> list[str]:
    return sorted(_available_runtimes().keys())


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
            env=build_subprocess_env(),
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
        ("deno", ["deno", "run", "--allow-read"]),
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
        runtime_ver = f"{runtime_name} {_version_string(bin_path, '--version', fallback=runtime_name)[:30]}"
        return {**result, "runtime": runtime_ver}
    return _err("multi_language_executor.tool_unavailable", "No JavaScript runtime found (tried bun, deno, node).")


def _run_typescript(code: str, stdin: str, timeout: float) -> dict[str, Any]:
    # Prefer bun/deno/tsx, then compile with tsc+node, then ts-node as a last resort.
    for runtime_name, cmd_template, ext in [
        ("bun", ["bun", "run", "--smol"], "ts"),
        ("deno", ["deno", "run", "--allow-read"], "ts"),
        ("tsx", ["tsx"], "ts"),
    ]:
        bin_path = _which(runtime_name)
        if bin_path is None:
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, f"main.{ext}")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)
            result = _run_subprocess(cmd_template + [fpath], tmpdir, stdin, timeout)
        runtime_ver = f"{runtime_name} {_version_string(bin_path, '--version', fallback=runtime_name)[:30]}"
        return {**result, "runtime": runtime_ver}

    tsc_bin = _which("tsc")
    node_bin = _which("node")
    if tsc_bin and node_bin:
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "main.ts")
            outdir = os.path.join(tmpdir, "dist")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)
            with open(os.path.join(tmpdir, "tsconfig.json"), "w", encoding="utf-8") as f:
                f.write(
                    '{'
                    '"compilerOptions":{'
                    '"target":"ES2020",'
                    '"module":"commonjs",'
                    '"moduleResolution":"node",'
                    '"strict":false,'
                    '"skipLibCheck":true,'
                    '"esModuleInterop":true,'
                    f'"outDir":"{outdir}"'
                    '},'
                    '"include":["main.ts"]'
                    '}'
                )
            compile = subprocess.run(
                [tsc_bin, "--project", os.path.join(tmpdir, "tsconfig.json")],
                capture_output=True,
                text=True,
                timeout=max(timeout, 30),
                cwd=tmpdir,
                env=build_subprocess_env(),
            )
            if compile.returncode != 0:
                return {
                    "stdout": "",
                    "stderr": compile.stderr[:_OUTPUT_TRUNCATE] or compile.stdout[:_OUTPUT_TRUNCATE],
                    "exit_code": compile.returncode,
                    "elapsed_ms": 0,
                    "runtime": f"tsc {_version_string(tsc_bin, '--version', fallback='tsc')}",
                }
            result = _run_subprocess([node_bin, os.path.join(outdir, "main.js")], tmpdir, stdin, timeout)
        return {**result, "runtime": f"tsc+node {_version_string(tsc_bin, '--version', fallback='tsc')[:20]}"}

    tsnode = _which("ts-node")
    if tsnode:
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "main.ts")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)
            cmd = [
                tsnode,
                "--compiler-options",
                '{"module":"commonjs","moduleResolution":"node"}',
                fpath,
            ]
            result = _run_subprocess(cmd, tmpdir, stdin, timeout)
        return {**result, "runtime": f"ts-node {_version_string(tsnode, '--version', fallback='ts-node')[:20]}"}
    return _err("multi_language_executor.tool_unavailable", "No TypeScript runtime found (tried bun, deno, tsx, tsc+node, ts-node).")


def _run_go(code: str, stdin: str, timeout: float) -> dict[str, Any]:
    go_bin = _which("go")
    if go_bin is None:
        return _err("multi_language_executor.tool_unavailable", "Go is not installed on this executor.")
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "main.go")
        with open(fpath, "w") as f:
            f.write(code)
        result = _run_subprocess([go_bin, "run", fpath], tmpdir, stdin, timeout)
    runtime_ver = _version_string(go_bin, "version", fallback="go")
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
            [rustc, src, "-o", out],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=tmpdir,
            env=build_subprocess_env(),
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
    return {**result, "runtime": _version_string(rustc, "--version", fallback="rustc")}


_RUNNERS = {
    "javascript": _run_javascript,
    "typescript": _run_typescript,
    "go": _run_go,
    "rust": _run_rust,
}


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute code in a sandboxed subprocess for the specified language.

    Required:
    - ``language`` (str) — one of the supported runtimes (see ``_SUPPORTED``):
      ``node``, ``deno``, ``bun``, ``go``, ``rust``.
    - ``code`` (str) — source code to execute.

    Optional:
    - ``stdin`` (str) — data piped to stdin.
    - ``timeout_seconds`` (float, default 10.0, max 30.0).

    Runtime requirement: the selected language binary must be installed and
    on PATH. Returns ``tool_unavailable`` with a descriptive message if absent
    (e.g. ``"node not found"``).

    Returns ``{stdout, stderr, exit_code, execution_time_ms, timed_out, language}``.
    """
    language = str(payload.get("language") or "").strip().lower()
    if not language:
        return _err("multi_language_executor.missing_language", f"language is required. Supported: {', '.join(_SUPPORTED)}")
    if language not in _RUNNERS:
        return _err("multi_language_executor.unsupported_language", f"Unsupported language '{language}'. Supported: {', '.join(_SUPPORTED)}")
    available = _available_runtimes()
    if language not in available:
        return _err(
            "multi_language_executor.tool_unavailable",
            f"{language} is not available on this executor. Available languages: {', '.join(available_languages()) or 'none'}",
        )

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
