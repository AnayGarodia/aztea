"""
healthcare_expert.py — structured healthcare triage copilot.
Educational guidance only. Not a diagnosis.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM_PROMPT = """\
You are a cautious clinical triage assistant.
You never provide a diagnosis or treatment orders.
Prioritize patient safety, escalation for emergency red flags, and practical next steps.
Return only valid JSON.
"""

_USER_PROMPT = """\
Create structured guidance from this patient context.

Symptoms: {symptoms}
Age years: {age_years}
Sex: {sex}
Medical history: {medical_history}
Medications: {medications}
Duration: {duration}
Urgency context: {urgency_context}
Goal: {goal}

Return JSON:
{{
  "triage_level": "self_care|primary_care_24h|urgent_care_today|emergency_now",
  "possible_considerations": [
    {{"condition":"string","confidence":"low|medium|high","rationale":"string"}}
  ],
  "red_flags": [
    {{"flag":"string","why_urgent":"string"}}
  ],
  "next_steps": ["string"],
  "questions_for_clinician": ["string"],
  "disclaimer": "string"
}}
"""


def _listify(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [text]


def _parse_json_response(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {"error": "parse_error", "raw": cleaned[:1200]}
    parsed = json.loads(match.group(0))
    if isinstance(parsed, dict):
        return parsed
    return {"error": "parse_error", "raw": cleaned[:1200]}


def run(payload: dict[str, Any]) -> dict[str, Any]:
    symptoms = _listify(payload.get("symptoms"))
    if not symptoms:
        return {"error": "symptoms must contain at least one item"}

    age_years = payload.get("age_years")
    sex = str(payload.get("sex") or "unspecified").strip()
    history = _listify(payload.get("medical_history"))
    medications = _listify(payload.get("medications"))
    duration = str(payload.get("duration") or "").strip() or "unspecified"
    urgency_context = str(payload.get("urgency_context") or "").strip() or "unspecified"
    goal = str(payload.get("goal") or "").strip() or "triage guidance"

    req = CompletionRequest(
        messages=[
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=_USER_PROMPT.format(
                    symptoms=json.dumps(symptoms[:20], ensure_ascii=True),
                    age_years=age_years if isinstance(age_years, int) else "unspecified",
                    sex=sex[:64],
                    medical_history=json.dumps(history[:20], ensure_ascii=True),
                    medications=json.dumps(medications[:20], ensure_ascii=True),
                    duration=duration[:120],
                    urgency_context=urgency_context[:300],
                    goal=goal[:240],
                ),
            ),
        ],
        temperature=0.1,
        max_tokens=1800,
    )
    llm = run_with_fallback(req)
    result = _parse_json_response(llm.text)
    if "disclaimer" not in result:
        result["disclaimer"] = "Educational guidance only, not a diagnosis or treatment plan."
    return result
