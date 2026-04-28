"""Curated public pipeline recipes."""

from __future__ import annotations

from server.builtin_agents.constants import (
    CODEREVIEW_AGENT_ID,
    DEPENDENCY_AUDITOR_AGENT_ID,
    LINTER_AGENT_ID,
    PACKAGE_FINDER_AGENT_ID,
    TEST_GENERATOR_AGENT_ID,
)

from core import pipelines

PLATFORM_RECIPES_OWNER_ID = "platform:recipes"


BUILTIN_RECIPES: list[dict] = [
    {
        "recipe_id": "modernize-python",
        "name": "modernize-python",
        "description": "Run type-aware modernization checks, lint fixes, and a final review over Python code.",
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
                    "id": "review",
                    "agent_id": CODEREVIEW_AGENT_ID,
                    "depends_on": ["lint"],
                    "input_map": {"code": "$input.code"},
                },
            ]
        },
    },
    {
        "recipe_id": "audit-deps",
        "name": "audit-deps",
        "description": "Audit a dependency set for issues, then suggest replacement packages or upgrades.",
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
                {
                    "id": "suggest",
                    "agent_id": PACKAGE_FINDER_AGENT_ID,
                    "depends_on": ["audit"],
                    "input_map": {"query": "$audit.output.summary"},
                },
            ]
        },
    },
    {
        "recipe_id": "review-and-test",
        "name": "review-and-test",
        "description": "Review code and then generate tests for the same change.",
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
                    "id": "tests",
                    "agent_id": TEST_GENERATOR_AGENT_ID,
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
