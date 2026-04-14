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
    "llama-3.1-70b-versatile",
]

_SYSTEM_PROMPT = (
    "You are an impartial arbiter. Analyze the dispute evidence and return strict JSON "
    'with keys: {"verdict": "...", "reasoning": "...", "confidence": 0.0-1.0}. '
    "Valid verdict values are: caller_wins, agent_wins, split, void."
)


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

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured.")

    disputes.set_dispute_status(dispute_id, "judging")
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
