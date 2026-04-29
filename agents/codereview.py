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
- assign CWE IDs and OWASP categories when applicable
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
_VALID_FOCUS = {"all", "security", "performance", "bugs", "style", "correctness", "maintainability"}
_VALID_SEVERITY = {"critical", "high", "medium", "low", "info"}
_VALID_CATEGORY = {"security", "performance", "bug", "style", "maintainability", "correctness"}


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
    line_hint = str(item.get("line_hint") or item.get("line") or item.get("location") or "").strip()
    fix = str(item.get("fix") or item.get("suggestion") or "").strip()
    cwe_id = item.get("cwe_id")
    if cwe_id is not None:
        cwe_id = str(cwe_id).strip() or None
    owasp_category = item.get("owasp_category")
    if owasp_category is not None:
        owasp_category = str(owasp_category).strip() or None
    return {
        "line_hint": line_hint,
        "severity": _normalize_severity(item.get("severity")),
        "category": _normalize_category(item.get("category")),
        "cwe_id": cwe_id,
        "owasp_category": owasp_category,
        "description": description[:600],
        "fix": fix[:800],
    }


def _severity_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in ("critical", "high", "medium", "low", "info")}
    for issue in issues:
        sev = str(issue.get("severity") or "medium")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


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
    positive_aspects = [str(item).strip() for item in positive_aspects if str(item).strip()][:8]

    test_recommendations = payload.get("test_recommendations")
    if not isinstance(test_recommendations, list):
        test_recommendations = []
    test_recommendations = [str(item).strip() for item in test_recommendations if str(item).strip()][:10]

    summary = str(payload.get("summary") or "").strip()
    if not summary:
        if normalized_issues:
            summary = f"Found {len(normalized_issues)} review issue(s) that should be addressed before relying on this code in production."
        else:
            summary = "No material issues were identified in this review pass."

    counts = _severity_counts(normalized_issues)
    security_critical = any(issue["category"] == "security" and issue["severity"] in {"critical", "high"} for issue in normalized_issues)
    language_detected = str(payload.get("language_detected") or language or "auto").strip() or "auto"

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
        return _err("code_review_agent.missing_input", "Either code or diff is required.")

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

    req = CompletionRequest(
        model="",
        messages=[
            Message("system", _SYSTEM),
            Message(
                "user",
                _USER.format(
                    language=language_value if language_value != "auto" else "auto-detect",
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
    except Exception as exc:
        return _err("code_review_agent.llm_unavailable", f"Code review model unavailable: {type(exc).__name__}")

    raw_text = _strip_fences(resp.text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = {
            "summary": raw_text[:800] or "The reviewer returned an unreadable response.",
            "issues": [],
            "positive_aspects": [],
            "test_recommendations": [],
        }

    result = _normalize_review_output(
        parsed,
        language=language_value,
        filename=filename_value,
        focus=focus_value,
        review_target=review_target,
    )
    result["llm_used"] = True
    result["degraded_mode"] = False
    return result
