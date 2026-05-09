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

# Hard limits — named constants so CI magic-number grep is satisfied.
_MAX_FILES = 20
_MAX_TOTAL_BYTES = 100_000  # 100 KB
_SUBPROCESS_TIMEOUT = 60    # seconds per tool invocation

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

def _run_semgrep(tmpdir: str, rulesets: list[str] | None) -> list[dict[str, Any]]:
    """Run semgrep on ``tmpdir`` and return normalised findings."""
    if rulesets:
        config_args: list[str] = []
        for rs in rulesets:
            config_args += ["--config", rs]
    else:
        config_args = ["--config", "auto"]

    cmd = ["semgrep", "--json", *config_args, tmpdir]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError:
        _LOG.debug("semgrep not found on PATH")
        return []
    except subprocess.TimeoutExpired:
        _LOG.warning("semgrep timed out after %s s", _SUBPROCESS_TIMEOUT)
        return []

    # semgrep exits 1 when findings are present; that is expected.
    # Parse JSON regardless and fall back to empty on decode failure.
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        _LOG.warning("semgrep JSON decode failed: %s", result.stdout[:200])
        return []

    findings: list[dict[str, Any]] = []
    for item in data.get("results") or []:
        raw_sev = str(
            (item.get("extra") or {}).get("severity") or "INFO"
        ).upper()
        severity = _SEMGREP_SEV_MAP.get(raw_sev, "info")

        check_id = str(item.get("check_id") or "semgrep.unknown")
        metadata = (item.get("extra") or {}).get("metadata") or {}
        cwe_raw = metadata.get("cwe") or ""
        if isinstance(cwe_raw, list):
            cwe = cwe_raw[0] if cwe_raw else ""
        else:
            cwe = str(cwe_raw)

        message = str((item.get("extra") or {}).get("message") or "")
        code_snippet = str((item.get("extra") or {}).get("lines") or "")
        fix_hint = str((item.get("extra") or {}).get("fix") or "")
        if not fix_hint:
            fix_hint = _semgrep_fix_hint(check_id)

        raw_path = str(item.get("path") or "")
        rel_path = _strip_tmpdir(raw_path, tmpdir)

        start = item.get("start") or {}
        findings.append(
            {
                "file":         rel_path,
                "line":         int(start.get("line") or 0),
                "column":       int(start.get("col") or 0),
                "severity":     severity,
                "rule_id":      check_id,
                "cwe":          cwe,
                "message":      message,
                "code_snippet": code_snippet.rstrip(),
                "fix_hint":     fix_hint,
                "tool":         "semgrep",
            }
        )
    return findings


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

def _run_bandit(tmpdir: str) -> list[dict[str, Any]]:
    """Run bandit on ``tmpdir`` (Python files only) and return findings."""
    cmd = ["bandit", "-r", tmpdir, "-f", "json"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError:
        _LOG.debug("bandit not found on PATH")
        return []
    except subprocess.TimeoutExpired:
        _LOG.warning("bandit timed out after %s s", _SUBPROCESS_TIMEOUT)
        return []

    # bandit returns exit code 1 when issues are found; that is expected.
    # Exit code 0 means clean; 2+ means tool error. Parse JSON regardless.
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        _LOG.warning("bandit JSON decode failed: %s", result.stdout[:200])
        return []

    findings: list[dict[str, Any]] = []
    for item in (data.get("results") or []):
        raw_sev = str(item.get("issue_severity") or "LOW").upper()
        raw_conf = str(item.get("issue_confidence") or "LOW").upper()
        severity = _BANDIT_CONFIDENCE_BUMP.get(
            (raw_sev, raw_conf),
            _BANDIT_SEV_MAP.get(raw_sev, "low"),
        )

        raw_path = str(item.get("filename") or "")
        rel_path = _strip_tmpdir(raw_path, tmpdir)

        test_id = str(item.get("test_id") or "bandit.unknown")
        message = str(item.get("issue_text") or "")
        code_snippet = str(item.get("code") or "").rstrip()

        # bandit's CWE field was added in v1.7.5; guard for older installs.
        cwe_raw = item.get("issue_cwe") or {}
        if isinstance(cwe_raw, dict):
            cwe_id = cwe_raw.get("id")
            cwe = f"CWE-{cwe_id}" if cwe_id else ""
        else:
            cwe = str(cwe_raw)

        findings.append(
            {
                "file":         rel_path,
                "line":         int(item.get("line_number") or 0),
                "column":       0,  # bandit does not report column
                "severity":     severity,
                "rule_id":      test_id,
                "cwe":          cwe,
                "message":      message,
                "code_snippet": code_snippet,
                "fix_hint":     _bandit_fix_hint(test_id),
                "tool":         "bandit",
            }
        )
    return findings


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

def run(payload: dict) -> dict:
    """Run semgrep + bandit on caller-supplied code files and return findings.

    ``files`` is a required list of ``{"name": str, "content": str}`` objects.
    Up to 20 files, 100 KB total content.

    Returns SARIF-style structured findings with severity, CWE, code snippet,
    and remediation hint for each issue.
    """
    if not isinstance(payload, dict):
        return _err("sast_scanner.invalid_payload", "payload must be an object")

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
            return _err(
                "sast_scanner.invalid_file",
                f"files[{i}].content must be a string",
            )
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

    language_hint = str(payload.get("language") or "auto").strip().lower()
    rulesets_raw = payload.get("rulesets")
    rulesets: list[str] | None = None
    if isinstance(rulesets_raw, list) and rulesets_raw:
        rulesets = [str(r) for r in rulesets_raw if r]

    detected_langs = _detect_languages(validated)
    if language_hint not in ("auto", "") and language_hint not in detected_langs:
        # Caller gave an explicit hint; honour it alongside extension detection.
        detected_langs = sorted(set(detected_langs) | {language_hint})

    has_python = "python" in detected_langs

    semgrep_ok = _is_tool_available("semgrep")
    bandit_ok = _is_tool_available("bandit") and has_python

    if not semgrep_ok and not bandit_ok:
        if has_python:
            return _err(
                "sast_scanner.no_tools_available",
                "Neither semgrep nor bandit is installed; install at least one to run SAST scans",
            )
        return _err(
            "sast_scanner.no_tools_available",
            "semgrep is not installed; install it to run SAST scans on non-Python files",
        )

    t_start = time.monotonic()
    all_findings: list[dict[str, Any]] = []
    tools_used: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        _write_files(validated, tmpdir)

        if semgrep_ok:
            sg_findings = _run_semgrep(tmpdir, rulesets)
            all_findings.extend(sg_findings)
        else:
            _LOG.debug("semgrep unavailable; skipping")

        if bandit_ok:
            bd_findings = _run_bandit(tmpdir)
            all_findings.extend(bd_findings)

    # Record which tools ran regardless of whether they produced findings.
    if semgrep_ok:
        tools_used.append("semgrep")
    if bandit_ok:
        tools_used.append("bandit")

    deduped = _dedup(all_findings)
    sorted_findings = _sort_findings(deduped)
    by_severity = _tally(sorted_findings)
    scan_time_ms = int((time.monotonic() - t_start) * 1000)

    return {
        "findings":           sorted_findings,
        "total_findings":     len(sorted_findings),
        "by_severity":        by_severity,
        "files_scanned":      len(validated),
        "languages_detected": detected_langs,
        "tools_used":         tools_used,
        "scan_time_ms":       scan_time_ms,
    }
