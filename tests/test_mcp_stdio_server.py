from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest


# 1.6.3: the canonical MCP server module moved from scripts/ into the SDK
# package (PR #38). scripts/aztea_mcp_server.py is now a 30-line shim that
# imports `main` only — `_AUTH_TOOL`, `MCPStdioServer`, etc. live in
# aztea.mcp.server. Use a real package import so relative imports inside
# the new module (e.g. `from . import manifest`) resolve.
import sys as _sys
_SDK = str(Path(__file__).resolve().parents[1] / "sdks" / "python-sdk")
if _SDK not in _sys.path:
    _sys.path.insert(0, _SDK)
import importlib as _importlib
_MODULE = _importlib.import_module("aztea.mcp.server")


class _DummyBridge:
    def tools(self):
        return []

    def call_tool(self, _tool_name: str, _arguments: dict):
        return True, {}


class _FakeStdin:
    def __init__(self, raw: bytes) -> None:
        self.buffer = io.BytesIO(raw)


def test_auth_tool_uses_snake_case_input_schema_key():
    assert "input_schema" in _MODULE._AUTH_TOOL
    assert "inputSchema" not in _MODULE._AUTH_TOOL


def test_read_message_rejects_invalid_content_length(monkeypatch):
    server = _MODULE.MCPStdioServer(bridge=_DummyBridge(), refresh_seconds=60)
    monkeypatch.setattr(_MODULE.sys, "stdin", _FakeStdin(b"Content-Length: abc\r\n\r\n{}"))
    with pytest.raises(ValueError, match="Invalid Content-Length"):
        server._read_message()


def test_registry_bridge_headers_include_client_id():
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    headers = bridge._headers()
    assert headers["X-Aztea-Version"] == "1.0"
    assert headers["X-Aztea-Client"] == "claude-code"


