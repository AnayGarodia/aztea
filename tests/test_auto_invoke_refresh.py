"""Tests for the auto-invoke refresh PR.

Covers the components that don't need a live FastAPI client:
    - Embedding scorer fail-safes (disabled flag, missing backend, empty inputs)
    - Routing metrics helper never raises
    - Gateway free-tier specs ship at $0.00
    - `aztea mcp install` writes + removes CLAUDE.md / PostToolUse hook
      idempotently

Live-server behavior (compact dry_run shape, decision counters firing) is
covered by tests/integration/test_auto_hire.py.
"""

from __future__ import annotations

import json
from pathlib import Path


# ── Embedding scorer fail-safes ────────────────────────────────────────────


def _stub_candidate(description: str = "Find leaked credentials in code"):
    """Build a CandidateAgent dataclass with the fields the scorer reads."""
    from core.registry import auto_hire as ah
    return ah.CandidateAgent(
        agent_id="agt-stub",
        slug="secret_scanner",
        name="Secret Scanner",
        description=description,
        tags=["security"],
        category="Security",
        price_per_call_usd=0.0,
        trust_score=80.0,
        success_rate=0.9,
        stability_tier="stable",
        input_schema={},
        raw={},
    )


def test_semantic_scorer_returns_zero_when_flag_disabled(monkeypatch):
    """AZTEA_AUTO_INVOKE_EMBEDDINGS=0 disables the term entirely."""
    monkeypatch.setenv("AZTEA_AUTO_INVOKE_EMBEDDINGS", "0")
    from core.registry import auto_hire as ah
    delta, why = ah._score_semantic_similarity(_stub_candidate(), "find leaked secrets")
    assert delta == 0.0
    assert why == []


def test_semantic_scorer_returns_zero_when_intent_empty(monkeypatch):
    monkeypatch.setenv("AZTEA_AUTO_INVOKE_EMBEDDINGS", "1")
    from core.registry import auto_hire as ah
    delta, why = ah._score_semantic_similarity(_stub_candidate(), "")
    assert delta == 0.0
    assert why == []


def test_semantic_scorer_returns_zero_when_description_empty(monkeypatch):
    monkeypatch.setenv("AZTEA_AUTO_INVOKE_EMBEDDINGS", "1")
    from core.registry import auto_hire as ah
    delta, why = ah._score_semantic_similarity(
        _stub_candidate(description=""), "find leaked secrets",
    )
    assert delta == 0.0
    assert why == []


def test_semantic_scorer_returns_zero_when_backend_unavailable(monkeypatch):
    """Backend missing (zero-vector or exception) → 0 contribution, never raises."""
    monkeypatch.setenv("AZTEA_AUTO_INVOKE_EMBEDDINGS", "1")
    from core.registry import auto_hire as ah
    monkeypatch.setattr(ah, "_embed_intent_cached", lambda _: None)
    delta, why = ah._score_semantic_similarity(_stub_candidate(), "find leaked secrets")
    assert delta == 0.0
    assert why == []


def test_semantic_scorer_adds_positive_bonus_on_strong_match(monkeypatch):
    """When the embedding backend returns aligned vectors, the bonus is positive."""
    monkeypatch.setenv("AZTEA_AUTO_INVOKE_EMBEDDINGS", "1")
    from core.registry import auto_hire as ah
    aligned = tuple([1.0] + [0.0] * 383)
    monkeypatch.setattr(ah, "_embed_intent_cached", lambda _: aligned)
    monkeypatch.setattr(ah, "_embed_agent_cached", lambda *_: aligned)
    delta, why = ah._score_semantic_similarity(_stub_candidate(), "find leaked secrets")
    # Aligned unit vectors → cosine 1.0 → exactly _SEMANTIC_BONUS_MAX points.
    assert delta == ah._SEMANTIC_BONUS_MAX
    assert why and why[0].startswith("semantic match")


# ── Routing metrics ────────────────────────────────────────────────────────


def test_record_route_decision_never_raises():
    """Defensive: bad inputs must not crash the route handler."""
    from core import observability
    observability.record_route_decision("auto_invoked", "ok", 0.012)
    observability.record_route_decision("gated", "low_confidence", 0.0)
    observability.record_route_decision("dry_run", "dry_run", 0.001)
    observability.record_route_decision(None, None, -1.0)  # type: ignore[arg-type]


# ── Gateway free-tier specs ────────────────────────────────────────────────


def test_gateway_free_tier_specs_ship_at_zero():
    """The set in constants.py must agree with each spec's price field."""
    from server.builtin_agents.constants import GATEWAY_FREE_TIER_AGENT_IDS
    from server.builtin_agents.specs import builtin_agent_specs
    by_id = {s.get("agent_id"): s for s in builtin_agent_specs()}
    for agent_id in GATEWAY_FREE_TIER_AGENT_IDS:
        spec = by_id.get(agent_id)
        assert spec is not None, f"Spec missing for gateway agent {agent_id}"
        assert spec.get("price_per_call_usd") == 0.0, (
            f"{spec.get('name')!r} is listed as a gateway free-tier agent but its "
            f"spec price is {spec.get('price_per_call_usd')!r}; the spec is the "
            f"source of truth, update it (and pricing_overlay.py if variable-priced)."
        )


