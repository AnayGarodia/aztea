# OWNS: Reproduce CI failures by actually running the failing command in a
#       clean sandbox, classify the failure type, and return a structured
#       diagnosis with a concrete fix suggestion.
# NOT OWNS: Fixing the code (callers own that); CI provider integration;
#            network-isolated execution (sandbox has outbound access for
#            package installs — note this in output).
# INVARIANTS:
#   - Never run a command that matches _BLOCKED_COMMAND_RE.
#   - Total execution time across all commands never exceeds _MAX_TOTAL_TIMEOUT.
#   - Log input is rejected above _MAX_LOG_BYTES.
#   - working_dir_files is capped at _MAX_FILES files and _MAX_TOTAL_FILE_BYTES total.
# DECISIONS:
#   - Shell=True is used so the exact CI command string runs as-is; the
#     blocked-command regex is the safety layer rather than an allow-list.
#   - LLM synthesis is optional — if unavailable, pattern-matched diagnosis
#     is returned from _classify_failure so the agent is always useful.
# KNOWN DEBT:
#   - No network isolation in the subprocess; dependency installs work but a
#     malicious payload could make outbound calls.

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile

# Resource limits for the sandboxed CI command. Defense-in-depth alongside
# the regex command blocklist and wall-clock timeout. RLIMIT_NPROC is
# intentionally NOT applied — macOS counts the user's total process count
# against the limit, breaking shell→language-runtime→test chains in normal
# dev sessions. RLIMIT_AS and RLIMIT_FSIZE provide the load-bearing guards.
_SUBPROCESS_RLIMIT_AS_BYTES = 1024 * 1024 * 1024  # 1 GB
_SUBPROCESS_RLIMIT_FSIZE_BYTES = 128 * 1024 * 1024  # 128 MB


def _apply_subprocess_rlimits() -> None:
    if os.name != "posix":
        return
    try:
        import resource as _resource
    except ImportError:
        return
    for kind, limit in (
        (_resource.RLIMIT_AS, _SUBPROCESS_RLIMIT_AS_BYTES),
        (_resource.RLIMIT_FSIZE, _SUBPROCESS_RLIMIT_FSIZE_BYTES),
    ):
        try:
            _resource.setrlimit(kind, (limit, limit))
        except (ValueError, OSError):
            pass
import time
from typing import Any
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

from core.llm import CompletionRequest, Message, run_with_fallback

# ── limits ────────────────────────────────────────────────────────────────────
_MAX_LOG_BYTES = 50_000
_MAX_FILES = 20
_MAX_TOTAL_FILE_BYTES = 100_000
_MAX_FILE_BYTES = 50_000
_DEFAULT_TIMEOUT = 30
_MAX_SINGLE_TIMEOUT = 120
_MAX_TOTAL_TIMEOUT = 120
_MAX_OUTPUT_CHARS = 3_000

# ── failure type constants ─────────────────────────────────────────────────────
_FT_CODE = "code_error"
_FT_DEP = "dependency_error"
_FT_ENV = "env_error"
_FT_CONFIG = "config_error"
_FT_FLAKY = "flaky_test"
_FT_TIMEOUT = "timeout"
_FT_UNKNOWN = "unknown"

# ── dangerous command patterns — blocked before execution ──────────────────────
# These are best-effort; the subprocess still runs with the caller's file system.
# Goal: prevent accidental catastrophic commands, not adversarial bypass.
_BLOCKED_COMMAND_RE = re.compile(
    r"(?i)"
    r"rm\s+-[^\s]*r[^\s]*\s+/|"      # rm -rf /
    r"\bdd\b.+of=/dev/[sh]d|"        # dd targeting a block device
    r":\(\).*:\|:&|"                  # fork bomb :(){ :|:& };:
    r"mkfs\b|"                        # filesystem format
    r">\s*/dev/sd|"                   # overwrite block device
    r"shutdown\s|reboot\s|halt\s"     # host power commands
)

