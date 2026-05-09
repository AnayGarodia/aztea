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
    "in-scope, or the dispute is frivolous, the verdict must be agent_wins."
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
    for name in names:
        if _env_enabled(name):
            return True
    return False


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


def _local_dispute_fallback(context: dict) -> dict:
    dispute = context.get("dispute") or {}
    job = context.get("job") or {}
    reason = str(dispute.get("reason") or "").strip()
    evidence = str(dispute.get("evidence") or "").strip()
    combined = " ".join(
        part for part in [reason, evidence, str(job.get("error_message") or "")] if part
    ).strip()

    caller_hits = _token_matches(combined, _CALLER_WIN_HINTS)
    agent_hits = _token_matches(combined, _AGENT_WIN_HINTS)
    output_payload = job.get("output_payload")
    if not output_payload:
        caller_hits.append("missing_output")
    lowered_combined = combined.lower()
    if (
        "frivolous" in lowered_combined
        or "output was accurate" in lowered_combined
        or "output is accurate" in lowered_combined
    ):
        agent_hits.extend(["frivolous_dispute", "accurate_output"])

    side = str(dispute.get("side") or "").strip().lower()
    caller_score = len(caller_hits) + (1 if side == "caller" else 0)
    agent_score = len(agent_hits) + (1 if side == "agent" else 0)
    delta = caller_score - agent_score

    if delta >= 2:
        verdict = "caller_wins"
    elif delta <= -2:
        verdict = "agent_wins"
    else:
        verdict = "agent_wins"

    confidence = min(0.9, max(0.55, 0.55 + (abs(delta) * 0.08)))
    fallback_reason = (
        "Deterministic fallback judge (live LLM disabled): "
        f"caller_signals={caller_hits or ['none']}, agent_signals={agent_hits or ['none']}, "
        f"side={side or 'unknown'}, verdict={verdict}."
    )
    return {"verdict": verdict, "reasoning": fallback_reason, "confidence": confidence}


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


