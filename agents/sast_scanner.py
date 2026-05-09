"""
sast_scanner.py — Static Application Security Testing via real tool execution.

# OWNS: running semgrep and bandit on caller-supplied source files in an
#        isolated tempdir and returning structured, SARIF-style findings.
# NOT OWNS: secret detection (secret_scanner.py), dependency CVE lookup
#            (dependency_auditor.py), or any LLM-based code commentary.
# INVARIANTS:
#   - Input files are written to a fresh tempdir that is always cleaned up.
#   - No finding ever contains the full file content; only the offending
#     snippet extracted by the tool itself is forwarded.
#   - If neither semgrep nor bandit is installed, return a structured error
#     rather than raising an exception.
#   - bandit returning a non-zero exit code when findings exist is expected
#     and must NOT be treated as a tool failure.
# DECISIONS:
#   - We shell out to semgrep/bandit rather than importing their Python APIs
#     because their CLIs are more stable across versions and the subprocess
#     boundary keeps our process clean.
#   - Language auto-detection is extension-based; more exotic extensions
#     (e.g. .jsx, .mjs) are intentionally mapped to their closest canonical
#     language so semgrep ruleset selection stays predictable.
# KNOWN DEBT:
#   - semgrep `--config auto` requires network access on first run to pull
#     rule bundles. Offline environments need SEMGREP_RULES set externally.

Input:
  {
    "files":     [{"name": str, "content": str}],   # 1-20 files, <=100 KB total
    "language":  "auto"|"python"|"javascript"|"typescript"|"go"|"java",  # optional
    "rulesets":  ["python.flask.security", ...]      # optional; auto if omitted
  }

Output (success):
  {
    "findings":            [<finding>],
    "total_findings":      int,
    "by_severity":         {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
    "files_scanned":       int,
    "languages_detected":  [str],
    "tools_used":          [str],
    "scan_time_ms":        int,
  }

Output (error):
  {"error": {"code": str, "message": str}}
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

_MAX_FILES = 20
_MAX_TOTAL_BYTES = 100_000  # 100 KB
_SUBPROCESS_TIMEOUT = 60    # seconds per tool invocation
_TOOL_OUTPUT_PREVIEW_CHARS = 200

# Severity ordering used for sorting (lower index = shown first).
_SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
_SEVERITY_RANK: dict[str, int] = {s: i for i, s in enumerate(_SEVERITY_ORDER)}

# Extension -> canonical language name consumed by semgrep `--lang`.
_EXT_TO_LANG: dict[str, str] = {
    ".py":   "python",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".mjs":  "javascript",
    ".cjs":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".go":   "go",
    ".java": "java",
}

# Semgrep JSON severity -> our canonical severity.
_SEMGREP_SEV_MAP: dict[str, str] = {
    "ERROR":   "high",
    "WARNING": "medium",
    "INFO":    "low",
}

# Bandit severity labels -> our canonical severity.
_BANDIT_SEV_MAP: dict[str, str] = {
    "HIGH":   "high",
    "MEDIUM": "medium",
    "LOW":    "low",
}

# Bandit confidence labels adjust final severity: HIGH+HIGH -> critical,
# LOW+LOW -> info. All other combos stay at the base severity mapping above.
_BANDIT_CONFIDENCE_BUMP: dict[tuple[str, str], str] = {
    ("HIGH", "HIGH"): "critical",
    ("LOW",  "LOW"):  "info",
}



def _is_tool_available(name: str) -> bool:
    """Return True if ``name`` is on PATH."""
    try:
        result = subprocess.run(
            ["which", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _detect_languages(files: list[dict]) -> list[str]:
    """Derive the sorted, deduplicated set of languages from file extensions."""
    langs: set[str] = set()
    for f in files:
        name = str(f.get("name") or "")
        suffix = Path(name).suffix.lower()
        lang = _EXT_TO_LANG.get(suffix)
        if lang:
            langs.add(lang)
    return sorted(langs)


def _write_files(files: list[dict], tmpdir: str) -> None:
    """Write caller-supplied files into ``tmpdir``.

    Preserves the original relative path structure so tool output paths are
    human-readable.
    """
    for f in files:
        rel_name = str(f.get("name") or "file.txt")
        # Strip leading slashes so Path.joinpath does not escape tmpdir.
        rel_name = rel_name.lstrip("/\\")
        dest = Path(tmpdir) / rel_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(str(f.get("content") or ""), encoding="utf-8")


# ---------------------------------------------------------------------------
# semgrep
# ---------------------------------------------------------------------------

def _semgrep_config_args(rulesets: list[str] | None) -> list[str]:
    """Pure: build ``--config`` flag pairs for the semgrep CLI."""
    if not rulesets:
        return ["--config", "auto"]
    args: list[str] = []
    for rs in rulesets:
        args += ["--config", rs]
    return args


def _semgrep_invoke(tmpdir: str, rulesets: list[str] | None) -> dict[str, Any] | None:
    """Side-effect: run the semgrep CLI; returns parsed JSON or ``None`` on failure."""
    cmd = ["semgrep", "--json", *_semgrep_config_args(rulesets), tmpdir]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError:
        _LOG.debug("semgrep not found on PATH")
        return None
    except subprocess.TimeoutExpired:
        _LOG.warning("semgrep timed out after %s s", _SUBPROCESS_TIMEOUT)
        return None
    # semgrep exits 1 when findings are present; that is expected.
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        _LOG.warning("semgrep JSON decode failed: %s", result.stdout[:_TOOL_OUTPUT_PREVIEW_CHARS])
        return None


def _semgrep_finding(item: dict[str, Any], tmpdir: str) -> dict[str, Any]:
    """Pure: shape one semgrep ``results[]`` row into the agent's finding schema."""
    extra = item.get("extra") or {}
    severity = _SEMGREP_SEV_MAP.get(str(extra.get("severity") or "INFO").upper(), "info")
    check_id = str(item.get("check_id") or "semgrep.unknown")
    metadata = extra.get("metadata") or {}
    cwe_raw = metadata.get("cwe") or ""
    cwe = (cwe_raw[0] if isinstance(cwe_raw, list) and cwe_raw else str(cwe_raw))
    fix_hint = str(extra.get("fix") or "") or _semgrep_fix_hint(check_id)
    start = item.get("start") or {}
    return {
        "file":         _strip_tmpdir(str(item.get("path") or ""), tmpdir),
        "line":         int(start.get("line") or 0),
        "column":       int(start.get("col") or 0),
        "severity":     severity,
        "rule_id":      check_id,
        "cwe":          cwe,
        "message":      str(extra.get("message") or ""),
        "code_snippet": str(extra.get("lines") or "").rstrip(),
        "fix_hint":     fix_hint,
        "tool":         "semgrep",
    }


