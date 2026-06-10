from __future__ import annotations

from types import SimpleNamespace

from agents import multi_language_executor
from agents import python_executor
from core.executor_sandbox import build_subprocess_env


def test_build_subprocess_env_strips_host_secrets(monkeypatch):
    monkeypatch.setenv("AZTEA_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/home/aztea/app/venv/bin:/usr/bin")
    env = build_subprocess_env()
    # 2026-05-18 (D12): parent PATH no longer leaks. The sanitised default
    # is used so the venv prefix can't reach the child process.
    assert env["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    assert "/home/aztea/app/venv/bin" not in env["PATH"]
    assert "AZTEA_API_KEY" not in env


def test_build_subprocess_env_caller_can_override_path(monkeypatch):
    """Explicit extra_env override beats the sanitised default."""
    monkeypatch.setenv("PATH", "/home/aztea/venv/bin")
    env = build_subprocess_env(extra_env={"PATH": "/explicit/path"})
    assert env["PATH"] == "/explicit/path"


def test_build_subprocess_env_drops_home(monkeypatch):
    """Regression: HOME was leaking `/home/aztea` to user code via ``process.env``."""
    monkeypatch.setenv("HOME", "/home/aztea")
    env = build_subprocess_env()
    assert "HOME" not in env


def test_multi_language_executor_uses_sanitized_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setattr(multi_language_executor, "_which", lambda name: "/usr/bin/node" if name == "node" else None)
    captured: dict[str, str] = {}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, cwd=None, env=None, **kwargs):
        del cmd, input, capture_output, text, timeout, cwd, kwargs
        captured.update(env or {})
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(multi_language_executor.subprocess, "run", fake_run)
    result = multi_language_executor.run({"language": "javascript", "code": "console.log('ok')"})
    assert result["exit_code"] == 0
    assert "GITHUB_TOKEN" not in captured


def test_multi_language_executor_available_languages_reflect_installed_runtimes(monkeypatch):
    monkeypatch.setattr(
        multi_language_executor,
        "_which",
        lambda name: {
            "node": "/usr/bin/node",
            "tsc": "/usr/bin/tsc",
            "go": "/usr/bin/go",
        }.get(name),
    )
    assert multi_language_executor.available_languages() == ["go", "javascript", "typescript"]


def test_multi_language_executor_typescript_uses_tsc_plus_node(monkeypatch):
    monkeypatch.setattr(
        multi_language_executor,
        "_which",
        lambda name: {
            "node": "/usr/bin/node",
            "tsc": "/usr/bin/tsc",
        }.get(name),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, cwd=None, env=None, **kwargs):
        del input, capture_output, text, timeout, env, kwargs
        calls.append(cmd)
        if cmd[0] == "/usr/bin/tsc":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[0] == "/usr/bin/node":
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        if cmd[:2] == ["/usr/bin/tsc", "--version"]:
            return SimpleNamespace(returncode=0, stdout="Version 5.6.3\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="Version 5.6.3\n", stderr="")

    monkeypatch.setattr(multi_language_executor.subprocess, "run", fake_run)
    result = multi_language_executor.run({"language": "typescript", "code": "export const x = 1; console.log(x);"})
    assert result["exit_code"] == 0
    assert result["stdout"] == "ok\n"
    assert result["runtime"].startswith("tsc+node")
    assert any(cmd[0] == "/usr/bin/tsc" and "--project" in cmd for cmd in calls)


def test_python_executor_subprocess_uses_sanitized_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret")
    captured: dict[str, str] = {}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, cwd=None, env=None, **kwargs):
        del cmd, input, capture_output, text, timeout, cwd, kwargs
        captured.update(env or {})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(python_executor.subprocess, "run", fake_run)
    result = python_executor.run({"code": "print('ok')", "explain": False})
    assert result["exit_code"] == 0
    assert "OPENAI_API_KEY" not in captured