# ── CI log command extraction patterns ────────────────────────────────────────
# Ordered most-specific first.  Each pattern captures one group: the command text.
_CI_COMMAND_PATTERNS = [
    # GitHub Actions:  "  Run pytest tests/"
    re.compile(r"^\s{0,6}Run\s+(.+)$", re.MULTILINE),
    # Shell prompt lines:  "$ pytest tests/"  or  "+ pytest tests/"
    re.compile(r"^(?:\$|\+)\s+(.+)$", re.MULTILINE),
    # CircleCI bash header
    re.compile(r"^#!/bin/bash\s*-eo\s+pipefail\s*\n(.+)$", re.MULTILINE),
    # Travis  "The command \"...\" exited with ..."
    re.compile(r'The command "(.+?)" exited with', re.IGNORECASE),
    # Bare pytest / python -m / npm / pip invocations anywhere in the log
    re.compile(
        r"^((?:pytest|python\s+-m\s+\S+|npm\s+(?:test|run\s+\S+)|pip\s+install\s+\S+"
        r"|go\s+test\s+\S*|cargo\s+test)\b.*)$",
        re.MULTILINE,
    ),
]

# 2026-05-18 (A6): test-runner output patterns. Callers often paste the
# pytest / jest / go-test SUMMARY without the triggering shell command —
# the previous extractor matched only commands and bailed with no_commands
# for the most common case. These patterns let us infer the runner from
# its output format so a vanilla "FAILED tests/foo.py::bar" log is
# reproducible.
_PYTEST_OUTPUT_PATTERN = re.compile(
    r"^(?:FAILED|PASSED|ERROR)\s+([\w./-]+\.py)(?:::|\s|$)",
    re.MULTILINE,
)
_JEST_OUTPUT_PATTERN = re.compile(
    r"^(?:FAIL|PASS)\s+([\w./-]+\.(?:test|spec)\.(?:js|jsx|ts|tsx))",
    re.MULTILINE,
)
_GO_TEST_OUTPUT_PATTERN = re.compile(
    r"^---\s+FAIL:\s+\w+",
    re.MULTILINE,
)


def _infer_command_from_output(
    log: str, language: str | None, working_dir_files: list[dict[str, Any]] | None,
) -> str | None:
    """Pure: infer a likely test command from pytest/jest/go output patterns.

    Used as a fallback when ``_extract_commands_from_log`` returns empty:
    the log may contain only test-runner SUMMARY lines (e.g. pytest's
    "FAILED tests/x.py::t") without the triggering ``$ pytest tests/x.py``.
    We prefer language-specific runner inference over guessing.
    """
    lang = (language or "").strip().lower()
    if _PYTEST_OUTPUT_PATTERN.search(log) or lang == "python":
        return "pytest"
    if _JEST_OUTPUT_PATTERN.search(log) or lang in ("javascript", "typescript", "node"):
        return "npm test"
    if _GO_TEST_OUTPUT_PATTERN.search(log) or lang == "go":
        return "go test ./..."
    # Last-resort filename inference: a single python test file with no
    # explicit language hint is almost always pytest.
    files = working_dir_files or []
    if any(
        str(f.get("name", "")).startswith(("test_", "tests/")) and
        str(f.get("name", "")).endswith(".py")
        for f in files
    ):
        return "pytest"
    return None

# ── failure classification heuristics ─────────────────────────────────────────
_DEP_PATTERNS = re.compile(
    r"ModuleNotFoundError|No module named|Cannot find module"
    r"|npm ERR!|pip.*(?:ERROR|Failed)\b|ImportError"
    r"|Package .* not found|no such package",
    re.IGNORECASE,
)
_ENV_PATTERNS = re.compile(
    r"KeyError.*environ|Environment variable .* not set|\$\w+ is not set"
    r"|MISSING.*env|env.*not found",
    re.IGNORECASE,
)
_CONFIG_PATTERNS = re.compile(
    r"No such file or directory.*(?:\.ya?ml|\.json|\.toml|\.cfg|\.ini)"
    r"|Invalid.*config|ParseError|yaml\.scanner|json\.decoder",
    re.IGNORECASE,
)
_SYNTAX_PATTERNS = re.compile(
    r"SyntaxError|IndentationError|unexpected token|Unexpected identifier",
    re.IGNORECASE,
)

