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
    "error_kind": "compile" | "runtime" | None,  # why a nonzero exit happened
    "execution_time_ms": int,
    "error": ...               # only on tool failure (not code failure)
  }
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from core.executor_sandbox import build_subprocess_env
from agents._contracts import agent_error as _err

_TIMEOUT_MAX = 30
_OUTPUT_TRUNCATE = 20_000

# Strip ANSI escape sequences from sandboxed subprocess output before
# returning to the caller — prevents screen-clear / cursor-positioning
# / OSC-prompt-spoof attacks on buyer terminals.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)


def _strip_terminal_escapes(text: str) -> str:
    if not text:
        return text
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    return cleaned.replace("\x07", "").replace("\x08", "")

_SUPPORTED = ("javascript", "typescript", "go", "rust")

# Pre-execution SSRF/network block. The Python executor enforces a
# "no network at all from the sandbox" policy via a regex pre-check
# (see agents/python_executor.py::_is_safe). The JS/TS/Go/Rust runners
# previously had no such pre-check, which the 2026-05-09 stress test
# confirmed: a JS `fetch('http://169.254.169.254/...')` connected to
# the AWS instance metadata service and returned HTTP 401 — the
# IMDS endpoint was reachable. This static check matches the Python
# policy across all four runtimes by refusing literal references to
# private/loopback IPs and metadata hostnames, plus network-capable
# stdlib imports/builtins. It is intentionally conservative: a
# determined attacker can still construct the address dynamically,
# but at that point the same pre-execution policy that applies to
# Python applies here. Defense in depth (network namespace isolation)
# remains the right long-term fix; this closes the parity gap today.
_PRIVATE_HOST_PATTERNS = (
    r"\b169\.254\.\d+\.\d+\b",                        # link-local incl. AWS/Azure IMDS
    r"\b127(?:\.\d+){3}\b",                           # loopback /8
    r"\b10(?:\.\d+){3}\b",                            # RFC1918 10/8
    r"\b192\.168\.\d+\.\d+\b",                        # RFC1918 192.168/16
    r"\b172\.(?:1[6-9]|2\d|3[0-1])\.\d+\.\d+\b",      # RFC1918 172.16/12
    r"\[?::1\]?",                                     # IPv6 loopback
    r"\bfd[0-9a-f]{2}:",                              # IPv6 ULA fc00::/7 lower half
    r"\blocalhost\b",
    r"\bmetadata\.google\.internal\b",                # GCP IMDS
    r"\bmetadata\.azure\.com\b",                      # Azure IMDS
)
_NETWORK_API_PATTERNS_RAW = {
    # JS/TS: top-level fetch + http/https/net/dns/dgram modules.
    # Also block process/filesystem-escape modules (child_process, fs, worker_threads,
    # cluster, vm). Without these the runtime crashes opaquely (HTTP 502) when the
    # caller tries to shell out; rejecting up-front returns a structured error.
    ("javascript", "typescript"): (
        r"\bfetch\s*\(",
        r"\bXMLHttpRequest\b",
        r"""require\s*\(\s*['"](?:http|https|net|dns|dgram|tls|child_process|fs|fs/promises|worker_threads|cluster|vm|os|process)['"]\s*\)""",
        r"""(?:from|import)\s+['"](?:node:)?(?:http|https|net|dns|dgram|tls|child_process|fs|fs/promises|worker_threads|cluster|vm|os|process)['"]""",
    ),
    # Go: net, net/http, net/url with Get/Dial/Post.
    ("go",): (
        r"""(?:^|\s)import\s+\(?[^)]*['"]net(?:/(?:http|url|rpc))?['"]""",
        r"\bnet\.(?:Dial|Listen|Resolve)",
        r"\bhttp\.(?:Get|Post|NewRequest|Head|PostForm)\b",
    ),
    # Rust: std::net plus common HTTP crates.
    ("rust",): (
        r"\bstd::net::",
        r"\b(?:reqwest|hyper|surf|ureq|isahc)::",
        r"\bTcpStream::connect\b",
    ),
}
# Process-spawn escapes, kept separate from the network patterns so the
# rejection message tells the caller what actually tripped: Go and Rust
# could previously shell out (e.g. exec.Command("curl", ...)) and reach
# the network indirectly, sidestepping the SSRF pre-filter entirely.
# JS/TS spawn surfaces (child_process et al.) are already covered by the
# module lists in _NETWORK_API_PATTERNS_RAW above.
_PROCESS_SPAWN_PATTERNS_RAW = {
    ("go",): (
        r"""(?:^|\s)import\s+\(?[^)]*['"]os/exec['"]""",
        r"\bexec\.(?:Command|CommandContext|LookPath)\b",
        r"\bos\.StartProcess\b",
        r"\bsyscall\.(?:Exec|ForkExec)\b",
    ),
    ("rust",): (
        r"\bstd::process::",
        r"\bprocess::Command\b",
        r"\bCommand::new\b",
    ),
}
_NETWORK_API_PATTERNS = {
    langs: tuple(re.compile(p, re.MULTILINE) for p in patterns)
    for langs, patterns in _NETWORK_API_PATTERNS_RAW.items()
}
_PROCESS_SPAWN_PATTERNS = {
    langs: tuple(re.compile(p, re.MULTILINE) for p in patterns)
    for langs, patterns in _PROCESS_SPAWN_PATTERNS_RAW.items()
}
_PRIVATE_HOST_RE = re.compile("|".join(_PRIVATE_HOST_PATTERNS), re.IGNORECASE)


