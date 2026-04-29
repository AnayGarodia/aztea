from __future__ import annotations

from server.builtin_agents.specs import builtin_agent_specs, builtin_catalog_metadata


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


def test_builtin_catalog_metadata_returns_none_for_removed_agents():
    removed_github_fetcher = "5896576f-bbe6-59e4-83c1-5106002e7d10"
    metadata = builtin_catalog_metadata(removed_github_fetcher)
    assert metadata is None