def _run_semgrep(tmpdir: str, rulesets: list[str] | None) -> list[dict[str, Any]]:
    """Side-effect: run semgrep on ``tmpdir``; returns normalised findings (empty on failure)."""
    data = _semgrep_invoke(tmpdir, rulesets)
    if data is None:
        return []
    return [_semgrep_finding(item, tmpdir) for item in data.get("results") or []]


def _semgrep_fix_hint(check_id: str) -> str:
    """Return a brief fix hint derived from a semgrep rule ID when the rule
    does not embed a ``fix`` field."""
    cid = check_id.lower()
    if "sql" in cid:
        return "Use parameterised queries instead of string concatenation."
    if "xss" in cid or "html" in cid:
        return "Escape user-controlled output before rendering in HTML."
    if "subprocess" in cid or "shell" in cid:
        return "Avoid shell=True; pass arguments as a list to subprocess."
    if "pickle" in cid or "deseri" in cid:
        return "Avoid deserialising untrusted data; prefer JSON or msgpack."
    if "dynamic-code" in cid or "code-injection" in cid:
        return "Remove dynamic code execution; use a safe, explicit alternative."
    if "crypto" in cid or "md5" in cid or "sha1" in cid:
        return "Replace with a modern hash (SHA-256+) or authenticated cipher."
    if "secret" in cid or "hardcoded" in cid or "password" in cid:
        return "Move credentials to environment variables or a secret manager."
    return "Review the flagged code and apply the principle of least privilege."


# ---------------------------------------------------------------------------
# bandit (Python only)
# ---------------------------------------------------------------------------

