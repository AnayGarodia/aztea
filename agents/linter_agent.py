"""
linter_agent.py — Lint Python/JS/TS code without a local toolchain

For Python: runs ruff (style + bugs) via subprocess.
For JS/TS: uses LLM analysis (eslint not bundled server-side).

Input:
  {
    "code": "source code",
    "language": "python|javascript|typescript|auto",   # default: auto
    "filename": "optional_hint.py",                    # used for extension-based detection
    "checks": ["style", "bugs", "complexity"]          # default: all
  }

Output:
  {
    "language": str,
    "tool": "ruff|llm",
    "issues": [{
      "rule": str,
      "message": str,
      "line": int | null,
      "column": int | null,
      "severity": "error|warning|info",
      "fix_available": bool
    }],
    "total_issues": int,
    "error_count": int,
    "warning_count": int,
    "clean": bool,
    "summary": str
  }
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

from core.llm import CompletionRequest, Message, run_with_fallback

_MAX_CODE_CHARS = 30_000

_SYSTEM = """\
You are an expert code reviewer acting as a linter. Analyze the provided code for:
1. Style violations (naming, formatting, unused imports/variables)
2. Potential bugs (undefined variables, type mismatches, missing error handling)
3. Complexity issues (deep nesting, long functions, duplicated logic)

Return ONLY valid JSON — no markdown fences, no prose outside the object."""

_USER = """\
Language: {language}
Filename hint: {filename}
Checks: {checks}

Code:
```
{code}
```

Return JSON:
{{
  "issues": [
    {{
      "rule": "rule id or short label like 'unused-import'",
      "message": "clear description of the issue",
      "line": integer or null,
      "column": integer or null,
      "severity": "error|warning|info",
      "fix_available": false
    }}
  ],
  "summary": "1-2 sentence plain summary of code quality"
}}"""


def _detect_language(code: str, filename: str) -> str:
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in (".py",):
            return "python"
        if ext in (".js", ".mjs", ".cjs"):
            return "javascript"
        if ext in (".ts", ".tsx"):
            return "typescript"
    # Heuristic from code content
    if re.search(r"\bdef \w+\(|import \w+|from \w+ import", code):
        return "python"
    if re.search(r"const |let |var |function |=>|require\(|import ", code):
        return "javascript"
    return "python"


def _ruff_available() -> bool:
    return shutil.which("ruff") is not None


def _run_ruff(code: str, checks: list[str]) -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmppath = f.name

    try:
        # Select rule sets based on requested checks
        select_rules = []
        if "bugs" in checks:
            select_rules.extend(["E", "F", "B"])  # pycodestyle + pyflakes + bugbear
        if "style" in checks:
            select_rules.extend(["I", "N", "W"])   # isort + pep8-naming + pycodestyle warnings
        if "complexity" in checks:
            select_rules.extend(["C", "PLR"])       # mccabe + pylint refactor

        select_arg = ",".join(select_rules) if select_rules else "ALL"

        result = subprocess.run(
            ["ruff", "check", "--output-format=json", f"--select={select_arg}", tmppath],
            capture_output=True,
            text=True,
            timeout=15,
        )
        raw = result.stdout.strip()
        if not raw:
            return []
        data = json.loads(raw)
        issues = []
        for item in data:
            code_val = item.get("code") or ""
            msg = item.get("message") or ""
            loc = item.get("location") or {}
            fix = item.get("fix") is not None
            # ruff severity: error if code starts with E/F/B, else warning
            sev = "error" if code_val.startswith(("E", "F", "B")) else "warning"
            issues.append({
                "rule": code_val,
                "message": msg,
                "line": loc.get("row"),
                "column": loc.get("column"),
                "severity": sev,
                "fix_available": fix,
            })
        return issues
    except Exception:
        return []
    finally:
        try:
            os.unlink(tmppath)
        except Exception:
            pass


def _run_llm_lint(code: str, language: str, filename: str, checks: list[str]) -> tuple[list[dict], str]:
    prompt = _USER.format(
        language=language,
        filename=filename or f"snippet.{language[:2]}",
        checks=", ".join(checks),
        code=code[:_MAX_CODE_CHARS],
    )
    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", prompt)],
        max_tokens=1500,
        json_mode=True,
    ))
    raw = resp.text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw)
    return parsed.get("issues", []), parsed.get("summary", "")


def run(payload: dict) -> dict:
    code = str(payload.get("code") or "").strip()
    if not code:
        raise ValueError("'code' is required.")
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

    tool = "llm"
    summary = ""
    issues: list[dict] = []

    if language == "python" and _ruff_available():
        tool = "ruff"
        issues = _run_ruff(code, checks)
        # Compute summary via LLM if we have issues
        if issues:
            total = len(issues)
            errors = sum(1 for i in issues if i["severity"] == "error")
            warnings = total - errors
            summary = f"ruff found {total} issue(s): {errors} error(s), {warnings} warning(s)."
        else:
            summary = "No issues found by ruff."
    else:
        try:
            issues, summary = _run_llm_lint(code, language, filename, checks)
        except Exception as exc:
            return {
                "language": language,
                "tool": "llm",
                "issues": [],
                "total_issues": 0,
                "error_count": 0,
                "warning_count": 0,
                "clean": False,
                "summary": f"LLM analysis failed: {exc}",
            }

    error_count = sum(1 for i in issues if i.get("severity") == "error")
    warning_count = sum(1 for i in issues if i.get("severity") == "warning")

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