def _judge_once(
    model_chain: list[str],
    context: dict,
    *,
    system_prompt: str = _SYSTEM_PROMPT,
    temperature: float = 0.0,
) -> dict:
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
    content = llm_resp.text
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
    confidence_raw = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    verdict, reasoning, confidence = _guard_judge_consistency(
        verdict, reasoning, confidence
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


def _try_hosted_judgment(dispute_id: str, context: dict) -> dict | None:
    """Attempt to resolve a dispute via the hosted aztea.ai judge.

    Returns the standard run_judgment result dict on success, or None if
    hosted mode is disabled or the call fails for any reason. Caller
    falls back to local LLM / deterministic on None.
    """
    # Local import to avoid a hard dep when hosted_client is absent (e.g.
    # in trimmed-down test fixtures).
    from core.hosted_client import get_hosted_client

    client = get_hosted_client()
    if not client.is_enabled():
        return None
    response = client.judge_dispute(context)
    if not response or not isinstance(response, dict):
        return None
    try:
        verdict = _normalize_verdict(response.get("verdict"))
    except ValueError:
        logger.warning(
            "judges: hosted judge returned invalid verdict %r for dispute %s",
            response.get("verdict"),
            dispute_id,
        )
        return None
    reasoning = str(response.get("reasoning") or "").strip() or "Hosted judge."
    model_label = str(response.get("model") or "hosted").strip() or "hosted"
    # Record both primary + secondary as the hosted verdict; aztea.ai's
    # hosted judge already runs heterogeneous votes server-side.
    disputes.record_judgment(
        dispute_id,
        judge_kind="llm_primary",
        verdict=verdict,
        reasoning=reasoning,
        model=f"hosted:{model_label}",
    )
    disputes.record_judgment(
        dispute_id,
        judge_kind="llm_secondary",
        verdict=verdict,
        reasoning=reasoning,
        model=f"hosted:{model_label}",
    )
    disputes.set_dispute_consensus(dispute_id, verdict)
    return {
        "status": "consensus",
        "outcome": verdict,
        "judgments": disputes.get_judgments(dispute_id),
    }


def run_judgment(dispute_id: str) -> dict:
    """Run LLM-based adjudication for a dispute and record the judgment vote.

    Requires ``AZTEA_ENABLE_LIVE_DISPUTE_JUDGES=1`` in env; returns a
    ``skipped`` result without calling the LLM if the flag is unset.

    A dispute needs two agreeing judge votes before it can be auto-resolved.
    This function contributes one vote; consensus is checked by
    ``disputes.set_dispute_consensus`` after the vote is stored.

    Returns a dict with ``status``, ``outcome``, ``vote``, and ``reasoning``.
    Raises ``ValueError`` if the dispute is not found or already in a terminal
    state (``"resolved"`` or ``"final"``).
    """
    _guard = _validate_dispute_judgeable(dispute_id)
    _guard.raise_on_err()
    context = _guard.value

    current_status = str(context["dispute"].get("status") or "").strip().lower()
    if current_status in {"resolved", "final"}:
        return {
            "status": current_status,
            "outcome": context["dispute"].get("outcome"),
            "judgments": disputes.get_judgments(dispute_id),
        }

    disputes.set_dispute_status(dispute_id, "judging")

    # Hosted-first: if this instance is configured to call aztea.ai's
    # hosted judge, use it. The hosted endpoint runs the heterogeneous
    # two-judge protocol server-side and bills against the instance's
    # hosted account. On any error (network, auth, malformed response)
    # we fall through to the local LLM path below.
    hosted_result = _try_hosted_judgment(dispute_id, context)
    if hosted_result is not None:
        return hosted_result

    live_enabled = _env_enabled_any(
        "AZTEA_ENABLE_LIVE_DISPUTE_JUDGES", "AGENTMARKET_ENABLE_LIVE_DISPUTE_JUDGES"
    )
    if not live_enabled:
        fallback = _local_dispute_fallback(context)
        fallback_verdict = fallback["verdict"]
        fallback_reason = fallback["reasoning"]
        disputes.record_judgment(
            dispute_id,
            judge_kind="llm_primary",
            verdict=fallback_verdict,
            reasoning=fallback_reason,
            model="fallback",
        )
        disputes.record_judgment(
            dispute_id,
            judge_kind="llm_secondary",
            verdict=fallback_verdict,
            reasoning=fallback_reason,
            model="fallback",
        )
        disputes.set_dispute_consensus(dispute_id, fallback_verdict)
        return {
            "status": "consensus",
            "outcome": fallback_verdict,
            "judgments": disputes.get_judgments(dispute_id),
        }

    primary_chain = list(DEFAULT_CHAIN)
    try:
        primary = _judge_once(primary_chain, context)
    except Exception:
        # WHY: a provider/network/rate-limit failure here would otherwise leave
        # the dispute stuck in 'judging' forever; reset to 'pending' so the
        # next sweeper pass retries.
        disputes.set_dispute_status(dispute_id, "pending")
        raise
    disputes.record_judgment(
        dispute_id,
        judge_kind="llm_primary",
        verdict=primary["verdict"],
        reasoning=primary["reasoning"],
        model=primary["model"],
    )

    # Heterogeneous secondary judge: build a chain that EXCLUDES whichever
    # model the primary actually used. The eval found both judges resolving
    # to the same model (`groq:llama-3.3-70b-versatile`) when only one
    # provider was configured — failure modes correlate, which means two
    # confirming votes from the same brain aren't independent. Excluding the
    # primary's model forces a different LLM family to weigh in. If no
    # alternative is configured, we still vary the system prompt
    # ("devil's-advocate" framing) and temperature so the second judgment
    # is at least probabilistically decorrelated.
    primary_model = str(primary.get("model") or "").strip()
    secondary_chain_full = DEFAULT_CHAIN[1:] + DEFAULT_CHAIN[:1]
    secondary_chain = [
        spec for spec in secondary_chain_full if spec != primary_model
    ]
    fallback_to_same_model = not secondary_chain
    if fallback_to_same_model:
        # Only one provider available — keep the original chain order so the
        # call still resolves, but vary the prompt+temperature.
        secondary_chain = list(DEFAULT_CHAIN)
    try:
        secondary = _judge_once(
            secondary_chain,
            context,
            system_prompt=_SYSTEM_PROMPT_DEVILS_ADVOCATE,
        # Slight temperature lift on the second pass so the verdict isn't a
        # near-deterministic re-roll of the same logits when we're forced
        # to use the same model. Keeps the verdict space the same JSON
        # shape but introduces meaningful variance.
        temperature=0.4 if fallback_to_same_model else 0.1,
        )
    except Exception:
        # Secondary LLM failed after primary already recorded. Promote primary
        # to consensus via the deterministic tiebreaker so we don't strand the
        # dispute at 'judging' with one orphan judgment.
        tiebreaker = _local_dispute_fallback(context)
        tiebreaker_verdict = tiebreaker["verdict"]
        disputes.record_judgment(
            dispute_id,
            judge_kind="llm_secondary",
            verdict=tiebreaker_verdict,
            reasoning="Secondary LLM unavailable; deterministic fallback used: "
            + tiebreaker["reasoning"],
            model="fallback",
        )
        disputes.set_dispute_consensus(dispute_id, tiebreaker_verdict)
        return {
            "status": "consensus",
            "outcome": tiebreaker_verdict,
            "judgments": disputes.get_judgments(dispute_id),
        }
    disputes.record_judgment(
        dispute_id,
        judge_kind="llm_secondary",
        verdict=secondary["verdict"],
        reasoning=secondary["reasoning"],
        model=secondary["model"]
        + (
            " (devil's-advocate prompt; same model as primary — heterogeneous LLM unavailable)"
            if fallback_to_same_model
            and str(secondary.get("model") or "") == primary_model
            else ""
        ),
    )

    if primary["verdict"] == secondary["verdict"] and primary["verdict"] != "split":
        disputes.set_dispute_consensus(dispute_id, primary["verdict"])
        status = "consensus"
        outcome = primary["verdict"]
    else:
        tiebreaker = _local_dispute_fallback(context)
        tiebreaker_verdict = tiebreaker["verdict"]
        disputes.record_judgment(
            dispute_id,
            judge_kind="human_admin",
            verdict=tiebreaker_verdict,
            reasoning="Deterministic tiebreaker after LLM disagreement: "
            + tiebreaker["reasoning"],
            model="deterministic",
            admin_user_id="system_tiebreaker",
        )
        if (
            tiebreaker_verdict != "split"
            and tiebreaker_verdict in {primary["verdict"], secondary["verdict"]}
        ):
            disputes.set_dispute_consensus(dispute_id, tiebreaker_verdict)
            status = "consensus"
            outcome = tiebreaker_verdict
        else:
            disputes.set_dispute_tied(dispute_id)
            status = "tied"
            outcome = None

    return {
        "status": status,
        "outcome": outcome,
        "judgments": disputes.get_judgments(dispute_id),
    }


def _local_quality_fallback(
    *,
    input_payload: dict | None,
    output_payload: dict | None,
    agent_description: str = "",
) -> dict:
    payload = output_payload if isinstance(output_payload, dict) else {}
    if not payload:
        return {"verdict": "fail", "score": 1, "reason": "Output payload is empty."}
    if any(payload.get(field) for field in ("error", "errors", "exception")):
        return {
            "verdict": "fail",
            "score": 2,
            "reason": "Output payload contains explicit error fields.",
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
    return {"verdict": verdict, "score": score, "reason": reason}


def run_quality_judgment(
    *,
    input_payload: dict,
    output_payload: dict,
    agent_description: str,
    agent_name: str = "",
    quality_hint: str = "",
) -> dict:
    """Score a completed job output for quality using an LLM judge.

    Requires ``AZTEA_ENABLE_LIVE_QUALITY_JUDGE=1``; returns a local heuristic
    fallback score (based on output length / structure) if the flag is unset.

    The judgment score (0–5) is used by ``core.payout_curve`` to compute any
    quality-based clawback. The LLM is given the original input payload, the
    agent's output, and a description of what the agent is supposed to do.

    Returns ``{score, reasoning, method}`` where ``method`` is either
    ``"llm"`` or ``"heuristic"``.
    """
    if not _env_enabled_any(
        "AZTEA_ENABLE_LIVE_QUALITY_JUDGE", "AGENTMARKET_ENABLE_LIVE_QUALITY_JUDGE"
    ):
        return _local_quality_fallback(
            input_payload=input_payload,
            output_payload=output_payload,
            agent_description=agent_description,
        )

    user_prompt = json.dumps(
        {
            "input_payload": input_payload,
            "output_payload": output_payload,
            "agent_name": agent_name,
            "agent_description": agent_description,
            "quality_hint": quality_hint,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    try:
        llm_resp = run_with_fallback(
            CompletionRequest(
                model="",
                messages=[
                    Message("system", _QUALITY_SYSTEM_PROMPT),
                    Message("user", user_prompt),
                ],
                temperature=0.0,
                json_mode=True,
            )
        )
        content = llm_resp.text
    except Exception:
        return _local_quality_fallback(
            input_payload=input_payload,
            output_payload=output_payload,
            agent_description=agent_description,
        )
    if not content:
        return _local_quality_fallback(
            input_payload=input_payload,
            output_payload=output_payload,
            agent_description=agent_description,
        )
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return _local_quality_fallback(
            input_payload=input_payload,
            output_payload=output_payload,
            agent_description=agent_description,
        )
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "fail"}:
        return _local_quality_fallback(
            input_payload=input_payload,
            output_payload=output_payload,
            agent_description=agent_description,
        )
    try:
        score = int(parsed.get("score"))
    except (TypeError, ValueError):
        score = 1 if verdict == "fail" else 7
    score = max(1, min(10, score))
    reason = str(parsed.get("reason") or "").strip() or "No reason provided."
    return {"verdict": verdict, "score": score, "reason": reason}