def test_registry_bridge_uses_lazy_tool_list_when_flag_enabled(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._entries = [
        {
            "agent_id": "agent-1",
            "tool_name": "python_code_executor",
            "tool": {
                "name": "python_code_executor",
                "description": "Execute Python snippets.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        }
    ]
    tools = bridge.tools()
    names = [tool["name"] for tool in tools]
    # Lazy mode: 4 core lazy tools + 3 always-visible resource-grouped tools.
    # Order matters: lazy core first, then grouped resource dispatchers.
    assert names[:4] == [
        "search_specialists",
        "describe_specialist",
        "call_specialist",
        "do_specialist_task",
    ]
    # Co-pilot mode adds aztea_call_streaming and aztea_steer to the lazy
    # surface so MCP clients can stream + steer running jobs without going
    # through the verbose manage_job dispatcher for these hot paths.
    assert set(names[4:]) == {
        "manage_job",
        "manage_budget",
        "manage_workflow",
        "aztea_call_streaming",
        "aztea_steer",
    }
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert tools[2]["annotations"]["readOnlyHint"] is False


def test_registry_bridge_lazy_search_and_describe(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._entries = [
        {
            "agent_id": "agent-1",
            "tool_name": "python_code_executor",
            "tool": {
                "name": "python_code_executor",
                "description": "Execute Python snippets.",
                "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}},
                "output_schema": {"type": "object"},
            },
            "catalog_metadata": {
                "category": "Code Execution",
                "tooling_kind": "sandbox_execution",
                "stability_tier": "stable",
                "codex_recommended": True,
                "short_use_cases": ["run a snippet"],
                "price_per_call_usd": 0.06,
                "success_rate": 0.97,
                "trust_score": 91,
                "avg_latency_ms": 800,
            },
        }
    ]

    ok, search = bridge.call_tool("aztea_search", {"query": "python snippets"})
    assert ok is True
    assert search["results"][0]["slug"] == "python_code_executor"
    assert search["results"][0]["category"] == "Code Execution"
    assert search["results"][0]["codex_recommended"] is True

    ok, described = bridge.call_tool("aztea_describe", {"slug": "python_code_executor"})
    assert ok is True
    assert described["input_schema"]["properties"]["code"]["type"] == "string"
    assert described["category"] == "Code Execution"
    assert described["codex_recommended"] is True


def test_registry_bridge_describe_accepts_agent_suffix_alias(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._entries = [
        {
            "agent_id": "agent-review",
            "tool_name": "code_review_agent",
            "tool": {
                "name": "code_review_agent",
                "description": "Review code and diffs for correctness.",
                "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}},
                "output_schema": {"type": "object"},
            },
            "catalog_metadata": {
                "name": "Code Review Agent",
                "category": "Code Review",
                "tooling_kind": "structured_review",
                "stability_tier": "stable",
                "codex_recommended": True,
                "short_use_cases": ["review a diff"],
                "price_per_call_usd": 0.01,
                "success_rate": 0.9,
                "trust_score": 60,
                "avg_latency_ms": 1200,
            },
        }
    ]

    ok, described = bridge.call_tool("aztea_describe", {"slug": "code_review"})
    assert ok is True
    assert described["slug"] == "code_review_agent"


def test_initialize_instructions_encourage_proactive_orchestration():
    server = _MODULE.MCPStdioServer(bridge=_DummyBridge(), refresh_seconds=60)
    instructions = server._initialize_result()["instructions"]
    # Categorical routing rule replaces the old "use Aztea" exhortation: the
    # decision rule is the load-bearing sentence, plus an explicit no-brand-keyword
    # clause so the model picks specialists on intent matching alone.
    assert "Decision rule" in instructions
    assert "do_specialist_task" in instructions
    # PR #38 rewrote instructions: "brand keyword" → "the word 'Aztea'".
    # Same intent — model picks specialists by category match, not by the
    # user typing "use Aztea". Pin the new wording so a future rewrite that
    # drops the no-brand-keyword guidance entirely fails this assertion.
    assert "do NOT need" in instructions and "Aztea" in instructions
    # PR #38 also dropped the literal `aztea_hire_batch` / `aztea_hire_async`
    # references from the boot instructions in favour of the verb-first
    # `manage_workflow(action="hire_batch", ...)` form. Assert on the
    # category names + grouped dispatcher instead.
    assert "hire_batch" in instructions
    assert "manage_workflow" in instructions or "do_specialist_task" in instructions


def test_registry_bridge_lazy_search_returns_workflow_hints_for_parallel_tasks(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    ok, result = bridge.call_tool("aztea_search", {"query": "review many files in parallel with a budget", "limit": 5})
    assert ok is True
    hints = result.get("workflow_hints") or []
    assert any("aztea_hire_batch" in hint for hint in hints)
    assert any("aztea_set_session_budget" in hint for hint in hints)


def test_word_truncate_breaks_on_word_boundary():
    # Regression for the 2026-05-01 prod audit: "…code-level f", "…claude-code "
    long = "Use when the user wants live CVE data for a package and wants more"
    out = _MODULE._word_truncate(long, 30)
    assert out.endswith("…")
    head = out.rstrip("…").rstrip()
    # Last visible character must be the end of a complete word
    assert " " in long[: len(head) + 1]
    # No-op for short inputs
    assert _MODULE._word_truncate("short", 50) == "short"


def test_verb_rule_promotes_sql_explainer_for_explain_query():
    # Regression: db_sandbox previously outranked sql_explainer for "explain SQL".
    promoted = _MODULE._verb_rule_score("sql_explainer", ["explain", "sql", "query"])
    demoted = _MODULE._verb_rule_score("db_sandbox", ["explain", "sql", "query"])
    assert promoted > 0
    assert demoted < 0
    # Sandbox stays on top for "run SQL"
    run_promoted = _MODULE._verb_rule_score("db_sandbox", ["run", "sql", "query"])
    assert run_promoted > 0
    # Topic-only query (no verb) leaves both at zero
    assert _MODULE._verb_rule_score("db_sandbox", ["sql"]) == 0
    assert _MODULE._verb_rule_score("sql_explainer", ["sql"]) == 0


def test_describe_surfaces_output_schema_fields(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._entries = [
        {
            "agent_id": "lint",
            "tool_name": "linter_agent",
            "tool": {
                "name": "linter_agent",
                "description": "Lint Python.",
                "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
                "output_schema": {
                    "type": "object",
                    "properties": {"issues": {"type": "array"}, "clean": {"type": "boolean"}},
                    "required": ["issues", "clean"],
                },
            },
            "catalog_metadata": {"category": "Code Quality"},
        }
    ]
    ok, described = bridge.call_tool("aztea_describe", {"slug": "linter_agent"})
    assert ok is True
    # Pre-2026-05-01 audit: output_schema returned but never highlighted.
    assert set(described["output_fields"]) == {"issues", "clean"}
    assert described["output_required_fields"] == ["issues", "clean"]


def test_aztea_do_tool_is_registered_in_lazy_surface():
    """The fast-path auto-invoke tool must be exposed alongside the legacy
    search/describe/call lazy trio. Catches accidental removal during
    refactors of the lazy tool registration."""
    assert _MODULE._LAZY_DO_TOOL["name"] == "do_specialist_task"
    schema = _MODULE._LAZY_DO_TOOL["input_schema"]
    assert "intent" in schema["properties"]
    assert "max_cost_usd" in schema["properties"]
    assert "dry_run" in schema["properties"]
    assert schema["required"] == ["intent"]
    # Backward-compat: the legacy `aztea_do` name must still resolve.
    assert _MODULE._LAZY_TOOL_NAME_ALIASES["aztea_do"] == "do_specialist_task"


def test_legacy_lazy_tool_names_alias_to_verb_first_dispatch():
    """Old clients (cached tool lists, hardcoded SDK examples) keep calling
    `aztea_do` / `aztea_search` / `aztea_describe` / `aztea_call`. The
    dispatch must normalize these so behavior is identical to the new names."""
    aliases = _MODULE._LAZY_TOOL_NAME_ALIASES
    assert aliases == {
        "aztea_do": "do_specialist_task",
        "aztea_search": "search_specialists",
        "aztea_describe": "describe_specialist",
        "aztea_call": "call_specialist",
        # Grouped resource dispatchers — same backward-compat technique.
        "aztea_job": "manage_job",
        "aztea_budget": "manage_budget",
        "aztea_workflow": "manage_workflow",
    }
    # New names ARE the canonical names on the four lazy tool dicts.
    assert _MODULE._LAZY_SEARCH_TOOL["name"] == "search_specialists"
    assert _MODULE._LAZY_DESCRIBE_TOOL["name"] == "describe_specialist"
    assert _MODULE._LAZY_CALL_TOOL["name"] == "call_specialist"
    assert _MODULE._LAZY_DO_TOOL["name"] == "do_specialist_task"


def test_mcp_text_formatter_makes_search_results_readable():
    text = _MODULE._mcp_text_from_payload(
        {
            "query": "review many files",
            "results": [
                {
                    "slug": "aztea_hire_batch",
                    "name": "aztea_hire_batch",
                    "category": "Platform",
                    "price_per_call_usd": None,
                    "trust_score": None,
                    "success_rate": None,
                    "quality_summary": "Claude-ready | stable",
                    "best_for": ["parallel subtasks"],
                }
            ],
            "workflow_hints": ["This task looks parallelizable. Consider aztea_hire_batch for many independent subtasks."],
            "next_step": "Best match: aztea_hire_batch.",
        }
    )
    assert "Aztea matches for: review many files" in text
    assert "parallel subtasks" in text
    assert "Workflow hints:" in text


def test_aztea_call_forwards_output_format_into_underlying_call(monkeypatch):
    """Regression: aztea_call(slug=..., arguments={...}, output_format='markdown')
    used to silently drop output_format. The bridge must merge it into the
    inner tool_arguments so the registry call attaches `rendered_output`."""
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._auth_required = False
    bridge._entries = [
        {
            "agent_id": "agent-1",
            "tool_name": "linter_agent",
            "tool": {
                "name": "linter_agent",
                "description": "lint",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        }
    ]

    real_call_tool = _MODULE.RegistryBridge.call_tool
    captured: dict = {}

    def _spy_call_tool(self, slug, tool_arguments):
        # The aztea_call branch recurses with the resolved slug. Capture that
        # second hop and short-circuit; the first hop (slug='aztea_call')
        # still goes through the real method.
        if slug != "aztea_call":
            captured["slug"] = slug
            captured["tool_arguments"] = dict(tool_arguments)
            return True, {"ok": True}
        return real_call_tool(self, slug, tool_arguments)

    monkeypatch.setattr(_MODULE.RegistryBridge, "call_tool", _spy_call_tool)
    bridge.call_tool(
        "aztea_call",
        {
            "slug": "linter_agent",
            "arguments": {"language": "python", "code": "x = 1"},
            "output_format": "markdown",
        },
    )
    assert captured["slug"] == "linter_agent"
    assert captured["tool_arguments"]["output_format"] == "markdown"
    assert captured["tool_arguments"]["code"] == "x = 1"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5 contract tests — lock the verb-first rename + alias map so a future
# refactor can't silently regress them. These tests assert the rename is the
# canonical state, not just one valid state.
# ─────────────────────────────────────────────────────────────────────────────

def _make_bridge_with_one_agent(monkeypatch):
    """Tiny helper: a bridge populated with one agent so tools/list returns
    the four lazy tools deterministically."""
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    bridge._entries = [
        {
            "agent_id": "agent-1",
            "tool_name": "python_code_executor",
            "tool": {
                "name": "python_code_executor",
                "description": "Execute Python snippets.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        }
    ]
    return bridge


def test_published_tools_list_uses_verb_first_canonical_names(monkeypatch):
    """tools/list must publish ONLY the verb-first names; legacy aztea_*
    names must NOT appear as separate entries — they are dispatch-time
    aliases, not catalog entries. Two duplicate entries would dilute the
    model's selection signal."""
    bridge = _make_bridge_with_one_agent(monkeypatch)
    names = {tool["name"] for tool in bridge.tools()}
    assert {"do_specialist_task", "search_specialists",
            "describe_specialist", "call_specialist",
            "manage_job", "manage_budget", "manage_workflow"} <= names
    # Legacy names must NOT appear in the published catalog.
    assert "aztea_do" not in names
    assert "aztea_search" not in names
    assert "aztea_describe" not in names
    assert "aztea_call" not in names
    assert "aztea_job" not in names
    assert "aztea_budget" not in names
    assert "aztea_workflow" not in names


@pytest.mark.parametrize("legacy,canonical", [
    ("aztea_search", "search_specialists"),
    ("aztea_describe", "describe_specialist"),
    ("aztea_do", "do_specialist_task"),
    ("aztea_job", "manage_job"),
    ("aztea_budget", "manage_budget"),
    ("aztea_workflow", "manage_workflow"),
    # aztea_call → call_specialist requires HTTP plumbing the dummy bridge
    # doesn't have, so we cover that pair via the smoke test in
    # test_buyer_surface_smoke.py instead.
])
def test_each_legacy_alias_dispatches_to_its_verb_first_handler(
    monkeypatch, legacy, canonical
):
    """tools/call with the legacy name must reach the same handler as the
    canonical name. We verify by patching the alias map and confirming the
    dispatch normalization happens before any handler dispatch."""
    # Helper sets LAZY_MCP_SCHEMAS via monkeypatch; the bridge itself is
    # unused here — only the alias map matters for this assertion.
    _make_bridge_with_one_agent(monkeypatch)
    assert _MODULE._LAZY_TOOL_NAME_ALIASES[legacy] == canonical


def test_call_specialist_rejects_both_legacy_and_new_names_as_slug(monkeypatch):
    """Recursion guard: call_specialist(slug=<any-lazy-tool-name>) must be
    rejected. Without this, a model could recurse infinitely by passing
    'aztea_call' or 'call_specialist' as the slug."""
    bridge = _MODULE.RegistryBridge(base_url="https://aztea.test", api_key="az_test")
    forbidden_slugs = [
        "aztea_call", "call_specialist",
        "aztea_search", "search_specialists",
        "aztea_describe", "describe_specialist",
        "aztea_do", "do_specialist_task",
    ]
    for slug in forbidden_slugs:
        ok, result = bridge.call_tool("call_specialist", {"slug": slug})
        assert ok is False, f"slug={slug!r} should have been rejected"
        assert result["error"] == "INVALID_INPUT"
        # Same rejection must happen via the legacy alias entry point.
        ok2, result2 = bridge.call_tool("aztea_call", {"slug": slug})
        assert ok2 is False, f"legacy aztea_call should also reject slug={slug!r}"
        assert result2["error"] == "INVALID_INPUT"


def test_server_instructions_contain_four_categories_and_decision_rule():
    """Lock the categorical framing: all four category labels must appear,
    plus the decision-rule sentence. Catches a future revert to enumerated
    triggers."""
    server = _MODULE.MCPStdioServer(bridge=_DummyBridge(), refresh_seconds=60)
    instructions = server._initialize_result()["instructions"]
    for category in ("EXECUTION", "LIVE DATA", "INDEPENDENT VERDICT", "MULTI-STEP WORKFLOW"):
        assert category in instructions, f"missing category: {category}"
    # The decision rule is the load-bearing sentence.
    assert "Decision rule" in instructions
    assert "work *on*" in instructions and "work that *uses*" in instructions


def test_server_instructions_lead_with_verb_first_names_in_default_path():
    """The DEFAULT path in instructions must name do_specialist_task before
    any aztea_* legacy reference. Legacy names should appear only in a
    backward-compat NOTE near the end."""
    server = _MODULE.MCPStdioServer(bridge=_DummyBridge(), refresh_seconds=60)
    instructions = server._initialize_result()["instructions"]
    do_pos = instructions.find("do_specialist_task")
    legacy_pos = instructions.find("aztea_do")
    assert do_pos != -1, "do_specialist_task must appear in instructions"
    assert legacy_pos == -1 or do_pos < legacy_pos, (
        "verb-first name must appear before any legacy name reference"
    )


def test_server_version_was_bumped_for_cache_invalidation():
    """v0.3.0 minimum — clients comparing versions must see a change so
    they re-pull the renamed tool list. Pinning protects against an
    accidental version revert (0.2.0 covered the lazy-four rename;
    0.3.0 covers extending the rename to manage_job/budget/workflow)."""
    parts = _MODULE._SERVER_VERSION.split(".")
    assert len(parts) >= 2
    major, minor = int(parts[0]), int(parts[1])
    assert (major, minor) >= (0, 3), (
        f"server version {_MODULE._SERVER_VERSION!r} is below 0.3.0"
    )


def test_lazy_tool_alias_map_is_exhaustive_and_consistent():
    """Every legacy lazy tool has an alias entry, and every alias target is
    the canonical name on its corresponding _LAZY_*_TOOL dict (for the four
    lazy tools) or a grouped-dispatcher name (for manage_job/budget/workflow).
    No orphans."""
    aliases = _MODULE._LAZY_TOOL_NAME_ALIASES
    assert set(aliases.keys()) == {
        "aztea_do", "aztea_search", "aztea_describe", "aztea_call",
        "aztea_job", "aztea_budget", "aztea_workflow",
    }
    lazy_canonical_names = {
        _MODULE._LAZY_DO_TOOL["name"],
        _MODULE._LAZY_SEARCH_TOOL["name"],
        _MODULE._LAZY_DESCRIBE_TOOL["name"],
        _MODULE._LAZY_CALL_TOOL["name"],
    }
    grouped_canonical_names = {"manage_job", "manage_budget", "manage_workflow"}
    assert set(aliases.values()) == lazy_canonical_names | grouped_canonical_names


def test_do_specialist_task_description_disclaims_brand_keyword_dependency():
    """The description must explicitly say the user does NOT need to say
    'Aztea'. Regression guard against re-introducing brand-keyword
    dependency in a future edit."""
    desc = _MODULE._LAZY_DO_TOOL["description"]
    # The disclaimer can read "do NOT need" or "does NOT need" depending on
    # subject conjugation. Both are acceptable; the load-bearing tokens are
    # the negation + the word "brand".
    assert "NOT need" in desc and "brand" in desc.lower()
    # All four category labels also appear in the tool description, not
    # just in the server-level instructions.
    for category in ("EXECUTION", "LIVE DATA", "INDEPENDENT VERDICT", "MULTI-STEP WORKFLOW"):
        assert category in desc, f"missing category in do_specialist_task: {category}"


def test_aztea_nudge_hook_outputs_valid_json_with_routing_rule():
    """Layer 3 contract: the personal hook must emit valid JSON containing
    the routing rule. CI machines without ~/.claude/ skip silently."""
    import json
    import subprocess
    from pathlib import Path
    hook = Path.home() / ".claude" / "hooks" / "aztea_nudge.sh"
    if not hook.is_file():
        pytest.skip("aztea_nudge.sh not installed (~/.claude/hooks/) — Layer 3 hook not present in this environment")
    proc = subprocess.run([str(hook)], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, f"hook exit code {proc.returncode}, stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    for category in ("EXECUTION", "LIVE DATA", "INDEPENDENT VERDICT", "MULTI-STEP WORKFLOW"):
        assert category in ctx, f"hook context missing category: {category}"
    assert "do_specialist_task" in ctx
