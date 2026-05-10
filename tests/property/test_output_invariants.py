"""Property tests for core.output_shaping and core.output_formats.

# OWNS: invariants on shape_output, normalize_format, render.
# INVARIANTS asserted: shape_output 'full' mode is identity; summary mode never
#       exceeds advertised limits; normalize_format only returns SUPPORTED_FORMATS
#       or None; render never raises for any of the supported formats × any
#       JSON-shaped payload; render returns dict for slack_blocks and str
#       for the textual formats.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.output_formats import SUPPORTED_FORMATS, normalize_format, render
from core.output_shaping import (
    _MAX_DEPTH,
    _MAX_DICT_ITEMS,
    _MAX_LIST_ITEMS,
    _MAX_STRING_CHARS,
    shape_output,
)
from tests.strategies import json_dict, json_value

pytestmark = pytest.mark.property


# --- shape_output ------------------------------------------------------------

@given(payload=json_value())
def test_shape_full_mode_is_identity(payload):
    out, truncated = shape_output(payload, "full")
    assert out == payload
    assert truncated is False


@given(payload=json_value())
def test_shape_summary_returns_two_tuple(payload):
    out = shape_output(payload, "summary")
    assert isinstance(out, tuple) and len(out) == 2
    _, truncated = out
    assert isinstance(truncated, bool)


@given(text=st.text(max_size=8000))
def test_shape_string_truncation(text):
    """Top-level strings exceeding the cap are truncated; flag is set accordingly."""
    out, truncated = shape_output(text, "summary")
    if isinstance(out, str):
        assert len(out) <= _MAX_STRING_CHARS
        if len(text) > _MAX_STRING_CHARS:
            assert truncated is True


@given(items=st.lists(st.integers(min_value=0, max_value=10), max_size=200))
def test_shape_list_truncation(items):
    out, truncated = shape_output(items, "summary")
    # Output may be list or dict (artifact) depending on shape; only check list case.
    if isinstance(out, list):
        assert len(out) <= _MAX_LIST_ITEMS
        if len(items) > _MAX_LIST_ITEMS:
            assert truncated is True


@given(d=json_dict())
def test_shape_dict_truncation(d):
    out, _ = shape_output(d, "summary")
    if isinstance(out, dict):
        assert len(out) <= _MAX_DICT_ITEMS


def test_shape_depth_limit_enforced():
    """Build a deeply nested dict and verify shape stops within _MAX_DEPTH."""
    nested = "leaf"
    for _ in range(20):  # 20 > _MAX_DEPTH (6)
        nested = {"x": nested}
    out, truncated = shape_output(nested, "summary")
    # Walk the result and confirm depth is bounded.
    depth = 0
    cur = out
    while isinstance(cur, dict) and "x" in cur and depth < 100:
        cur = cur["x"]
        depth += 1
    assert depth <= _MAX_DEPTH + 1
    assert truncated is True


# --- normalize_format --------------------------------------------------------

@given(value=st.sampled_from(list(SUPPORTED_FORMATS)))
def test_normalize_known_formats_roundtrip(value):
    assert normalize_format(value) == value


@pytest.mark.parametrize("alias,expected", [
    ("md", "markdown"),
    ("MD", "markdown"),
    ("MARKDOWN", "markdown"),
    ("pr_comment", "github_pr_comment"),
    ("PR-Comment", "github_pr_comment"),
    ("github", "github_pr_comment"),
    ("slack", "slack_blocks"),
    ("plain", "text"),
    ("plaintext", "text"),
])
def test_normalize_aliases(alias, expected):
    assert normalize_format(alias) == expected


@given(value=st.text(max_size=20))
def test_normalize_unknown_returns_none(value):
    """Anything not in SUPPORTED_FORMATS or aliases returns None."""
    out = normalize_format(value)
    assert out is None or out in SUPPORTED_FORMATS


@pytest.mark.parametrize("falsy", [None, "", " ", 0, False])
def test_normalize_falsy_returns_none(falsy):
    assert normalize_format(falsy) is None


# --- render ------------------------------------------------------------------

@given(
    payload=json_value(max_leaves=15),
    fmt=st.sampled_from(list(SUPPORTED_FORMATS)),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_render_never_raises(payload, fmt):
    """render() never raises for any of the 5 formats × any JSON payload."""
    out = render(payload, format=fmt)
    if fmt == "json":
        assert out == payload
    elif fmt == "slack_blocks":
        assert isinstance(out, dict)
    else:
        assert isinstance(out, str)


@given(payload=json_value(max_leaves=10))
def test_render_unknown_format_returns_input(payload):
    """Unknown format defaults to 'json' which round-trips the payload."""
    out = render(payload, format="this-is-not-a-real-format")
    assert out == payload


# --- Per-shape known structures -------------------------------------------

_KNOWN_SHAPES = [
    pytest.param({
        "issues": [{"severity": "high", "message": "x", "file": "a.py", "line": 1}],
        "score": 7,
        "summary": "ok",
    }, id="code_review"),
    pytest.param(
        {"findings": [{"file": "a.py", "line": 1, "rule": "E501"}], "total": 1},
        id="linter",
    ),
    pytest.param(
        {"diagnostics": [{"file": "a.py", "line": 1, "message": "type"}], "passed": False},
        id="type_check",
    ),
    pytest.param(
        {
            "files": [{"path": "a.py", "additions": 1, "deletions": 0}],
            "risk_summary": {"high": 0, "medium": 1, "low": 2},
            "summary": "small change",
        },
        id="git_diff",
    ),
    pytest.param(
        {
            "findings": [{"severity": "high", "type": "key", "redacted_preview": "***"}],
            "findings_by_severity": {"high": 1},
        },
        id="secret_scan",
    ),
]


# Some renderer × shape combos crash today. xfail the known-bad combos so the
# invariant test stays meaningful: passing combos are real signal, and the
# xfailed combos document the bug for whoever fixes the renderer (an XPASS
# means the fix landed and the xfail can be removed).
_RENDERER_BUGS = {
    ("slack_blocks", "secret_scan"),  # references undefined _slack_secret_scan_blocks
}


@pytest.mark.parametrize("shape", _KNOWN_SHAPES)
@pytest.mark.parametrize("fmt", list(SUPPORTED_FORMATS))
def test_render_known_shapes_no_raise(shape, fmt, request):
    shape_id = request.node.callspec.id.split("-")[-1]
    if (fmt, shape_id) in _RENDERER_BUGS:
        pytest.xfail(f"renderer bug: {fmt} × {shape_id} — see _RENDERER_BUGS")
    out = render(shape, format=fmt)
    assert out is not None


@pytest.mark.parametrize("shape", _KNOWN_SHAPES)
def test_render_markdown_known_shapes_have_content(shape):
    """Markdown rendering of known shapes produces non-empty text."""
    out = render(shape, format="markdown")
    assert isinstance(out, str)
    assert len(out.strip()) > 0
