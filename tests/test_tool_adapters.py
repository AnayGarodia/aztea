from __future__ import annotations

from core import tool_adapters


def _sample_agent() -> dict:
    return {
        "agent_id": "agent-123",
        "name": "Review Agent",
        "description": "Review code for bugs.",
        "price_per_call_usd": 0.05,
        "trust_score": 88,
        "success_rate": 0.97,
        "avg_latency_ms": 1200,
        "total_calls": 42,
        "by_client": {"claude-code": 91.0, "codex": 84.0},
        "pii_safe": True,
        "outputs_not_stored": True,
        "audit_logged": True,
        "region_locked": "us",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        "output_schema": {"type": "object"},
    }


def test_openai_chat_manifest_includes_meta_and_registry_tools():
    payload = tool_adapters.build_openai_chat_manifest([_sample_agent()])
    names = {tool["function"]["name"] for tool in payload["tools"]}
    assert "aztea_estimate_cost" in names
    assert "review_agent" in names
    registry_tool = next(tool for tool in payload["tools"] if tool["function"]["name"] == "review_agent")
    assert registry_tool["function"]["metadata"]["aztea_tool_kind"] == "registry_agent"
    assert registry_tool["function"]["metadata"]["aztea_agent_id"] == "agent-123"
    assert payload["tool_lookup"]["review_agent"]["kind"] == "registry_agent"
    assert payload["tool_lookup"]["review_agent"]["agent_id"] == "agent-123"
    assert payload["tool_lookup"]["review_agent"]["privacy"]["region_locked"] == "us"
    assert registry_tool["function"]["metadata"]["trust_score_by_client"]["claude-code"] == 91.0


def test_openai_responses_manifest_uses_top_level_function_fields():
    payload = tool_adapters.build_openai_responses_manifest([_sample_agent()])
    review_tool = next(tool for tool in payload["tools"] if tool["name"] == "review_agent")
    assert review_tool["type"] == "function"
    assert review_tool["parameters"]["type"] == "object"
    assert review_tool["strict"] is False
    assert payload["tool_lookup"]["review_agent"]["kind"] == "registry_agent"
    assert payload["tool_lookup"]["review_agent"]["agent_id"] == "agent-123"
    assert payload["tool_lookup"]["review_agent"]["trust_score_by_client"]["codex"] == 84.0


def test_gemini_manifest_wraps_function_declarations():
    payload = tool_adapters.build_gemini_manifest([_sample_agent()])
    declarations = payload["function_declarations"]
    names = {tool["name"] for tool in declarations}
    assert "aztea_estimate_cost" in names
    assert "review_agent" in names
    assert payload["tools"][0]["functionDeclarations"] == declarations
    assert payload["tool_lookup"]["review_agent"]["kind"] == "registry_agent"
    assert payload["tool_lookup"]["review_agent"]["agent_id"] == "agent-123"
    assert payload["tool_lookup"]["review_agent"]["trust_score_by_client"]["claude-code"] == 91.0
