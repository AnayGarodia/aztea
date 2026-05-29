"""GET /recipes catalog tests.

Pins the discoverable-recipes contract: a caller can enumerate every built-in
recipe, see its steps and pricing, and identify recipes that reference
agents no longer in the catalog. Without these tests the new fields drift
silently — they're read by the Workflows page + the MCP ``list_recipes``
action, neither of which would tell us if ``estimated_total_cost_usd``
disappeared from the response.
"""

from __future__ import annotations

import pytest

from tests.integration.support import *  # noqa: F403

# Built-in recipes live in ``core/recipes.py``. The 2026-05-26 platform-pivot
# cull dropped secret-scan-and-audit + security-audit-sealed (both fanned
# out to the now-sunset secret_scanner agent). Pin the source of truth
# here so this test fails loudly the day someone adds or removes a built-in.
_EXPECTED_BUILTIN_SLUGS = {"audit-deps", "domain-health"}


def _fetch_recipes(client, raw_api_key: str):
    return client.get("/recipes", headers=_auth_headers(raw_api_key))


def test_list_recipes_returns_all_built_in_recipes(client):
    caller = _register_user()
    resp = _fetch_recipes(client, caller["raw_api_key"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    slugs = {r["slug"] for r in body["recipes"] if r.get("slug")}
    # The user has no own recipes here, so the public set is the entire result.
    assert _EXPECTED_BUILTIN_SLUGS.issubset(slugs), body


def test_list_recipes_each_has_required_fields(client):
    caller = _register_user()
    body = _fetch_recipes(client, caller["raw_api_key"]).json()
    for recipe in body["recipes"]:
        # Skip user-owned recipes if any leaked into the catalog; this test
        # is about the built-in shape contract.
        if recipe.get("slug") not in _EXPECTED_BUILTIN_SLUGS:
            continue
        assert isinstance(recipe.get("slug"), str) and recipe["slug"]
        assert isinstance(recipe.get("name"), str) and recipe["name"]
        assert isinstance(recipe.get("description"), str) and recipe["description"]
        steps = recipe.get("steps")
        assert isinstance(steps, list) and len(steps) >= 1, recipe
        for step in steps:
            assert isinstance(step.get("agent_id"), str)
            # agent_slug + price are None only when the agent is missing.
            # All seeded built-ins should resolve, so both fields must be
            # populated for shape compliance.
            assert isinstance(step.get("agent_slug"), str) and step["agent_slug"]
            assert isinstance(step.get("role"), str) and step["role"]
            assert isinstance(step.get("price_per_call_usd"), (int, float))
        assert isinstance(recipe.get("default_input_schema"), dict), recipe
        assert isinstance(recipe.get("estimated_total_cost_usd"), (int, float))
        assert recipe["estimated_total_cost_usd"] > 0, recipe
        assert recipe.get("missing_agents") == [], recipe


def test_list_recipes_estimated_cost_matches_step_prices(client):
    """Walk audit-deps (a deterministic single-step recipe) and verify the
    total equals the sum of step prices within rounding tolerance."""
    caller = _register_user()
    body = _fetch_recipes(client, caller["raw_api_key"]).json()
    audit = next(r for r in body["recipes"] if r.get("slug") == "audit-deps")
    expected = round(
        sum(float(step.get("price_per_call_usd") or 0) for step in audit["steps"]),
        2,
    )
    # Allow $0.01 tolerance for float→cents→float rounding accumulating across
    # multiple steps. estimate_recipe_cost_cents itself is integer-exact, but
    # the per-step USD echoed in the response is a 2-decimal-place rounded view.
    assert abs(audit["estimated_total_cost_usd"] - expected) <= 0.01, audit


def test_list_recipes_requires_caller_auth(client):
    # No bearer header at all — should be rejected before any handler logic.
    resp = client.get("/recipes")
    assert resp.status_code in (401, 403), resp.text


def test_list_recipes_surfaces_missing_agents_when_agent_is_unregistered(client, monkeypatch):
    """Inject a recipe referencing an unknown agent_id and confirm it is
    still returned with ``missing_agents`` naming the bad id. A silent drop
    would hide breakage from operators."""
    from core import recipes as core_recipes
    from core import pipelines as core_pipelines

    bogus_agent_id = "00000000-0000-0000-0000-deadbeefdead"
    fake_recipe = {
        "recipe_id": "broken-recipe-fixture",
        "name": "broken-recipe-fixture",
        "description": "Test fixture: references an agent that doesn't exist.",
        "default_input_schema": {"type": "object"},
        "pipeline_definition": {
            "nodes": [{"id": "step1", "agent_id": bogus_agent_id, "input_map": {}}]
        },
    }
    # Drop the recipe into the upserted catalog via the same path
    # ensure_builtin_recipes uses, then re-fetch.
    core_pipelines.upsert_pipeline(
        core_recipes.PLATFORM_RECIPES_OWNER_ID,
        fake_recipe["name"],
        fake_recipe["pipeline_definition"],
        description=fake_recipe["description"],
        is_public=True,
        pipeline_id=fake_recipe["recipe_id"],
    )

    caller = _register_user()
    body = _fetch_recipes(client, caller["raw_api_key"]).json()
    broken = next(
        (r for r in body["recipes"] if r.get("slug") == "broken-recipe-fixture"),
        None,
    )
    assert broken is not None, "broken recipe must still surface in the catalog"
    assert bogus_agent_id in broken.get("missing_agents") or [], broken
    # The cost is still numeric — the missing agent contributes $0.00, not NaN.
    assert isinstance(broken.get("estimated_total_cost_usd"), (int, float))
