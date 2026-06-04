"""Smoke tests for the repo-root `./setup` installer script.

No side effects: we only exercise --help, argument validation, and --dry-run
(which prints the planned commands without executing them).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[1] / "setup"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SETUP), *args], capture_output=True, text=True
    )


def test_setup_exists():
    assert SETUP.exists(), f"missing {SETUP}"


def test_setup_bash_syntax_ok():
    result = subprocess.run(["bash", "-n", str(SETUP)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_setup_help_exits_zero():
    result = _run("--help")
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_setup_unknown_client_fails_fast():
    result = _run("--client", "emacs")
    assert result.returncode == 2
    assert "Unknown client" in result.stderr


def test_setup_dry_run_plans_without_executing():
    result = _run("--dry-run", "--client", "claude")
    assert result.returncode == 0, result.stderr
    assert "aztea mcp install --client claude --json" in result.stdout
    assert "pip install -e" in result.stdout
    assert "skipped in --dry-run" in result.stdout  # login not executed


def test_setup_dry_run_all_clients_loops():
    result = _run("--dry-run", "--client", "all")
    assert result.returncode == 0
    for client in ("claude", "cursor", "vscode", "windsurf", "codex"):
        assert f"--client {client} --json" in result.stdout


def test_setup_dry_run_pretool_block_passthrough():
    result = _run("--dry-run", "--client", "claude", "--pretool-block")
    assert result.returncode == 0
    assert "--pretool-block" in result.stdout


def test_setup_redacts_api_key_in_echo():
    secret = "az_supersecret_value_123"
    env = {**os.environ, "AZTEA_API_KEY": secret}
    result = subprocess.run(
        ["bash", str(SETUP), "--dry-run", "--client", "claude"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0
    assert secret not in result.stdout  # never echoed to logs/scrollback
    assert "--api-key ***" in result.stdout


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_setup_passes_shellcheck():
    result = subprocess.run(["shellcheck", str(SETUP)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
