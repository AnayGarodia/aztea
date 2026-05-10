"""Output renderer matrix — every supported format × representative shapes.

# OWNS: dense parametrize coverage of render(format, output) over the full
#       SUPPORTED_FORMATS × representative-shapes grid, including negative
#       shapes (None, scalars, empty containers).
# DECISIONS: complements tests/property/test_output_invariants.py — that file
#       fuzzes with Hypothesis; this file enforces a deterministic matrix so
#       failures cite a specific (format, shape) pair.
"""
from __future__ import annotations

import pytest

from core.output_formats import SUPPORTED_FORMATS, render

pytestmark = pytest.mark.surface


_NEGATIVE_SHAPES = [
    pytest.param(None, id="none"),
    pytest.param("", id="empty_string"),
    pytest.param("hello", id="plain_string"),
    pytest.param(0, id="zero_int"),
    pytest.param(False, id="false_bool"),
    pytest.param([], id="empty_list"),
    pytest.param({}, id="empty_dict"),
    pytest.param([1, 2, 3], id="int_list"),
    pytest.param({"a": 1}, id="trivial_dict"),
    pytest.param({"output": "nested"}, id="single_key_dict"),
]


# Renderer × shape combos that crash today. Tracked as xfail so they remain
# visible until the renderer is fixed (see also property suite findings).
_RENDERER_BUGS: set[tuple[str, str]] = {
    # secret-scan blocks helper missing — covered in property suite.
}


@pytest.mark.parametrize("shape", _NEGATIVE_SHAPES)
@pytest.mark.parametrize("fmt", list(SUPPORTED_FORMATS))
def test_render_negative_shape_no_raise(shape, fmt, request):
    """Renderer must accept any JSON-shaped input across every format."""
    shape_id = request.node.callspec.id.split("-")[-1]
    if (fmt, shape_id) in _RENDERER_BUGS:
        pytest.xfail(f"renderer bug for ({fmt}, {shape_id})")
    out = render(shape, format=fmt)
    if fmt == "json":
        assert out == shape
    elif fmt == "slack_blocks":
        assert isinstance(out, dict)
    else:
        assert isinstance(out, str)


@pytest.mark.parametrize("fmt", list(SUPPORTED_FORMATS))
def test_render_handles_dict_with_unicode(fmt):
    """Unicode payloads must round-trip without crashing the renderer."""
    payload = {"summary": "café — résumé ✓", "score": 7}
    out = render(payload, format=fmt)
    assert out is not None


@pytest.mark.parametrize("fmt", list(SUPPORTED_FORMATS))
def test_render_handles_deeply_nested(fmt):
    """The renderer must not infinite-recurse on deeply nested input."""
    nested = "leaf"
    for _ in range(20):
        nested = {"x": nested}
    out = render(nested, format=fmt)
    assert out is not None


@pytest.mark.parametrize("fmt", list(SUPPORTED_FORMATS))
def test_render_returns_correct_top_level_type(fmt):
    """Contract: 'json' returns the input verbatim (any type); 'slack_blocks'
    returns a dict; everything else returns a str."""
    payload = {"key": "value"}
    out = render(payload, format=fmt)
    if fmt == "json":
        assert out == payload
    elif fmt == "slack_blocks":
        assert isinstance(out, dict)
    else:
        assert isinstance(out, str)


def test_render_json_alias_unknown_falls_through():
    """An unknown format string falls back to 'json' (returns input verbatim)."""
    payload = {"foo": "bar"}
    assert render(payload, format="this-is-not-real") == payload


def test_render_handles_meta_kwarg():
    """agent_meta is optional; passing a dict must not change return-type contract."""
    payload = {"summary": "ok"}
    out = render(payload, format="markdown", agent_meta={"name": "x", "trust_score": 80})
    assert isinstance(out, str)
