"""
linter_agent.py — Lint Python/JS/TS code without an LLM fallback.

Python uses ruff. JavaScript and TypeScript use eslint via npx when Node is
available; otherwise the agent returns a structured tool_unavailable error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

_MAX_CODE_CHARS = 30_000


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _detect_language(code: str, filename: str) -> str:
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext == ".py":
            return "python"
        if ext in {".js", ".mjs", ".cjs"}:
            return "javascript"
        if ext in {".ts", ".tsx"}:
            return "typescript"
    if re.search(r"\bdef \w+\(|import \w+|from \w+ import", code):
        return "python"
    if re.search(r"const |let |var |function |=>|require\(|import ", code):
        return "javascript"
    return "python"


def _ruff_available() -> bool:
    return shutil.which("ruff") is not None


def _npx_available() -> bool:
    return shutil.which("npx") is not None and shutil.which("node") is not None


def _run_ruff(code: str, checks: list[str]) -> tuple[list[dict], str]:
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        tmppath = f.name

    try:
        select_rules: list[str] = []
        if "bugs" in checks:
            # E/F/B = pycodestyle errors, pyflakes, flake8-bugbear.
            # S = flake8-bandit (dynamic-execution builtins, hardcoded
            # passwords, unsafe deserialization, weak crypto). External
            # eval (2026-05-03) found we did not flag dangerous dynamic
            # execution builtins; adding S to the bug-check default
            # closes that gap.
            select_rules.extend(["E", "F", "B", "S"])
        if "style" in checks:
            select_rules.extend(["I", "N", "W"])
        if "complexity" in checks:
            select_rules.extend(["C", "PLR"])
        if "security" in checks:
            select_rules.extend(["S"])
        select_arg = ",".join(sorted(set(select_rules))) if select_rules else "ALL"

        result = subprocess.run(
            [
                "ruff",
                "check",
                "--output-format=json",
                f"--select={select_arg}",
                tmppath,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        raw = result.stdout.strip()
        data = json.loads(raw or "[]")
        issues: list[dict] = []
        for item in data:
            code_val = item.get("code") or ""
            loc = item.get("location") or {}
            issues.append(
                {
                    "rule": code_val,
                    "message": item.get("message") or "",
                    "line": loc.get("row"),
                    "column": loc.get("column"),
                    "severity": "error"
                    if code_val.startswith(("E", "F", "B", "S"))
                    else "warning",
                    "fix_available": item.get("fix") is not None,
                }
            )
        if issues:
            errors = sum(1 for item in issues if item["severity"] == "error")
            warnings = len(issues) - errors
            summary = f"ruff found {len(issues)} issue(s): {errors} error(s), {warnings} warning(s)."
        else:
            summary = "No issues found by ruff."
        return issues, summary
    finally:
        try:
            os.unlink(tmppath)
        except Exception:
            pass


def _eslint_cache_root() -> str:
    """Per-process npm cache root — isolates this call's `npx` install from any
    other concurrent linter run.

    The shared default cache (`~/.npm`) hits an ENOTEMPTY race when two
    `npx --yes eslint` invocations land at the same time and both try to
    atomically rename their freshly-extracted `node_modules/eslint` into the
    same `_npx/<hash>/` slot. Giving each call its own cache directory
    sidesteps the rename collision entirely. We also reuse a stable
    per-process subdir so a single worker amortizes install cost across
    sequential calls.
    """
    base = os.environ.get("AZTEA_LINTER_NPM_CACHE")
    if base:
        return base
    return os.path.join(tempfile.gettempdir(), f"aztea-eslint-cache-{os.getpid()}")


def _run_eslint(code: str, language: str, filename: str) -> tuple[list[dict], str]:
    suffix = ".ts" if language == "typescript" else ".js"
    lint_name = filename or f"snippet{suffix}"
    base_cmd = [
        "npx",
        "--yes",
        "eslint",
        "--stdin",
        "--stdin-filename",
        lint_name,
        "--format",
        "json",
        "--no-config-lookup",
        "--rule",
        "no-undef:error",
        "--rule",
        "no-unused-vars:warn",
        "--rule",
        "no-unreachable:error",
        "--rule",
        "no-eval:error",
        "--rule",
        "no-var:warn",
    ]
    if language == "typescript":
        base_cmd = [
            "npx",
            "--yes",
            "-p",
            "eslint",
            "-p",
            "@typescript-eslint/parser",
            "eslint",
            "--stdin",
            "--stdin-filename",
            lint_name,
            "--format",
            "json",
            "--no-config-lookup",
            "--parser",
            "@typescript-eslint/parser",
            "--rule",
            "no-unused-vars:warn",
            "--rule",
            "no-unreachable:error",
            "--rule",
            "no-eval:error",
            "--rule",
            "no-var:warn",
        ]

    cache_dir = _eslint_cache_root()
    os.makedirs(cache_dir, exist_ok=True)
    env = {**os.environ, "npm_config_cache": cache_dir, "NPM_CONFIG_CACHE": cache_dir}

    result = None
    last_err = ""
    # Retry on the known ENOTEMPTY/EEXIST race in case concurrent calls
    # within the same process still collide. The per-process cache dir
    # makes this rare; the retry covers the residual case and a transient
    # network blip on first install.
    for attempt in range(3):
        try:
            result = subprocess.run(
                base_cmd,
                input=code,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            last_err = f"eslint timed out after {exc.timeout}s"
            continue
        if result.returncode in {0, 1}:
            break
        last_err = (result.stderr or result.stdout or "").strip()
        # Only retry on the install-race signatures.
        if not re.search(r"ENOTEMPTY|EEXIST|EACCES|ETIMEDOUT", last_err, re.IGNORECASE):
            break
    if result is None or result.returncode not in {0, 1}:
        raise RuntimeError(last_err or "eslint failed")

    raw = result.stdout.strip() or "[]"
    parsed = json.loads(raw)
    files = parsed if isinstance(parsed, list) else []
    messages = files[0].get("messages") if files else []
    issues: list[dict] = []
    for item in messages or []:
        severity = "error" if int(item.get("severity") or 0) >= 2 else "warning"
        issues.append(
            {
                "rule": item.get("ruleId") or "eslint",
                "message": item.get("message") or "",
                "line": item.get("line"),
                "column": item.get("column"),
                "severity": severity,
                "fix_available": item.get("fix") is not None,
            }
        )
    if issues:
        errors = sum(1 for item in issues if item["severity"] == "error")
        warnings = len(issues) - errors
        summary = f"eslint found {len(issues)} issue(s): {errors} error(s), {warnings} warning(s)."
    else:
        summary = "No issues found by eslint."
    return issues, summary


def run(payload: dict) -> dict:
    """Lint source code using ruff (Python) or eslint (JS/TS) without an LLM.

    Required: ``code`` (str).
    Optional:
    - ``language`` (str, default ``"auto"``) — ``"python"`` | ``"javascript"``
      | ``"typescript"`` | ``"auto"`` (detect from content).
    - ``config`` (str) — raw ruff.toml or .eslintrc content to apply.
    - ``fix`` (bool, default False) — return auto-fixed code alongside findings.

    Runtime requirements:
    - Python: ``ruff`` must be on PATH.
    - JS/TS: Node.js + npx must be on PATH; ``eslint`` is installed ad-hoc via
      ``npx`` if missing. Returns ``tool_unavailable`` if Node is absent.

    Returns ``{findings: [{file, line, col, rule, message, severity}],
    total, fixed_code?}``.  No LLM is involved; output is deterministic.
    """
    code = str(payload.get("code") or "").strip()
    if not code:
        return _err("linter_agent.missing_code", "'code' is required.")
    if len(code) > _MAX_CODE_CHARS:
        code = code[:_MAX_CODE_CHARS]

    filename = str(payload.get("filename") or "").strip()
    language = str(payload.get("language") or "auto").strip().lower()
    if language == "auto":
        language = _detect_language(code, filename)

    checks_raw = payload.get("checks")
    if isinstance(checks_raw, list) and checks_raw:
        checks = [str(c).lower() for c in checks_raw]
    else:
        checks = ["style", "bugs", "complexity"]

    if language == "python":
        if not _ruff_available():
            return _err(
                "linter_agent.tool_unavailable",
                "ruff is not available on this executor.",
            )
        issues, summary = _run_ruff(code, checks)
        tool = "ruff"
    elif language in {"javascript", "typescript"}:
        if not _npx_available():
            return _err(
                "linter_agent.tool_unavailable",
                f"{language} linting requires node+npx on this executor.",
            )
        try:
            issues, summary = _run_eslint(code, language, filename)
        except Exception as exc:
            return _err("linter_agent.tool_unavailable", f"eslint unavailable: {exc}")
        tool = "eslint"
    else:
        return _err(
            "linter_agent.invalid_language", f"Unsupported language: {language}"
        )

    error_count = sum(1 for item in issues if item.get("severity") == "error")
    warning_count = sum(1 for item in issues if item.get("severity") == "warning")
    return {
        "language": language,
        "tool": tool,
        "issues": issues,
        "total_issues": len(issues),
        "error_count": error_count,
        "warning_count": warning_count,
        "clean": len(issues) == 0,
        "summary": summary,
    }
