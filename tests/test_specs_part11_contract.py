"""
test_specs_part11_contract.py — shape + invariant tests for the seven new
agent specs returned by load_builtin_specs_part11().

Tests the spec data independently of the catalog assembler so a contract
break surfaces here before it lands in builtin_agent_specs().
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from server.builtin_agents.specs_part11 import load_builtin_specs_part11
from server.builtin_agents.constants import (
    CODEBASE_REVIEWER_AGENT_ID,
    COMPLIANCE_ATTESTOR_AGENT_ID,
    PENDING_INFRA_AGENT_IDS,
)


@pytest.fixture(scope="module")
def specs() -> list[dict[str, Any]]:
    return load_builtin_specs_part11()


_REQUIRED_TOP_LEVEL = {
    "agent_id", "name", "description", "endpoint_url",
    "price_per_call_usd", "tags", "match_keywords",
    "input_schema", "output_schema", "output_examples",
}


# ---------------------------------------------------------------------------
# 1. Count
# ---------------------------------------------------------------------------


def test_returns_seven_specs(specs):
    """After the 2026-05-23 editorial cut: 2 reference agents + 5 pending."""
    assert len(specs) == 7


# ---------------------------------------------------------------------------
# 2. Required top-level keys
# ---------------------------------------------------------------------------


def test_every_spec_has_required_top_level_keys(specs):
    for spec in specs:
        missing = _REQUIRED_TOP_LEVEL - set(spec.keys())
        assert not missing, (
            f"spec {spec.get('agent_id', '?')} missing required keys: {missing}"
        )


# ---------------------------------------------------------------------------
# 3. Endpoint prefix
# ---------------------------------------------------------------------------


def test_every_endpoint_starts_with_internal_prefix(specs):
    for spec in specs:
        assert spec["endpoint_url"].startswith("internal://"), (
            f"{spec['agent_id']}: endpoint must use internal://, "
            f"got {spec['endpoint_url']!r}"
        )


# ---------------------------------------------------------------------------
# 4–5. JSON Schema meta-validation
# ---------------------------------------------------------------------------


def _is_valid_json_schema_object(schema: Any) -> bool:
    """Pure: a minimal JSON-Schema shape check.

    Real jsonschema.validate_schema is more thorough, but for our use case
    the spec normalizer's _validate_jsonschema_shape already enforces:
    'type' == 'object', 'properties' is a dict, optional 'required' is a
    list of strings.
    """
    if not isinstance(schema, dict):
        return False
    if schema.get("type") != "object":
        return False
    if not isinstance(schema.get("properties"), dict):
        return False
    if "required" in schema and not (
        isinstance(schema["required"], list)
        and all(isinstance(r, str) for r in schema["required"])
    ):
        return False
    return True


def test_every_input_schema_validates(specs):
    for spec in specs:
        assert _is_valid_json_schema_object(spec["input_schema"]), (
            f"{spec['agent_id']}: input_schema fails JSON-Schema shape check: "
            f"{spec['input_schema']!r}"
        )


def test_every_output_schema_validates(specs):
    for spec in specs:
        assert _is_valid_json_schema_object(spec["output_schema"]), (
            f"{spec['agent_id']}: output_schema fails JSON-Schema shape check"
        )


# ---------------------------------------------------------------------------
# 6. No keyword collision with pre-existing agents (auto-hire ranking sanity)
# ---------------------------------------------------------------------------


def test_no_match_keyword_collides_with_curated_pre_existing_agent(specs):
    """Each new spec's match_keywords must not duplicate keywords claimed
    by ACTIVELY CURATED older agents (sunset agents are excluded since
    they don't participate in auto-hire ranking). Otherwise auto-hire's
    ranking gets confused between old + new candidates.

    Why "collides" only counts on EXACT match: substring collisions are
    impossible to avoid (e.g. both flake_hunter and the existing
    ci_failure_reproducer talk about "test"). We only flag identical
    strings.
    """
    from server.builtin_agents.specs_part1 import load_builtin_specs_part1
    from server.builtin_agents.specs_part2 import load_builtin_specs_part2
    from server.builtin_agents.constants import (
        CURATED_PUBLIC_BUILTIN_AGENT_IDS, SUNSET_DEPRECATED_AGENT_IDS,
    )
    # Only count keywords from agents that are currently CURATED_PUBLIC
    # AND not in PENDING_INFRA_AGENT_IDS (the latter aren't in auto-hire).
    pre_existing: set[str] = set()
    for old_spec in load_builtin_specs_part1() + load_builtin_specs_part2():
        agent_id = old_spec.get("agent_id")
        if (agent_id in CURATED_PUBLIC_BUILTIN_AGENT_IDS
                and agent_id not in SUNSET_DEPRECATED_AGENT_IDS):
            for kw in old_spec.get("match_keywords", []):
                pre_existing.add(kw.lower())
    for spec in specs:
        for kw in spec.get("match_keywords", []):
            assert kw.lower() not in pre_existing, (
                f"{spec['agent_id']} ({spec['name']}): match_keyword "
                f"{kw!r} collides with a currently-active curated agent"
            )


# ---------------------------------------------------------------------------
# 7. Reference agents have category + cacheable
# ---------------------------------------------------------------------------


def test_d16_and_c11_have_category_and_cacheable(specs):
    by_id = {s["agent_id"]: s for s in specs}
    for ref_id in (CODEBASE_REVIEWER_AGENT_ID, COMPLIANCE_ATTESTOR_AGENT_ID):
        spec = by_id[ref_id]
        assert "category" in spec, f"{ref_id}: missing category"
        assert "cacheable" in spec, f"{ref_id}: missing cacheable"
        assert isinstance(spec["category"], str) and spec["category"]
        assert isinstance(spec["cacheable"], bool)


# ---------------------------------------------------------------------------
# 8. Pending agents intentionally lack category
# ---------------------------------------------------------------------------


def test_pending_agents_lack_category(specs):
    """The five pending-infra agents bypass the stricter normalisation gate
    so they don't need category/cacheable. This is INTENTIONAL: their
    specs only become production-grade once their external dep is wired."""
    by_id = {s["agent_id"]: s for s in specs}
    for agent_id in PENDING_INFRA_AGENT_IDS:
        spec = by_id[agent_id]
        # category absent OR explicitly None — both are fine.
        assert "category" not in spec, (
            f"{agent_id}: should NOT have category until graduated from "
            f"PENDING_INFRA_AGENT_IDS"
        )


# ---------------------------------------------------------------------------
# 9. Price is a real float (not a string)
# ---------------------------------------------------------------------------


def test_price_per_call_usd_is_numeric(specs):
    for spec in specs:
        price = spec["price_per_call_usd"]
        assert isinstance(price, (int, float)) and not isinstance(price, bool), (
            f"{spec['agent_id']}: price must be numeric, got "
            f"{type(price).__name__}={price!r}"
        )
        assert price >= 0, (
            f"{spec['agent_id']}: negative price {price}"
        )


# ---------------------------------------------------------------------------
# 10. Match keywords are lowercase
# ---------------------------------------------------------------------------


def test_match_keywords_are_lowercase(specs):
    for spec in specs:
        for kw in spec.get("match_keywords", []):
            assert kw == kw.lower(), (
                f"{spec['agent_id']}: keyword {kw!r} must be all-lowercase"
            )


# ---------------------------------------------------------------------------
# 11. Tags contain no duplicates within a spec
# ---------------------------------------------------------------------------


def test_tags_no_duplicates_within_spec(specs):
    for spec in specs:
        tags = spec.get("tags", [])
        assert len(tags) == len(set(tags)), (
            f"{spec['agent_id']}: duplicate tag in {tags!r}"
        )


# ---------------------------------------------------------------------------
# Bonus: ID uniqueness within the slate
# ---------------------------------------------------------------------------


def test_no_duplicate_agent_ids_within_specs(specs):
    ids = [s["agent_id"] for s in specs]
    assert len(ids) == len(set(ids)), "duplicate agent_id found in specs_part11"


# ---------------------------------------------------------------------------
# Bonus: every spec has at least one output_example
# ---------------------------------------------------------------------------


def test_every_spec_has_at_least_one_output_example(specs):
    for spec in specs:
        examples = spec.get("output_examples", [])
        assert isinstance(examples, list) and len(examples) >= 1, (
            f"{spec['agent_id']}: must have at least one output_example"
        )
        for ex in examples:
            assert isinstance(ex, dict)
            assert "input" in ex and "output" in ex


# ---------------------------------------------------------------------------
# Bonus: endpoint slugs match agent module names
# ---------------------------------------------------------------------------


def test_endpoint_slug_matches_agent_module(specs):
    """internal://flake_hunter must correspond to importable agents.flake_hunter."""
    import importlib
    for spec in specs:
        slug = spec["endpoint_url"].removeprefix("internal://")
        # Importing the module should succeed.
        mod = importlib.import_module(f"agents.{slug}")
        assert callable(mod.run), f"agents.{slug}.run missing or not callable"