# ── LLM prompt ────────────────────────────────────────────────────────────────
_DIAGNOSIS_SYSTEM = """\
You are a CI/CD debugging expert. You will be given a failing CI command,
its stderr output, and a pre-classified failure type. Return exactly two
sections with no markdown headers:

DIAGNOSIS: (2-3 sentences explaining WHY the failure occurred)
FIX: (one concrete, actionable fix — a command, config change, or code edit)

Be specific. Do not repeat the failure type. Do not guess if the data is
insufficient — say so in the DIAGNOSIS section."""


# ── helpers ───────────────────────────────────────────────────────────────────


def _trunc(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n...[truncated {len(text) - limit} chars]...\n" + text[-half:]


def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _node_version() -> str:
    try:
        r = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() if r.returncode == 0 else "not found"
    except Exception:
        return "not found"


def _environment_info() -> dict:
    return {
        "python_version": _python_version(),
        "node_version": _node_version(),
        "os": platform.system(),
    }


def _extract_commands_from_log(log: str) -> list[str]:
    """Return candidate commands found in the CI log, deduped and ordered."""
    seen: set[str] = set()
    commands: list[str] = []
    for pattern in _CI_COMMAND_PATTERNS:
        for match in pattern.finditer(log):
            cmd = match.group(1).strip()
            # Skip comment lines and very short strings
            if not cmd or cmd.startswith("#") or len(cmd) < 4:
                continue
            if cmd not in seen:
                seen.add(cmd)
                commands.append(cmd)
    return commands


def _is_blocked(cmd: str) -> bool:
    return bool(_BLOCKED_COMMAND_RE.search(cmd))


def _classify_failure(exit_code: int, stdout: str, stderr: str) -> str:
    combined = stderr + "\n" + stdout
    if exit_code == 124 or "timed out" in combined.lower():
        return _FT_TIMEOUT
    if _DEP_PATTERNS.search(combined):
        return _FT_DEP
    if _ENV_PATTERNS.search(combined):
        return _FT_ENV
    if _CONFIG_PATTERNS.search(combined):
        return _FT_CONFIG
    if _SYNTAX_PATTERNS.search(combined):
        return _FT_CODE
    if exit_code != 0 and re.search(r"FAILED|AssertionError|Error:", combined):
        return _FT_CODE
    if exit_code != 0:
        return _FT_UNKNOWN
    return _FT_UNKNOWN


_STATIC_DIAGNOSES: dict[str, tuple[str, str]] = {
    _FT_ENV: (
        "A required environment variable is missing or undefined. "
        "The command references a variable that was not set before execution.",
        "Export the missing variable in your CI environment configuration "
        "(e.g. GitHub Actions `env:` block or CircleCI `environment:` key).",
    ),
    _FT_CONFIG: (
        "A configuration file expected by the command is absent or malformed. "
        "The process could not parse its configuration before starting work.",
        "Ensure the config file is committed to the repo and the working directory "
        "is correct when the command runs.",
    ),
    _FT_TIMEOUT: (
        "The command exceeded its time limit and was killed. "
        "This can indicate an infinite loop, a hanging network call, or a genuinely slow operation.",
        "Increase the timeout limit, add explicit timeouts to network calls, "
        "or isolate which test/step is slow with `--timeout` flags.",
    ),
    _FT_CODE: (
        "A test assertion or runtime error caused the command to exit non-zero. "
        "Check the stderr output above for the specific failure.",
        "Fix the failing assertion or exception shown in stderr. "
        "Run the exact failing test locally to reproduce.",
    ),
}
_UNKNOWN_DIAGNOSIS: tuple[str, str] = (
    "The command exited with a non-zero code but the cause could not be "
    "automatically classified from the output.",
    "Inspect the stderr output above for clues. "
    "Try running the command locally with verbose flags.",
)
_DEP_FIRST_LINE_FALLBACK_CHARS = 120


def _diagnose_dep_failure(stderr: str) -> tuple[str, str]:
    """Pure: tailored diagnosis when the failure regex caught a missing-dependency line."""
    first_line = next(
        (ln for ln in stderr.splitlines() if _DEP_PATTERNS.search(ln)),
        stderr[:_DEP_FIRST_LINE_FALLBACK_CHARS],
    )
    return (
        f"A required package or module is missing: {first_line.strip()!r}. "
        "The dependency is referenced in code but not installed in the environment.",
        "Run the appropriate install command (e.g. `pip install -r requirements.txt` "
        "or `npm install`) and ensure it runs before the test step.",
    )


def _fallback_diagnosis(failure_type: str, stderr: str) -> tuple[str, str]:
    """Pure: pattern-match diagnosis used when the LLM is unavailable."""
    if failure_type == _FT_DEP:
        return _diagnose_dep_failure(stderr)
    return _STATIC_DIAGNOSES.get(failure_type, _UNKNOWN_DIAGNOSIS)


# When the failing command's own output already tells the full story, hitting
# an LLM only buys hedging language. We skip the LLM and return a deterministic
# one-liner derived from the offending stderr/stdout fragment. Caller pays less
# AND gets a cleaner answer. Conservative — only matches well-known patterns
# whose stderr is self-explanatory; flaky/dep/timeout failures still go to LLM
# because there the LLM genuinely adds context the raw output doesn't.
_SELF_EXPLANATORY_PATTERNS = (
    # Python assertion / exception with the inline value:
    re.compile(r"\bAssertionError\b[:\s]*.{0,200}"),
    # pytest-style: "assert X == Y" line
    re.compile(r"^\s*assert\s+.{1,200}", re.MULTILINE),
    # JS-style assertion with explicit values
    re.compile(r"\bAssertionError\b:\s*expected\s+.{0,200}"),
)


def _is_self_explanatory(failure_type: str, stdout: str, stderr: str) -> str | None:
    """Return the matching fragment if the failure speaks for itself, else None.

    Only triggers on `code_error` — for `dependency_error`, `flaky_test`,
    `timeout`, `env_error` etc., the LLM still earns its keep because the
    diagnosis depends on context the raw output doesn't carry.
    """
    if failure_type != _FT_CODE:
        return None
    haystack = (stderr or "") + "\n" + (stdout or "")
    for pat in _SELF_EXPLANATORY_PATTERNS:
        match = pat.search(haystack)
        if match:
            fragment = match.group(0).strip().splitlines()[-1].strip()
            if fragment:
                return fragment[:200]
    return None


def _llm_diagnosis(
    failing_command: str, stderr: str, failure_type: str
) -> tuple[str, str]:
    """Return (diagnosis, suggested_fix) from LLM, falling back to pattern match."""
    prompt = (
        f"Failing command: {failing_command}\n"
        f"Failure type: {failure_type}\n"
        f"stderr (last 2000 chars):\n{stderr[-2000:]}"
    )
    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=_DIAGNOSIS_SYSTEM),
            Message(role="user", content=prompt),
        ],
        temperature=0.1,
        max_tokens=350,
    )
    try:
        raw = run_with_fallback(req)
        text = raw.text.strip()
        diagnosis = ""
        suggested_fix = ""
        for line in text.splitlines():
            if line.startswith("DIAGNOSIS:"):
                diagnosis = line[len("DIAGNOSIS:"):].strip()
            elif line.startswith("FIX:"):
                suggested_fix = line[len("FIX:"):].strip()
        # If the LLM didn't follow the format exactly, use the whole response
        if not diagnosis:
            diagnosis = text[:400]
        if not suggested_fix:
            suggested_fix = "See diagnosis above."
        return diagnosis, suggested_fix
    except Exception:
        _LOG.warning("LLM diagnosis failed for ci_failure_reproducer", exc_info=True)
        return _fallback_diagnosis(failure_type, stderr)


