from __future__ import annotations

from types import SimpleNamespace

from agents import multi_language_executor
from agents import python_executor
from core.executor_sandbox import build_subprocess_env


def test_build_subprocess_env_strips_host_secrets(monkeypatch):
    monkeypatch.setenv("AZTEA_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_subprocess_env()
    assert env["PATH"] == "/usr/bin"
    assert "AZTEA_API_KEY" not in env


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
