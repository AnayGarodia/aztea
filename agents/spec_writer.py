"""
spec_writer.py — Technical spec writer agent

Input:
  {
    "requirements": "free-text feature description or user story",
    "format": "prd|rfc|adr|api_spec|auto",
    "stack": "",          # optional: tech stack (e.g. "FastAPI + React + SQLite")
    "audience": "engineers|product|both",
    "context": ""         # optional: existing system context
  }

Output:
  {
    "title": str,
    "format": str,
    "sections": [{"heading": str, "content": str}],
    "open_questions": [str],
    "out_of_scope": [str],
    "estimated_complexity": "S|M|L|XL",
    "full_text": str
  }
"""
from __future__ import annotations

import json
import re

from core.llm import CompletionRequest, Message, run_with_fallback

_SYSTEM = """\
You are a principal engineer and technical writer who produces specs that actually get implemented. \
Your documents are precise, complete, and opinionated — you make decisions rather than listing options.

Depending on format:
- PRD (Product Requirements Document): user problem, goals, non-goals, requirements, success metrics
- RFC (Request for Comments): motivation, design, alternatives considered, drawbacks, implementation plan
- ADR (Architecture Decision Record): context, decision, consequences, alternatives rejected
- API Spec: endpoints, request/response schemas, auth, errors, rate limits, examples
- Auto: choose the most appropriate format for the input

Be concrete. Include data models, state machines, edge cases, and security considerations where relevant. \
Name specific technologies, not just categories.

Return ONLY valid JSON — no markdown fences, no prose outside the JSON object."""

_USER = """\
Write a technical spec for the following requirements.
Format: {format}
Tech stack: {stack}
Audience: {audience}
Existing system context: {context}

Requirements:
{requirements}

Return a JSON object:
{{
  "title": "concise spec title",
  "format": "prd|rfc|adr|api_spec",
  "sections": [
    {{
      "heading": "section title",
      "content": "section body in markdown"
    }}
  ],
  "open_questions": ["list of unresolved decisions needing input"],
  "out_of_scope": ["explicit non-goals to prevent scope creep"],
  "estimated_complexity": "S (days) | M (1–2 weeks) | L (1 month) | XL (quarter+)",
  "full_text": "the complete spec as a single markdown string"
}}"""

_MAX_REQ_CHARS = 8_000


def run(payload: dict) -> dict:
    requirements = str(payload.get("requirements") or "").strip()
    if not requirements:
        raise ValueError("'requirements' is required.")

    fmt = str(payload.get("format") or "auto")
    stack = str(payload.get("stack") or "Not specified.")[:300]
    audience = str(payload.get("audience") or "engineers")
    context = str(payload.get("context") or "Not provided.")[:600]

    prompt = _USER.format(
        format=fmt,
        stack=stack,
        audience=audience,
        context=context,
        requirements=requirements[:_MAX_REQ_CHARS],
    )

    resp = run_with_fallback(CompletionRequest(
        model="",
        messages=[Message("system", _SYSTEM), Message("user", prompt)],
        max_tokens=3500,
        json_mode=True,
    ))
    raw = _strip_fences(resp.text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned non-JSON: {e}\n\n{raw[:300]}") from e


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    return m.group(1).strip() if m else text
