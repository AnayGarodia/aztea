"""Plan B Phase 2 (2026-05-27) — schema validation guardrail + Claude pin
for auto_call_agent.

Covers:
  1. _validate_payload_against_schema accepts valid payloads, rejects
     wrong-type / missing fields, and is a no-op when schema is absent.
  2. The decide() path returns reason='schema_validation_failed' (not
     auto_invoked=True) when the extracted payload is malformed.
  3. The single-field extraction LLM chain is pinned with Claude first.
"""

from __future__ import annotations

from unittest.mock import patch

from core.registry import auto_hire


# ---------------------------------------------------------------------------
# _validate_payload_against_schema unit tests
# ---------------------------------------------------------------------------


def test_validate_accepts_valid_payload():
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    ok, errors = auto_hire._validate_payload_against_schema({"text": "hello"}, schema)
    assert ok is True
    assert errors == []


def test_validate_rejects_wrong_type():
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    ok, errors = auto_hire._validate_payload_against_schema({"text": 42}, schema)
    assert ok is False
    assert len(errors) == 1
    assert "text" in errors[0]


def test_validate_rejects_missing_required():
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["text", "count"],
    }
    ok, errors = auto_hire._validate_payload_against_schema({"text": "hi"}, schema)
    assert ok is False
    assert any("count" in e for e in errors)


def test_validate_returns_ok_when_schema_absent():
    """No schema = no contract to enforce; the agent validates server-side."""
    ok, errors = auto_hire._validate_payload_against_schema({"foo": "bar"}, None)
    assert ok is True
    assert errors == []
    ok2, errors2 = auto_hire._validate_payload_against_schema({"foo": "bar"}, {})
    assert ok2 is True
    assert errors2 == []


def test_validate_caps_errors_at_five():
    """Audit + response bodies stay bounded under pathological multi-error payloads."""
    schema = {
        "type": "object",
        "properties": {
            f"f{i}": {"type": "string"} for i in range(10)
        },
        "required": [f"f{i}" for i in range(10)],
    }
    payload = {f"f{i}": 1 for i in range(10)}  # wrong type on every required field
    ok, errors = auto_hire._validate_payload_against_schema(payload, schema)
    assert ok is False
    assert len(errors) == 5  # capped


def test_validate_handles_broken_schema_gracefully():
    """A broken declared schema shouldn't block the call — server-side validation still runs."""
    broken_schema = {"type": "not_a_real_type"}
    ok, errors = auto_hire._validate_payload_against_schema({"foo": "bar"}, broken_schema)
    # Fail-open: bad schema means we can't validate, so we don't block.
    assert ok is True
    assert errors == []


# ---------------------------------------------------------------------------
# decide() integration — schema_validation_failed reason
# ---------------------------------------------------------------------------


def _candidate(slug: str, schema: dict, *, price: float = 0.02) -> auto_hire.CandidateAgent:
    """Build a minimal candidate for tests. Uses the public dataclass."""
    return auto_hire.CandidateAgent(
        agent_id=f"id-{slug}",
        slug=slug,
        name=slug.replace("-", " ").title(),
        description="Counts words.",
        tags=["text", "counter"],
        category="text",
        price_per_call_usd=price,
        trust_score=80.0,
        success_rate=0.95,
        stability_tier="general_availability",
        input_schema=schema,
        raw={
            "agent_id": f"id-{slug}",
            "name": slug.replace("-", " ").title(),
            "description": "Counts words.",
            "input_schema": schema,
            "tags": ["text", "counter"],
            "review_status": "approved",
        },
    )


def test_attempt_auto_invoke_returns_schema_validation_failed_for_bad_extraction():
    """When the extracted payload violates the schema, refuse explicitly.

    Tests _attempt_auto_invoke directly to bypass the ranking/confidence gates
    (which depend on signals the unit test can't realistically populate).
    """
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    candidate = _candidate("word-counter", schema)
    # Ranked is what _attempt_auto_invoke takes. Build one manually.
    top = auto_hire.Ranked(candidate=candidate, score=100.0, reasons=["test"])

    with patch.object(
        auto_hire, "_resolve_payload",
        return_value=({"text": 42}, []),
    ), patch.object(
        auto_hire, "_check_confidence_gate",
        return_value=(0.95, None),  # high confidence, not blocked
    ):
        decision = auto_hire._attempt_auto_invoke(
            top=top,
            ranked=[top],
            intent_text="count the words",
            explicit_input=None,
            max_cost_usd=1.0,
            aggressive=False,
        )
    assert decision.auto_invoked is False
    assert decision.reason == "schema_validation_failed"
    assert decision.missing_fields  # contains the error messages


def test_attempt_auto_invoke_proceeds_when_payload_matches_schema():
    """The happy path still works after the guardrail addition."""
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    candidate = _candidate("word-counter", schema)
    top = auto_hire.Ranked(candidate=candidate, score=100.0, reasons=["test"])

    with patch.object(
        auto_hire, "_resolve_payload",
        return_value=({"text": "hello world"}, []),
    ), patch.object(
        auto_hire, "_check_confidence_gate",
        return_value=(0.95, None),
    ):
        decision = auto_hire._attempt_auto_invoke(
            top=top,
            ranked=[top],
            intent_text="count the words in 'hello world'",
            explicit_input=None,
            max_cost_usd=1.0,
            aggressive=False,
        )
    assert decision.auto_invoked is True
    assert decision.payload == {"text": "hello world"}


# ---------------------------------------------------------------------------
# Model chain pin
# ---------------------------------------------------------------------------


def test_extraction_model_chain_pins_claude_first():
    """Single-field extraction must try Claude Haiku before falling back."""
    chain = auto_hire._AUTO_HIRE_EXTRACTION_MODEL_SINGLE
    assert chain, "extraction chain must be non-empty"
    assert chain[0].startswith("anthropic:claude-haiku"), (
        f"Expected Claude Haiku first; got {chain[0]!r}. Set "
        f"AZTEA_AUTO_HIRE_EXTRACTION_MODEL_SINGLE to override."
    )


def test_multi_field_extraction_model_chain_pins_sonnet_first():
    """Multi-field extraction needs more reasoning; Sonnet is the right tier."""
    chain = auto_hire._AUTO_HIRE_EXTRACTION_MODEL_MULTI
    assert chain
    assert chain[0].startswith("anthropic:claude-sonnet"), (
        f"Expected Claude Sonnet first; got {chain[0]!r}."
    )


def test_extraction_uses_json_mode_and_pinned_chain():
    """The LLM extractor must pass json_mode=True AND the pinned chain to run_with_fallback."""
    captured = {}

    def fake_run(req, *, model_chain=None, **kwargs):
        captured["json_mode"] = req.json_mode
        captured["model_chain"] = model_chain
        from types import SimpleNamespace
        return SimpleNamespace(text='{"value": "elasticsearch"}')

    # The extractor imports run_with_fallback INSIDE the function, so we
    # patch it on the core.llm package (the same name the inline import resolves).
    import core.llm
    with patch.object(core.llm, "run_with_fallback", fake_run):
        result = auto_hire._llm_extract_field(
            intent="check vulns in elasticsearch 7.0",
            field_name="package_name",
            field_spec={"type": "string", "description": "The package to audit."},
        )
    assert result == "elasticsearch"
    assert captured["json_mode"] is True
    assert captured["model_chain"] == auto_hire._AUTO_HIRE_EXTRACTION_MODEL_SINGLE
