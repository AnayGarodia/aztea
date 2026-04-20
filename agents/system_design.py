"""
system_design.py — System design reviewer and architecture planner agent.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM_PROMPT = """\
You are a principal architect performing a production-readiness system design review.
Return only valid JSON. No markdown, no commentary outside JSON.
"""

_USER_PROMPT = """\
Design and review this system proposal.

Context:
{context}

Requirements:
{requirements}

Constraints:
{constraints}

Scale assumptions:
{scale_assumptions}

Tech preferences:
{stack}

Non-functional requirements:
{nfrs}

Return JSON with this shape:
{{
  "architecture_summary": "string",
  "components": [{{"name":"string","responsibility":"string","storage":"string","failure_mode":"string"}}],
  "request_flow": ["string"],
  "data_model": [{{"entity":"string","fields":["string"],"indexes":["string"]}}],
  "apis": [{{"name":"string","method":"string","path":"string","idempotency":"string"}}],
  "tradeoff_matrix": [{{"decision":"string","option_a":"string","option_b":"string","chosen":"string","rationale":"string"}}],
  "scaling_plan": {{"hotspots":["string"],"mitigations":["string"],"capacity_triggers":["string"]}},
  "observability_plan": {{"slis":["string"],"slo_targets":["string"],"alerts":["string"]}},
  "security_controls": ["string"],
  "phase_plan": [{{"phase":"string","goal":"string","deliverables":["string"],"risk":"string"}}],
  "top_risks": [{{"risk":"string","impact":"string","owner":"string","mitigation":"string"}}]
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
    context = str(payload.get("context") or "").strip()
    requirements = _listify(payload.get("requirements"))
    constraints = _listify(payload.get("constraints"))
    scale_assumptions = _listify(payload.get("scale_assumptions"))
    stack = _listify(payload.get("stack"))
    nfrs = _listify(payload.get("non_functional_requirements"))

    if not context:
        return {"error": "context is required"}
    if not requirements:
        return {"error": "requirements must contain at least one item"}

    req = CompletionRequest(
        messages=[
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=_USER_PROMPT.format(
                    context=context[:3000],
                    requirements=json.dumps(requirements[:20], ensure_ascii=True),
                    constraints=json.dumps(constraints[:20], ensure_ascii=True),
                    scale_assumptions=json.dumps(scale_assumptions[:20], ensure_ascii=True),
                    stack=json.dumps(stack[:20], ensure_ascii=True),
                    nfrs=json.dumps(nfrs[:20], ensure_ascii=True),
                ),
            ),
        ],
        temperature=0.2,
        max_tokens=2200,
    )
    llm = run_with_fallback(req)
    return _parse_json_response(llm.text)

