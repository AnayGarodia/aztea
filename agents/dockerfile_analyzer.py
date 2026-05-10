"""
dockerfile_analyzer.py — Static analysis of Dockerfiles via hadolint with regex fallback.

# OWNS: linting a caller-supplied Dockerfile string using hadolint when available,
#        falling back to regex-based heuristic checks when it is not, and computing
#        structural properties (pinned base image, root user, secret env vars).
# NOT OWNS: image vulnerability scanning (use dependency_auditor.py or cve_lookup.py),
#            running the image or any container runtime interaction, secrets detection
#            in source code (use secret_scanner.py).
# INVARIANTS:
#   - The Dockerfile is written to a fresh tempdir that is always cleaned up.
#   - No exception propagates out of run(); every failure returns a structured error dict.
#   - Structural checks always run regardless of which linting path was used.
# DECISIONS:
#   - We shell out to hadolint rather than a Python parsing library because hadolint
#     understands Dockerfile semantics (multi-stage builds, ARG inheritance, etc.)
#     far better than regex. The subprocess boundary keeps our process clean.
#   - The regex fallback is conservative: it only fires on obvious, high-signal patterns
#     to limit false positives in the absence of a real linter.
#   - Score is floored at 0 to prevent confusing negative numbers on error-heavy files.

Input:  {"dockerfile": str, "filename": str (optional, default "Dockerfile")}
Output: see run() docstring.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

# Subprocess timeout in seconds for hadolint invocations.
_HADOLINT_TIMEOUT = 15

# Score penalties per severity bucket.
_PENALTY_ERROR = 10
_PENALTY_WARNING = 5
_PENALTY_INFO = 1

# Maximum Dockerfile size we will accept (256 KB is far above any real Dockerfile).
_MAX_DOCKERFILE_BYTES = 262_144

# Map hadolint `level` strings to our canonical severity labels.
_HADOLINT_LEVEL_MAP: dict[str, str] = {
    "error":   "error",
    "warning": "warning",
    "info":    "info",
    "style":   "info",
    "ignore":  "info",
}

# Fix hints keyed on hadolint rule codes. Unknown rules get an empty string.
_RULE_FIX_HINTS: dict[str, str] = {
    "DL3007": "Pin to a specific image tag",
    "DL3008": "Pin apt-get package versions",
    "DL3009": "Delete apt-get lists after install",
    "DL3025": "Use JSON array for CMD/ENTRYPOINT",
    "DL4006": "Add pipefail to RUN with pipes",
    "SC2086": "Quote shell variables",
}

# Regex patterns for the fallback path.
# Each entry: (compiled pattern, severity, rule, message template).
# Patterns are applied line-by-line where that makes sense; multi-line patterns
# are applied on the full content.
_REGEX_LINE_CHECKS: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"^\s*FROM\s+\S+:latest\b", re.IGNORECASE),
        "warning", "DL3007", "Unpinned base image tag",
    ),
    (
        # FROM with no colon at all (no tag, no digest) — catches "FROM ubuntu" etc.
        re.compile(r"^\s*FROM\s+(?!.*:)(?!.*@)\S+\s*$", re.IGNORECASE),
        "warning", "DL3007", "Unpinned base image tag",
    ),
    (
        re.compile(r"^\s*ADD\s+(?!--chown)", re.IGNORECASE),
        "warning", "DL3020", "Use COPY instead of ADD for files",
    ),
    (
        re.compile(r"curl\s.*\|\s*(ba)?sh", re.IGNORECASE),
        "error", "SC2148", "Piping to shell is dangerous",
    ),
    (
        re.compile(r"wget\s.*\|\s*(ba)?sh", re.IGNORECASE),
        "error", "SC2148", "Piping to shell is dangerous",
    ),
    (
        re.compile(
            r"^\s*ENV\s+.*(PASSWORD|SECRET|TOKEN|KEY|API_KEY)\b",
            re.IGNORECASE,
        ),
        "error", "DL3044", "Possible secret in ENV",
    ),
    (
        re.compile(r"^\s*USER\s+root\b", re.IGNORECASE),
        "warning", "DL3002", "Do not switch to root user",
    ),
    (
        # RUN lines that chain commands with && but lack set -o pipefail or set -e.
        re.compile(r"^\s*RUN\s+(?!.*set\s+-[eo].*pipefail)(?!.*set\s+-e\b).*&&", re.IGNORECASE),
        "info", "DL4006", "Consider adding pipefail",
    ),
]

# Pattern to detect a valid non-latest tag (e.g. "ubuntu:22.04" or "@sha256:...").
_PINNED_TAG_RE = re.compile(
    r"^\s*FROM\s+\S+(?::(?!latest\b)\S+|@sha256:[0-9a-f]{64})\b",
    re.IGNORECASE,
)

# Pattern to detect any FROM instruction (for no-USER-after-FROM check).
_FROM_RE = re.compile(r"^\s*FROM\b", re.IGNORECASE)
_USER_RE = re.compile(r"^\s*USER\s+(\S+)", re.IGNORECASE)
_USER_ROOT_RE = re.compile(r"^\s*USER\s+root\b", re.IGNORECASE)
_ENV_SECRET_RE = re.compile(
    r"^\s*ENV\s+.*(PASSWORD|SECRET|TOKEN|KEY|API_KEY)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Tool availability
# ---------------------------------------------------------------------------

def _is_hadolint_available() -> bool:
    """Return True if hadolint is on PATH."""
    try:
        result = subprocess.run(
            ["which", "hadolint"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Structural checks (always run)
# ---------------------------------------------------------------------------

def _structural_checks(content: str) -> dict[str, bool]:
    """Derive pinned_base_image, runs_as_root, and has_secrets_in_env from raw content."""
    lines = content.splitlines()

    has_from = False
    has_pinned = False
    # Track the effective USER at the end of the last build stage.
    last_user: str | None = None
    has_secrets = False

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if _FROM_RE.match(line):
            has_from = True
            if _PINNED_TAG_RE.match(line):
                has_pinned = True
            # A new FROM resets the user context (new build stage).
            last_user = None

        m = _USER_RE.match(line)
        if m:
            last_user = m.group(1).lower()

        if _ENV_SECRET_RE.match(line):
            has_secrets = True

    pinned_base_image = has_from and has_pinned
    # runs_as_root when there is no USER at all or the last USER is root/0.
    runs_as_root = last_user is None or last_user in ("root", "0")

    return {
        "pinned_base_image": pinned_base_image,
        "runs_as_root": runs_as_root,
        "has_secrets_in_env": has_secrets,
    }


# ---------------------------------------------------------------------------
# hadolint primary path
# ---------------------------------------------------------------------------

def _fix_hint(rule: str) -> str:
    """Return the fix hint for a known rule, or empty string."""
    return _RULE_FIX_HINTS.get(rule, "")


def _run_hadolint(dockerfile_path: str) -> tuple[list[dict[str, Any]], bool]:
    """Invoke hadolint and return (findings, success).

    ``success`` is False only when the subprocess itself could not be started
    or returned an unexpected error that prevents JSON parsing. Findings-level
    exit codes (1) are expected and treated as success.
    """
    try:
        result = subprocess.run(
            ["hadolint", "--format", "json", dockerfile_path],
            capture_output=True,
            text=True,
            timeout=_HADOLINT_TIMEOUT,
        )
    except FileNotFoundError:
        _LOG.debug("hadolint not found on PATH")
        return [], False
    except subprocess.TimeoutExpired:
        _LOG.warning("hadolint timed out after %s s", _HADOLINT_TIMEOUT)
        return [], False

    # hadolint exits 1 when findings are present; exit 0 means clean.
    # Both are expected. Other non-zero codes may indicate a parse error.
    try:
        raw_findings = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        _LOG.warning("hadolint JSON decode failed: %.200s", result.stdout)
        return [], False

    if not isinstance(raw_findings, list):
        _LOG.warning("hadolint output was not a list: %.200s", result.stdout)
        return [], False

    findings: list[dict[str, Any]] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "info").lower()
        severity = _HADOLINT_LEVEL_MAP.get(level, "info")
        rule = str(item.get("code") or "")
        message = str(item.get("message") or "")
        line = int(item.get("line") or 0)
        findings.append(
            {
                "line": line,
                "severity": severity,
                "rule": rule,
                "message": message,
                "fix_hint": _fix_hint(rule),
            }
        )

    return findings, True


# ---------------------------------------------------------------------------
# Regex fallback path
# ---------------------------------------------------------------------------

def _run_regex(content: str) -> list[dict[str, Any]]:
    """Apply heuristic regex checks and return findings."""
    findings: list[dict[str, Any]] = []
    lines = content.splitlines()

    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for pattern, severity, rule, message in _REGEX_LINE_CHECKS:
            if pattern.search(line):
                findings.append(
                    {
                        "line": lineno,
                        "severity": severity,
                        "rule": rule,
                        "message": message,
                        "fix_hint": _fix_hint(rule),
                    }
                )
                # One match per rule per line is enough; move to the next line.
                break

    # Additional check: no USER instruction at all.
    has_user = any(
        _USER_RE.match(line) for line in lines
        if line.strip() and not line.strip().startswith("#")
    )
    if not has_user and any(_FROM_RE.match(line) for line in lines):
        findings.append(
            {
                "line": 0,
                "severity": "warning",
                "rule": "DL3002",
                "message": "No USER instruction found — container will run as root",
                "fix_hint": _fix_hint("DL3002"),
            }
        )

    return findings


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score(by_severity: dict[str, int]) -> int:
    """Compute a 0-100 quality score from severity counts."""
    deductions = (
        by_severity.get("error", 0) * _PENALTY_ERROR
        + by_severity.get("warning", 0) * _PENALTY_WARNING
        + by_severity.get("info", 0) * _PENALTY_INFO
    )
    return max(0, 100 - deductions)


def _tally(findings: list[dict[str, Any]]) -> dict[str, int]:
    """Count findings per severity bucket."""
    counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        sev = f.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(payload: dict) -> dict:
    """Analyse a Dockerfile string and return structured linting findings.

    Uses hadolint when available; falls back to regex-based heuristics otherwise.
    Structural properties (pinned base image, root user, secret ENV vars) are always
    derived from the raw content regardless of which linting path ran.
    """
    if not isinstance(payload, dict):
        return _err("dockerfile_analyzer.invalid_payload", "payload must be an object")

    dockerfile = payload.get("dockerfile")
    if not dockerfile or not isinstance(dockerfile, str) or not dockerfile.strip():
        # Fall back to MCP-attached workspace context when Claude Code is
        # running in a project that contains a Dockerfile and the user has
        # approved sharing. Avoids forcing the caller to re-paste the file.
        from core.workspace_helpers import extract_workspace_context

        bundle = extract_workspace_context(payload)
        if bundle is not None:
            ws_dockerfile = bundle.manifests.get("Dockerfile")
            if ws_dockerfile and ws_dockerfile.strip():
                dockerfile = ws_dockerfile
        if not dockerfile or not isinstance(dockerfile, str) or not dockerfile.strip():
            return _err(
                "dockerfile_analyzer.missing_dockerfile",
                "'dockerfile' is required and must be a non-empty string",
            )

    filename = str(payload.get("filename") or "Dockerfile")

    if len(dockerfile.encode("utf-8")) > _MAX_DOCKERFILE_BYTES:
        return _err(
            "dockerfile_analyzer.dockerfile_too_large",
            f"Dockerfile exceeds the maximum allowed size of {_MAX_DOCKERFILE_BYTES} bytes",
        )

    t_start = time.monotonic()

    structural = _structural_checks(dockerfile)
    hadolint_available = _is_hadolint_available()

    findings: list[dict[str, Any]]
    tool_used: str

    if hadolint_available:
        with tempfile.TemporaryDirectory() as tmpdir:
            df_path = str(Path(tmpdir) / filename)
            Path(df_path).write_text(dockerfile, encoding="utf-8")
            findings, ok = _run_hadolint(df_path)

        if not ok:
            # hadolint failed in an unexpected way — fall back gracefully.
            _LOG.warning("hadolint invocation failed; falling back to regex checks")
            findings = _run_regex(dockerfile)
            tool_used = "regex"
        else:
            tool_used = "hadolint"
    else:
        findings = _run_regex(dockerfile)
        tool_used = "regex"

    by_severity = _tally(findings)
    scan_time_ms = int((time.monotonic() - t_start) * 1000)

    return {
        "findings": findings,
        "total_findings": len(findings),
        "by_severity": by_severity,
        "score": _score(by_severity),
        "pinned_base_image": structural["pinned_base_image"],
        "runs_as_root": structural["runs_as_root"],
        "has_secrets_in_env": structural["has_secrets_in_env"],
        "tool_used": tool_used,
        "scan_time_ms": scan_time_ms,
    }
