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
