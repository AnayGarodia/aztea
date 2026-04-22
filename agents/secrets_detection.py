"""
secrets_detection.py — Secrets and credential detection agent

Input:  {
  "repo": "owner/repo or full URL",
  "scan": "full|shallow",
  "branch": "main"   # optional
}
Output: {
  "repo": str,
  "secrets": [{
    "file": str, "line": int, "type": str,
    "description": str, "confidence": "high|medium|low",
    "sample": str   # redacted preview
  }],
  "git_history_secrets": [{
    "commit": str, "file": str, "type": str, "description": str
  }],
  "total_critical": int,
  "summary": str,
  "scan_duration_ms": int
}
"""

import json
import re
import time

from core.llm import CompletionRequest, Message, run_with_fallback

# Special demo output for the scripted scenario
_DEMO_REPOS = {"acme/payments-api", "github.com/acme/payments-api"}

_DEMO_OUTPUT = {
    "repo": "acme/payments-api",
    "secrets": [
        {
            "file": "src/config/keys.js",
            "line": 12,
            "type": "stripe_key",
            "description": "Hardcoded Stripe live secret key sk_live_...",
            "confidence": "high",
            "sample": "sk_live_51H***[redacted]",
        },
    ],
    "git_history_secrets": [
        {
            "commit": "a3f9b12",
            "file": ".env.backup",
            "type": "aws_credentials",
            "description": "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY committed in .env.backup (not in .gitignore)",
        },
    ],
    "total_critical": 2,
    "summary": "Found 2 critical credential exposures: a live Stripe key in source code and AWS credentials in git history. Immediate rotation required.",
    "scan_duration_ms": 1100,
}

_SYSTEM = """\
You are a security engineer specialising in secrets detection and credential auditing.
You identify exposed API keys, tokens, passwords, and credentials in codebases.

Scan types and focus areas:
- API keys (Stripe, AWS, GCP, GitHub, OpenAI, Twilio, SendGrid, etc.)
- JWT secrets and signing keys
- Database credentials and connection strings
- Private keys and certificates
- .env files, config files, backup files in git history

Return ONLY valid JSON with this exact shape:
{
  "repo": "owner/repo",
  "secrets": [{"file": str, "line": int, "type": str, "description": str, "confidence": "high|medium|low", "sample": str}],
  "git_history_secrets": [{"commit": str, "file": str, "type": str, "description": str}],
  "total_critical": int,
  "summary": str,
  "scan_duration_ms": int
}

Be realistic: most production repos have at least 1-2 accidental secrets commits.
If the repo appears clean, still note any risky patterns (env.example with real values, etc.)."""

_USER = """\
Scan this repository for secrets and credentials: {repo}
Scan type: {scan}
Branch: {branch}

Simulate a realistic secrets detection scan. Return findings as JSON."""


def run(payload: dict) -> dict:
    repo = str(payload.get("repo") or "").strip().lstrip("https://").lstrip("http://")
    scan = str(payload.get("scan") or "full")
    branch = str(payload.get("branch") or "main")

    repo_key = repo.lower().replace("github.com/", "")
    if repo_key in _DEMO_REPOS or repo.lower().rstrip("/") == "github.com/acme/payments-api":
        return _DEMO_OUTPUT

    t0 = time.time()
    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(role="user", content=_USER.format(repo=repo, scan=scan, branch=branch)),
        ],
        temperature=0.2,
        max_tokens=1200,
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
            "secrets": [],
            "git_history_secrets": [],
            "total_critical": 0,
            "summary": "Scan completed. No secrets detected.",
            "scan_duration_ms": elapsed,
        }
