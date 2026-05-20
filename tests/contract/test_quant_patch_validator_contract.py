"""Spec ↔ agent ↔ runbook contract enforcement.

# OWNS: invariants tying the agent code, the spec entry in
#        `server/builtin_agents/specs_part2.py`, and the runbook in
#        `docs/runbooks/quant-patch-validator.md` together. Drift in
#        any of the three trips this suite.
# NOT OWNS: agent behaviour itself (covered elsewhere).
"""

from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest

from agents.quant_patch_validator import run as validator_run
from server.builtin_agents.constants import QUANT_PATCH_VALIDATOR_AGENT_ID
from server.builtin_agents.specs import builtin_agent_specs


_RUNBOOK_PATH = Path(__file__).resolve().parents[2] / "docs" / "runbooks" / "quant-patch-validator.md"


@pytest.fixture(scope="module")
def spec() -> dict:
    out = next(
        (s for s in builtin_agent_specs() if s["agent_id"] == QUANT_PATCH_VALIDATOR_AGENT_ID),
        None,
    )
    assert out is not None, "quant_patch_validator spec missing from registry"
    return out


def test_spec_input_schema_lists_required_fields(spec):
    schema = spec["input_schema"]
    required = set(schema.get("required", []))
    assert required == {"reference_code", "candidate_code"}, required


def test_spec_input_schema_well_formed_json_schema(spec):
    # Validates the schema itself against the JSON Schema meta-schema.
    jsonschema.Draft7Validator.check_schema(spec["input_schema"])
    jsonschema.Draft7Validator.check_schema(spec["output_schema"])


def test_spec_input_validation_rejects_missing_required():
    """Removing any required field in the call payload must trigger
    `missing_*_code` error from the agent."""
    out = validator_run({"candidate_code": "def f(x): return x"})
    assert out["error"]["code"] == "quant_patch_validator.missing_reference_code"
    out = validator_run({"reference_code": "def f(x): return x"})
    assert out["error"]["code"] == "quant_patch_validator.missing_candidate_code"


def test_agent_output_validates_against_output_schema(spec):
    """Agent's real output must validate against the documented schema."""
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    # Strip error envelopes — schema validates the success path.
    assert "error" not in out
    jsonschema.validate(out, spec["output_schema"])


def test_every_output_example_validates(spec):
    """Each `output_examples[i].output` matches `output_schema`."""
    examples = spec.get("output_examples") or []
    assert examples, "spec must declare at least one output example"
    for idx, ex in enumerate(examples):
        try:
            jsonschema.validate(ex["output"], spec["output_schema"])
        except jsonschema.ValidationError as exc:
            pytest.fail(f"output_examples[{idx}] failed schema validation: {exc.message}")


def test_spec_price_matches_runbook_text(spec):
    body = _RUNBOOK_PATH.read_text(encoding="utf-8")
    # The runbook documents the v1 price line: "$1.50 (flat)"
    assert f"${spec['price_per_call_usd']:.2f}" in body, "runbook price drifted from spec"


def test_spec_agent_id_matches_runbook(spec):
    body = _RUNBOOK_PATH.read_text(encoding="utf-8")
    assert spec["agent_id"] in body, "runbook agent_id drifted from spec"


def test_spec_endpoint_is_internal(spec):
    assert spec["endpoint_url"].startswith("internal://")


def test_spec_examples_sensitive_is_true(spec):
    assert spec.get("examples_sensitive") is True, (
        "examples_sensitive must remain True for v1 — see CLAUDE.md privacy invariant"
    )


def test_spec_category_is_code_quality(spec):
    assert spec["category"] == "Code Quality"


def test_spec_tags_include_quant_and_ai_validation(spec):
    tags = set(spec.get("tags") or [])
    assert "quant" in tags
    assert "ai-validation" in tags
