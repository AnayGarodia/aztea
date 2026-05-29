# SPDX-License-Identifier: Apache-2.0
"""Golden-file tests for core.publish_inference.

# OWNS: behavioral lock on the publish-inference engine. Any change to
#       inference rules (category keyword list, default price, schema
#       mapping for an annotation, etc.) MUST be paired with an explicit
#       regeneration of the goldens — otherwise this test breaks loudly.
# INVARIANTS:
#   - infer() is a pure function (same input ⇒ same output).
#   - JSON-encoded spec for every fixture matches its checked-in golden.
#   - infer() NEVER raises — even on malformed source.

To regenerate goldens after an intentional rule change:

    AZTEA_REGOLD=1 .venv/bin/python -m pytest tests/test_publish_inference.py

Then inspect the diff in `tests/fixtures/publish_inference/*.golden.json` and
commit. The CI run with no env var must match the on-disk golden.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.publish_inference import infer


_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "publish_inference"

# Per-fixture inference inputs. The handler source is read from `<id>.py`;
# `hint` and `filename` are the extra kwargs the caller passes.
_FIXTURE_INPUTS: dict[str, dict[str, object]] = {
    "01_minimal_untyped": {"filename": "01_minimal_untyped.py"},
    "02_pydantic_input": {"filename": "stripe_webhook_validator.py"},
    "03_typeddict_input": {"filename": "url_fetcher.py"},
    "04_module_docstring_only": {"filename": "pdf_parser.py"},
    "05_function_docstring_only": {"filename": "dockerfile_linter.py"},
    "06_optional_union_literal": {"filename": "dependency_auditor.py"},
    "07_nested_generics": {"filename": "record_grouper.py"},
    "08_class_based_handler": {"filename": "08_class_based_handler.py"},
    "09_multiple_public_functions": {"filename": "code_inspector.py"},
    "10_with_hint": {
        "filename": "10_with_hint.py",
        "hint": "Scrape a webpage and return its main-content text for security review.",
    },
}


def _golden_path(fixture_id: str) -> Path:
    return _FIXTURES / f"{fixture_id}.golden.json"


def _read_source(fixture_id: str) -> str:
    return (_FIXTURES / f"{fixture_id}.py").read_text()


@pytest.mark.parametrize("fixture_id", sorted(_FIXTURE_INPUTS.keys()))
def test_inference_matches_golden(fixture_id: str):
    """Each fixture must produce exactly the bytes stored in `<id>.golden.json`.

    A failure here means EITHER the inference engine changed behavior (in
    which case regenerate the golden with AZTEA_REGOLD=1 and commit the
    diff) OR you broke an invariant by accident (in which case fix the
    code, not the golden).
    """
    source = _read_source(fixture_id)
    kwargs = _FIXTURE_INPUTS[fixture_id]
    spec = infer(source, **kwargs)  # type: ignore[arg-type]
    actual = json.dumps(spec.to_jsonable(), indent=2, sort_keys=True) + "\n"
    golden = _golden_path(fixture_id)
    if os.environ.get("AZTEA_REGOLD") == "1":
        golden.write_text(actual)
        return
    if not golden.exists():
        pytest.fail(
            f"Missing golden for fixture {fixture_id!r}. Regenerate with: "
            f"AZTEA_REGOLD=1 pytest tests/test_publish_inference.py"
        )
    expected = golden.read_text()
    assert actual == expected, (
        f"Inference output for {fixture_id!r} drifted from the golden file.\n"
        "If this change is intentional, regenerate with: "
        "AZTEA_REGOLD=1 pytest tests/test_publish_inference.py"
    )


# ─── Invariant tests ────────────────────────────────────────────────────────


def test_infer_never_raises_on_malformed_source():
    """The publish flow's multi-turn contract depends on this — infer must
    always return a spec so the caller can surface `missing` to the user."""
    for source in (
        "not python at all",
        "def half_a_function(",
        "",
        "    ",
        None,  # type: ignore[arg-type] — defensive: caller may pass None
    ):
        spec = infer(source)  # type: ignore[arg-type]
        assert spec.name  # always populated
        assert spec.slug  # always populated
        assert "name" in spec.missing or spec.name != "Untitled Agent"


def test_infer_is_deterministic():
    """Same input twice ⇒ identical output (down to dict iteration order)."""
    src = (_FIXTURES / "06_optional_union_literal.py").read_text()
    a = infer(src, filename="x.py")
    b = infer(src, filename="x.py")
    assert json.dumps(a.to_jsonable(), sort_keys=True) == json.dumps(
        b.to_jsonable(), sort_keys=True
    )


def test_default_price_matches_cli_default():
    """The CLI default is $0.05 at sdks/python-sdk/aztea/cli/publish.py:541.
    The inference engine must agree, otherwise wizard mode would surprise
    long-time publishers when their previous flag-driven default vanishes."""
    from core.publish_inference import DEFAULT_PRICE_USD
    assert DEFAULT_PRICE_USD == 0.05


def test_security_category_inferred_from_keywords():
    """Cross-check that the keyword-based category bumper actually moves a
    spec out of the 'developer-tools' default when security signals show up."""
    src = "def handler(payload):\n    return {}\n"
    spec = infer(src, hint="A CVE scanner for npm packages")
    assert spec.category == "security"


def test_pydantic_input_produces_explicit_properties():
    """Pydantic-modelled inputs are the highest-fidelity inference path —
    the schema must have every field of the BaseModel, with the right type."""
    src = (_FIXTURES / "02_pydantic_input.py").read_text()
    spec = infer(src, filename="stripe_webhook_validator.py")
    assert spec.input_schema["type"] == "object"
    props = spec.input_schema["properties"]
    assert "signature" in props and props["signature"]["type"] == "string"
    assert "tolerance_seconds" in props and props["tolerance_seconds"]["type"] == "integer"
    # tolerance_seconds has a default ⇒ NOT required.
    assert "tolerance_seconds" not in spec.input_schema.get("required", [])
    assert "signature" in spec.input_schema["required"]
