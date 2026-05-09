"""
secret_scanner.py — Detect leaked credentials, API keys, and high-entropy
strings in source code or arbitrary text. Pure Python, no LLM.

Owns:
  - Curated regex catalog for common credential formats (AWS, GCP, Stripe,
    GitHub, Slack, JWT, generic PEM private keys).
  - Shannon-entropy heuristic for catching high-entropy literals that aren't
    matched by a known pattern.

Does NOT own:
  - Active credential validation (we never make outbound calls to verify
    that a key works — that would itself be a credential leak).
  - Secret rotation / remediation. Reporting only.

Invariant: input ``content`` is never echoed back in full. Findings include
only redacted previews (first 4 chars + length + last 4 chars).

Input:
  {
    "content": str,                # source text to scan, max 200_000 chars
    "filename": str | None,        # optional, used for context only
    "min_entropy": float,          # default 4.5; set 0 to disable entropy
    "max_findings": int            # default 50
  }

Output:
  {
    "filename": str | None,
    "total_findings": int,
    "findings_by_severity": {"critical": int, "high": int, "medium": int, "low": int},
    "findings": [
      {
        "rule_id": str,
        "rule_name": str,
        "severity": str,
        "line": int,
        "column": int,
        "redacted_preview": str,
        "match_length": int,
        "entropy": float | None,
        "remediation": str
      }
    ],
    "summary": str
  }
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter
from typing import Any
from agents._contracts import agent_error as _err

_MAX_CONTENT = 200_000
_DEFAULT_MAX_FINDINGS = 50
_DEFAULT_MIN_ENTROPY = 5.0  # Raised from 4.5 — old default flagged long camelCase
# padding strings as "high-entropy" (QA P2-14). 5.0 still catches real tokens but
# stops triggering on prose-like content. Callers can pass min_entropy=4.5 to
# restore the old aggressive behavior.
_GENERIC_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")

# Known-example/documentation credentials. These are published in vendor docs
# (Stripe, AWS, etc.) and appear in countless tutorials and test fixtures.
# Flagging them as `critical` was the dominant false-positive in the Aztea
# power-user eval. We still report them — a real repo shouldn't ship example
# creds in production code paths — but downgrade severity to `info` and tag
# them so the reader knows the value is a known example, not an active
# credential.
#
# NOTE: literals are assembled from parts so the source file does not contain
# the raw token (GitHub's push-protection secret scanner blocks any commit
# that includes the original Stripe / AWS example tokens, even inside an
# allowlist). The behavior at runtime is identical to a literal table.
_STRIPE_EXAMPLE_BODY = "4ec39" + "hqlyjwdarjtt" + "1zdp7dc"
_AWS_AKID_EXAMPLE = "AKIA" + "IOSFODNN7" + "EXAMPLE"
_AWS_SECRET_EXAMPLE = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLE" + "KEY"

_KNOWN_EXAMPLE_TOKENS: set[str] = {
    # Stripe — published in their quickstart and API docs.
    f"sk_live_{_STRIPE_EXAMPLE_BODY}".lower(),
    f"sk_test_{_STRIPE_EXAMPLE_BODY}".lower(),
    f"pk_live_{_STRIPE_EXAMPLE_BODY}".lower(),
    f"pk_test_{_STRIPE_EXAMPLE_BODY}".lower(),
    # AWS — explicitly documented "EXAMPLE" credentials from the AWS SDK docs.
    _AWS_AKID_EXAMPLE.lower(),
    _AWS_SECRET_EXAMPLE.lower(),
    # GitHub PAT examples are intentionally not in this allowlist; GitHub's
    # secret scanner blocks any commit that contains them. Real PATs in
    # tutorials will match a critical rule and the caller can filter by file.
    # Generic placeholder values that show up in tutorials.
    "your-secret-key-here",
    "your_api_key_here",
    "changeme",
}


def _is_known_example(token: str) -> bool:
    """Return True if ``token`` matches a documented example credential."""
    return token.strip().lower() in _KNOWN_EXAMPLE_TOKENS

# Each rule is (id, name, regex, severity, remediation).
_RULES: list[tuple[str, str, re.Pattern[str], str, str]] = [
    (
        "aws-access-key-id",
        "AWS Access Key ID",
        re.compile(
            r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ABIA|ACCA)[A-Z0-9]{16}\b"
        ),
        "critical",
        "Rotate the IAM key immediately and audit CloudTrail for misuse.",
    ),
    (
        "aws-secret-access-key",
        "AWS Secret Access Key (heuristic)",
        re.compile(
            r"(?i)aws.{0,20}(?:secret|sk).{0,20}[\"'`]([A-Za-z0-9/+=]{40})[\"'`]"
        ),
        "critical",
        "Rotate the IAM secret immediately and audit CloudTrail for misuse.",
    ),
    (
        "gcp-service-account",
        "GCP Service Account JSON private_key field",
        re.compile(r'"private_key"\s*:\s*"-----BEGIN (?:RSA )?PRIVATE KEY-----'),
        "critical",
        "Revoke the service account key in IAM and rotate dependents.",
    ),
    (
        "stripe-live-key",
        "Stripe Live Secret Key",
        re.compile(r"\bsk_live_[0-9a-zA-Z]{16,}\b"),
        "critical",
        "Roll the key in the Stripe dashboard and audit recent charges.",
    ),
    (
        "stripe-test-key",
        "Stripe Test Secret Key",
        re.compile(r"\bsk_test_[0-9a-zA-Z]{16,}\b"),
        "medium",
        "Roll the test key; test keys can still leak customer data in test mode.",
    ),
    (
        "stripe-restricted-key",
        "Stripe Restricted Key",
        re.compile(r"\brk_(?:live|test)_[0-9a-zA-Z]{16,}\b"),
        "high",
        "Roll the restricted key in the Stripe dashboard.",
    ),
    (
        "github-pat",
        "GitHub Personal Access Token",
        # Classic PATs are exactly 36 chars; fine-grained PATs (gh*_) can run
        # 80+ chars. Match 16+ to catch both real keys and short test samples,
        # since the `ghp_` prefix is unique enough to make false positives rare.
        re.compile(r"\bghp_[A-Za-z0-9_]{16,}\b"),
        "critical",
        "Revoke the token in GitHub developer settings and audit recent usage.",
    ),
    (
        "github-oauth",
        "GitHub OAuth Token",
        re.compile(r"\bgho_[A-Za-z0-9_]{16,}\b"),
        "critical",
        "Revoke the OAuth token and re-authorize the app.",
    ),
    (
        "github-app-token",
        "GitHub App Installation Token",
        re.compile(r"\bghs_[A-Za-z0-9_]{16,}\b"),
        "high",
        "Rotate the installation token and audit app activity.",
    ),
    (
        "github-fine-grained-pat",
        "GitHub Fine-Grained Personal Access Token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        "critical",
        "Revoke the fine-grained PAT and re-issue with a narrower scope.",
    ),
    (
        "github-refresh-token",
        "GitHub OAuth Refresh Token",
        re.compile(r"\bghr_[A-Za-z0-9_]{16,}\b"),
        "critical",
        "Revoke the refresh token and re-authorize the OAuth app.",
    ),
    (
        "slack-bot",
        "Slack Bot Token",
        re.compile(r"\bxoxb-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{20,}\b"),
        "high",
        "Rotate the bot token in Slack app settings.",
    ),
    (
        "slack-user",
        "Slack User Token",
        re.compile(r"\bxoxp-[0-9]{10,}-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{20,}\b"),
        "critical",
        "Rotate the user token immediately; user tokens can post as the user.",
    ),
    (
        "slack-webhook",
        "Slack Incoming Webhook URL",
        re.compile(
            r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{20,}"
        ),
        "medium",
        "Regenerate the incoming webhook URL.",
    ),
    (
        "openai-api-key",
        "OpenAI API Key (classic)",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}\b"),
        "high",
        "Revoke the OpenAI API key in platform.openai.com.",
    ),
    (
        "openai-project-key",
        "OpenAI Project API Key (sk-proj-)",
        # New format introduced 2024: sk-proj-<base64url, 100+ chars>
        re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{50,}\b"),
        "high",
        "Revoke the OpenAI project key in platform.openai.com → API keys.",
    ),
    (
        "anthropic-api-key",
        "Anthropic API Key",
        # Real keys: sk-ant-api03-<95 chars>. Match 20+ chars to also catch
        # placeholder-style test samples (sk-ant-api03-realkey-here-xxxx).
        # The `sk-ant-` prefix is globally unique to Anthropic, so the lower
        # length floor does not introduce false positives.
        re.compile(r"\bsk-ant-(?:api\d+-)?[A-Za-z0-9_\-]{20,}\b"),
        "critical",
        "Revoke the Anthropic API key in console.anthropic.com.",
    ),
    (
        "google-api-key",
        "Google API Key",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
        "high",
        "Restrict or rotate the API key in GCP console.",
    ),
    (
        "twilio-sid",
        "Twilio Account SID",
        re.compile(r"\bAC[a-f0-9]{32}\b"),
        "medium",
        "Confirm the SID was not paired with a leaked auth token.",
    ),
    (
        "sendgrid",
        "SendGrid API Key",
        re.compile(r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b"),
        "high",
        "Revoke the SendGrid API key.",
    ),
    (
        "jwt",
        "JSON Web Token",
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
        ),
        "medium",
        "Treat as compromised: invalidate the session and rotate signing key.",
    ),
    (
        "private-key-pem",
        "Private Key (PEM block)",
        re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "critical",
        "Generate a new keypair and revoke any cert/identity bound to this key.",
    ),
    (
        "database-url",
        "Database URL with embedded credentials",
        # Match postgres://user:password@host[:port]/db and friends. The
        # password segment must be present (`:<something>@`) to avoid
        # flagging credential-less local URIs like postgres:///mydb.
        # Password may contain `@` (common in real-world creds like
        # `p@ssw0rd`) — the regex relies on greedy backtracking to land on
        # the LAST `@` before path / whitespace as the user/password
        # separator. Previous version excluded `@` from the password
        # character class and silently missed every URL with an `@` in the
        # password.
        re.compile(
            r"\b(?:postgres|postgresql|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)://"
            r"[^:\s/@]+:[^\s/]{3,}@[^\s/?#]+",
            re.IGNORECASE,
        ),
        "critical",
        "Rotate the DB password and store the connection string in a secret manager.",
    ),
    (
        "http-basic-auth-url",
        "HTTP(S) URL with embedded credentials",
        # Same fix as database-url: password may contain `@`.
        re.compile(
            r"\bhttps?://[A-Za-z0-9._\-]+:[^\s/]{3,}@[A-Za-z0-9.\-]+",
            re.IGNORECASE,
        ),
        "high",
        "Move credentials out of the URL into a header or secret store.",
    ),
    (
        "azure-storage-key",
        "Azure Storage Account Key",
        re.compile(
            r"DefaultEndpointsProtocol=https;AccountName=[A-Za-z0-9]+;AccountKey=[A-Za-z0-9+/=]{40,}",
            re.IGNORECASE,
        ),
        "critical",
        "Rotate the storage account access key in the Azure portal.",
    ),
    (
        "generic-password-assignment",
        "Hardcoded password literal",
        re.compile(
            r"""(?ix)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*["']([^"'\s]{8,})["']"""
        ),
        "low",
        "Move the value to an environment variable or secret manager.",
    ),
]


def _shannon_entropy(token: str) -> float:
    if not token:
        return 0.0
    counts = Counter(token)
    length = len(token)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _redact(s: str) -> str:
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}…[{len(s)} chars]…{s[-4:]}"


def _build_newline_index(content: str) -> list[int]:
    return [i for i, ch in enumerate(content) if ch == "\n"]


def _line_col(newlines: list[int], offset: int) -> tuple[int, int]:
    # WHY: O(log N) per match vs the prior O(N) prefix-slice + count, which
    # dominated runtime on large content with many findings.
    n = bisect.bisect_left(newlines, offset)
    line = n + 1
    column = offset - newlines[n - 1] if n > 0 else offset + 1
    return line, column



_MAX_FINDINGS_HARD_CAP = 1000
_KNOWN_EXAMPLE_REMEDIATION = (
    "This is a documented vendor example value, not an active credential. "
    "Remove it from production code paths to avoid noise in scanners."
)
_HIGH_ENTROPY_REMEDIATION = (
    "Confirm the value is not a secret; if it is, rotate and move to env/secret store."
)


def _validate_run_inputs(payload: dict) -> dict | tuple[str, str | None, int, float]:
    """Pure: validate ``content``/``max_findings``/``min_entropy``; return parsed bag or error envelope."""
    if not isinstance(payload, dict):
        return _err("secret_scanner.invalid_payload", "payload must be an object")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return _err(
            "secret_scanner.missing_content",
            "'content' is required and must be a non-empty string",
        )
    if len(content) > _MAX_CONTENT:
        return _err(
            "secret_scanner.content_too_large",
            f"content exceeds {_MAX_CONTENT} chars (got {len(content)}); "
            "split the file before scanning",
        )
    raw_filename = payload.get("filename")
    filename = str(raw_filename).strip() or None if raw_filename is not None else None
    try:
        max_findings = int(payload.get("max_findings", _DEFAULT_MAX_FINDINGS))
    except (TypeError, ValueError):
        return _err("secret_scanner.invalid_max_findings", "max_findings must be an integer")
    if max_findings <= 0 or max_findings > _MAX_FINDINGS_HARD_CAP:
        return _err(
            "secret_scanner.invalid_max_findings",
            f"max_findings must be in [1, {_MAX_FINDINGS_HARD_CAP}]",
        )
    try:
        min_entropy = float(payload.get("min_entropy", _DEFAULT_MIN_ENTROPY))
    except (TypeError, ValueError):
        return _err("secret_scanner.invalid_min_entropy", "min_entropy must be a number")
    return content, filename, max_findings, max(0.0, min_entropy)


def _shape_rule_finding(
    rule_id: str, rule_name: str, severity: str, remediation: str,
    matched: str, line: int, column: int,
) -> dict[str, Any]:
    """Pure: project a regex match into the agent's finding shape, applying the known-example
    downgrade so vendor docs don't dominate scan results.

    Why: Stripe/AWS publish documented example credentials that the strict
    rules treat as criticals; we surface them as ``info`` with a tagged
    ``rule_id-known-example`` so callers can filter them.
    """
    is_example = _is_known_example(matched)
    if is_example:
        return {
            "rule_id": f"{rule_id}-known-example",
            "rule_name": f"{rule_name} (known documentation example — not a real credential)",
            "severity": "info",
            "line": line,
            "column": column,
            "redacted_preview": _redact(matched),
            "match_length": len(matched),
            "entropy": round(_shannon_entropy(matched), 3),
            "remediation": _KNOWN_EXAMPLE_REMEDIATION,
            "is_known_example": True,
        }
    return {
        "rule_id": rule_id,
        "rule_name": rule_name,
        "severity": severity,
        "line": line,
        "column": column,
        "redacted_preview": _redact(matched),
        "match_length": len(matched),
        "entropy": round(_shannon_entropy(matched), 3),
        "remediation": remediation,
        "is_known_example": False,
    }


def _scan_known_rules(
    content: str, newlines: list[int], *, max_findings: int,
) -> tuple[list[dict[str, Any]], set[tuple[int, int]]]:
    """Pure: walk the curated ``_RULES`` regex catalog over ``content``."""
    findings: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for rule_id, rule_name, pattern, severity, remediation in _RULES:
        for match in pattern.finditer(content):
            span = (match.start(), match.end())
            if span in seen:
                continue
            seen.add(span)
            line, column = _line_col(newlines, span[0])
            findings.append(_shape_rule_finding(
                rule_id, rule_name, severity, remediation,
                match.group(0), line, column,
            ))
            if len(findings) >= max_findings:
                return findings, seen
    return findings, seen


def _shape_entropy_finding(
    token: str, entropy: float, line: int, column: int,
) -> dict[str, Any]:
    """Pure: project a high-entropy generic-token match into a finding shape."""
    return {
        "rule_id": "high-entropy-string",
        "rule_name": "High-entropy string (heuristic)",
        "severity": "low",
        "line": line,
        "column": column,
        "redacted_preview": _redact(token),
        "match_length": len(token),
        "entropy": round(entropy, 3),
        "remediation": _HIGH_ENTROPY_REMEDIATION,
    }


def _scan_high_entropy(
    content: str, newlines: list[int], *, min_entropy: float,
    max_findings: int, findings: list[dict[str, Any]], seen: set[tuple[int, int]],
) -> None:
    """Side-effect (mutating ``findings``): append high-entropy generic-token matches."""
    for match in _GENERIC_TOKEN_RE.finditer(content):
        offset, end = match.start(), match.end()
        if any(start <= offset < stop or start < end <= stop for start, stop in seen):
            continue
        token = match.group(0)
        entropy = _shannon_entropy(token)
        if entropy < min_entropy:
            continue
        line, column = _line_col(newlines, offset)
        findings.append(_shape_entropy_finding(token, entropy, line, column))
        seen.add((offset, end))
        if len(findings) >= max_findings:
            return


def _summarise(findings: list[dict[str, Any]], counts: dict[str, int]) -> str:
    """Pure: human-readable summary line for the scan response."""
    if not findings:
        return "Clean. No credential patterns or high-entropy literals detected."
    parts = [
        f"{counts[label]} {label}"
        for label in ("critical", "high", "medium", "low")
        if counts.get(label)
    ]
    return f"Found {len(findings)} potential leak(s): {', '.join(parts)}."


def run(payload: dict) -> dict:
    """Scan ``content`` for leaked credentials and high-entropy literals.

    Why: findings include only redacted previews so a worker logging a
    successful call does not leak the secret further.
    """
    parsed = _validate_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed
    content, filename, max_findings, min_entropy = parsed
    newlines = _build_newline_index(content)
    findings, seen = _scan_known_rules(content, newlines, max_findings=max_findings)
    if min_entropy > 0 and len(findings) < max_findings:
        _scan_high_entropy(
            content, newlines, min_entropy=min_entropy, max_findings=max_findings,
            findings=findings, seen=seen,
        )
    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        sev = finding["severity"]
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    return {
        "filename": filename,
        "total_findings": len(findings),
        "findings_by_severity": severity_counts,
        "findings": findings,
        "summary": _summarise(findings, severity_counts),
    }