def _first_pattern_hit(
    language: str, code: str, pattern_table: dict
) -> bool:
    """Pure: does any pattern registered for ``language`` match ``code``?"""
    for langs, patterns in pattern_table.items():
        if language not in langs:
            continue
        if any(pattern.search(code) for pattern in patterns):
            return True
    return False


def _is_code_network_safe(language: str, code: str) -> tuple[bool, str | None]:
    """Pre-execution SSRF/network/process-spawn safety check.

    Returns ``(True, None)`` if the code is allowed. Returns
    ``(False, reason)`` with a human-readable explanation when a
    private-host literal, a network-capable API surface, or a
    process-spawn surface appears in the source. The reason is surfaced
    verbatim in the structured error envelope so callers can fix the
    offending construct.
    """
    if _PRIVATE_HOST_RE.search(code):
        return (
            False,
            "Code contains a literal reference to a private, loopback, "
            "or cloud-metadata host. The sandbox has no network and "
            "must not be used to reach internal services (SSRF policy).",
        )
    if _first_pattern_hit(language, code, _NETWORK_API_PATTERNS):
        return (
            False,
            "Code uses a network-capable API surface "
            "(http/https/net/fetch/etc.). The sandbox is offline; "
            "remove the network call or run on a host that has "
            "explicit egress.",
        )
    if _first_pattern_hit(language, code, _PROCESS_SPAWN_PATTERNS):
        return (
            False,
            "Code spawns external processes (os/exec, std::process, "
            "Command::new, etc.). The sandbox does not allow shelling "
            "out — spawned binaries would sidestep the offline/SSRF "
            "policy. Inline the logic instead of calling external tools.",
        )
    return True, None



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
        text = " ".join(
            part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
        ).strip()
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

    rust_runtimes = [
        name for name in ("rust-script", "cargo-script", "rustc") if _which(name)
    ]
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
        return {
            "stdout": "",
            "stderr": f"Timed out after {timeout}s.",
            "exit_code": 124,
            "timed_out": True,
        }
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    stdout = _strip_terminal_escapes(proc.stdout[:_OUTPUT_TRUNCATE])
    stderr = _strip_terminal_escapes(proc.stderr[:_OUTPUT_TRUNCATE])
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": proc.returncode,
        "elapsed_ms": elapsed_ms,
    }


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
    return _err(
        "multi_language_executor.tool_unavailable",
        "No JavaScript runtime found (tried bun, deno, node).",
    )


# In TypeScript fallback chain we try direct runtimes (bun/deno/tsx) first,
# then the slower tsc-then-node path; ts-node is a last resort.
_TS_DIRECT_RUNTIMES: tuple[tuple[str, list[str], str], ...] = (
    ("bun", ["bun", "run", "--smol"], "ts"),
    ("deno", ["deno", "run", "--allow-read"], "ts"),
    ("tsx", ["tsx"], "ts"),
)
_TS_TSCONFIG_TEMPLATE = (
    '{"compilerOptions":{"target":"ES2020","module":"commonjs",'
    '"moduleResolution":"node","strict":false,"skipLibCheck":true,'
    '"esModuleInterop":true,"outDir":"%s"},"include":["main.ts"]}'
)
_TSC_MIN_TIMEOUT = 30
_VERSION_PREFIX_CHARS = 30
_VERSION_SHORT_CHARS = 20


def _try_ts_direct_runtime(code: str, stdin: str, timeout: float) -> dict[str, Any] | None:
    """Side-effect: try bun/deno/tsx in order; returns the run dict or ``None`` if none available."""
    for runtime_name, cmd_template, ext in _TS_DIRECT_RUNTIMES:
        bin_path = _which(runtime_name)
        if bin_path is None:
            continue
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, f"main.{ext}")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(code)
            result = _run_subprocess(cmd_template + [fpath], tmpdir, stdin, timeout)
        ver = _version_string(bin_path, "--version", fallback=runtime_name)[:_VERSION_PREFIX_CHARS]
        return {**result, "runtime": f"{runtime_name} {ver}"}
    return None


