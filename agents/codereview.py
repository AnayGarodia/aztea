"""
codereview.py — Structured code review agent for snippets and diffs.

Input:
  {
    "code": "...",              # optional if diff is provided
    "diff": "...",              # optional unified diff / patch
    "filename": "src/app.py",   # optional filename hint
    "language": "auto",
    "focus": "all",
    "context": "optional context"
  }

Output:
  {
    "language_detected": str,
    "review_target": "code|diff|code_and_diff",
    "filename": str,
    "focus": str,
    "score": int,                # 1–10
    "security_critical": bool,
    "complexity_score": int,     # 1–10
    "issue_count": int,
    "severity_counts": {...},
    "issues": [...],
    "positive_aspects": [str],
    "test_recommendations": [str],
    "summary": str
  }
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a staff-level software engineer and application security reviewer.

Review code or a diff like a production reviewer:
- prefer correctness, security, performance, and maintainability over style trivia
- name concrete failure modes
- assign CWE IDs and OWASP categories only when the issue is genuinely security-relevant
- do not classify plain crashes, exceptions, missing validation, or divide-by-zero paths as security issues unless attacker control and exploitable impact are explicit
- suggest fixes that are specific and implementable
- avoid filler, hedging, and generic praise

Return only valid JSON."""

_USER = """\
Review this submission.

Language hint: {language}
Focus: {focus}
Filename: {filename}
Context: {context}
Review target: {review_target}

Return a JSON object with EXACTLY these fields:
{{
  "language_detected": "actual language or patch type",
  "score": integer from 1 to 10,
  "security_critical": boolean,
  "complexity_score": integer from 1 to 10,
  "issues": [
    {{
      "line_hint": "line number, range, or quoted snippet",
      "severity": "critical|high|medium|low|info",
      "category": "security|performance|bug|style|maintainability|correctness",
      "cwe_id": "CWE-XXX or null",
      "owasp_category": "OWASP category or null",
      "description": "specific problem and why it matters",
      "fix": "concrete code-level fix"
    }}
  ],
  "positive_aspects": ["specific things done well"],
  "test_recommendations": ["specific test scenarios to add"],
  "summary": "2-3 sentence verdict"
}}

Submission:
```text
{review_input}
```"""

