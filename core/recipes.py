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
    DNS_INSPECTOR_AGENT_ID,
    SECRET_SCANNER_AGENT_ID,
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
        "recipe_id": "security-audit-sealed",
        "name": "security-audit-sealed",
        "description": (
            "Secret scan + dependency audit, with every step's output captured "
            "in a workspace and the whole run sealed under a signed Ed25519 "
            "manifest. Use this when you need a verifiable audit trail (security "
            "review, compliance, vendor due diligence)."
        ),
        "default_input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Source content to scan for leaked credentials.",
                },
                "manifest": {
                    "type": "string",
                    "description": "Dependency manifest (package.json or requirements.txt) to audit.",
                },
            },
            "required": ["content", "manifest"],
        },
        "pipeline_definition": {
            # Workspaces v0: each step's output is auto-written to the run's
            # workspace, and the workspace is sealed on completion. Callers get
            # `workspace_id` in the run-status response; `GET /workspaces/{id}/
            # manifest` returns the signed evidence and `POST /workspaces/{id}/
            # verify` is publicly callable (no auth) so an auditor can validate
            # the seal without a key.
            "auto_workspace": True,
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
            ],
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