def _write_working_files(tmpdir: str, files: list[dict]) -> str | None:
    """Write caller-supplied files into tmpdir. Returns error string or None."""
    total_bytes = 0
    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        content = str(entry.get("content") or "")
        if not name:
            continue
        if len(content) > _MAX_FILE_BYTES:
            return f"File '{name}' exceeds {_MAX_FILE_BYTES} byte limit."
        total_bytes += len(content)
        if total_bytes > _MAX_TOTAL_FILE_BYTES:
            return f"Total working_dir_files size exceeds {_MAX_TOTAL_FILE_BYTES} bytes."
        # Block path traversal
        full = os.path.realpath(os.path.join(tmpdir, name))
        if not full.startswith(os.path.realpath(tmpdir)):
            return f"File path '{name}' is outside sandbox (path traversal rejected)."
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    return None


def _run_command(
    cmd: str, tmpdir: str, timeout: int
) -> dict[str, Any]:
    """Run a single shell command and return timing + output."""
    start = time.monotonic()
    timed_out = False
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=_apply_subprocess_rlimits if os.name == "posix" else None,
        )
        stdout = result.stdout
        stderr = result.stderr
        exit_code = result.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stderr += f"\nCommand timed out after {timeout}s."
        exit_code = 124
        timed_out = True
    except Exception as exc:
        stdout = ""
        stderr = str(exc)
        exit_code = 1
    duration_ms = int((time.monotonic() - start) * 1000)
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }


