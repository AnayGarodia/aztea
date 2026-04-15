"""
judges.py — LLM arbitration helpers for disputes.
"""

from __future__ import annotations

import json
import os
from typing import Any

from groq import Groq

from core import disputes

_MODELS = [
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-scout-17b-16e-instruct",
]
_QUALITY_MODEL = "llama-3.3-70b-versatile"

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


def _env_enabled(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


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


def _judge_once(client: Groq, model: str, context: dict) -> dict:
    completion = client.chat.completions.create(
        model=model,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(context)},
        ],
    )
    content = (completion.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError(f"Judge model '{model}' returned empty content.")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Judge model '{model}' returned non-JSON content.") from exc
    verdict = _normalize_verdict(parsed.get("verdict"))
    reasoning = str(parsed.get("reasoning") or "").strip()
    if not reasoning:
        raise RuntimeError(f"Judge model '{model}' returned empty reasoning.")
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
        "model": model,
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
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    live_enabled = _env_enabled("AGENTMARKET_ENABLE_LIVE_DISPUTE_JUDGES")
    if not api_key or not live_enabled:
        reason = str(context["dispute"].get("reason") or "").lower()
        evidence = str(context["dispute"].get("evidence") or "").lower()
        joined = f"{reason} {evidence}"
        if any(token in joined for token in ("incomplete", "wrong", "error", "missing", "refund", "broken")):
            fallback_verdict = "caller_wins"
            fallback_reason = "Fallback judge: dispute indicates delivery failure."
        elif any(token in joined for token in ("scope", "requirement", "changed", "abuse", "harass", "spam")):
            fallback_verdict = "agent_wins"
            fallback_reason = "Fallback judge: dispute indicates caller-side scope or conduct issue."
        else:
            fallback_verdict = "split"
            fallback_reason = "Fallback judge: evidence is mixed."
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

    client = Groq(api_key=api_key)

    primary = _judge_once(client, _MODELS[0], context)
    disputes.record_judgment(
        dispute_id,
        judge_kind="llm_primary",
        verdict=primary["verdict"],
        reasoning=primary["reasoning"],
        model=primary["model"],
    )

    secondary = _judge_once(client, _MODELS[1], context)
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
        disputes.set_dispute_status(dispute_id, "tied")
        status = "tied"
        outcome = None

    return {
        "status": status,
        "outcome": outcome,
        "judgments": disputes.get_judgments(dispute_id),
    }


def _local_quality_fallback(output_payload: dict | None) -> dict:
    payload = output_payload if isinstance(output_payload, dict) else {}
    if not payload:
        return {"verdict": "fail", "score": 1, "reason": "Output payload is empty."}
    return {"verdict": "pass", "score": 7, "reason": "Output payload is non-empty."}


def run_quality_judgment(
    *,
    input_payload: dict,
    output_payload: dict,
    agent_description: str,
) -> dict:
    if not _env_enabled("AGENTMARKET_ENABLE_LIVE_QUALITY_JUDGE"):
        return _local_quality_fallback(output_payload)
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return _local_quality_fallback(output_payload)

    user_prompt = json.dumps(
        {
            "input_payload": input_payload,
            "output_payload": output_payload,
            "agent_description": agent_description,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    client = Groq(api_key=api_key)
    completion = client.chat.completions.create(
        model=_QUALITY_MODEL,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _QUALITY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = (completion.choices[0].message.content or "").strip()
    if not content:
        return _local_quality_fallback(output_payload)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return _local_quality_fallback(output_payload)
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "fail"}:
        return _local_quality_fallback(output_payload)
    try:
        score = int(parsed.get("score"))
    except (TypeError, ValueError):
        score = 1 if verdict == "fail" else 7
    score = max(1, min(10, score))
    reason = str(parsed.get("reason") or "").strip() or "No reason provided."
    return {"verdict": verdict, "score": score, "reason": reason}
