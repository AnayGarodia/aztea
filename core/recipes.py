"""Curated public pipeline recipes.

Every node in every recipe must point at an agent in
``CURATED_PUBLIC_BUILTIN_AGENT_IDS``. A pipeline that fans out to a sunset
agent silently fails or refunds, which is worse than not shipping the
recipe at all.
"""

from __future__ import annotations

import logging

from core import pipelines
from server.builtin_agents.constants import (
    CURATED_PUBLIC_BUILTIN_AGENT_IDS,
    DEPENDENCY_AUDITOR_AGENT_ID,
)

_LOG = logging.getLogger(__name__)

PLATFORM_RECIPES_OWNER_ID = "platform:recipes"

# Step "role" assigned to the first node in the DAG (no depends_on). Subsequent
# nodes get "follower". This is a presentation label only — the executor reads
# depends_on, not role — but the UI uses it to render the first step as the
# primary intent of the recipe.
_RECIPE_PRIMARY_ROLE = "primary"
_RECIPE_FOLLOWER_ROLE = "follower"


def step_role(node: dict) -> str:
    """Pure: classify a recipe node as primary or follower for UI rendering."""
    depends_on = node.get("depends_on") or []
    if isinstance(depends_on, list) and depends_on:
        return _RECIPE_FOLLOWER_ROLE
    return _RECIPE_PRIMARY_ROLE


def estimate_recipe_cost_cents(
    definition: dict, agent_price_cents_by_id: dict[str, int]
) -> tuple[int, list[str]]:
    """Pure: sum per-agent prices over a pipeline definition's nodes.

    Returns ``(total_cents, missing_agent_ids)``. An agent whose id isn't in
    ``agent_price_cents_by_id`` (e.g. a sunset agent removed from the catalog
    after this recipe was authored) is skipped from the total and surfaced in
    the second tuple element so the caller can show ``missing_agents`` to the
    UI. We choose to return rather than raise so a half-broken recipe still
    appears in the catalog — the user can see what's broken instead of a
    silent 500.
    """
    total = 0
    missing: list[str] = []
    nodes = (definition or {}).get("nodes") or []
    if not isinstance(nodes, list):
        return 0, []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        agent_id = str(node.get("agent_id") or "").strip()
        if not agent_id:
            continue
        if agent_id in agent_price_cents_by_id:
            total += int(agent_price_cents_by_id[agent_id])
        else:
            missing.append(agent_id)
    return total, missing


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
    # 2026-05-26: secret-scan-and-audit + security-audit-sealed removed
    # in the platform-pivot cull. Both fanned out to SECRET_SCANNER (now
    # sunset). `ensure_builtin_recipes()` below deletes stale recipes
    # from previous deploys, so any callers still listing these will see
    # them disappear cleanly. Re-introduce when a curated scanner returns.
    #
    # 2026-06-21: domain-health removed in the frontier-evidence cull. It
    # fanned out to DNS_INSPECTOR (now sunset). Tombstoned the same way —
    # `ensure_builtin_recipes()` retires it on startup. Re-introduce if a
    # curated live-lookup agent returns to the catalog.
]


# Sanity: every node must reference a live curated public agent so callers
# don't get silent failures.
for _recipe in BUILTIN_RECIPES:
    for _node in _recipe["pipeline_definition"]["nodes"]:
        assert (
            _node["agent_id"] in CURATED_PUBLIC_BUILTIN_AGENT_IDS
        ), f"recipe {_recipe['recipe_id']} references non-public agent {_node['agent_id']}"


def get_builtin_recipe_input_schema(recipe_id: str) -> dict | None:
    """Return the declared input schema for a built-in recipe, or None.

    Used by ``POST /recipes/{recipe_id}/run`` to validate caller input
    BEFORE pipeline execution starts. H-4 (audit 2026-05-19): pre-fix the
    ``domain-health`` recipe declared ``required: ["domains"]`` but
    callers who passed singular ``{"domain": "x"}`` got a cryptic
    ``ValueError: Could not resolve '$input.domains'`` from the executor's
    template-resolution layer. Now the validator runs first and emits a
    clean ``recipe.invalid_input`` with the missing field name.
    """
    rid = str(recipe_id or "").strip()
    if not rid:
        return None
    for recipe in BUILTIN_RECIPES:
        if str(recipe.get("recipe_id") or "") == rid:
            schema = recipe.get("default_input_schema")
            if isinstance(schema, dict) and schema:
                return schema
            return None
    return None


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
    # WHY: pipelines has no delete API; mark stale recipes private + tombstoned.
    # `list_recipes` only returns is_public=True rows, so they're hidden from
    # non-admin callers while preserved for receipt/back-compat reads.
    try:
        existing = pipelines.list_pipelines(
            PLATFORM_RECIPES_OWNER_ID, include_public=True
        )
    except Exception:
        _LOG.warning("recipes: failed to list existing pipelines for cleanup", exc_info=True)
        existing = []
    for row in existing:
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
            _LOG.warning(
                "recipes: failed to tombstone stale recipe %s",
                existing_id,
                exc_info=True,
            )
    return ensured