def _try_ts_tsc_node(code: str, stdin: str, timeout: float) -> dict[str, Any] | None:
    """Side-effect: tsc compile then node-run; ``None`` if either binary is missing."""
    tsc_bin = _which("tsc")
    node_bin = _which("node")
    if not (tsc_bin and node_bin):
        return None
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "main.ts")
        outdir = os.path.join(tmpdir, "dist")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(code)
        with open(os.path.join(tmpdir, "tsconfig.json"), "w", encoding="utf-8") as f:
            f.write(_TS_TSCONFIG_TEMPLATE % outdir)
        compile_proc = subprocess.run(
            [tsc_bin, "--project", os.path.join(tmpdir, "tsconfig.json")],
            capture_output=True, text=True,
            timeout=max(timeout, _TSC_MIN_TIMEOUT), cwd=tmpdir,
            env=build_subprocess_env(),
        )
        if compile_proc.returncode != 0:
            ver = _version_string(tsc_bin, "--version", fallback="tsc")
            return {
                "stdout": "",
                "stderr": (compile_proc.stderr[:_OUTPUT_TRUNCATE]
                           or compile_proc.stdout[:_OUTPUT_TRUNCATE]),
                "exit_code": compile_proc.returncode,
                "elapsed_ms": 0,
                "runtime": f"tsc {ver}",
                "error_kind": "compile",
            }
        result = _run_subprocess(
            [node_bin, os.path.join(outdir, "main.js")], tmpdir, stdin, timeout,
        )
    ver = _version_string(tsc_bin, "--version", fallback="tsc")[:_VERSION_SHORT_CHARS]
    return {**result, "runtime": f"tsc+node {ver}"}


def _try_ts_node(code: str, stdin: str, timeout: float) -> dict[str, Any] | None:
    """Side-effect: ts-node fallback; ``None`` if absent."""
    tsnode = _which("ts-node")
    if not tsnode:
        return None
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
    ver = _version_string(tsnode, "--version", fallback="ts-node")[:_VERSION_SHORT_CHARS]
    return {**result, "runtime": f"ts-node {ver}"}


def _run_typescript(code: str, stdin: str, timeout: float) -> dict[str, Any]:
    """Side-effect: run TS with the first available runtime in the configured chain."""
    for attempt in (_try_ts_direct_runtime, _try_ts_tsc_node, _try_ts_node):
        result = attempt(code, stdin, timeout)
        if result is not None:
            return result
    return _err(
        "multi_language_executor.tool_unavailable",
        "No TypeScript runtime found (tried bun, deno, tsx, tsc+node, ts-node).",
    )


def _run_go(code: str, stdin: str, timeout: float) -> dict[str, Any]:
    go_bin = _which("go")
    if go_bin is None:
        return _err(
            "multi_language_executor.tool_unavailable",
            "Go is not installed on this executor.",
        )
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "main.go")
        with open(fpath, "w") as f:
            f.write(code)
        result = _run_subprocess([go_bin, "run", fpath], tmpdir, stdin, timeout)
    runtime_ver = _version_string(go_bin, "version", fallback="go")
    if result.get("exit_code", 0) != 0 and _GO_COMPILE_ERROR_RE.search(
        str(result.get("stderr") or "")
    ):
        result["error_kind"] = "compile"
    return {**result, "runtime": runtime_ver}


_RUSTC_COMPILE_TIMEOUT_S = 60
# `go run` interleaves compile and run in one subprocess; compiler
# diagnostics are the only way to tell a build break from a runtime crash.
# Matches "./main.go:3:5: undefined: foo" and the "# command-line-arguments"
# build-failure banner.
_GO_COMPILE_ERROR_RE = re.compile(r"\.go:\d+:\d+:|^# command-line-arguments", re.MULTILINE)


def _run_rust_via_script(code: str, stdin: str, timeout: float) -> dict[str, Any] | None:
    """Side-effect: ``rust-script``/``cargo-script`` shortcut; ``None`` if neither is on PATH."""
    rust_script = _which("rust-script") or _which("cargo-script")
    if not rust_script:
        return None
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "main.rs")
        with open(fpath, "w") as f:
            f.write(code)
        result = _run_subprocess([rust_script, fpath], tmpdir, stdin, timeout)
    return {**result, "runtime": "rust-script"}


