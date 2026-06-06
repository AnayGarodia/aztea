"""
privacy.py — Pure PII/sensitivity primitives shared across the codebase.

# OWNS: the field-name redaction allowlist, the deep redactor, the hardcoded
#   sensitive-agent set, and the agent-flag sensitivity predicate.
# NOT OWNS: input-based gates like "private_task" (that lives with the request
#   envelope handling), recording policy, or any I/O.
# INVARIANTS: every function here is PURE (no I/O, no mutation of inputs) so it
#   can be reused from both server shards and core/ without violating the
#   one-way dependency rule (core never imports server).
# DECISIONS: extracted from server/application_parts/part_003.py (2026-06) so
#   the hosted-skill learnings distiller in core/observability.py can apply the
#   exact same gate + redaction the public work-example recorder uses. part_003
#   now imports these under its historical private names.
"""

from __future__ import annotations

import re
from typing import Any

# Hardcoded sensitive built-in agent IDs. Defense against spec drift: even if a
# spec flag is dropped, these never feed cross-tenant surfaces (public examples,
# learnings distillation). Mirrors the rationale documented inline below.
SENSITIVE_EXAMPLE_AGENT_IDS: frozenset[str] = frozenset(
    {
        # Secret Scanner — inputs are credentials/source code by definition.
        "1021c65c-d2bf-54ff-823a-897f9deb1029",
        # Python Code Executor — caller source routinely contains private logic.
        "040dc3f5-afe7-5db7-b253-4936090cc7af",
        # DB Sandbox — caller schemas/queries leak business model details.
        "be4d6c18-629d-5b1c-8c46-f82c00db4995",
        # Multi-language Executor — same rationale as the Python sandbox.
        "d4b2c3e5-f6a7-5b8c-9d0e-1f2a3b4c5d6e",
        # Dependency Auditor — manifests disclose private internal package names.
        "11fab82a-426e-513e-abf3-528d99ef2b87",
    }
)

# Field-name-based redaction. Names matched case-insensitively as substrings.
# Catches conditionally-sensitive outputs (e.g. join_token from a share action)
# that a per-agent flag would miss.
SENSITIVE_FIELD_SUBSTRINGS: tuple[str, ...] = (
    "token", "secret", "password", "passwd", "passphrase",
    "private_key", "api_key", "auth", "credential",
    "signed_payload", "signature_priv",
    "join_token", "share_id", "session_cookie", "cookie",
    "public_url", "capture_url", "tunnel_url", "webhook_url",
    "x-aztea-signature",
)


def is_sensitive_field_name(name: str) -> bool:
    """Pure: True if a key name matches the redaction allowlist (case-insensitive)."""
    lowered = str(name or "").lower()
    return any(marker in lowered for marker in SENSITIVE_FIELD_SUBSTRINGS)


def redact_sensitive(value: Any) -> Any:
    """Pure: deep-walk a value, replacing sensitive-named fields with '<redacted>'.

    Lists are walked element-wise, dicts recursively, scalars pass through. The
    result is a new object — the input is never mutated.
    """
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if is_sensitive_field_name(key)
                  else redact_sensitive(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


# High-signal secret / PII shapes for scrubbing scalar FREE-TEXT (dispute
# reason/evidence, judge reasoning, distilled bullet text). redact_sensitive is
# field-NAME based and cannot touch a value buried in prose; this is the
# value-based complement. Deliberately conservative: only well-known token
# shapes + emails, to avoid mangling legitimate behavioral guidance.
_FREETEXT_SCRUB_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),   # email
    re.compile(r"\b(?:sk|az|azac|azw|azc)[-_][A-Za-z0-9]{12,}\b"),     # openai/aztea-style keys
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),                            # github PAT
    re.compile(r"\bxox[bpao]-[A-Za-z0-9\-]{10,}\b"),                    # slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                # aws access key
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}\b"),               # bearer token
)


def scrub_freetext(text: str) -> str:
    """Pure: redact well-known secret/PII token shapes from a scalar string.

    Complements redact_sensitive (which only redacts by field NAME). Used on
    caller-derived free-text before it reaches the distiller LLM and on the
    distilled bullet before it is persisted, so cross-tenant prose can't carry a
    pasted key or email through to another owner / a future injected prompt.
    """
    out = text or ""
    for pat in _FREETEXT_SCRUB_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out


def is_example_sensitive_agent(agent: dict) -> bool:
    """Pure: True if an agent's flags forbid cross-tenant reuse of its I/O.

    The same five-layer gate the public work-example recorder uses (minus the
    input-based private_task check): hardcoded ID set, examples_sensitive,
    Security category, pii_safe self-declaration, outputs_not_stored promise.
    Any one is disqualifying. Used to skip a skill entirely before distilling
    learnings from its caller comments / examples / disputes.
    """
    if not isinstance(agent, dict):
        return False
    agent_id = str(agent.get("agent_id") or "").strip()
    if agent_id in SENSITIVE_EXAMPLE_AGENT_IDS:
        return True
    if bool(agent.get("examples_sensitive")):
        return True
    if str(agent.get("category") or "").strip().lower() == "security":
        return True
    if bool(agent.get("pii_safe")):
        return True
    if bool(agent.get("outputs_not_stored")):
        return True
    return False
