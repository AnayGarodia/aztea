from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-master-key")

import pytest  # noqa: E402

from server.builtin_agents.specs import (  # noqa: E402
    builtin_agent_specs,
    builtin_catalog_metadata,
)


def test_builtin_specs_have_catalog_contract_fields():
    specs = builtin_agent_specs()
    assert specs
    for spec in specs:
        assert str(spec.get("agent_id") or "").strip()
        assert str(spec.get("endpoint_url") or "").startswith("internal://")
        examples = spec.get("output_examples")
        assert isinstance(examples, list) and examples
        if spec.get("deprecated"):
            continue
        if spec.get("agent_id") == "9cf0d9d0-4a10-58c9-b97a-6b5f81b1cf33":
            # quality judge is intentionally internal-only and not part of
            # public marketplace discovery quality constraints.
            continue
        metadata = builtin_catalog_metadata(spec["agent_id"])
        assert metadata is not None
        assert isinstance(metadata["category"], str) and metadata["category"].strip()
        assert isinstance(metadata["cacheable"], bool)
        assert isinstance(metadata["is_featured"], bool)
        assert isinstance(metadata["runtime_requirements"], list)
        assert isinstance(metadata["tooling_kind"], str) and metadata["tooling_kind"].strip()
        assert isinstance(metadata["stability_tier"], str) and metadata["stability_tier"].strip()
        assert isinstance(metadata["codex_recommended"], bool)
        assert isinstance(metadata["short_use_cases"], list)


def test_builtin_catalog_metadata_returns_none_for_removed_agents():
    removed_github_fetcher = "5896576f-bbe6-59e4-83c1-5106002e7d10"
    metadata = builtin_catalog_metadata(removed_github_fetcher)
    assert metadata is None


def test_jsonschema_shape_validator_rejects_obvious_breakage():
    """The shape validator runs at module load, so a malformed schema in
    a spec file would crash import. Exercise it directly with hostile
    inputs to make sure it catches the cases we care about — a typo in
    a future spec must not pass silently into the MCP manifest."""
    from server.builtin_agents.specs import _validate_jsonschema_shape

    # Happy paths — must not raise.
    _validate_jsonschema_shape({}, field="input_schema", agent_id="x")
    _validate_jsonschema_shape({"type": "object"}, field="input_schema", agent_id="x")
    _validate_jsonschema_shape(
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        field="input_schema",
        agent_id="x",
    )

    bad_inputs = [
        ("not-a-dict", "must be a dict"),
        ({"type": "array"}, "type must be 'object'"),
        ({"properties": "not-a-dict"}, "properties must be a dict"),
        ({"required": "not-a-list"}, "required must be a list of strings"),
        ({"required": [1, 2]}, "required must be a list of strings"),
    ]
    for bad, expect_in_msg in bad_inputs:
        with pytest.raises(ValueError) as exc_info:
            _validate_jsonschema_shape(bad, field="input_schema", agent_id="x")
        assert expect_in_msg in str(exc_info.value), str(exc_info.value)


def test_builtin_dispatch_table_covers_every_internal_endpoint():
    """The 35-branch if-chain in part_004 was replaced with a dispatch dict.
    Every agent that registers an ``internal://`` endpoint MUST have a
    runner in BUILTIN_AGENT_RUNNERS, otherwise the call path raises
    'Unsupported built-in agent' at runtime. This test catches the drift
    at CI time so adding a new built-in can't regress production silently.
    """
    import server.application as server_app  # noqa: F401  (force shard load)

    BUILTIN_AGENT_RUNNERS = getattr(server_app, "BUILTIN_AGENT_RUNNERS")
    from server.builtin_agents.constants import BUILTIN_INTERNAL_ENDPOINTS

    missing = set(BUILTIN_INTERNAL_ENDPOINTS) - set(BUILTIN_AGENT_RUNNERS)
    extra = set(BUILTIN_AGENT_RUNNERS) - set(BUILTIN_INTERNAL_ENDPOINTS)
    assert not missing, f"agents in BUILTIN_INTERNAL_ENDPOINTS without a runner: {missing}"
    assert not extra, f"runners with no internal endpoint: {extra}"

    for runner in BUILTIN_AGENT_RUNNERS.values():
        assert callable(runner)
