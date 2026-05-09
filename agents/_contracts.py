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

_LOG = logging.getLogger("aztea.agents")

_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```$")

_TRUNC_MIN_HEAD_TAIL = 64
_TRUNC_MARGIN = 32


def agent_error(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if details:
        err["details"] = details
    return {"error": err}


def strip_json_fences(text: str) -> str:
    s = str(text or "").strip()
    s = _FENCE_OPEN_RE.sub("", s)
    s = _FENCE_CLOSE_RE.sub("", s)
    return s.strip()


def parse_json_payload(raw_text: str) -> Any:
    return json.loads(strip_json_fences(raw_text))


def annotate_success(
    payload: dict[str, Any],
    *,
    billing_units_actual: int | None = None,
    llm_used: bool | None = None,
    degraded_mode: bool | None = None,
) -> dict[str, Any]:
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
    head_tail_split: bool = True,
) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    if not head_tail_split or limit < _TRUNC_MIN_HEAD_TAIL:
        dropped = len(s) - limit
        return f"{s[:limit]}\n... [truncated {dropped} chars]"
    half = (limit - _TRUNC_MARGIN) // 2
    dropped = len(s) - 2 * half
    return f"{s[:half]}\n... [truncated {dropped} chars] ...\n{s[-half:]}"


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return ""


def llm_complete(
    system: str,
    user: str,
    *,
    temperature: float = 0.15,
    max_tokens: int = 800,
    agent_slug: str = "agent",
) -> str | None:
    try:
        from core.llm import CompletionRequest, Message, run_with_fallback
    except ImportError as exc:
        _LOG.warning("llm_complete[%s]: llm module unavailable: %s", agent_slug, exc)
        return None
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
        # WHY: provider-chain failures (rate limit, auth, no providers configured) must
        # not raise out of an agent — retrieval-based agents degrade by returning raw data.
        _LOG.warning("llm_complete[%s]: provider chain failed: %s", agent_slug, exc)
        return None
    if raw is None:
        return None
    text = (raw.text or "").strip()
    return text or None
