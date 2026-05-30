"""
judges.py — LLM arbitration helpers for disputes.

Three execution modes, in priority order:

1. **Hosted** — when AZTEA_HOSTED_API_URL is set, the hosted aztea.ai
   service runs the judge and we just record its verdict. This is the
   path the hosted product uses; it burns aztea.ai's LLM credits.
2. **Local LLM** — when AZTEA_ENABLE_LIVE_DISPUTE_JUDGES=1 and the
   instance has its own LLM provider configured. Two heterogeneous
   judges run via `_judge_once`.
3. **Deterministic fallback** — keyword-based heuristic when neither
   hosted nor local LLM is available. Always returns a verdict so a
   dispute never strands.

Hosted failures fall through to local-LLM; local-LLM failures fall
through to deterministic. Disputes never get stuck at "judging".
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from core import disputes
from core.functional import Err, Ok, Result
from core.llm import CompletionRequest, Message, run_with_fallback
from core.llm.registry import DEFAULT_CHAIN

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an impartial arbiter. Analyze the dispute evidence and return strict JSON "
    'with keys: {"verdict": "...", "reasoning": "...", "confidence": 0.0-1.0}. '
    "Valid verdict values are: caller_wins, agent_wins, split, void. The filer "
    "has the burden of proof. If your reasoning says the output was accurate, "
    "in-scope, or the dispute is frivolous, the verdict must be agent_wins. "
    # 2026-05-18 (D3): operators have a slot to defend their work. When the "
    "dispute object contains a non-empty ``operator_response_text``, weight "
    "it alongside the caller's reason/evidence. An operator who provides a "
    "specific, evidence-grounded defense should not lose the dispute just "
    "because the filer wrote first. Treat absence of operator_response_text "
    "as a neutral signal — the response window may simply have expired."
)
_QUALITY_SYSTEM_PROMPT = (
    "You are a strict quality judge for agent outputs. "
    "Judge correctness against the agent's contract, not whether the output found problems. "
    "For checker-style tools (for example linters, type checkers, security scanners, and validators), "
    "a clean result with zero findings can still be fully correct if the output is structured and internally consistent. "
    "Do not fail an output just because it contains no issues. "
    'Return strict JSON: {"verdict":"pass"|"fail","score":1-10,"reason":"..."} '
    "where score is an integer."
)
_CALLER_WIN_HINTS = {
    "incomplete",
    "wrong",
    "error",
    "missing",
    "refund",
    "broken",
    "incorrect",
    "hallucinated",
    "timeout",
    "failed",
}
_AGENT_WIN_HINTS = {
    "scope",
    "requirement",
    "changed",
    "abuse",
    "harass",
    "spam",
    "outside scope",
    "nonpayment",
    "threat",
    "frivolous",
    "accurate",
    "correct output",
}
_CALLER_WIN_REASONING_HINTS = {
    "agent failed",
    "agent output is wrong",
    "output is wrong",
    "incorrect output",
    "missing required",
    "incomplete output",
}
_AGENT_WIN_REASONING_HINTS = {
    "dispute is frivolous",
    "frivolous dispute",
    "agent's output was accurate",
    "agent output was accurate",
    "output was accurate",
    "agent's output is accurate",
    "agent output is accurate",
    "output is accurate",
    "agent fulfilled",
    "agent complied",
    "caller changed scope",
}


def _env_enabled(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _env_enabled_any(*names: str) -> bool:
    return any(_env_enabled(name) for name in names)


def _normalize_verdict(value: Any) -> str:
    verdict = str(value or "").strip().lower()
    if verdict not in disputes.DISPUTE_OUTCOMES:
        raise ValueError(f"Invalid judge verdict '{verdict}'.")
    return verdict


def _build_user_prompt(context: dict) -> str:
    payload = {
        "job": context["job"],
        "agent_input_schema": context.get("agent_input_schema") or {},
        "dispute": context["dispute"],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def _token_matches(text: str, vocabulary: set[str]) -> list[str]:
    lowered = str(text or "").lower()
    return sorted(token for token in vocabulary if token in lowered)


def _reasoning_contains(reasoning: str, phrases: set[str]) -> bool:
    lowered = str(reasoning or "").lower()
    return any(phrase in lowered for phrase in phrases)


def _guard_judge_consistency(
    verdict: str, reasoning: str, confidence: float
) -> tuple[str, str, float]:
    """Correct obvious verdict/reasoning contradictions before settlement."""
    if verdict == "caller_wins" and _reasoning_contains(
        reasoning, _AGENT_WIN_REASONING_HINTS
    ):
        return (
            "agent_wins",
            reasoning
            + " Deterministic consistency guard corrected verdict from caller_wins to agent_wins.",
            min(confidence, 0.65),
        )
    if verdict == "agent_wins" and _reasoning_contains(
        reasoning, _CALLER_WIN_REASONING_HINTS
    ):
        return (
            "caller_wins",
            reasoning
            + " Deterministic consistency guard corrected verdict from agent_wins to caller_wins.",
            min(confidence, 0.65),
        )
    return verdict, reasoning, confidence


_FRIVOLOUS_PHRASES = (
    "frivolous",
    "output was accurate",
    "output is accurate",
)
_DISPUTE_VERDICT_DELTA_THRESHOLD = 2


def _collect_dispute_signal_hits(combined: str, job: dict) -> tuple[list[str], list[str]]:
    """Pure: per-side hint matches in dispute reason/evidence + job error message."""
    caller_hits = _token_matches(combined, _CALLER_WIN_HINTS)
    agent_hits = _token_matches(combined, _AGENT_WIN_HINTS)
    if not job.get("output_payload"):
        caller_hits.append("missing_output")
    lowered_combined = combined.lower()
    if any(phrase in lowered_combined for phrase in _FRIVOLOUS_PHRASES):
        agent_hits.extend(["frivolous_dispute", "accurate_output"])
    return caller_hits, agent_hits


def _local_dispute_fallback(context: dict) -> dict:
    """Pure: deterministic verdict when no LLM judge is available.

    Why: keeps disputes from stranding when live judges are off; bias
    toward agent_wins on near-ties matches the historical default.

    F5 (red-team 2026-05-19): the previous version added ``+1`` to the
    score of whichever side filed the dispute. Because callers file the
    vast majority of disputes, that bonus structurally biased the
    fallback toward ``caller_wins`` on every secondary-LLM outage. A
    JWT-validator dispute reproed this — primary LLM said agent_wins,
    secondary LLM was unreachable, fallback flipped to caller_wins, and
    the filing deposit refunded. The scoring is now purely
    evidence-driven: side identifies who filed but doesn't pre-load the
    score.
    """
    dispute = context.get("dispute") or {}
    job = context.get("job") or {}
    combined = " ".join(part for part in [
        str(dispute.get("reason") or "").strip(),
        str(dispute.get("evidence") or "").strip(),
        str(job.get("error_message") or ""),
    ] if part).strip()
    caller_hits, agent_hits = _collect_dispute_signal_hits(combined, job)
    side = str(dispute.get("side") or "").strip().lower()
    caller_score = len(caller_hits)
    agent_score = len(agent_hits)
    delta = caller_score - agent_score
    verdict = "caller_wins" if delta >= _DISPUTE_VERDICT_DELTA_THRESHOLD else "agent_wins"
    confidence = min(0.9, max(0.55, 0.55 + (abs(delta) * 0.08)))
    return {
        "verdict": verdict,
        "reasoning": (
            "Deterministic fallback judge (live LLM disabled): "
            f"caller_signals={caller_hits or ['none']}, "
            f"agent_signals={agent_hits or ['none']}, "
            f"side={side or 'unknown'}, verdict={verdict}."
        ),
        "confidence": confidence,
    }


_SYSTEM_PROMPT_DEVILS_ADVOCATE = (
    "You are a strict second-opinion arbiter. Your job is to STRESS-TEST the "
    "filer's claim from a different angle than the primary judge would. Read "
    "the dispute text carefully. If the dispute reason is vague, lacks "
    "specific contractual or factual deviation, or amounts to dissatisfaction "
    "without proof of error, lean toward agent_wins. If the dispute cites a "
    "specific verifiable failure of the agent's contract, lean toward "
    'caller_wins. Return strict JSON {"verdict": "caller_wins"|"agent_wins"|'
    '"split"|"void", "reasoning": "...", "confidence": 0.0-1.0}.'
)


def _parse_judge_response(content: str) -> tuple[str, str, float]:
    """Pure: parse the LLM judge JSON response; raises ``RuntimeError`` on bad shape.

    Why: separating the parse keeps ``_judge_once`` focused on the
    network call + final shaping; failure modes here are JSON / missing
    reasoning, all distinct from provider failures.
    """
    if not content:
        raise RuntimeError("Judge returned empty content.")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Judge returned non-JSON content.") from exc
    verdict = _normalize_verdict(parsed.get("verdict"))
    reasoning = str(parsed.get("reasoning") or "").strip()
    if not reasoning:
        raise RuntimeError("Judge returned empty reasoning.")
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return verdict, reasoning, max(0.0, min(1.0, confidence))


def _judge_once(
    model_chain: list[str],
    context: dict,
    *,
    system_prompt: str = _SYSTEM_PROMPT,
    temperature: float = 0.0,
) -> dict:
    """Side-effect: one LLM judgement pass against the supplied model chain."""
    llm_resp = run_with_fallback(
        CompletionRequest(
            model="",
            messages=[
                Message("system", system_prompt),
                Message("user", _build_user_prompt(context)),
            ],
            temperature=temperature,
            json_mode=True,
        ),
        model_chain=model_chain,
    )
    verdict, reasoning, confidence = _parse_judge_response(llm_resp.text)
    verdict, reasoning, confidence = _guard_judge_consistency(
        verdict, reasoning, confidence,
    )
    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "confidence": confidence,
        "model": f"{llm_resp.provider}:{llm_resp.model}",
    }


def _validate_dispute_judgeable(dispute_id: str) -> "Result[dict, str]":
    """Pure guard: returns Ok(context) if the dispute exists and is not terminal."""
    context = disputes.get_dispute_context(dispute_id)
    if context is None:
        return Err(f"Dispute '{dispute_id}' not found.")
    return Ok(context)


def _parse_hosted_judgment_response(
    response: Any, dispute_id: str,
) -> tuple[str, str, str] | None:
    """Pure: pull ``(verdict, reasoning, model_label)`` out of the hosted response."""
    if not response or not isinstance(response, dict):
        return None
    try:
        verdict = _normalize_verdict(response.get("verdict"))
    except ValueError:
        logger.warning(
            "judges: hosted judge returned invalid verdict %r for dispute %s",
            response.get("verdict"), dispute_id,
        )
        return None
    reasoning = str(response.get("reasoning") or "").strip() or "Hosted judge."
    model_label = str(response.get("model") or "hosted").strip() or "hosted"
    return verdict, reasoning, model_label


def _try_hosted_judgment(dispute_id: str, context: dict) -> dict | None:
    """Side-effect: resolve a dispute via the hosted aztea.ai judge if available.

    Why: aztea.ai's hosted judge runs heterogeneous votes server-side,
    so we record both primary + secondary as the same verdict. Returns
    ``None`` when hosted mode is disabled or the call fails for any
    reason; callers fall back to local LLM / deterministic.
    """
    # WHY (rule 11): local import avoids a hard dep when hosted_client is
    # absent in trimmed-down test fixtures.
    from core.hosted_client import get_hosted_client

    client = get_hosted_client()
    if not client.is_enabled():
        return None
    parsed = _parse_hosted_judgment_response(client.judge_dispute(context), dispute_id)
    if parsed is None:
        return None
    verdict, reasoning, model_label = parsed
    for kind in ("llm_primary", "llm_secondary"):
        disputes.record_judgment(
            dispute_id, judge_kind=kind, verdict=verdict,
            reasoning=reasoning, model=f"hosted:{model_label}",
        )
    disputes.set_dispute_consensus(dispute_id, verdict)
    return {
        "status": "consensus",
        "outcome": verdict,
        "judgments": disputes.get_judgments(dispute_id),
    }


_TERMINAL_DISPUTE_STATUSES = frozenset({"resolved", "final"})
_FALLBACK_PRIMARY_TEMPERATURE = 0.1
_FALLBACK_SAME_MODEL_TEMPERATURE = 0.4


def _terminal_dispute_response(context: dict, dispute_id: str) -> dict:
    """Pure-ish: return the existing outcome when a dispute is already resolved/final."""
    return {
        "status": str(context["dispute"].get("status") or "").strip().lower(),
        "outcome": context["dispute"].get("outcome"),
        "judgments": disputes.get_judgments(dispute_id),
    }


def _record_dual_fallback_consensus(dispute_id: str, context: dict) -> dict:
    """Side-effect: record two fallback judgments + set consensus.

    Why: when live judges are disabled the deterministic fallback
    contributes both votes; the dispute settles immediately so it never
    sits in 'judging' waiting for a human.
    """
    fallback = _local_dispute_fallback(context)
    verdict = fallback["verdict"]
    reasoning = fallback["reasoning"]
    for kind in ("llm_primary", "llm_secondary"):
        disputes.record_judgment(
            dispute_id, judge_kind=kind, verdict=verdict,
            reasoning=reasoning, model="fallback",
        )
    disputes.set_dispute_consensus(dispute_id, verdict)
    return {
        "status": "consensus",
        "outcome": verdict,
        "judgments": disputes.get_judgments(dispute_id),
    }


def _run_primary_judgment(dispute_id: str, context: dict) -> dict:
    """Side-effect: run the primary LLM judge; recovers stuck status on failure.

    Why: a provider failure mid-judge would otherwise leave the dispute
    in 'judging' forever; resetting to 'pending' lets the sweeper retry.
    """
    try:
        primary = _judge_once(list(DEFAULT_CHAIN), context)
    except Exception:
        disputes.set_dispute_status(dispute_id, "pending")
        raise
    disputes.record_judgment(
        dispute_id, judge_kind="llm_primary",
        verdict=primary["verdict"], reasoning=primary["reasoning"],
        model=primary["model"],
    )
    return primary


def _select_secondary_chain(primary_model: str) -> tuple[list[str], bool]:
    """Pure: pick a chain for the secondary judge that excludes the primary's model.

    Why: heterogeneous voting needs failure-mode independence. If only one
    provider is configured the chain falls back to the same model, but
    callers vary the prompt + temperature so the verdict is at least
    probabilistically decorrelated.
    """
    rotated = DEFAULT_CHAIN[1:] + DEFAULT_CHAIN[:1]
    chain = [spec for spec in rotated if spec != primary_model]
    if chain:
        return chain, False
    return list(DEFAULT_CHAIN), True


def _settle_via_tiebreaker_after_secondary_failure(
    dispute_id: str, context: dict,
) -> dict:
    """Side-effect: secondary LLM failed — record a fallback judgment + set consensus.

    Why: without this the dispute would strand at 'judging' with only one
    orphan judgment.

    B13, 2026-05-19: also writes a structured ``secondary_judge_fallback``
    audit event so dispute_status responses can surface ``degraded_mode``
    + ``degraded_reason`` to the caller. Pre-fix the fallback was visible
    only by inspecting judge_models_used (= ["llm_primary", "fallback"]),
    which silently collapsed the advertised "two-judge guarantee" to one
    judge + a coin-flip without telling anyone.
    """
    tiebreaker = _local_dispute_fallback(context)
    verdict = tiebreaker["verdict"]
    disputes.record_judgment(
        dispute_id, judge_kind="llm_secondary",
        verdict=verdict,
        reasoning="Secondary LLM unavailable; deterministic fallback used: "
        + tiebreaker["reasoning"],
        model="fallback",
    )
    disputes.set_dispute_consensus(dispute_id, verdict)
    disputes.append_audit_event(
        dispute_id,
        "secondary_judge_fallback",
        extra={
            "reason": "secondary_judge_llm_unavailable",
            "verdict": verdict,
            "fallback_kind": "deterministic_tiebreaker",
        },
    )
    return {
        "status": "consensus",
        "outcome": verdict,
        "judgments": disputes.get_judgments(dispute_id),
        # B13: surface the degraded path so the caller can decide whether
        # to accept the consensus or escalate.
        "degraded_mode": True,
        "degraded_reason": "secondary_judge_llm_unavailable",
    }


def _run_secondary_or_promote_primary(
    dispute_id: str, context: dict, primary: dict,
) -> tuple[dict | None, dict | None]:
    """Side-effect: run the secondary judge or promote primary on LLM failure.

    Returns ``(secondary_dict, None)`` on success, or
    ``(None, response)`` when the secondary failed and the dispute has
    already been settled via the deterministic tiebreaker.
    """
    primary_model = str(primary.get("model") or "").strip()
    secondary_chain, fallback_to_same_model = _select_secondary_chain(primary_model)
    try:
        secondary = _judge_once(
            secondary_chain, context,
            system_prompt=_SYSTEM_PROMPT_DEVILS_ADVOCATE,
            temperature=(
                _FALLBACK_SAME_MODEL_TEMPERATURE
                if fallback_to_same_model else _FALLBACK_PRIMARY_TEMPERATURE
            ),
        )
    except Exception:
        return None, _settle_via_tiebreaker_after_secondary_failure(dispute_id, context)
    suffix = (
        " (devil's-advocate prompt; same model as primary — heterogeneous LLM unavailable)"
        if fallback_to_same_model and str(secondary.get("model") or "") == primary_model
        else ""
    )
    disputes.record_judgment(
        dispute_id, judge_kind="llm_secondary",
        verdict=secondary["verdict"], reasoning=secondary["reasoning"],
        model=secondary["model"] + suffix,
    )
    return secondary, None


def _resolve_consensus_or_tie(
    dispute_id: str, context: dict, primary: dict, secondary: dict,
) -> dict:
    """Side-effect: settle a dispute on agreement, deterministic tiebreaker on disagreement."""
    if primary["verdict"] == secondary["verdict"] and primary["verdict"] != "split":
        disputes.set_dispute_consensus(dispute_id, primary["verdict"])
        return {
            "status": "consensus",
            "outcome": primary["verdict"],
            "judgments": disputes.get_judgments(dispute_id),
        }
    tiebreaker = _local_dispute_fallback(context)
    tiebreaker_verdict = tiebreaker["verdict"]
    disputes.record_judgment(
        dispute_id, judge_kind="human_admin",
        verdict=tiebreaker_verdict,
        reasoning="Deterministic tiebreaker after LLM disagreement: "
        + tiebreaker["reasoning"],
        model="deterministic", admin_user_id="system_tiebreaker",
    )
    if (
        tiebreaker_verdict != "split"
        and tiebreaker_verdict in {primary["verdict"], secondary["verdict"]}
    ):
        disputes.set_dispute_consensus(dispute_id, tiebreaker_verdict)
        return {
            "status": "consensus",
            "outcome": tiebreaker_verdict,
            "judgments": disputes.get_judgments(dispute_id),
        }
    disputes.set_dispute_tied(dispute_id)
    return {
        "status": "tied",
        "outcome": None,
        "judgments": disputes.get_judgments(dispute_id),
    }


def run_judgment(dispute_id: str) -> dict:
    """Side-effect: orchestrate LLM-based adjudication for a dispute.

    Why: a dispute needs two agreeing judge votes before it can be
    auto-resolved. The orchestrator chains the two votes plus a
    deterministic tiebreaker so disputes always reach a terminal state.
    Raises ``ValueError`` if the dispute is not found or already in a
    terminal state (``"resolved"`` or ``"final"``).
    """
    guard = _validate_dispute_judgeable(dispute_id)
    guard.raise_on_err()
    context = guard.value
    current_status = str(context["dispute"].get("status") or "").strip().lower()
    if current_status in _TERMINAL_DISPUTE_STATUSES:
        return _terminal_dispute_response(context, dispute_id)
    disputes.set_dispute_status(dispute_id, "judging")
    hosted_result = _try_hosted_judgment(dispute_id, context)
    if hosted_result is not None:
        return hosted_result
    live_enabled = _env_enabled_any(
        "AZTEA_ENABLE_LIVE_DISPUTE_JUDGES", "AGENTMARKET_ENABLE_LIVE_DISPUTE_JUDGES",
    )
    if not live_enabled:
        return _record_dual_fallback_consensus(dispute_id, context)
    primary = _run_primary_judgment(dispute_id, context)
    secondary, early_response = _run_secondary_or_promote_primary(
        dispute_id, context, primary,
    )
    if early_response is not None:
        return early_response
    return _resolve_consensus_or_tie(dispute_id, context, primary, secondary)


def _output_schema_permits_error_envelope(output_schema: dict | None) -> bool:
    """Pure: True if the agent's documented output_schema explicitly lists
    `error` (or `errors`, `exception`) as an allowed top-level field.

    Used by the quality judge to distinguish a schema-permitted structured
    error envelope (agent did its job — reported failure cleanly per
    contract) from an unstructured crash (agent broke). B11, 2026-05-19.

    Conservative — only returns True when the schema is a dict with a
    `properties` dict that includes one of the error-shaped keys. Any
    other shape (None, list, missing properties) is treated as "schema
    doesn't permit error envelope" so the heuristic stays strict for
    agents that haven't declared one.
    """
    if not isinstance(output_schema, dict):
        return False
    properties = output_schema.get("properties")
    if not isinstance(properties, dict):
        return False
    return any(key in properties for key in ("error", "errors", "exception"))


def _looks_like_unstructured_crash(payload: dict) -> bool:
    """Pure: True if any string value in payload smells like a stack trace
    or unstructured exception spill — the signal that an agent crashed
    rather than reported a structured error.

    Heuristic — looks for ``Traceback``, ``Exception:``, ``Error:`` at
    line starts, and common Python/JS stack-trace markers. Conservative
    by design (we'd rather miss a real crash than false-positive on an
    agent reporting an error field).
    """
    crash_markers = (
        "Traceback (most recent call last)",
        "\nTraceback ",
        "\n    at ",  # JS stack frames
        "Exception: ",
        "SyntaxError: ",
        "TypeError: ",
        "ValueError: ",
        "RuntimeError: ",
        "NameError: ",
        "AttributeError: ",
        "KeyError: ",
        "IndexError: ",
    )
    return any(
        isinstance(value, str) and any(m in value for m in crash_markers)
        for value in payload.values()
    )


def _local_quality_fallback(
    *,
    input_payload: dict | None,
    output_payload: dict | None,
    agent_description: str = "",
    output_schema: dict | None = None,
) -> dict:
    """B11, 2026-05-19: distinguish schema-permitted error envelopes
    (agent's documented success path — pass) from unstructured crashes
    (agent broke — fail). Pre-fix this heuristic fired "fail" on any
    payload with an `error` key, which punished agents like jwt_validator
    that correctly return `{error: "invalid_signature", ...}` for invalid
    input. The agent did its job; the judgment now reflects that.
    """
    payload = output_payload if isinstance(output_payload, dict) else {}
    if not payload:
        return {
            "verdict": "fail",
            "score": 1,
            "reason": "Output payload is empty.",
            "judge_reason_detail": "empty_payload",
        }
    has_error_field = any(
        payload.get(field) for field in ("error", "errors", "exception")
    )
    if has_error_field:
        schema_permits_error = _output_schema_permits_error_envelope(output_schema)
        unstructured = _looks_like_unstructured_crash(payload)
        if schema_permits_error and not unstructured:
            # Agent returned a documented structured error envelope — that's
            # success-with-degraded-result, not failure. Score modestly to
            # reflect that the run worked but produced a non-happy output.
            return {
                "verdict": "pass",
                "score": 6,
                "reason": (
                    "Output is a schema-permitted structured error envelope "
                    "(agent fulfilled its contract by reporting a clean "
                    "structured failure). B11, 2026-05-19."
                ),
                "judge_reason_detail": "schema_permitted_error_envelope",
            }
        # Either the schema doesn't declare an error field OR the payload
        # looks like an unstructured crash. Keep the historic fail verdict.
        return {
            "verdict": "fail",
            "score": 2,
            "reason": (
                "Output payload contains an unstructured error / exception "
                "trace not declared in the agent's output_schema."
            ),
            "judge_reason_detail": (
                "unstructured_crash" if unstructured else "undeclared_error_field"
            ),
        }

    filled_fields = [
        key for key, value in payload.items() if value not in (None, "", [], {}, ())
    ]
    text_chars = sum(
        len(value.strip()) for value in payload.values() if isinstance(value, str)
    )
    structured_sections = sum(
        1
        for value in payload.values()
        if isinstance(value, (dict, list)) and len(value) > 0
    )
    score = 5
    score += min(2, len(filled_fields) // 2)
    score += 1 if text_chars >= 120 else 0
    score += 1 if structured_sections > 0 else 0
    score = max(1, min(10, score))
    verdict = "pass" if score >= 6 else "fail"
    reason = (
        "Deterministic fallback quality judge (live LLM disabled): "
        f"filled_fields={len(filled_fields)}, text_chars={text_chars}, "
        f"structured_sections={structured_sections}, input_keys={len(input_payload or {})}, "
        f"agent_desc_present={bool(str(agent_description).strip())}."
    )
    return {
        "verdict": verdict,
        "score": score,
        "reason": reason,
        "judge_reason_detail": "deterministic_heuristic",
    }


_QUALITY_SCORE_MIN = 1
_QUALITY_SCORE_MAX = 10
_QUALITY_DEFAULT_PASS_SCORE = 7
_QUALITY_DEFAULT_FAIL_SCORE = 1


def _build_quality_prompt(
    *, input_payload: dict, output_payload: dict, agent_name: str,
    agent_description: str, quality_hint: str,
) -> str:
    """Pure: deterministic JSON prompt for the quality LLM (sort_keys for cache stability)."""
    return json.dumps(
        {
            "input_payload": input_payload,
            "output_payload": output_payload,
            "agent_name": agent_name,
            "agent_description": agent_description,
            "quality_hint": quality_hint,
        },
        sort_keys=True, ensure_ascii=True,
    )


def _invoke_quality_llm(user_prompt: str) -> str | None:
    """Side-effect: call the LLM chain; ``None`` on any provider/network failure."""
    try:
        llm_resp = run_with_fallback(CompletionRequest(
            model="",
            messages=[
                Message("system", _QUALITY_SYSTEM_PROMPT),
                Message("user", user_prompt),
            ],
            temperature=0.0,
            json_mode=True,
        ))
    except Exception:
        return None
    return llm_resp.text or None


def _parse_quality_verdict(content: str) -> dict | None:
    """Pure: project the LLM's JSON response into ``{verdict, score, reason}``; ``None`` on bad shape."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "fail"}:
        return None
    try:
        score = int(parsed.get("score"))
    except (TypeError, ValueError):
        score = _QUALITY_DEFAULT_FAIL_SCORE if verdict == "fail" else _QUALITY_DEFAULT_PASS_SCORE
    score = max(_QUALITY_SCORE_MIN, min(_QUALITY_SCORE_MAX, score))
    reason = str(parsed.get("reason") or "").strip() or "No reason provided."
    return {"verdict": verdict, "score": score, "reason": reason}


def run_quality_judgment(
    *,
    input_payload: dict,
    output_payload: dict,
    agent_description: str,
    agent_name: str = "",
    quality_hint: str = "",
    output_schema: dict | None = None,
) -> dict:
    """Side-effect: score a completed job output for quality using an LLM judge.

    Why: the judgment score (1–10) drives ``core.payout_curve`` clawbacks.
    Returns ``{verdict, score, reason}``. Falls back to a deterministic
    heuristic when the live judge is disabled or any LLM step fails — the
    fallback never raises, so settlement is never blocked on quality.

    ``output_schema`` is forwarded to the deterministic fallback so the
    "schema-permitted error envelope" exemption (B11, 2026-05-19) can fire
    when the agent legitimately returns ``{error: ...}`` per its contract.
    """
    fallback_kwargs = dict(
        input_payload=input_payload, output_payload=output_payload,
        agent_description=agent_description, output_schema=output_schema,
    )
    if not _env_enabled_any(
        "AZTEA_ENABLE_LIVE_QUALITY_JUDGE", "AGENTMARKET_ENABLE_LIVE_QUALITY_JUDGE",
    ):
        return _local_quality_fallback(**fallback_kwargs)
    user_prompt = _build_quality_prompt(
        input_payload=input_payload, output_payload=output_payload,
        agent_name=agent_name, agent_description=agent_description,
        quality_hint=quality_hint,
    )
    content = _invoke_quality_llm(user_prompt)
    if not content:
        return _local_quality_fallback(**fallback_kwargs)
    parsed = _parse_quality_verdict(content)
    if parsed is None:
        return _local_quality_fallback(**fallback_kwargs)
    return parsed