_MAX_CODE_CHARS = 14_000
_MAX_DIFF_CHARS = 16_000
_MAX_CONTEXT_CHARS = 600
_VALID_FOCUS = {
    "all",
    "security",
    "performance",
    "bugs",
    "style",
    "correctness",
    "maintainability",
}
_VALID_SEVERITY = {"critical", "high", "medium", "low", "info"}
_VALID_CATEGORY = {
    "security",
    "performance",
    "bug",
    "style",
    "maintainability",
    "correctness",
}
_DIVIDE_BY_ZERO_RE = re.compile(
    r"\b(divide|division|denominator|divide-by-zero|division by zero|zero guard)\b",
    re.IGNORECASE,
)
_SECURITY_EXPLOIT_RE = re.compile(
    r"\b(attacker|exploit|remote|dos|denial of service|privilege|bypass|arbitrary)\b",
    re.IGNORECASE,
)
_DIFF_HUNK_RE = re.compile(r"^@@ .+ @@")
_ADDED_LINE_PREFIX_RE = re.compile(r"^\+(?!\+\+)")
_SECRET_LOG_RE = re.compile(
    r"(console\.log|print|logger\.\w+|logging\.\w+)\s*\(?.*\b(token|secret|api[_-]?key|authorization|password|passwd|bearer)\b",
    re.IGNORECASE,
)
_EVAL_RE = re.compile(r"\b(eval|exec)\s*\(", re.IGNORECASE)
_VERIFY_FALSE_RE = re.compile(r"\bverify\s*=\s*False\b", re.IGNORECASE)
_SUBPROCESS_SHELL_RE = re.compile(
    r"\bsubprocess\.\w+\([^)]*shell\s*=\s*True", re.IGNORECASE
)
_SQL_INTERP_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE)\b.*(\+|%s|f[\"'])", re.IGNORECASE
)
_BARE_EXCEPT_RE = re.compile(r"^\s*except\s*:\s*$")


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _strip_fences(text: str) -> str:
    text = str(text or "").strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return match.group(1).strip() if match else text


def _normalize_severity(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _VALID_SEVERITY else "medium"


def _normalize_category(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _VALID_CATEGORY else "correctness"


def _normalize_issue(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    description = str(item.get("description") or item.get("title") or "").strip()
    if not description:
        return None
    line_hint = str(
        item.get("line_hint") or item.get("line") or item.get("location") or ""
    ).strip()
    fix = str(item.get("fix") or item.get("suggestion") or "").strip()
    cwe_id = item.get("cwe_id")
    if cwe_id is not None:
        cwe_id = str(cwe_id).strip() or None
    owasp_category = item.get("owasp_category")
    if owasp_category is not None:
        owasp_category = str(owasp_category).strip() or None
    normalized = {
        "line_hint": line_hint,
        "severity": _normalize_severity(item.get("severity")),
        "category": _normalize_category(item.get("category")),
        "cwe_id": cwe_id,
        "owasp_category": owasp_category,
        "description": description[:600],
        "fix": fix[:800],
    }
    return _postprocess_issue(normalized)


def _postprocess_issue(issue: dict[str, Any]) -> dict[str, Any]:
    description = str(issue.get("description") or "")
    fix = str(issue.get("fix") or "")
    line_hint = str(issue.get("line_hint") or "")
    combined = " ".join(part for part in (description, fix, line_hint) if part)
    cwe_id = str(issue.get("cwe_id") or "").strip().upper()

    # The reviewer has been prone to labeling plain divide-by-zero bugs as
    # critical security vulnerabilities with CWE/OWASP tags. Without explicit
    # attacker-controlled exploitability, keep those as correctness bugs.
    if (
        cwe_id == "CWE-369" or _DIVIDE_BY_ZERO_RE.search(combined)
    ) and not _SECURITY_EXPLOIT_RE.search(combined):
        issue["severity"] = "medium"
        issue["category"] = "correctness"
        issue["cwe_id"] = None
        issue["owasp_category"] = None

    if issue.get("category") == "security":
        has_security_context = bool(issue.get("cwe_id") or issue.get("owasp_category"))
        if not has_security_context and not _SECURITY_EXPLOIT_RE.search(combined):
            issue["category"] = "correctness"
            if issue.get("severity") == "critical":
                issue["severity"] = "high"

    return issue


def _severity_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in ("critical", "high", "medium", "low", "info")}
    for issue in issues:
        sev = str(issue.get("severity") or "medium")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _issue_key(issue: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(issue.get("line_hint") or "").strip().lower(),
        str(issue.get("category") or "").strip().lower(),
        str(issue.get("description") or "").strip().lower(),
    )


def _extract_candidate_lines(code_text: str, diff_text: str) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    if diff_text:
        current_hunk = ""
        for raw_line in diff_text.splitlines():
            if _DIFF_HUNK_RE.match(raw_line):
                current_hunk = raw_line.strip()
                continue
            if _ADDED_LINE_PREFIX_RE.match(raw_line):
                lines.append((current_hunk or "diff", raw_line[1:]))
    if not lines and code_text:
        for index, raw_line in enumerate(code_text.splitlines(), start=1):
            lines.append((f"line {index}", raw_line))
    return lines


def _score_from_issues(issues: list[dict[str, Any]]) -> int:
    if not issues:
        return 9
    penalty = 0
    for issue in issues:
        severity = str(issue.get("severity") or "medium")
        penalty += {
            "critical": 4,
            "high": 3,
            "medium": 2,
            "low": 1,
            "info": 0,
        }.get(severity, 2)
    return max(1, 10 - penalty)


def _heuristic_issue(
    *,
    line_hint: str,
    severity: str,
    category: str,
    description: str,
    fix: str,
    cwe_id: str | None = None,
    owasp_category: str | None = None,
) -> dict[str, Any]:
    return {
        "line_hint": line_hint,
        "severity": severity,
        "category": category,
        "cwe_id": cwe_id,
        "owasp_category": owasp_category,
        "description": description,
        "fix": fix,
    }


def _heuristic_review(
    *,
    code_text: str,
    diff_text: str,
    filename: str,
    focus: str,
    review_target: str,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    positive_aspects: list[str] = []
    tests: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    for line_hint, raw_line in _extract_candidate_lines(code_text, diff_text):
        line = raw_line.strip()
        if not line:
            continue
        candidates: list[dict[str, Any]] = []
        if _SECRET_LOG_RE.search(line):
            candidates.append(
                _heuristic_issue(
                    line_hint=f"{line_hint}: {line[:120]}",
                    severity="high",
                    category="security",
                    cwe_id="CWE-532",
                    owasp_category="A09:2021-Security Logging and Monitoring Failures",
                    description="The code appears to log a secret-bearing value. Authorization tokens and API keys routinely leak through log drains and support tooling.",
                    fix="Remove the sensitive value from logs entirely or replace it with a fixed redacted marker.",
                )
            )
            tests.append(
                "Add a logging test that asserts tokens, passwords, and API keys are redacted before emission."
            )
        if _EVAL_RE.search(line):
            candidates.append(
                _heuristic_issue(
                    line_hint=f"{line_hint}: {line[:120]}",
                    severity="high",
                    category="security",
                    cwe_id="CWE-95",
                    owasp_category="A03:2021-Injection",
                    description="The code executes dynamic code via eval/exec. That is dangerous unless the input is tightly controlled and fully trusted.",
                    fix="Replace dynamic execution with a structured parser, allowlist, or direct function dispatch.",
                )
            )
            tests.append(
                "Add a test that proves untrusted input cannot change executed code paths."
            )
        if _VERIFY_FALSE_RE.search(line):
            candidates.append(
                _heuristic_issue(
                    line_hint=f"{line_hint}: {line[:120]}",
                    severity="high",
                    category="security",
                    cwe_id="CWE-295",
                    owasp_category="A02:2021-Cryptographic Failures",
                    description="TLS certificate verification is disabled. That allows man-in-the-middle interception of supposedly secure traffic.",
                    fix="Keep certificate verification enabled and trust a specific CA bundle if custom trust is required.",
                )
            )
            tests.append(
                "Add an integration test that keeps certificate verification enabled for outbound HTTPS requests."
            )
        if _SUBPROCESS_SHELL_RE.search(line):
            candidates.append(
                _heuristic_issue(
                    line_hint=f"{line_hint}: {line[:120]}",
                    severity="medium",
                    category="security",
                    cwe_id="CWE-78",
                    owasp_category="A03:2021-Injection",
                    description="The code invokes subprocess with shell=True. If any untrusted input reaches that command string, it becomes a command-injection risk.",
                    fix="Pass argv as a list and keep shell=False unless a shell is strictly necessary and all inputs are constant.",
                )
            )
            tests.append(
                "Add a test proving untrusted input cannot alter the executed command."
            )
        if _SQL_INTERP_RE.search(line):
            candidates.append(
                _heuristic_issue(
                    line_hint=f"{line_hint}: {line[:120]}",
                    severity="high",
                    category="security",
                    cwe_id="CWE-89",
                    owasp_category="A03:2021-Injection",
                    description="The code appears to build SQL with string interpolation or concatenation. That is a common injection path.",
                    fix="Use parameterized queries or a query builder that binds user input separately from SQL text.",
                )
            )
            tests.append(
                "Add a test with attacker-controlled input and assert the query is parameterized rather than interpolated."
            )
        if _BARE_EXCEPT_RE.match(raw_line):
            candidates.append(
                _heuristic_issue(
                    line_hint=f"{line_hint}: {line[:120]}",
                    severity="low",
                    category="maintainability",
                    description="Bare except catches every exception class, including interrupts and unrelated runtime failures. That makes debugging and recovery harder.",
                    fix="Catch the specific exception types you expect and preserve unexpected failures.",
                )
            )
            tests.append(
                "Add a failure-path test that distinguishes expected exceptions from unexpected runtime bugs."
            )

        for candidate in candidates:
            key = _issue_key(candidate)
            if key not in seen:
                seen.add(key)
                issues.append(candidate)

    if not issues:
        if review_target == "diff":
            positive_aspects.append(
                "The change appears mechanically small and no high-signal deterministic issues were detected in added lines."
            )
        else:
            positive_aspects.append(
                "No high-signal deterministic issues were detected by the rule-based review pass."
            )
    elif review_target == "diff":
        positive_aspects.append(
            "The review stayed focused on the changed lines rather than speculating about untouched code."
        )

    unique_tests = []
    for item in tests:
        if item not in unique_tests:
            unique_tests.append(item)

    summary = (
        "Rule-based review detected concrete issues that should be addressed before shipping."
        if issues
        else "The fallback deterministic review did not find any high-signal issues."
    )
    return {
        "language_detected": "diff" if review_target == "diff" else "auto",
        "review_target": review_target,
        "filename": filename,
        "focus": focus,
        "score": _score_from_issues(issues),
        "security_critical": any(
            item["category"] == "security" and item["severity"] in {"critical", "high"}
            for item in issues
        ),
        "complexity_score": 4,
        "issue_count": len(issues),
        "severity_counts": _severity_counts(issues),
        "issues": issues,
        "positive_aspects": positive_aspects,
        "test_recommendations": unique_tests[:8],
        "summary": summary,
    }


def _normalize_review_output(
    raw: dict[str, Any] | None,
    *,
    language: str,
    filename: str,
    focus: str,
    review_target: str,
) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    issues_raw = payload.get("issues")
    normalized_issues: list[dict[str, Any]] = []
    if isinstance(issues_raw, list):
        for item in issues_raw:
            normalized = _normalize_issue(item)
            if normalized is not None:
                normalized_issues.append(normalized)

    try:
        score = int(payload.get("score"))
    except (TypeError, ValueError):
        score = 6 if not normalized_issues else 5
    score = max(1, min(10, score))

    try:
        complexity_score = int(payload.get("complexity_score"))
    except (TypeError, ValueError):
        complexity_score = 4
    complexity_score = max(1, min(10, complexity_score))

    positive_aspects = payload.get("positive_aspects")
    if not isinstance(positive_aspects, list):
        positive_aspects = []
    positive_aspects = [
        str(item).strip() for item in positive_aspects if str(item).strip()
    ][:8]

    test_recommendations = payload.get("test_recommendations")
    if not isinstance(test_recommendations, list):
        test_recommendations = []
    test_recommendations = [
        str(item).strip() for item in test_recommendations if str(item).strip()
    ][:10]

    summary = str(payload.get("summary") or "").strip()
    if not summary:
        if normalized_issues:
            summary = f"Found {len(normalized_issues)} review issue(s) that should be addressed before relying on this code in production."
        else:
            summary = "No material issues were identified in this review pass."

    counts = _severity_counts(normalized_issues)
    security_critical = any(
        issue["category"] == "security" and issue["severity"] in {"critical", "high"}
        for issue in normalized_issues
    )
    language_detected = (
        str(payload.get("language_detected") or language or "auto").strip() or "auto"
    )

    return {
        "language_detected": language_detected,
        "review_target": review_target,
        "filename": filename,
        "focus": focus,
        "score": score,
        "security_critical": security_critical,
        "complexity_score": complexity_score,
        "issue_count": len(normalized_issues),
        "severity_counts": counts,
        "issues": normalized_issues,
        "positive_aspects": positive_aspects,
        "test_recommendations": test_recommendations,
        "summary": summary[:1000],
    }


def _merge_review_results(
    primary: dict[str, Any],
    supplemental: dict[str, Any],
    *,
    language_value: str,
) -> dict[str, Any]:
    merged_issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in (primary.get("issues") or [], supplemental.get("issues") or []):
        if not isinstance(source, list):
            continue
        for issue in source:
            if not isinstance(issue, dict):
                continue
            normalized = _normalize_issue(issue)
            if normalized is None:
                continue
            key = _issue_key(normalized)
            if key in seen:
                continue
            seen.add(key)
            merged_issues.append(normalized)

    positive_aspects: list[str] = []
    for source in (
        primary.get("positive_aspects") or [],
        supplemental.get("positive_aspects") or [],
    ):
        for item in source if isinstance(source, list) else []:
            text = str(item).strip()
            if text and text not in positive_aspects:
                positive_aspects.append(text)

    test_recommendations: list[str] = []
    for source in (
        primary.get("test_recommendations") or [],
        supplemental.get("test_recommendations") or [],
    ):
        for item in source if isinstance(source, list) else []:
            text = str(item).strip()
            if text and text not in test_recommendations:
                test_recommendations.append(text)

    counts = _severity_counts(merged_issues)
    score_candidates = [
        int(value)
        for value in (
            primary.get("score"),
            supplemental.get("score"),
            _score_from_issues(merged_issues),
        )
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    ]
    score = max(
        1,
        min(
            10,
            min(score_candidates)
            if score_candidates
            else _score_from_issues(merged_issues),
        ),
    )

    complexity_candidates = [
        int(value)
        for value in (
            primary.get("complexity_score"),
            supplemental.get("complexity_score"),
        )
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    ]
    complexity_score = max(
        1, min(10, max(complexity_candidates) if complexity_candidates else 4)
    )

    summary = (
        str(primary.get("summary") or "").strip()
        or str(supplemental.get("summary") or "").strip()
    )
    if not summary:
        summary = (
            f"Found {len(merged_issues)} review issue(s) that should be addressed before relying on this code in production."
            if merged_issues
            else "No material issues were identified in this review pass."
        )

    return {
        "language_detected": str(
            primary.get("language_detected")
            or supplemental.get("language_detected")
            or language_value
            or "auto"
        ),
        "review_target": str(
            primary.get("review_target") or supplemental.get("review_target") or "code"
        ),
        "filename": str(
            primary.get("filename") or supplemental.get("filename") or ""
        ).strip(),
        "focus": str(primary.get("focus") or supplemental.get("focus") or "all"),
        "score": score,
        "security_critical": any(
            item["category"] == "security" and item["severity"] in {"critical", "high"}
            for item in merged_issues
        ),
        "complexity_score": complexity_score,
        "issue_count": len(merged_issues),
        "severity_counts": counts,
        "issues": merged_issues,
        "positive_aspects": positive_aspects[:8],
        "test_recommendations": test_recommendations[:10],
        "summary": summary[:1000],
    }


def run(
    code: str = "",
    language: str = "auto",
    focus: str = "all",
    context: str = "",
    diff: str = "",
    filename: str = "",
) -> dict[str, Any]:
    code_text = str(code or "").strip()
    diff_text = str(diff or "").strip()
    if not code_text and not diff_text:
        return _err(
            "code_review_agent.missing_input", "Either code or diff is required."
        )

    focus_value = str(focus or "all").strip().lower()
    if focus_value not in _VALID_FOCUS:
        return _err(
            "code_review_agent.invalid_focus",
            f"focus must be one of: {', '.join(sorted(_VALID_FOCUS))}",
        )

    filename_value = str(filename or "").strip()[:240]
    language_value = str(language or "auto").strip().lower() or "auto"
    context_value = str(context or "").strip()[:_MAX_CONTEXT_CHARS] or "Not provided."

    review_parts: list[str] = []
    review_target = "code"
    if code_text:
        review_parts.append(f"[CODE]\n{code_text[:_MAX_CODE_CHARS]}")
    if diff_text:
        review_parts.append(f"[DIFF]\n{diff_text[:_MAX_DIFF_CHARS]}")
        review_target = "code_and_diff" if code_text else "diff"
    review_input = "\n\n".join(review_parts)
    heuristic_result = _heuristic_review(
        code_text=code_text,
        diff_text=diff_text,
        filename=filename_value,
        focus=focus_value,
        review_target=review_target,
    )

    req = CompletionRequest(
        model="",
        messages=[
            Message("system", _SYSTEM),
            Message(
                "user",
                _USER.format(
                    language=language_value
                    if language_value != "auto"
                    else "auto-detect",
                    focus=focus_value,
                    filename=filename_value or "Not provided.",
                    context=context_value,
                    review_target=review_target,
                    review_input=review_input,
                ),
            ),
        ],
        max_tokens=2200,
        json_mode=True,
    )

    try:
        resp = run_with_fallback(req)
    except Exception:
        fallback = _merge_review_results(
            heuristic_result, {}, language_value=language_value
        )
        fallback["llm_used"] = False
        fallback["degraded_mode"] = True
        if not fallback["summary"]:
            fallback["summary"] = (
                "The review model was unavailable, so this result was generated from deterministic rule checks only."
            )
        return fallback

    raw_text = _strip_fences(resp.text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = {}

    llm_result = _normalize_review_output(
        parsed,
        language=language_value,
        filename=filename_value,
        focus=focus_value,
        review_target=review_target,
    )
    result = _merge_review_results(
        llm_result, heuristic_result, language_value=language_value
    )
    if not parsed:
        result["summary"] = (
            "The review model returned an unreadable response, so the result was recovered from deterministic rule checks."
            if result["issues"]
            else "The review model returned an unreadable response and no deterministic issues were detected."
        )
    result["llm_used"] = True
    result["degraded_mode"] = False
    return result
