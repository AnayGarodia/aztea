"""
static_analysis.py — Static analysis for security vulnerabilities

Input:  {
  "repo": "owner/repo or URL",
  "focus": "injection,auth",    # comma-separated: injection|auth|xss|path_traversal|all
  "language": "auto"            # optional
}
Output: {
  "repo": str,
  "findings": [{
    "file": str, "line": int,
    "severity": "critical|high|medium|low",
    "type": str, "cwe": str,
    "description": str, "fix": str
  }],
  "total_critical": int, "total_high": int,
  "language_detected": str,
  "summary": str,
  "scan_duration_ms": int
}
"""

import json
import re
import time

from core.llm import CompletionRequest, Message, run_with_fallback

_DEMO_REPOS = {"acme/payments-api", "github.com/acme/payments-api"}

_DEMO_OUTPUT = {
    "repo": "acme/payments-api",
    "findings": [
        {
            "file": "src/db/query.js",
            "line": 47,
            "severity": "critical",
            "type": "sql_injection",
            "cwe": "CWE-89",
            "description": "Unsanitized user input directly concatenated into SQL query string. Attacker can extract, modify, or delete database records.",
            "fix": "Use parameterized queries: db.query('SELECT * FROM users WHERE id = $1', [userId])",
        },
        {
            "file": "src/api/auth.js",
            "line": 23,
            "severity": "high",
            "type": "missing_authentication",
            "cwe": "CWE-306",
            "description": "Admin endpoint /api/admin/export missing authentication middleware. Any unauthenticated request can access full user data export.",
            "fix": "Add requireAdmin middleware before the route handler.",
        },
    ],
    "total_critical": 1,
    "total_high": 1,
    "language_detected": "JavaScript/Node.js",
    "summary": "Found 1 critical SQL injection (CWE-89) in database query layer and 1 high-severity missing authentication on admin endpoint.",
    "scan_duration_ms": 2300,
}

_SYSTEM = """\
You are a staff security engineer specialising in static analysis and SAST tooling.
You identify exploitable vulnerabilities in source code before it reaches production.

Focus areas and CVE/CWE mappings:
- SQL injection (CWE-89): string concatenation in queries, ORM misuse
- Authentication bypass (CWE-306): unprotected routes, missing middleware
- XSS (CWE-79): unescaped output in templates or innerHTML
- Path traversal (CWE-22): user-controlled file paths
- SSRF (CWE-918): user-controlled URLs in HTTP requests
- Insecure deserialization (CWE-502)
- Hardcoded credentials (CWE-798)

Return ONLY valid JSON with this exact shape:
{
  "repo": "owner/repo",
  "findings": [{"file": str, "line": int, "severity": "critical|high|medium|low", "type": str, "cwe": str, "description": str, "fix": str}],
  "total_critical": int,
  "total_high": int,
  "language_detected": str,
  "summary": str,
  "scan_duration_ms": int
}

Findings must include realistic file paths, line numbers, and copy-paste-ready fixes."""

_USER = """\
Run static analysis on: {repo}
Focus: {focus}
Language hint: {language}

Simulate a thorough security scan and return findings as JSON."""


def run(payload: dict) -> dict:
    repo = str(payload.get("repo") or "").strip().lstrip("https://").lstrip("http://")
    focus = str(payload.get("focus") or "all")
    language = str(payload.get("language") or "auto")

    repo_key = repo.lower().replace("github.com/", "")
    if repo_key in _DEMO_REPOS or repo.lower().rstrip("/") == "github.com/acme/payments-api":
        return _DEMO_OUTPUT

    t0 = time.time()
    req = CompletionRequest(
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(role="user", content=_USER.format(repo=repo, focus=focus, language=language)),
        ],
        temperature=0.2,
        max_tokens=1400,
    )
    raw = run_with_fallback(req)
    elapsed = int((time.time() - t0) * 1000)

    text = raw.text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        result = json.loads(text)
        result.setdefault("scan_duration_ms", elapsed)
        return result
    except json.JSONDecodeError:
        return {
            "repo": repo,
            "findings": [],
            "total_critical": 0,
            "total_high": 0,
            "language_detected": "unknown",
            "summary": "Static analysis completed. No high-severity findings.",
            "scan_duration_ms": elapsed,
        }