def test_python_executor_static_blocks_memory_bombs():
    """Regression test for the 2026-05-07 power-user eval. The original cap
    only matched ``literal_seq * literal_int`` and missed every realistic
    40 MB allocation pattern (bytearray, os.urandom, multi-level multiply)."""
    blockers = [
        "x = 'a' * (40*1024*1024)",
        "x = b'A' * (40*1024*1024)",
        "x = 'a' * 41943040",
        "x = 'a' * (10**8)",
        "x = bytearray(40*1024*1024)",
        "x = bytes(40*1024*1024)",
        "import os; os.urandom(40*1024*1024)",
        "import secrets; secrets.token_bytes(40*1024*1024)",
        "x = [0] * (40*1024*1024)",
    ]
    for code in blockers:
        result = python_executor.run({"code": code, "timeout": 3, "explain": False})
        assert "error" in result, code
        assert result["error"]["code"] == "python_executor.memory_limit", code

    passers = [
        "x = 'a' * 1024",
        "x = bytearray(1024)",
        "import os; os.urandom(16)",
    ]
    for code in passers:
        # ``os`` and ``os.urandom`` are still blocked by the broader sandbox
        # (any os import is rejected); only assert the static cap doesn't
        # fire on the small-allocation cases that don't touch os.
        if "os." in code:
            continue
        result = python_executor.run({"code": code, "timeout": 3, "explain": False})
        # Either the run completed normally OR it failed for an unrelated
        # reason — but it MUST NOT be the static-cap rejection.
        if "error" in result:
            assert (
                result["error"]["code"] != "python_executor.memory_limit"
            ), f"static cap false-positive on {code!r}"


def test_python_executor_applies_kernel_rlimits():
    """Defense-in-depth: confirm RLIMIT_AS / NPROC / FSIZE / CPU are
    actually set inside the sandbox subprocess. The 2026-05-07 eval
    flagged that 40 MB allocations succeeded — once the static cap was
    fixed, a kernel-level cap is the backstop for dynamically-shaped
    allocations the static analyzer can't see."""
    import os
    import sys

    if os.name != "posix":
        return  # rlimits are POSIX-only; skip on Windows
    if not sys.platform.startswith("linux"):
        # macOS Mach kernel silently ignores RLIMIT_AS / RLIMIT_FSIZE /
        # RLIMIT_CPU set via preexec_fn; the call returns 0 but getrlimit
        # still reports RLIM_INFINITY. Production runs on Linux where the
        # rlimits are honored — verify the wiring works there.
        return

    code = (
        "import resource\n"
        "as_lim = resource.getrlimit(resource.RLIMIT_AS)\n"
        "fs_lim = resource.getrlimit(resource.RLIMIT_FSIZE)\n"
        "cpu_lim = resource.getrlimit(resource.RLIMIT_CPU)\n"
        "print(f'AS={as_lim[0]} FSIZE={fs_lim[0]} CPU={cpu_lim[0]}')\n"
    )
    result = python_executor.run({"code": code, "timeout": 5, "explain": False})
    stdout = result.get("stdout", "")
    assert "AS=" in stdout, result
    as_bytes = int(stdout.split("AS=")[1].split()[0])
    fs_bytes = int(stdout.split("FSIZE=")[1].split()[0])
    cpu_seconds = int(stdout.split("CPU=")[1].split()[0])
    assert 0 < as_bytes < 2 * 1024 * 1024 * 1024, f"AS={as_bytes}"
    assert 0 < fs_bytes < 1024 * 1024 * 1024, f"FSIZE={fs_bytes}"
    assert 0 < cpu_seconds < 600, f"CPU={cpu_seconds}"


