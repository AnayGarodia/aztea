"""Every built-in recipe must be runnable with its declared schema.

Pre-fix (audit 2026-05-19), the ``domain-health`` recipe declared
``default_input_schema: {required: ["domains"]}`` but the example
invocation used singular ``domain`` and produced a cryptic
``ValueError: Could not resolve '$input.domains'`` from the template
resolver. The fix added input-schema validation before the executor
runs; these tests guarantee every recipe stays consistent with its
declared schema so future drift fails CI.
"""
from __future__ import annotations

import pytest

from core.recipes import BUILTIN_RECIPES, get_builtin_recipe_input_schema


def test_every_builtin_recipe_has_input_schema():
    """Recipes must declare default_input_schema so callers know what
    fields are required. Validation can't fire on a missing schema."""
    for recipe in BUILTIN_RECIPES:
        schema = recipe.get("default_input_schema")
        assert isinstance(schema, dict) and schema, (
            f"recipe {recipe['recipe_id']}: default_input_schema missing"
        )
        assert schema.get("type") == "object", (
            f"recipe {recipe['recipe_id']}: schema.type must be 'object'"
        )


def test_recipe_schema_required_fields_appear_in_pipeline_input_map():
    """Every field listed in a recipe's ``required`` must be referenced
    somewhere in the pipeline's input_map via ``$input.<field>``.
    H-4 root cause: ``domain-health`` declared ``required: ["domains"]``
    but no node read ``$input.domains`` correctly — drift between
    schema and template caused the silent failure."""
    for recipe in BUILTIN_RECIPES:
        schema = recipe.get("default_input_schema") or {}
        required = list(schema.get("required") or [])
        if not required:
            continue
        nodes = (recipe.get("pipeline_definition") or {}).get("nodes") or []
        full_template_text = ""
        for node in nodes:
            input_map = node.get("input_map") or {}
            full_template_text += " " + str(input_map)
        for field in required:
            assert f"$input.{field}" in full_template_text, (
                f"recipe {recipe['recipe_id']}: required field "
                f"{field!r} is not referenced via $input.{field} in "
                "any pipeline node's input_map. Either remove the "
                "required flag or wire the template."
            )


def test_get_builtin_recipe_input_schema_returns_schema_for_known_id():
    """API surface check — the lookup helper introduced for H-4 must
    return the schema for every BUILTIN_RECIPES entry."""
    for recipe in BUILTIN_RECIPES:
        result = get_builtin_recipe_input_schema(recipe["recipe_id"])
        assert result is not None
        assert result == recipe["default_input_schema"]


def test_get_builtin_recipe_input_schema_returns_none_for_unknown():
    """Defensive: unknown ids must not raise; they return None and the
    caller can treat that as 'no validation possible'."""
    assert get_builtin_recipe_input_schema("does-not-exist") is None
    assert get_builtin_recipe_input_schema("") is None
