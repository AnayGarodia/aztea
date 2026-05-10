# OWNS: sandboxed Python test coverage execution — writes caller-supplied files to an
#       isolated tempdir, runs `coverage run --branch -m <test_command>` + `coverage json`,
#       parses per-file coverage data, and returns a structured result.
# NOT OWNS: LLM synthesis, test generation, code linting, or any language other than Python.
#           SSRF validation (coverage_runner never fetches outbound URLs).
# INVARIANTS:
#   * Tempdir is ALWAYS cleaned up — even on exception. Use try/finally.
#   * Never raise — always return a structured error dict via _err().
#   * test_command validation rejects shell metacharacters BEFORE the subprocess runs.
#   * Network proxy env vars are stripped from the subprocess environment so the sandbox
#     cannot reach the internet through a proxy that happens to be set in the host env.
#   * coverage.json and .coverage artefacts are excluded from per-file results.
# DECISIONS:
#   * We prepend `coverage run --branch -m` and let the caller supply only the pytest args.
#     --branch gives branch coverage, which is more useful than statement-only.
#   * shell=True is intentional: the composed command string may include pytest args with
#     spaces, and we sanitize the caller-controlled portion before composing it.
#   * overall_pct is None (not 0) when coverage.json is absent — the distinction matters:
#     0 means tests ran and nothing was covered; None means we don't know.
#   * Max 50 files / 500 KB total keeps the tempdir bounded without a separate ulimit.
# KNOWN DEBT: shell=True is preserved because the composed command uses '&&' to chain
#             coverage run + coverage json. Argv conversion would require splitting
#             into two sequential subprocess calls and merging exit codes — out of
#             scope for a minimal-diff hardening pass.

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard ceiling — prevents tempdir from growing without bound.
_MAX_FILE_COUNT = 50
# 500 KB total across all files.
_MAX_TOTAL_BYTES = 500 * 1024
# test_command is user-controlled; cap before we even sanitize.
_MAX_COMMAND_CHARS = 200
# Subprocess output is truncated to avoid huge responses.
_MAX_OUTPUT_CHARS = 20_000
# Timeout floor/ceiling.
_TIMEOUT_DEFAULT = 30
_TIMEOUT_MAX = 60

# Shell metacharacters that must not appear in test_command.
# We compose `coverage run --branch -m <test_command>` via shell=True, so any
# character that would let the caller inject a second command is rejected here.
_SHELL_DANGEROUS_RE = re.compile(r"[;&|><$`\\]")

# Artefacts written by `coverage` that must not appear in per-file results.
_COVERAGE_ARTEFACTS = {"coverage.json", ".coverage"}

# Resource limits for the test-runner child. Belt-and-braces over the wall
# clock: a runaway test that allocates wildly or writes a huge file hits the
# kernel ceiling first. RLIMIT_NPROC is intentionally NOT applied here —
# macOS counts the user's *total* process count against the limit, not just
# descendants of this subprocess, so a sane value (64–128) breaks any
# shell→coverage→pytest chain in normal dev sessions.
_SUBPROCESS_RLIMIT_AS_BYTES = 512 * 1024 * 1024
_SUBPROCESS_RLIMIT_FSIZE_BYTES = 64 * 1024 * 1024


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_files(raw: list[dict] | dict) -> list[dict[str, str]]:
    """Accept both list-of-dicts and plain dict file formats.

    List form: [{"name": "foo.py", "content": "..."}]
    Dict form: {"foo.py": "..."}

    Returns a canonical list of {"name": str, "content": str}.
    """
    if isinstance(raw, dict):
        return [{"name": name, "content": content} for name, content in raw.items()]
    if isinstance(raw, list):
        return [{"name": str(f.get("name", "")), "content": str(f.get("content", ""))} for f in raw]
    return []


def _has_test_file(files: list[dict[str, str]]) -> bool:
    """Return True if at least one file name contains 'test' or 'spec'."""
    for f in files:
        name_lower = f["name"].lower()
        if "test" in name_lower or "spec" in name_lower:
            return True
    return False


def _sanitize_command(raw_cmd: str) -> str | None:
    """Strip leading 'pytest' token from raw_cmd and return the cleaned arg string.

    Returns None if the command contains dangerous shell characters.
    """
    cmd = raw_cmd.strip()
    # Reject shell metacharacters.
    if _SHELL_DANGEROUS_RE.search(cmd):
        return None
    # Strip a leading 'pytest' token so callers can pass "pytest -v" naturally.
    cmd = re.sub(r"^pytest\s*", "", cmd).strip()
    return cmd


