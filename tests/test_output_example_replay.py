"""Plan B Phase 3a (2026-05-27) — output-example replay at probe time.

Sellers declare ``output_examples: [{"input": ..., "output": ...}]`` at
registration. The replay verifier POSTs each declared input to the
endpoint and WARN's if the actual response shape doesn't match the
declared output.

The matcher is tolerant: agents are non-deterministic, so we check
top-level key presence + JSON-type compatibility, not byte equality.
"""

from __future__ import annotations

from core import listing_safety


def test_honest_seller_passes_replay():
    """When the actual response matches the declared shape, no findings fire."""
    findings = listing_safety.evaluate_output_example_replay(
        example_input={"text": "hello world"},
        declared_output={"word_count": 2, "summary": "two words"},
        actual_output={"word_count": 7, "summary": "different but same shape"},
    )
    assert findings == []


def test_lying_seller_caught_missing_key():
    """Declared output had a key the actual response doesn't include."""
    findings = listing_safety.evaluate_output_example_replay(
        example_input={"text": "hi"},
        declared_output={"word_count": 1, "summary": "one"},
        actual_output={"word_count": 1},  # missing 'summary'
    )
    assert len(findings) == 1
    assert findings[0].level == listing_safety.LEVEL_WARN
    assert findings[0].code == "probe.output_example_replay_mismatch"
    detail = findings[0].detail
    assert "summary" in detail["missing_keys"]


def test_lying_seller_caught_type_mismatch():
    """Declared output had a key whose actual type differs."""
    findings = listing_safety.evaluate_output_example_replay(
        example_input={"text": "hi"},
        declared_output={"word_count": 1},
        actual_output={"word_count": "one"},  # declared int, got str
    )
    assert len(findings) == 1
    detail = findings[0].detail
    assert detail["type_mismatches"][0]["key"] == "word_count"
    assert detail["type_mismatches"][0]["declared_type"] == "number"
    assert detail["type_mismatches"][0]["actual_type"] == "string"


def test_non_dict_actual_response_caught():
    """The seller's endpoint returned a string instead of an object."""
    findings = listing_safety.evaluate_output_example_replay(
        example_input={"text": "hi"},
        declared_output={"word_count": 1},
        actual_output="just a string, no JSON object",
    )
    assert len(findings) == 1
    assert findings[0].code == "probe.output_example_shape_mismatch"
    assert findings[0].detail["actual_type"] == "str"


def test_no_examples_declared_is_a_noop():
    """Sellers without declared examples can't be caught lying — nothing to compare."""
    findings = listing_safety.evaluate_output_example_replay(
        example_input={"text": "hi"},
        declared_output=None,
        actual_output={"foo": "bar"},
    )
    assert findings == []


def test_tolerant_to_non_determinism():
    """Same keys, same types, different values = no warning. Agents are stochastic."""
    findings = listing_safety.evaluate_output_example_replay(
        example_input={"prompt": "tell me a joke"},
        declared_output={
            "joke": "Why did the chicken cross the road?",
            "rating": 7.5,
            "tags": ["pun", "classic"],
        },
        actual_output={
            "joke": "I'm reading a book on anti-gravity. It's impossible to put down.",
            "rating": 9.0,
            "tags": ["pun", "physics"],
        },
    )
    assert findings == []


def test_bool_int_distinction_respected():
    """Python's bool-as-int subclass shouldn't fool the type matcher."""
    # declared True, actual 1 should mismatch (bool vs number)
    findings = listing_safety.evaluate_output_example_replay(
        example_input={},
        declared_output={"flag": True},
        actual_output={"flag": 1},
    )
    assert len(findings) == 1
    type_mismatches = findings[0].detail["type_mismatches"]
    assert any(m["key"] == "flag" for m in type_mismatches)


def test_nested_structure_only_top_level_checked():
    """The matcher is intentionally shallow — nested keys aren't enforced."""
    findings = listing_safety.evaluate_output_example_replay(
        example_input={},
        declared_output={"data": {"foo": "bar"}},
        actual_output={"data": {"completely": "different"}},
    )
    assert findings == []  # both top-level 'data' are dicts; shallow match OK
