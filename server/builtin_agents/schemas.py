"""JSON-schema helpers for built-in agent registration specs."""

from __future__ import annotations

from typing import Any


def output_schema_object(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties)}
    if required:
        schema["required"] = list(required)
    return schema


def quality_judge_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "input_payload": {"type": "object"},
            "output_payload": {"type": "object"},
            "agent_description": {"type": "string"},
        },
        "required": ["input_payload", "output_payload"],
    }
