from core import mcp_manifest


def test_build_mcp_tool_entries_converts_fields_schema_and_dedupes_names():
    agents = [
        {
            "agent_id": "11111111-1111-1111-1111-111111111111",
            "name": "Alpha Agent",
            "description": "Handles alpha workflows.",
            "input_schema": {
                "fields": [
                    {"name": "ticker", "type": "string", "required": True},
                    {"name": "depth", "type": "integer"},
                ]
            },
            "output_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
        {
            "agent_id": "22222222-2222-2222-2222-222222222222",
            "name": "Alpha Agent",
            "description": "Same name should still produce unique tool names.",
            "input_schema": {},
            "output_schema": {},
        },
    ]
    entries = mcp_manifest.build_mcp_tool_entries(agents)
    assert len(entries) == 2
    assert entries[0]["tool_name"] != entries[1]["tool_name"]
    first_input = entries[0]["tool"]["input_schema"]
    assert first_input["type"] == "object"
    assert first_input["properties"]["ticker"]["type"] == "string"
    assert first_input["properties"]["depth"]["type"] == "integer"
    assert first_input["required"] == ["ticker"]


def test_build_mcp_manifest_has_tools_count_and_timestamp():
    agents = [
        {
            "agent_id": "33333333-3333-3333-3333-333333333333",
            "name": "Manifest Agent",
            "description": "Manifest payload test.",
            "input_schema": {},
            "output_schema": {},
        }
    ]
    manifest = mcp_manifest.build_mcp_manifest(agents)
    assert manifest["count"] == 1
    assert len(manifest["tools"]) == 1
    assert not manifest["tools"][0]["name"].startswith("agentmarket__")
    assert manifest["generated_at"]