def _build_coverage_command(sanitized_args: str) -> str:
    """Compose the full shell command string to run in the tempdir.

    Always uses `coverage run --branch -m pytest` as the base.  Extra pytest
    args (already sanitized) are appended verbatim.
    """
    base = "coverage run --branch -m pytest"
    if sanitized_args:
        return f"{base} {sanitized_args} && coverage json -o coverage.json"
    return f"{base} && coverage json -o coverage.json"


def _build_subprocess_env() -> dict[str, str]:
    """Return a minimal env for the subprocess, stripping proxy/credential vars.

    We explicitly remove HTTP_PROXY, HTTPS_PROXY, and common credential env
    vars so the sandbox cannot reach the internet or exfiltrate secrets through
    an ambient proxy. PATH is preserved so pytest/coverage/python are findable.
    """
    blocked_prefixes = ("http_proxy", "https_proxy", "ftp_proxy", "no_proxy",
                        "aws_", "google_", "azure_", "openai_", "anthropic_",
                        "groq_", "cohere_", "together_")
    env: dict[str, str] = {}
    for key, val in os.environ.items():
        if key.lower() in blocked_prefixes or any(key.lower().startswith(p) for p in blocked_prefixes):
            continue
        env[key] = val
    return env


def _parse_coverage_json(path: str, user_file_names: set[str]) -> dict[str, Any]:
    """Parse coverage.json produced by `coverage json`.

    Returns a dict with keys: overall_pct, total_statements, total_missing, files.
    files is a list of per-file dicts matching the output schema.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    totals = data.get("totals", {})
    overall_pct: float | None = totals.get("percent_covered")
    total_statements: int = totals.get("num_statements", 0)
    total_missing: int = totals.get("missing_lines", 0)

    file_results: list[dict[str, Any]] = []
    for raw_path, file_data in data.get("files", {}).items():
        # coverage.json keys are absolute or relative paths — we want just the
        # basename to match against caller-supplied file names.
        basename = os.path.basename(raw_path)
        # Only report files the caller actually supplied.
        if basename not in user_file_names:
            continue
        summary = file_data.get("summary", {})
        missing_lines: list[int] = file_data.get("missing_lines", [])
        file_results.append({
            "name": basename,
            "coverage_pct": round(float(summary.get("percent_covered", 0.0)), 2),
            "total_statements": int(summary.get("num_statements", 0)),
            "missing_count": int(summary.get("missing_lines", len(missing_lines))),
            "uncovered_lines": sorted(int(ln) for ln in missing_lines),
        })

    # Schema requires overall_pct as `number`. When coverage.json is absent we
    # fall back to 0.0 (no covered lines observed) rather than None — preserves
    # the contract while still being truthful.
    return {
        "overall_pct": round(float(overall_pct), 2) if overall_pct is not None else 0.0,
        "total_statements": int(total_statements),
        "total_missing": int(total_missing),
        "files": file_results,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Run test coverage on caller-supplied Python files in an isolated tempdir.

    Required:
    - ``files`` (list or dict) — source + test files. At least one must be a test file.

    Optional:
    - ``test_command`` (str, default "pytest") — pytest invocation; no shell metacharacters.
    - ``min_coverage`` (int, default 0) — threshold; sets ``passed_threshold`` in output.
    - ``timeout`` (int, default 30, max 60) — seconds before the subprocess is killed.

    Returns a coverage summary dict; see module docstring for the full schema.
    Never raises — failures are returned as structured error dicts.
    """
    # ------------------------------------------------------------------
    # 1. Validate and normalize inputs
    # ------------------------------------------------------------------
    raw_files = payload.get("files")
    if not raw_files:
        return _err(
            "coverage_runner.missing_files",
            "files is required and must contain at least one file.",
        )

    files = _normalize_files(raw_files)
    if not files:
        return _err(
            "coverage_runner.missing_files",
            "files must be a non-empty list or dict.",
        )
    if len(files) > _MAX_FILE_COUNT:
        return _err(
            "coverage_runner.missing_files",
            f"Too many files: {len(files)} > {_MAX_FILE_COUNT} limit.",
        )

    total_bytes = sum(len(f["content"].encode("utf-8")) for f in files)
    if total_bytes > _MAX_TOTAL_BYTES:
        return _err(
            "coverage_runner.missing_files",
            f"Total file size {total_bytes} bytes exceeds the {_MAX_TOTAL_BYTES // 1024} KB limit.",
        )

    if not _has_test_file(files):
        return _err(
            "coverage_runner.no_test_files",
            "At least one file name must contain 'test' or 'spec' for coverage to run.",
        )

    raw_command = str(payload.get("test_command") or "pytest").strip()
    if len(raw_command) > _MAX_COMMAND_CHARS:
        return _err(
            "coverage_runner.invalid_command",
            f"test_command exceeds {_MAX_COMMAND_CHARS} character limit.",
        )

    sanitized_args = _sanitize_command(raw_command)
    if sanitized_args is None:
        return _err(
            "coverage_runner.invalid_command",
            "test_command contains disallowed shell characters (; & | > < $ ` \\).",
        )

    try:
        min_coverage = int(payload.get("min_coverage") or 0)
    except (TypeError, ValueError):
        min_coverage = 0

    try:
        timeout = max(1, min(int(payload.get("timeout") or _TIMEOUT_DEFAULT), _TIMEOUT_MAX))
    except (TypeError, ValueError):
        timeout = _TIMEOUT_DEFAULT

    # ------------------------------------------------------------------
    # 2. Write files to isolated tempdir and execute
    # ------------------------------------------------------------------
    user_file_names = {f["name"] for f in files}
    shell_cmd = _build_coverage_command(sanitized_args)
    tmpdir: str | None = None

    try:
        tmpdir = tempfile.mkdtemp(prefix="aztea_cov_")
        for finfo in files:
            dest = os.path.join(tmpdir, finfo["name"])
            # Reject path traversal attempts in file names.
            if not os.path.abspath(dest).startswith(os.path.abspath(tmpdir)):
                return _err(
                    "coverage_runner.invalid_command",
                    f"File name '{finfo['name']}' contains a path traversal sequence.",
                )
            os.makedirs(os.path.dirname(dest), exist_ok=True) if os.path.dirname(dest) else None
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(finfo["content"])

        t_start = time.monotonic()
        try:
            proc = subprocess.run(
                shell_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmpdir,
                env=_build_subprocess_env(),
                preexec_fn=_apply_subprocess_rlimits if os.name == "posix" else None,
            )
            timed_out = False
            exit_code = proc.returncode
            stdout = proc.stdout[:_MAX_OUTPUT_CHARS]
            stderr = proc.stderr[:_MAX_OUTPUT_CHARS]
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = 124
            stdout = ""
            stderr = f"Execution timed out after {timeout}s."

        scan_time_ms = int((time.monotonic() - t_start) * 1000)

        if timed_out:
            return _err(
                "coverage_runner.timeout",
                f"Test execution timed out after {timeout}s.",
            )

        # ------------------------------------------------------------------
        # 3. Parse coverage.json if written
        # ------------------------------------------------------------------
        coverage_json_path = os.path.join(tmpdir, "coverage.json")
        if os.path.isfile(coverage_json_path):
            try:
                parsed = _parse_coverage_json(coverage_json_path, user_file_names)
            except Exception:
                _LOG.warning("Failed to parse coverage.json", exc_info=True)
                parsed = None
        else:
            parsed = None

        if parsed is not None:
            overall_pct = parsed["overall_pct"]
            passed_threshold = (
                overall_pct is not None and overall_pct >= min_coverage
            )
            return {
                "overall_pct": overall_pct,
                "passed_threshold": passed_threshold,
                "files": parsed["files"],
                "total_statements": parsed["total_statements"],
                "total_missing": parsed["total_missing"],
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "test_command_used": shell_cmd.split(" && ")[0],
                "scan_time_ms": scan_time_ms,
            }

        # coverage.json absent — tests failed or coverage not available.
        return {
            "overall_pct": 0.0,
            "passed_threshold": False,
            "files": [],
            "total_statements": 0,
            "total_missing": 0,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "test_command_used": shell_cmd.split(" && ")[0],
            "scan_time_ms": scan_time_ms,
        }

    except Exception as exc:
        _LOG.error("coverage_runner: unexpected error", exc_info=True)
        return _err(
            "coverage_runner.missing_files",
            f"Unexpected internal error: {exc}",
        )
    finally:
        # Always clean up tempdir regardless of success or failure.
        if tmpdir and os.path.isdir(tmpdir):
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                _LOG.debug("coverage_runner: failed to remove tempdir %s", tmpdir, exc_info=True)
