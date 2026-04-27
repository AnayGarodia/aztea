from __future__ import annotations

from tests.integration.helpers import _auth_headers, _register_user


def test_platform_tool_adapter_endpoints_include_control_plane_tools(client):
    caller = _register_user()

    codex = client.get("/codex/tools", headers=_auth_headers(caller["raw_api_key"]))
    assert codex.status_code == 200, codex.text
    codex_body = codex.json()
    codex_names = {tool["name"] for tool in codex_body["tools"]}
    assert codex_body["tool_format"] == "openai_responses_function"
    assert codex_body["meta_tools_included"] is True
    assert {"aztea_estimate_cost", "aztea_hire_async", "aztea_run_recipe"} <= codex_names

    gemini = client.get("/gemini/tools", headers=_auth_headers(caller["raw_api_key"]))
    assert gemini.status_code == 200, gemini.text
    gemini_body = gemini.json()
    declarations = gemini_body["function_declarations"]
    gemini_names = {tool["name"] for tool in declarations}
    assert gemini_body["tool_format"] == "gemini_function_declarations"
    assert gemini_body["meta_tools_included"] is True
    assert {"aztea_estimate_cost", "aztea_compare_status", "aztea_pipeline_status"} <= gemini_names

    openai = client.get("/openai/tools", headers=_auth_headers(caller["raw_api_key"]))
    assert openai.status_code == 200, openai.text
    openai_body = openai.json()
    openai_names = {tool["function"]["name"] for tool in openai_body["tools"]}
    assert openai_body["tool_format"] == "openai_chat_completions"
    assert "aztea_estimate_cost" in openai_names