def _run_rust(code: str, stdin: str, timeout: float) -> dict[str, Any]:
    """Side-effect: run Rust via ``rust-script`` if present, else compile+run with ``rustc``."""
    short = _run_rust_via_script(code, stdin, timeout)
    if short is not None:
        return short
    rustc = _which("rustc")
    if rustc is None:
        return _err(
            "multi_language_executor.tool_unavailable",
            "Rust is not installed on this executor (tried rust-script, rustc).",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, "main.rs")
        out = os.path.join(tmpdir, "main")
        with open(src, "w") as f:
            f.write(code)
        compile_result = subprocess.run(
            [rustc, src, "-o", out],
            capture_output=True,
            text=True,
            timeout=_RUSTC_COMPILE_TIMEOUT_S,
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
                "error_kind": "compile",
            }
        result = _run_subprocess([out], tmpdir, stdin, timeout)
    return {**result, "runtime": _version_string(rustc, "--version", fallback="rustc")}


_RUNNERS = {
    "javascript": _run_javascript,
    "typescript": _run_typescript,
    "go": _run_go,
    "rust": _run_rust,
}


_MAX_CODE_CHARS = 100_000
_DEFAULT_TIMEOUT_S = 15
_TIMEOUT_EXIT_CODE = 124


def _validate_run_inputs(
    payload: dict[str, Any],
) -> dict | tuple[str, str, str, float]:
    """Pure: validate ``language``/``code``/``stdin``/``timeout_seconds``; returns parsed bag or error envelope."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    language = str(payload.get("language") or "").strip().lower()
    if not language:
        return _err(
            "multi_language_executor.missing_language",
            f"language is required. Supported: {', '.join(_SUPPORTED)}",
        )
    if language not in _RUNNERS:
        return _err(
            "multi_language_executor.unsupported_language",
            f"Unsupported language '{language}'. Supported: {', '.join(_SUPPORTED)}",
        )
    available = _available_runtimes()
    if language not in available:
        return _err(
            "multi_language_executor.tool_unavailable",
            f"{language} is not available on this executor. "
            f"Available languages: {', '.join(available_languages()) or 'none'}",
        )
    code = str(payload.get("code") or "").strip()
    if not code:
        return _err("multi_language_executor.missing_code", "code is required.")
    if len(code) > _MAX_CODE_CHARS:
        return _err(
            "multi_language_executor.code_too_long",
            f"code must be <= {_MAX_CODE_CHARS} characters.",
        )
    safe, reason = _is_code_network_safe(language, code)
    if not safe:
        return _err("multi_language_executor.blocked_unsafe_code", reason or "")
    stdin = str(payload.get("stdin") or "")
    timeout = float(min(float(payload.get("timeout_seconds") or _DEFAULT_TIMEOUT_S), _TIMEOUT_MAX))
    return language, code, stdin, timeout


def _shape_run_response(language: str, result: dict[str, Any]) -> dict[str, Any]:
    """Pure: project a runner's ``result`` dict into the agent's response shape.

    ``error_kind`` is "compile" when a runner's compile phase failed
    (rustc/tsc branches, go diagnostics), "runtime" for any other nonzero
    exit, and None on success — so callers can route syntax errors to a
    code-fix loop without parsing stderr themselves.
    """
    exit_code = result.get("exit_code", -1)
    error_kind = result.get("error_kind")
    if error_kind is None and exit_code != 0:
        error_kind = "runtime"
    return {
        "language": language,
        "runtime": result.get("runtime", language),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "exit_code": exit_code,
        "passed": exit_code == 0,
        "error_kind": error_kind,
        "execution_time_ms": result.get("elapsed_ms", 0),
    }


def _is_runner_timeout(result: dict[str, Any]) -> bool:
    """Pure: True when a runner's exit code or stderr signals a kill-on-timeout.

    Why: a SIGTERM-killed process must surface as a structured timeout
    error so the settlement layer refunds, not as a successful run.
    """
    if int(result.get("exit_code", -1)) == _TIMEOUT_EXIT_CODE:
        return True
    return "timed out" in str(result.get("stderr", "")).lower()


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute code in a sandboxed subprocess for the specified language.

    Why: a single agent that supports node/deno/bun/go/rust amortises the
    sandbox/SSRF wiring across runtimes; the per-language helper picks the
    best available runtime at call time so the deployment image only
    needs whichever interpreters operations chose to install.
    """
    parsed = _validate_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed
    language, code, stdin, timeout = parsed
    # Last-resort envelope: any unanticipated runner crash (subprocess SIGKILL,
    # binary stdout that breaks the parser, OOM) must surface as a structured
    # error so the platform refunds rather than returning an opaque HTTP 502.
    try:
        result = _RUNNERS[language](code, stdin, timeout)
    except Exception as exc:  # noqa: BLE001
        return _err("multi_language_executor.runner_failed", f"{language} runner crashed: {exc}")
    if "error" in result and isinstance(result.get("error"), dict):
        return result
    if _is_runner_timeout(result):
        return _err(
            "multi_language_executor.timeout",
            f"Execution timed out after {timeout}s. No partial output billed.",
        )
    return _shape_run_response(language, result)
