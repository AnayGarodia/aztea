"""
pr_reviewer.py — GitHub PR review agent

Input:
  {
    "pr_url": "https://github.com/owner/repo/pull/123",  # OR
    "diff": "raw unified diff text",
    "context": ""  # optional: repo purpose, coding standards
  }

Output:
  {
    "pr_title": str | null,
    "total_issues": int,
    "blocking": bool,
    "issues": [{
      "file": str,
      "line_hint": str,
      "severity": "critical|high|medium|low|info",
      "category": "security|bug|performance|logic|style",
      "description": str,
      "suggestion": str
    }],
    "summary": str,
    "verdict": "approve|request_changes|comment"
  }
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a senior engineer performing a GitHub pull request review. You are thorough, opinionated, \
and concrete — your reviews identify real bugs and security issues, not stylistic nitpicks.

Review the diff provided and produce a structured JSON review. For each issue:
- Cite the exact file and a short line-range or code snippet
- Classify severity honestly: critical (ship-blocking security/data-loss), high (likely bug in normal usage), \
  medium (edge-case bug or significant performance regression), low (minor correctness), info (style/docs)
- Give a concrete, actionable suggestion — not just "fix this"

Return ONLY valid JSON — no markdown fences, no prose outside the object."""

_USER = """\
Review this pull request diff.
Context: {context}

Diff:
{diff}

Return a JSON object:
{{
  "pr_title": null,
  "total_issues": integer,
  "blocking": true if any critical or high severity issues,
  "issues": [
    {{
      "file": "path/to/file.py or 'unknown'",
      "line_hint": "short code snippet or line range",
      "severity": "critical|high|medium|low|info",
      "category": "security|bug|performance|logic|style",
      "description": "what is wrong and why it matters",
      "suggestion": "concrete fix"
    }}
  ],
  "summary": "2–3 sentence overall assessment",
  "verdict": "approve|request_changes|comment"
}}"""

_GH_PATCH_HEADERS = {
    "Accept": "application/vnd.github.v3.diff",
    "User-Agent": "aztea-pr-reviewer/1.0",
}
_MAX_DIFF_CHARS = 16_000


def _fetch_github_diff(pr_url: str) -> str:
    m = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url.strip()
    )
    if not m:
        raise ValueError(f"Not a valid GitHub PR URL: {pr_url}")
    owner, repo, number = m.group(1), m.group(2), m.group(3)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
    req = urllib.request.Request(api_url, headers=_GH_PATCH_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API error {e.code}: {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to fetch PR diff: {e}") from e


def run(payload: dict) -> dict:
    pr_url = str(payload.get("pr_url") or "").strip()
    diff = str(payload.get("diff") or "").strip()
    context = str(payload.get("context") or "Not provided.")[:600]

    if not diff and pr_url:
        diff = _fetch_github_diff(pr_url)

    if not diff:
        raise ValueError("Provide either 'pr_url' (a GitHub PR URL) or 'diff' (raw unified diff text).")

    diff = diff[:_MAX_DIFF_CHARS]
    prompt = _USER.format(context=context, diff=diff)

    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", prompt)],
        max_tokens=2500,
        json_mode=True,
    ))
    raw = _strip_fences(resp.text)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned non-JSON: {e}\n\n{raw[:300]}") from e

    if pr_url and not result.get("pr_title"):
        m = re.search(r"github\.com/[^/]+/[^/]+/pull/(\d+)", pr_url)
        if m:
            result["pr_title"] = f"PR #{m.group(1)}"

    return result


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return m.group(1).strip() if m else text