def test_cve_lookup_pricing_overlay_zeroes_out():
    """CVE Lookup uses variable pricing — every tier must also be free."""
    from server.builtin_agents.constants import CVELOOKUP_AGENT_ID
    from server.builtin_agents.pricing_overlay import get_pricing_overlay
    overlay = get_pricing_overlay()
    entry = overlay.get(CVELOOKUP_AGENT_ID)
    assert entry is not None
    config = entry["pricing_config"]
    assert config["min_cents"] == 0
    for tier in config["tiers"]:
        assert tier["cents"] == 0, f"Gateway tier still charges: {tier!r}"


# ── Installer: reflex rule + PostToolUse hook ──────────────────────────────


def test_reflex_rule_write_is_idempotent(monkeypatch, tmp_path):
    md_path = tmp_path / "CLAUDE.md"
    from aztea.cli import mcp as mcp_cli
    monkeypatch.setattr(mcp_cli, "_CLAUDE_MD_PATH", md_path)

    assert mcp_cli._write_reflex_rule() is True
    first_contents = md_path.read_text(encoding="utf-8")
    assert mcp_cli._REFLEX_RULE_BEGIN in first_contents
    assert mcp_cli._REFLEX_RULE_END in first_contents

    # Second write is a no-op.
    assert mcp_cli._write_reflex_rule() is False
    assert md_path.read_text(encoding="utf-8") == first_contents


def test_reflex_rule_preserves_existing_content(monkeypatch, tmp_path):
    md_path = tmp_path / "CLAUDE.md"
    md_path.write_text("# My existing rules\n\nUse tabs.\n", encoding="utf-8")
    from aztea.cli import mcp as mcp_cli
    monkeypatch.setattr(mcp_cli, "_CLAUDE_MD_PATH", md_path)

    mcp_cli._write_reflex_rule()
    contents = md_path.read_text(encoding="utf-8")
    assert "# My existing rules" in contents
    assert "Use tabs." in contents
    assert mcp_cli._REFLEX_RULE_BEGIN in contents


def test_reflex_rule_remove_restores_prior_content(monkeypatch, tmp_path):
    md_path = tmp_path / "CLAUDE.md"
    original = "# My existing rules\n\nUse tabs.\n"
    md_path.write_text(original, encoding="utf-8")
    from aztea.cli import mcp as mcp_cli
    monkeypatch.setattr(mcp_cli, "_CLAUDE_MD_PATH", md_path)

    mcp_cli._write_reflex_rule()
    assert mcp_cli._REFLEX_RULE_BEGIN in md_path.read_text(encoding="utf-8")
    assert mcp_cli._remove_reflex_rule() is True
    cleaned = md_path.read_text(encoding="utf-8")
    assert mcp_cli._REFLEX_RULE_BEGIN not in cleaned
    assert "# My existing rules" in cleaned
    # Second remove is a no-op.
    assert mcp_cli._remove_reflex_rule() is False


def test_post_tool_hook_write_is_idempotent(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    from aztea.cli import mcp as mcp_cli
    monkeypatch.setattr(mcp_cli, "_CLAUDE_SETTINGS_PATH", settings_path)

    assert mcp_cli._write_post_tool_hook() is True
    first = settings_path.read_text(encoding="utf-8")
    assert mcp_cli._HOOK_MARKER in first

    assert mcp_cli._write_post_tool_hook() is False
    assert settings_path.read_text(encoding="utf-8") == first


def test_post_tool_hook_remove_preserves_other_settings(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"theme": "dark", "fontSize": 14}), encoding="utf-8",
    )
    from aztea.cli import mcp as mcp_cli
    monkeypatch.setattr(mcp_cli, "_CLAUDE_SETTINGS_PATH", settings_path)

    mcp_cli._write_post_tool_hook()
    assert mcp_cli._remove_post_tool_hook() is True
    final = json.loads(settings_path.read_text(encoding="utf-8"))
    assert final.get("theme") == "dark"
    assert final.get("fontSize") == 14
    assert "hooks" not in final or "PostToolUse" not in final.get("hooks", {})
    assert mcp_cli._remove_post_tool_hook() is False


def test_post_tool_hook_remove_keeps_other_user_hooks(monkeypatch, tmp_path):
    """User hooks under PostToolUse must survive when ours is removed."""
    settings_path = tmp_path / "settings.json"
    user_hook = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "echo bash-ran >&2"}],
    }
    settings_path.write_text(
        json.dumps({"hooks": {"PostToolUse": [user_hook]}}),
        encoding="utf-8",
    )
    from aztea.cli import mcp as mcp_cli
    monkeypatch.setattr(mcp_cli, "_CLAUDE_SETTINGS_PATH", settings_path)

    mcp_cli._write_post_tool_hook()
    mcp_cli._remove_post_tool_hook()
    final = json.loads(settings_path.read_text(encoding="utf-8"))
    assert final["hooks"]["PostToolUse"] == [user_hook]