def test_python_executor_runtime_memory_bomb_killed_by_rlimit():
    """If the static analyzer somehow misses an allocation pattern, the
    kernel RLIMIT_AS still kills the process. Use a dynamically-built
    integer (``int(open('/dev/urandom') ...)`` is blocked by the audit
    hook, so use a simple loop that confuses the static analyzer)."""
    code = (
        "buf = []\n"
        "while True:\n"
        "    buf.append(b'x' * 1_000_000)\n"  # 1 MB per iteration
    )
    result = python_executor.run({"code": code, "timeout": 8, "explain": False})
    # Either MemoryError, killed by signal, or timed out — anything except
    # "ran cleanly with the runaway buffer". The point is the sandbox
    # didn't let it eat the whole host.
    if "error" in result:
        # Static analyzer might catch this; either way it's contained.
        return
    assert result["exit_code"] != 0 or result.get("timed_out"), result


def test_python_executor_timeout_echoes_submitted_code():
    """A timed-out run must echo the (truncated) code so callers can debug
    the hang — without it, stderr says only "timed out" and the submission
    is gone."""
    code = "import time\ntime.sleep(30)\n"
    # explain=True deliberately: the timeout check must short-circuit the
    # explainer (status skipped_timeout) without ever invoking an LLM.
    result = python_executor.run({"code": code, "timeout": 1, "explain": True})
    assert result["timed_out"] is True, result
    assert "time.sleep(30)" in result.get("code_submitted", ""), result
    assert result["explanation_status"] == "skipped_timeout", result


def test_python_executor_success_omits_code_echo():
    """code_submitted is a timeout-debugging aid only; clean runs must not
    re-ship the submission back over the wire."""
    result = python_executor.run({"code": "print('hi')", "explain": False})
    assert result["exit_code"] == 0, result
    assert "code_submitted" not in result, result


def test_python_executor_explanation_status_disabled():
    result = python_executor.run({"code": "print('x')", "explain": False})
    assert result["explanation_status"] == "disabled", result
    assert result["explanation"] == ""


def test_python_executor_explanation_status_provider_failed(monkeypatch):
    """When every LLM provider fails, callers must see provider_failed —
    not an empty string indistinguishable from explain=False."""
    def _boom(*args, **kwargs):
        raise RuntimeError("no providers configured")

    monkeypatch.setattr(python_executor, "run_with_fallback", _boom)
    result = python_executor.run({"code": "print('x')", "explain": True})
    assert result["explanation_status"] == "provider_failed", result
    assert result["explanation"] == ""
    assert result["llm_used"] is False


def test_python_executor_explanation_status_ok(monkeypatch):
    class _FakeResponse:
        text = "Prints x to stdout."

    monkeypatch.setattr(
        python_executor, "run_with_fallback", lambda req: _FakeResponse()
    )
    result = python_executor.run({"code": "print('x')", "explain": True})
    assert result["explanation_status"] == "ok", result
    assert result["explanation"] == "Prints x to stdout."
    assert result["llm_used"] is True


def test_python_executor_static_alloc_limit_env_override(monkeypatch):
    """Operators can tune the pre-spawn allocation guard via env; the
    module reads it at import so we reload to exercise the parse+clamp."""
    import importlib

    monkeypatch.setenv("AZTEA_PYTHON_STATIC_ALLOC_LIMIT_MB", "16")
    reloaded = importlib.reload(python_executor)
    try:
        assert reloaded._STATIC_ALLOCATION_LIMIT_BYTES == 16 * 1024 * 1024
        result = reloaded.run(
            {"code": "buf = b'x' * (20 * 1024 * 1024)", "explain": False}
        )
        assert (result.get("error") or {}).get("code") == "python_executor.memory_limit"
    finally:
        monkeypatch.delenv("AZTEA_PYTHON_STATIC_ALLOC_LIMIT_MB")
        importlib.reload(python_executor)


def test_multi_language_pre_filter_blocks_go_process_spawn():
    """Go could shell out (exec.Command("curl", ...)) and reach the network
    indirectly, sidestepping the SSRF pre-filter. Now rejected up-front
    with a process-spawn-specific message."""
    safe, reason = multi_language_executor._is_code_network_safe(
        "go",
        'package main\nimport "os/exec"\nfunc main() { exec.Command("sh", "-c", "id").Run() }',
    )
    assert safe is False
    assert "spawns external processes" in (reason or "")


