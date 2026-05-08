"""Curated public pipeline recipes.

Every node in every recipe must point at an agent that is in
``CURATED_PUBLIC_BUILTIN_AGENT_IDS`` — a pipeline that fans out to a
sunset agent will silently fail or refund, which is worse than not
having the recipe at all. The 2026-05-07 eval found 4/5 recipes
referenced sunset slugs (code_review / linter / type_checker /
git_diff_analyzer); this rewrite uses only live agents.
"""

from __future__ import annotations

from core import pipelines
from server.builtin_agents.constants import (
    CURATED_PUBLIC_BUILTIN_AGENT_IDS,
    DEPENDENCY_AUDITOR_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID,
    SECRET_SCANNER_AGENT_ID,
)

PLATFORM_RECIPES_OWNER_ID = "platform:recipes"


BUILTIN_RECIPES: list[dict] = [
    {
        "recipe_id": "audit-deps",
        "name": "audit-deps",
        "description": "Audit a dependency manifest for known CVEs, license risks, and prioritized upgrades.",
        "default_input_schema": {
            "type": "object",
            "properties": {
                "manifest": {
                    "type": "string",
                    "description": "Contents of package.json or requirements.txt",
                }
            },
            "required": ["manifest"],
        },
        "pipeline_definition": {
            "nodes": [
                {
                    "id": "audit",
                    "agent_id": DEPENDENCY_AUDITOR_AGENT_ID,
                    "input_map": {"manifest": "$input.manifest"},
                },
            ]
        },
    },
    {
        "recipe_id": "secret-scan-and-audit",
        "name": "secret-scan-and-audit",
        "description": "Scan source for leaked credentials, then audit the dependency manifest for known CVEs.",
        "default_input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "manifest": {"type": "string"},
            },
            "required": ["content", "manifest"],
        },
        "pipeline_definition": {
            "nodes": [
                {
                    "id": "scan",
                    "agent_id": SECRET_SCANNER_AGENT_ID,
                    "input_map": {"content": "$input.content"},
                },
                {
                    "id": "audit",
                    "agent_id": DEPENDENCY_AUDITOR_AGENT_ID,
                    "depends_on": ["scan"],
                    "input_map": {"manifest": "$input.manifest"},
                },
            ]
        },
    },
    {
        "recipe_id": "domain-health",
        "name": "domain-health",
        "description": "Run DNS, SSL, and HTTP-header checks on one or more domains.",
        "default_input_schema": {
            "type": "object",
            "properties": {
                "domains": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["domains"],
        },
        "pipeline_definition": {
            "nodes": [
                {
                    "id": "inspect",
                    "agent_id": DNS_INSPECTOR_AGENT_ID,
                    "input_map": {
                        "domains": "$input.domains",
                        "checks": ["dns", "ssl", "http"],
                    },
                },
            ]
        },
    },
]


# Sanity: every node must reference a live curated public agent so callers
# don't get silent failures.
for _recipe in BUILTIN_RECIPES:
    for _node in _recipe["pipeline_definition"]["nodes"]:
        assert (
            _node["agent_id"] in CURATED_PUBLIC_BUILTIN_AGENT_IDS
        ), f"recipe {_recipe['recipe_id']} references non-public agent {_node['agent_id']}"


def ensure_builtin_recipes() -> list[dict]:
    """Upsert the platform's built-in pipeline templates on startup. Idempotent.

    Stale recipes from previous deploys (e.g. recipes that referenced
    now-sunset agents) are deleted so they don't show up in
    ``list_recipes`` and silently break for callers.
    """
    ensured: list[dict] = []
    keep_ids: set[str] = set()
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
        keep_ids.add(recipe["recipe_id"])
    # Best-effort cleanup of stale platform recipes referencing sunset agents.
    # The pipelines layer doesn't expose a delete API today, so we mark stale
    # recipes private by re-upserting them with a tombstone description and
    # is_public=False. Discovery surfaces (``list_recipes``) only return
    # ``is_public=True`` rows so this hides them from non-admin callers
    # without dropping the row (kept for receipt/back-compat reads).
    try:
        existing = pipelines.list_pipelines(
            PLATFORM_RECIPES_OWNER_ID, include_public=True
        )
    except Exception:
        existing = []
    for row in existing or []:
        existing_id = str(row.get("pipeline_id") or "").strip()
        if not existing_id or existing_id in keep_ids:
            continue
        try:
            pipelines.upsert_pipeline(
                PLATFORM_RECIPES_OWNER_ID,
                row.get("name") or existing_id,
                row.get("definition") or {"nodes": []},
                description=(
                    "[deprecated] This recipe referenced a sunset agent and "
                    "has been retired."
                ),
                is_public=False,
                pipeline_id=existing_id,
            )
        except Exception:
            pass
    return ensured