def _bandit_invoke(tmpdir: str) -> dict[str, Any] | None:
    """Side-effect: run the bandit CLI; returns parsed JSON or ``None`` on failure."""
    cmd = ["bandit", "-r", tmpdir, "-f", "json"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError:
        _LOG.debug("bandit not found on PATH")
        return None
    except subprocess.TimeoutExpired:
        _LOG.warning("bandit timed out after %s s", _SUBPROCESS_TIMEOUT)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        _LOG.warning("bandit JSON decode failed: %s", result.stdout[:_TOOL_OUTPUT_PREVIEW_CHARS])
        return None


def _bandit_cwe(item: dict[str, Any]) -> str:
    """Pure: shape bandit's CWE field, tolerating older bandit versions where it's a string."""
    cwe_raw = item.get("issue_cwe") or {}
    if isinstance(cwe_raw, dict):
        cwe_id = cwe_raw.get("id")
        return f"CWE-{cwe_id}" if cwe_id else ""
    return str(cwe_raw)


def _bandit_finding(item: dict[str, Any], tmpdir: str) -> dict[str, Any]:
    """Pure: shape one bandit ``results[]`` row into the agent's finding schema."""
    raw_sev = str(item.get("issue_severity") or "LOW").upper()
    raw_conf = str(item.get("issue_confidence") or "LOW").upper()
    severity = _BANDIT_CONFIDENCE_BUMP.get(
        (raw_sev, raw_conf), _BANDIT_SEV_MAP.get(raw_sev, "low"),
    )
    test_id = str(item.get("test_id") or "bandit.unknown")
    return {
        "file":         _strip_tmpdir(str(item.get("filename") or ""), tmpdir),
        "line":         int(item.get("line_number") or 0),
        "column":       0,  # bandit does not report column
        "severity":     severity,
        "rule_id":      test_id,
        "cwe":          _bandit_cwe(item),
        "message":      str(item.get("issue_text") or ""),
        "code_snippet": str(item.get("code") or "").rstrip(),
        "fix_hint":     _bandit_fix_hint(test_id),
        "tool":         "bandit",
    }


def _run_bandit(tmpdir: str) -> list[dict[str, Any]]:
    """Side-effect: run bandit on ``tmpdir`` (Python only); returns normalised findings."""
    data = _bandit_invoke(tmpdir)
    if data is None:
        return []
    return [_bandit_finding(item, tmpdir) for item in (data.get("results") or [])]


def _bandit_fix_hint(test_id: str) -> str:
    """Return a brief remediation hint keyed on bandit test IDs."""
    tid = test_id.lower()
    if "b301" in tid or "b302" in tid or "b303" in tid:
        return "Avoid insecure deserialisation (pickle/marshal/yaml.load); use safe alternatives."
    if "b304" in tid or "b305" in tid:
        return "Replace deprecated/insecure cipher with AES-GCM or ChaCha20-Poly1305."
    if "b306" in tid:
        return "Replace mktemp() with tempfile.NamedTemporaryFile() or tempfile.mkstemp()."
    if "b307" in tid:
        # bandit B307 flags dynamic code execution via built-in functions.
        return "Remove dynamic code execution; use ast.literal_eval() for safe expression parsing."
    if "b310" in tid or "b311" in tid or "b312" in tid:
        return "Validate and allowlist URLs before making outbound requests."
    if "b320" in tid:
        return "Disable XML external entity processing (set resolve_entities=False)."
    if "b404" in tid or "b602" in tid or "b603" in tid or "b604" in tid or "b605" in tid:
        return "Avoid shell=True; pass arguments as a list to subprocess."
    if "b501" in tid or "b502" in tid or "b503" in tid or "b504" in tid or "b505" in tid:
        return "Use TLS 1.2+ with certificate verification enabled."
    if "b506" in tid:
        return "Replace yaml.load() with yaml.safe_load()."
    if "b601" in tid:
        return "Avoid paramiko exec_command with shell interpolation; use channel.exec_command safely."
    if "b608" in tid:
        return "Use parameterised SQL queries; never concatenate user input into SQL strings."
    if "b701" in tid or "b702" in tid:
        return "Enable Jinja2 auto-escaping or use MarkupSafe to prevent XSS."
    return "Review the flagged code against the bandit rule documentation."


# ---------------------------------------------------------------------------
# Deduplication and sorting
# ---------------------------------------------------------------------------

def _dedup(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate findings keyed on (file, line, rule_id)."""
    seen: set[tuple[str, int, str]] = set()
    out: list[dict[str, Any]] = []
    for f in findings:
        key = (f["file"], f["line"], f["rule_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort findings critical-first, then by file and line for stable output."""
    return sorted(
        findings,
        key=lambda f: (
            _SEVERITY_RANK.get(f["severity"], len(_SEVERITY_ORDER)),
            f["file"],
            f["line"],
        ),
    )


def _tally(findings: list[dict[str, Any]]) -> dict[str, int]:
    """Count findings by severity bucket."""
    counts: dict[str, int] = {s: 0 for s in _SEVERITY_ORDER}
    for f in findings:
        sev = f.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _strip_tmpdir(path: str, tmpdir: str) -> str:
    """Remove the tmpdir prefix from an absolute path to restore the caller's filename."""
    prefix = tmpdir.rstrip("/\\") + os.sep
    if path.startswith(prefix):
        return path[len(prefix):]
    return path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _validate_files(payload: dict) -> dict | list[dict]:
    """Pure: enforce ``files`` shape, count, and total-byte limits."""
    files = payload.get("files")
    if not files or not isinstance(files, list):
        return _err(
            "sast_scanner.no_files",
            "'files' is required and must be a non-empty list of {name, content} objects",
        )
    validated: list[dict] = []
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            return _err(
                "sast_scanner.invalid_file",
                f"files[{i}] must be an object with 'name' and 'content' keys",
            )
        if not isinstance(f.get("content"), str):
            return _err("sast_scanner.invalid_file", f"files[{i}].content must be a string")
        validated.append(f)
    if len(validated) > _MAX_FILES:
        return _err(
            "sast_scanner.too_large",
            f"Too many files: {len(validated)} submitted, maximum is {_MAX_FILES}",
        )
    total_bytes = sum(len((f.get("content") or "").encode("utf-8")) for f in validated)
    if total_bytes > _MAX_TOTAL_BYTES:
        return _err(
            "sast_scanner.too_large",
            f"Total content size {total_bytes} bytes exceeds limit of {_MAX_TOTAL_BYTES} bytes",
        )
    return validated


def _resolve_languages_and_rulesets(
    payload: dict, validated: list[dict],
) -> tuple[list[str], list[str] | None]:
    """Pure: detect languages from filenames + caller hint; parse optional ``rulesets``."""
    language_hint = str(payload.get("language") or "auto").strip().lower()
    rulesets_raw = payload.get("rulesets")
    rulesets: list[str] | None = (
        [str(r) for r in rulesets_raw if r]
        if isinstance(rulesets_raw, list) and rulesets_raw
        else None
    )
    detected = _detect_languages(validated)
    if language_hint not in ("auto", "") and language_hint not in detected:
        detected = sorted(set(detected) | {language_hint})
    return detected, rulesets


def _select_tools(detected_langs: list[str]) -> dict | tuple[bool, bool]:
    """Pure-ish: query PATH for tool availability; returns flags or error envelope when nothing runs."""
    has_python = "python" in detected_langs
    semgrep_ok = _is_tool_available("semgrep")
    bandit_ok = _is_tool_available("bandit") and has_python
    if semgrep_ok or bandit_ok:
        return semgrep_ok, bandit_ok
    if has_python:
        return _err(
            "sast_scanner.no_tools_available",
            "Neither semgrep nor bandit is installed; install at least one to run SAST scans",
        )
    return _err(
        "sast_scanner.no_tools_available",
        "semgrep is not installed; install it to run SAST scans on non-Python files",
    )


def _scan_with_tools(
    validated: list[dict], rulesets: list[str] | None,
    semgrep_ok: bool, bandit_ok: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Side-effect: run enabled scanners; returns ``(all_findings, tools_used)``."""
    all_findings: list[dict[str, Any]] = []
    tools_used: list[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_files(validated, tmpdir)
        if semgrep_ok:
            all_findings.extend(_run_semgrep(tmpdir, rulesets))
            tools_used.append("semgrep")
        else:
            _LOG.debug("semgrep unavailable; skipping")
        if bandit_ok:
            all_findings.extend(_run_bandit(tmpdir))
            tools_used.append("bandit")
    return all_findings, tools_used


def run(payload: dict) -> dict:
    """Run semgrep + bandit on caller-supplied files and return SARIF-style findings.

    Why: SAST tools are the right thing to ship as a hosted agent because
    they require specific binaries on PATH; we provide a uniform, capped
    interface so callers don't need to care which scanner runs.
    """
    if not isinstance(payload, dict):
        return _err("sast_scanner.invalid_payload", "payload must be an object")
    validated = _validate_files(payload)
    if isinstance(validated, dict):
        return validated
    detected_langs, rulesets = _resolve_languages_and_rulesets(payload, validated)
    selected = _select_tools(detected_langs)
    if isinstance(selected, dict):
        return selected
    semgrep_ok, bandit_ok = selected
    t_start = time.monotonic()
    all_findings, tools_used = _scan_with_tools(validated, rulesets, semgrep_ok, bandit_ok)
    sorted_findings = _sort_findings(_dedup(all_findings))
    return {
        "findings":           sorted_findings,
        "total_findings":     len(sorted_findings),
        "by_severity":        _tally(sorted_findings),
        "files_scanned":      len(validated),
        "languages_detected": detected_langs,
        "tools_used":         tools_used,
        "scan_time_ms":       int((time.monotonic() - t_start) * 1000),
    }