def test_multi_language_pre_filter_blocks_rust_process_spawn():
    safe, reason = multi_language_executor._is_code_network_safe(
        "rust",
        'use std::process::Command;\nfn main() { Command::new("curl").status().unwrap(); }',
    )
    assert safe is False
    assert "spawns external processes" in (reason or "")


def test_multi_language_spawn_block_does_not_hit_other_languages():
    """The Go/Rust spawn patterns must not reject unrelated JS code that
    merely mentions 'Command' in a string or identifier."""
    safe, reason = multi_language_executor._is_code_network_safe(
        "javascript", 'const Command = {new: () => 1}; console.log(Command.new());'
    )
    assert safe is True, reason


def test_multi_language_network_message_distinct_from_spawn_message():
    safe, reason = multi_language_executor._is_code_network_safe(
        "go", 'package main\nimport "net/http"\nfunc main() { http.Get("https://example.com") }'
    )
    assert safe is False
    assert "network-capable API surface" in (reason or "")


def test_multi_language_rustc_compile_error_classified(monkeypatch):
    monkeypatch.setattr(
        multi_language_executor,
        "_which",
        lambda name: "/usr/bin/rustc" if name == "rustc" else None,
    )

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, cwd=None, env=None, **kwargs):
        del input, capture_output, text, timeout, env, kwargs
        if cmd[0] == "/usr/bin/rustc" and "-o" in cmd:
            return SimpleNamespace(
                returncode=1, stdout="", stderr="error[E0425]: cannot find value `x`"
            )
        return SimpleNamespace(returncode=0, stdout="rustc 1.77.0\n", stderr="")

    monkeypatch.setattr(multi_language_executor.subprocess, "run", fake_run)
    result = multi_language_executor.run(
        {"language": "rust", "code": "fn main() { println!(\"{}\", x); }"}
    )
    assert result["error_kind"] == "compile", result
    assert result["passed"] is False


def test_multi_language_go_compile_vs_runtime_classification(monkeypatch):
    monkeypatch.setattr(
        multi_language_executor,
        "_which",
        lambda name: "/usr/bin/go" if name == "go" else None,
    )

    responses = {"stderr": "./main.go:3:5: undefined: foo"}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, cwd=None, env=None, **kwargs):
        del input, capture_output, text, timeout, env, kwargs
        if cmd[:2] == ["/usr/bin/go", "run"]:
            return SimpleNamespace(returncode=1, stdout="", stderr=responses["stderr"])
        return SimpleNamespace(returncode=0, stdout="go version go1.22\n", stderr="")

    monkeypatch.setattr(multi_language_executor.subprocess, "run", fake_run)
    payload = {"language": "go", "code": "package main\nfunc main() { foo() }"}

    compile_result = multi_language_executor.run(payload)
    assert compile_result["error_kind"] == "compile", compile_result

    responses["stderr"] = "panic: runtime error: index out of range"
    runtime_result = multi_language_executor.run(payload)
    assert runtime_result["error_kind"] == "runtime", runtime_result


def test_multi_language_success_has_null_error_kind(monkeypatch):
    monkeypatch.setattr(
        multi_language_executor,
        "_which",
        lambda name: "/usr/bin/node" if name == "node" else None,
    )

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, cwd=None, env=None, **kwargs):
        del input, capture_output, text, timeout, env, kwargs
        return SimpleNamespace(returncode=0, stdout="hi\n", stderr="")

    monkeypatch.setattr(multi_language_executor.subprocess, "run", fake_run)
    result = multi_language_executor.run(
        {"language": "javascript", "code": "console.log('hi')"}
    )
    assert result["passed"] is True
    assert result["error_kind"] is None
