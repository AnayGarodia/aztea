"""
incident_response.py — Incident commander copilot for production outages.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM_PROMPT = """\
You are an experienced SRE incident commander.
Prioritize safe mitigation, clear communication, and fast validation loops.
Return only valid JSON.
"""

_USER_PROMPT = """\
Create an actionable incident response plan.

Incident title: {incident_title}
Severity hint: {severity}
Symptoms: {symptoms}
Service map: {service_map}
Recent changes: {recent_changes}
Telemetry: {telemetry}

Return JSON:
{{
  "severity_assessment": {{"level":"string","justification":"string"}},
  "probable_root_causes": [{{"cause":"string","confidence":"low|medium|high","evidence":["string"]}}],
  "first_15_min_actions": ["string"],
  "stabilization_plan": ["string"],
  "rollback_or_feature_flag_plan": ["string"],
  "communications": {{"internal_update":"string","status_page_update":"string","next_update_eta":"string"}},
  "verification_checks": ["string"],
  "timeline_30_60_90": {{"30":"string","60":"string","90":"string"}},
  "postmortem_followups": ["string"]
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
    incident_title = str(payload.get("incident_title") or "").strip()
    severity = str(payload.get("severity") or "unknown").strip().lower()
    symptoms = _listify(payload.get("symptoms"))
    service_map = _listify(payload.get("service_map"))
    recent_changes = _listify(payload.get("recent_changes"))
    telemetry = _listify(payload.get("telemetry"))

    if not incident_title:
        return {"error": "incident_title is required"}
    if not symptoms:
        return {"error": "symptoms must contain at least one item"}

    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=_USER_PROMPT.format(
                    incident_title=incident_title[:300],
                    severity=severity,
                    symptoms=json.dumps(symptoms[:30], ensure_ascii=True),
                    service_map=json.dumps(service_map[:30], ensure_ascii=True),
                    recent_changes=json.dumps(recent_changes[:30], ensure_ascii=True),
                    telemetry=json.dumps(telemetry[:30], ensure_ascii=True),
                ),
            ),
        ],
        temperature=0.15,
        max_tokens=2000,
    )
    llm = run_with_fallback(req)
    return _parse_json_response(llm.text)