# ── public entry point ─────────────────────────────────────────────────────────

def _normalize_run_inputs(
    payload: dict,
) -> dict | tuple[list[str], int, list[dict]]:
    """Pure: validate ``log``/``commands``/``timeout_seconds``/``working_dir_files``."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    log = payload.get("log")
    if not log or not isinstance(log, str):
        return _err("ci_failure_reproducer.no_log", "Field 'log' is required.")
    if len(log.encode("utf-8", errors="replace")) > _MAX_LOG_BYTES:
        return _err(
            "ci_failure_reproducer.log_too_large",
            f"Log must be under {_MAX_LOG_BYTES // 1000}KB.",
        )
    raw_commands: list[str] | None = payload.get("commands")
    if raw_commands and isinstance(raw_commands, list):
        commands = [str(c).strip() for c in raw_commands if str(c).strip()]
    else:
        commands = _extract_commands_from_log(log)
    working_dir_files: list[dict] = payload.get("working_dir_files") or []
    if not isinstance(working_dir_files, list):
        working_dir_files = []
    if len(working_dir_files) > _MAX_FILES:
        return _err(
            "ci_failure_reproducer.too_many_files",
            f"working_dir_files must not exceed {_MAX_FILES} entries.",
        )
    if not commands:
        # 2026-05-18 (A6): fall back to inferring the test runner from
        # pytest/jest/go output patterns or the language hint. Previously
        # this path returned no_commands for the most common case — a
        # caller pasting just the pytest FAILED summary lines.
        language = payload.get("language")
        inferred = _infer_command_from_output(log, language, working_dir_files)
        if inferred is not None:
            commands = [inferred]
    if not commands:
        return _err(
            "ci_failure_reproducer.no_commands",
            "Could not extract commands from log and none provided. "
            "Pass an explicit `commands` array or a `language` hint "
            "(python|javascript|typescript|go) so the runner can be inferred.",
        )
    try:
        per_cmd_timeout = max(
            1, min(int(payload.get("timeout_seconds") or _DEFAULT_TIMEOUT), _MAX_SINGLE_TIMEOUT)
        )
    except (TypeError, ValueError):
        per_cmd_timeout = _DEFAULT_TIMEOUT
    return commands, per_cmd_timeout, working_dir_files


def _execute_commands(
    tmpdir: str, commands: list[str], per_cmd_timeout: int,
) -> tuple[list[dict], dict | None]:
    """Side-effect: run ``commands`` sequentially; returns ``(commands_tried, first_failure_or_None)``."""
    commands_tried: list[dict] = []
    first_failure: dict | None = None
    total_elapsed = 0
    for cmd in commands:
        if _is_blocked(cmd):
            _LOG.warning("ci_failure_reproducer: blocked dangerous command: %s", cmd)
            continue
        remaining = _MAX_TOTAL_TIMEOUT - total_elapsed
        if remaining <= 0:
            break
        timeout = min(per_cmd_timeout, remaining)
        outcome = _run_command(cmd, tmpdir, timeout)
        total_elapsed += outcome["duration_ms"] // 1000
        commands_tried.append({
            "command": cmd,
            "exit_code": outcome["exit_code"],
            "duration_ms": outcome["duration_ms"],
        })
        if outcome["exit_code"] != 0 and first_failure is None:
            first_failure = {"command": cmd, **outcome}
    return commands_tried, first_failure


def _all_passed_response(commands_tried: list[dict]) -> dict:
    """Pure: response shape when every command passed in the sandbox."""
    last = commands_tried[-1]
    return {
        "failure_type": _FT_FLAKY,
        "failing_command": last["command"],
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "diagnosis": (
            "All commands passed in the sandbox. "
            "The failure may be environment-specific or a flaky test "
            "that does not reproduce consistently."
        ),
        "suggested_fix": (
            "Retry the CI job to check for flakiness. "
            "Compare sandbox environment variables with CI environment."
        ),
        "reproduction_command": last["command"],
        "commands_tried": commands_tried,
        "environment": _environment_info(),
    }


def _diagnose_failure(
    tmpdir: str, first_failure: dict, commands_tried: list[dict], per_cmd_timeout: int,
) -> dict:
    """Side-effect: classify + diagnose; rerun the command once on _FT_CODE to detect flake."""
    failing_command = first_failure["command"]
    exit_code = first_failure["exit_code"]
    stdout_raw = first_failure["stdout"]
    stderr_raw = first_failure["stderr"]
    failure_type = _classify_failure(exit_code, stdout_raw, stderr_raw)
    if failure_type == _FT_CODE:
        rerun = _run_command(failing_command, tmpdir, per_cmd_timeout)
        if rerun["exit_code"] == 0:
            failure_type = _FT_FLAKY
    # Short-circuit the LLM when the failure speaks for itself — sparing the
    # caller from the hedging-language tax. Only matches obvious AssertionError-
    # style failures; everything else still goes through the LLM path.
    self_explanatory = _is_self_explanatory(failure_type, stdout_raw, stderr_raw)
    if self_explanatory is not None:
        diagnosis = f"Assertion failed: {self_explanatory}"
        suggested_fix = (
            "The failing assertion is shown above. Fix the code or the test "
            "so the expected and actual values match."
        )
    else:
        diagnosis, suggested_fix = _llm_diagnosis(failing_command, stderr_raw, failure_type)
    return {
        "failure_type": failure_type,
        "failing_command": failing_command,
        "exit_code": exit_code,
        "stdout": _trunc(stdout_raw),
        "stderr": _trunc(stderr_raw),
        "diagnosis": diagnosis,
        "suggested_fix": suggested_fix,
        "reproduction_command": failing_command,
        "commands_tried": commands_tried,
        "environment": _environment_info(),
    }


def run(payload: dict) -> dict:
    """Reproduce a CI failure by running the failing command in a clean sandbox.

    Why: the agent reproduces the failure locally so the LLM-side diagnosis
    has fresh stderr to ground its suggested fix; ``working_dir_files`` lets
    callers attach the few files needed to make the command runnable.
    """
    parsed = _normalize_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed
    commands, per_cmd_timeout, working_dir_files = parsed
    tmpdir = tempfile.mkdtemp(prefix="aztea_ci_")
    try:
        if working_dir_files:
            write_err = _write_working_files(tmpdir, working_dir_files)
            if write_err:
                return _err("ci_failure_reproducer.invalid_files", write_err)
        commands_tried, first_failure = _execute_commands(tmpdir, commands, per_cmd_timeout)
        if first_failure is None:
            if not commands_tried:
                return _err(
                    "ci_failure_reproducer.no_commands",
                    "All extracted commands were blocked or none ran.",
                )
            return _all_passed_response(commands_tried)
        return _diagnose_failure(tmpdir, first_failure, commands_tried, per_cmd_timeout)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
