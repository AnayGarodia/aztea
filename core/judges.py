"""
judges.py — LLM arbitration helpers for disputes.
"""

from __future__ import annotations

import json
import os
from typing import Any

from core import disputes
from core.llm import CompletionRequest, Message, run_with_fallback
from core.llm.registry import DEFAULT_CHAIN

_SYSTEM_PROMPT = (
    "You are an impartial arbiter. Analyze the dispute evidence and return strict JSON "
    'with keys: {"verdict": "...", "reasoning": "...", "confidence": 0.0-1.0}. '
    "Valid verdict values are: caller_wins, agent_wins, split, void."
)
_QUALITY_SYSTEM_PROMPT = (
    "You are a strict quality judge for agent outputs. "
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


def _local_dispute_fallback(context: dict) -> dict:
    dispute = context.get("dispute") or {}
    job = context.get("job") or {}
    reason = str(dispute.get("reason") or "").strip()
    evidence = str(dispute.get("evidence") or "").strip()
    combined = " ".join(part for part in [reason, evidence, str(job.get("error_message") or "")] if part).strip()

    caller_hits = _token_matches(combined, _CALLER_WIN_HINTS)
    agent_hits = _token_matches(combined, _AGENT_WIN_HINTS)
    output_payload = job.get("output_payload")
    if not output_payload:
        caller_hits.append("missing_output")

    side = str(dispute.get("side") or "").strip().lower()
    caller_score = len(caller_hits) + (1 if side == "caller" else 0)
    agent_score = len(agent_hits) + (1 if side == "agent" else 0)
    delta = caller_score - agent_score

    if delta >= 2:
        verdict = "caller_wins"
    elif delta <= -2:
        verdict = "agent_wins"
    else:
        verdict = "split"

    confidence = min(0.9, max(0.55, 0.55 + (abs(delta) * 0.08)))
    fallback_reason = (
        "Deterministic fallback judge (live LLM disabled): "
        f"caller_signals={caller_hits or ['none']}, agent_signals={agent_hits or ['none']}, "
        f"side={side or 'unknown'}, verdict={verdict}."
    )
    return {"verdict": verdict, "reasoning": fallback_reason, "confidence": confidence}


def _judge_once(model_chain: list[str], context: dict) -> dict:
    llm_resp = run_with_fallback(
        CompletionRequest(
            model="",
            messages=[
                Message("system", _SYSTEM_PROMPT),
                Message("user", _build_user_prompt(context)),
            ],
            temperature=0.0,
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
    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "confidence": confidence,
        "model": f"{llm_resp.provider}:{llm_resp.model}",
    }


def run_judgment(dispute_id: str) -> dict:
    context = disputes.get_dispute_context(dispute_id)
    if context is None:
        raise ValueError(f"Dispute '{dispute_id}' not found.")

    current_status = str(context["dispute"].get("status") or "").strip().lower()
    if current_status in {"resolved", "final"}:
        return {
            "status": current_status,
            "outcome": context["dispute"].get("outcome"),
            "judgments": disputes.get_judgments(dispute_id),
        }

    disputes.set_dispute_status(dispute_id, "judging")
    live_enabled = _env_enabled_any("AZTEA_ENABLE_LIVE_DISPUTE_JUDGES", "AGENTMARKET_ENABLE_LIVE_DISPUTE_JUDGES")
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
    secondary_chain = DEFAULT_CHAIN[1:] + DEFAULT_CHAIN[:1]

    primary = _judge_once(primary_chain, context)
    disputes.record_judgment(
        dispute_id,
        judge_kind="llm_primary",
        verdict=primary["verdict"],
        reasoning=primary["reasoning"],
        model=primary["model"],
    )

    secondary = _judge_once(secondary_chain, context)
    disputes.record_judgment(
        dispute_id,
        judge_kind="llm_secondary",
        verdict=secondary["verdict"],
        reasoning=secondary["reasoning"],
        model=secondary["model"],
    )

    if primary["verdict"] == secondary["verdict"]:
        disputes.set_dispute_consensus(dispute_id, primary["verdict"])
        status = "consensus"
        outcome = primary["verdict"]
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
        return {"verdict": "fail", "score": 2, "reason": "Output payload contains explicit error fields."}

    filled_fields = [
        key
        for key, value in payload.items()
        if value not in (None, "", [], {}, ())
    ]
    text_chars = sum(len(value.strip()) for value in payload.values() if isinstance(value, str))
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
) -> dict:
    if not _env_enabled_any("AZTEA_ENABLE_LIVE_QUALITY_JUDGE", "AGENTMARKET_ENABLE_LIVE_QUALITY_JUDGE"):
        return _local_quality_fallback(
            input_payload=input_payload,
            output_payload=output_payload,
            agent_description=agent_description,
        )

    user_prompt = json.dumps(
        {
            "input_payload": input_payload,
            "output_payload": output_payload,
            "agent_description": agent_description,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
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
