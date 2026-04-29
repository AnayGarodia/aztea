"""Curated public pipeline recipes."""

from __future__ import annotations

from server.builtin_agents.constants import (
    CODEREVIEW_AGENT_ID,
    DEPENDENCY_AUDITOR_AGENT_ID,
    LINTER_AGENT_ID,
    TYPE_CHECKER_AGENT_ID,
)

from core import pipelines

PLATFORM_RECIPES_OWNER_ID = "platform:recipes"


BUILTIN_RECIPES: list[dict] = [
    {
        "recipe_id": "modernize-python",
        "name": "modernize-python",
        "description": "Run linting, type checking, and a final review over Python code.",
        "default_input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        "pipeline_definition": {
            "nodes": [
                {
                    "id": "lint",
                    "agent_id": LINTER_AGENT_ID,
                    "input_map": {"code": "$input.code"},
                },
                {
                    "id": "types",
                    "agent_id": TYPE_CHECKER_AGENT_ID,
                    "depends_on": ["lint"],
                    "input_map": {"code": "$input.code"},
                },
                {
                    "id": "review",
                    "agent_id": CODEREVIEW_AGENT_ID,
                    "depends_on": ["lint", "types"],
                    "input_map": {"code": "$input.code"},
                },
            ]
        },
    },
    {
        "recipe_id": "audit-deps",
        "name": "audit-deps",
        "description": "Audit a dependency set for known vulnerabilities and summarize the highest-priority issues.",
        "default_input_schema": {
            "type": "object",
            "properties": {"dependencies": {"type": "string"}},
            "required": ["dependencies"],
        },
        "pipeline_definition": {
            "nodes": [
                {
                    "id": "audit",
                    "agent_id": DEPENDENCY_AUDITOR_AGENT_ID,
                    "input_map": {"dependencies": "$input.dependencies"},
                },
            ]
        },
    },
    {
        "recipe_id": "review-and-lint",
        "name": "review-and-lint",
        "description": "Review code for issues, then lint for style and correctness.",
        "default_input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        "pipeline_definition": {
            "nodes": [
                {
                    "id": "review",
                    "agent_id": CODEREVIEW_AGENT_ID,
                    "input_map": {"code": "$input.code"},
                },
                {
                    "id": "lint",
                    "agent_id": LINTER_AGENT_ID,
                    "depends_on": ["review"],
                    "input_map": {"code": "$input.code"},
                },
            ]
        },
    },
]


def ensure_builtin_recipes() -> list[dict]:
    """Upsert the platform's built-in pipeline templates on startup. Idempotent."""
    ensured: list[dict] = []
    for recipe in BUILTIN_RECIPES:
        ensured.append(
            pipelines.upsert_pipeline(
                PLATFORM_RECIPES_OWNER_ID,
                recipe["name"],
                recipe["pipeline_definition"],
                description=recipe["description"],
                is_public=True,
                pipeline_id=recipe["recipe_id"],
            )
        )
    return ensured
