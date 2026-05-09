from __future__ import annotations

# OWNS: shared agent-side primitives — error envelopes, JSON-fence parsing,
#       success annotation, truncation, host extraction, LLM convenience wrapper.
# NOT OWNS: HTTP fetching (see _http.py), subprocess sandboxing (see _subprocess.py),
#           SSRF validation (see core.url_security), output shaping (see core.output_shaping).
# INVARIANTS:
#   * agent_error always returns {"error": {"code", "message", [details]}} — never raise.
#   * llm_complete must call CompletionRequest with model="" so the fallback chain selects the model.
#   * llm_complete must use raw.text, never raw.content (the latter is silently None at runtime).
# DECISIONS:
#   * llm_complete swallows provider failures and returns None so retrieval-based agents can
#     degrade gracefully. Callers decide whether to surface a partial result.

import json
import logging
import re
from typing import Any, Literal
from urllib.parse import urlparse

Severity = Literal["info", "low", "medium", "high", "critical"]
TruncationStyle = Literal["head_tail", "tail_only"]

_LOG = logging.getLogger("aztea.agents")

_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```$")

# Below this limit a head+tail split has no useful head; fall back to tail-only.
_TRUNC_MIN_HEAD_TAIL = 64
# Reserved for the `\n...[truncated N chars]...\n` marker so head+tail fits in `limit`.
_TRUNC_MARGIN = 32
_DEFAULT_LLM_TEMPERATURE = 0.15
_DEFAULT_LLM_MAX_TOKENS = 800


def agent_error(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical error envelope.

    Why: every agent must return a uniform shape so the settlement layer
    can refund automatically and renderers can produce consistent UX.
    """
    if not code or not isinstance(code, str):
        raise ValueError(f"agent_error: code must be a non-empty str, got {code!r}")
    if not isinstance(message, str):
        raise TypeError(f"agent_error: message must be str, got {type(message).__name__}")
    err: dict[str, Any] = {"code": code, "message": message}
    if details:
        err["details"] = details
    return {"error": err}


def strip_json_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from a JSON-looking string.

    Why: LLMs frequently wrap JSON output in ```json ... ``` despite being
    told not to; agents that parse with json.loads must strip first.
    """
    s = str(text or "").strip()
    s = _FENCE_OPEN_RE.sub("", s)
    s = _FENCE_CLOSE_RE.sub("", s)
    return s.strip()


def parse_json_payload(raw_text: str) -> Any:
    """Parse LLM output as JSON after fence-stripping. Raises ValueError on bad JSON."""
    return json.loads(strip_json_fences(raw_text))


def annotate_success(
    payload: dict[str, Any],
    *,
    billing_units_actual: int | None = None,
    llm_used: bool | None = None,
    degraded_mode: bool | None = None,
) -> dict[str, Any]:
    """Return a copy of ``payload`` with optional billing/quality annotations attached.

    Why: agents must signal degraded-mode and llm-used flags so the settlement
    layer can adjust pricing; doing it at the boundary (here) instead of inside
    each agent keeps the contract single-sourced.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"annotate_success: payload must be dict, got {type(payload).__name__}")
    result = dict(payload)
    if billing_units_actual is not None:
        result["billing_units_actual"] = int(billing_units_actual)
    if llm_used is not None:
        result["llm_used"] = bool(llm_used)
    if degraded_mode is not None:
        result["degraded_mode"] = bool(degraded_mode)
    return result


def truncate_with_marker(
    text: str,
    limit: int,
    *,
    style: TruncationStyle = "head_tail",
) -> str:
    """Shorten ``text`` to ``limit`` chars, preserving signal.

    Why: agent logs and stderr blobs need to be both inspectable and
    bounded; a head+tail split keeps the call site and the failure point
    visible while a tail-only style is preferred when the head is noisy
    boilerplate.
    """
    if limit <= 0:
        raise ValueError(f"truncate_with_marker: limit must be positive, got {limit!r}")
    s = str(text or "")
    if len(s) <= limit:
        return s
    if style == "tail_only" or limit < _TRUNC_MIN_HEAD_TAIL:
        dropped = len(s) - limit
        return f"{s[:limit]}\n... [truncated {dropped} chars]"
    half = (limit - _TRUNC_MARGIN) // 2
    dropped = len(s) - 2 * half
    return f"{s[:half]}\n... [truncated {dropped} chars] ...\n{s[-half:]}"


def host_of(url: str) -> str:
    """Return lowercase hostname or empty string. Pure; never raises.

    Why: agents need a stable host identifier for routing/SSRF logging
    and the urlparse error surface differs across malformed inputs.
    """
    try:
        return (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return ""


def _import_llm_module(agent_slug: str) -> tuple[Any, Any, Any] | None:
    """Side-effect: lazy import of core.llm primitives. Returns ``(CompletionRequest, Message, runner)`` or None.

    Why (rule 11): the import is intentionally lazy — agents must remain
    importable in test fixtures and on workers without provider keys.
    """
    try:
        from core.llm import CompletionRequest, Message, run_with_fallback
        return CompletionRequest, Message, run_with_fallback
    except ImportError as exc:
        _LOG.warning("llm_complete[%s]: llm module unavailable: %s", agent_slug, exc)
        return None


def llm_complete(
    system: str,
    user: str,
    *,
    temperature: float = _DEFAULT_LLM_TEMPERATURE,
    max_tokens: int = _DEFAULT_LLM_MAX_TOKENS,
    agent_slug: str = "agent",
) -> str | None:
    """Run an LLM completion and return stripped text, or None on degradation.

    Why: centralises the model="" + raw.text invariants so individual agents
    never reach for the LLM provider directly; returning None lets retrieval-
    based agents degrade gracefully when no provider is configured.
    """
    if not isinstance(system, str) or not isinstance(user, str):
        raise TypeError("llm_complete: system and user must be str")
    imported = _import_llm_module(agent_slug)
    if imported is None:
        return None
    CompletionRequest, Message, run_with_fallback = imported
    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=system),
            Message(role="user", content=user),
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    try:
        raw = run_with_fallback(req)
    except Exception as exc:
        # WHY: provider SDKs raise heterogeneous exceptions; degrade gracefully.
        _LOG.warning("llm_complete[%s]: provider chain failed: %s", agent_slug, exc)
        return None
    if raw is None:
        return None
    text = (raw.text or "").strip()
    return text or None
