"""Hypothesis property tests for ``core.error_codes.make_error``.

# OWNS: structural invariants of the wire-level error envelope.
# INVARIANTS asserted:
# - Output is always ``{"error", "message", "details"}`` with no extra keys.
# - ``error`` is always a non-empty str — empty/whitespace input falls back to
#   ``"request.invalid_input"``.
# - ``message`` is always a non-empty str — empty/whitespace input falls back
#   to ``"Request failed."``.
# - ``details`` is passed through verbatim when supplied via either ``details``
#   or the ``data`` alias kwarg (whichever is non-None).
# - The result is JSON-serialisable for any input that is itself JSON-shaped.
#
# Why property tests:
# - SDK clients (Python + TypeScript) and the React frontend all branch on
#   ``error`` and read ``details``. A regression in the envelope shape breaks
#   every downstream consumer at once. Hypothesis-driven examples catch the
#   shape-shifts unit tests would miss (empty strings, nested dicts, unusual
#   whitespace, alias precedence).
"""

from __future__ import annotations

import json
import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.error_codes import make_error
from tests.strategies import json_value

pytestmark = pytest.mark.property


# Bound the message/error inputs to keep generation cheap. The envelope cares
# about shape, not content length — 1KB inputs would just slow runs.
_message_text = st.text(alphabet=string.printable, max_size=200)
# Error codes in the real taxonomy are dot-namespaced, but make_error treats
# the input as opaque — generate arbitrary text to stress the str-coercion and
# strip-then-fallback logic.
_error_text = st.text(alphabet=string.printable, max_size=80)


@given(error=_error_text, message=_message_text, details=json_value(max_leaves=10))
def test_envelope_always_has_three_keys(error: str, message: str, details) -> None:
    envelope = make_error(error, message, details)
    assert set(envelope.keys()) == {"error", "message", "details"}


@given(error=_error_text, message=_message_text)
def test_error_field_is_non_empty_string(error: str, message: str) -> None:
    envelope = make_error(error, message)
    assert isinstance(envelope["error"], str)
    assert envelope["error"].strip() != ""


@given(message=_message_text)
def test_empty_or_whitespace_error_falls_back_to_invalid_input(message: str) -> None:
    # Any input that strips to "" must coerce to the documented fallback —
    # otherwise the SDK's machine-readable branch goes blank and consumers
    # can't tell "missing code" from "actually empty code".
    for empty_like in ("", "   ", "\t", "\n  \t"):
        envelope = make_error(empty_like, message)
        assert envelope["error"] == "request.invalid_input", empty_like


@given(error=_error_text)
def test_empty_or_whitespace_message_falls_back_to_request_failed(error: str) -> None:
    for empty_like in ("", "   ", "\t\n"):
        envelope = make_error(error, empty_like)
        assert envelope["message"] == "Request failed.", empty_like


@given(error=_error_text, message=_message_text, details=json_value(max_leaves=10))
def test_details_passes_through_verbatim(error: str, message: str, details) -> None:
    envelope = make_error(error, message, details)
    # The pass-through must be identity — equality of object graph, not just
    # JSON-equivalence. Otherwise nested dicts lose ordering / typed values
    # like booleans get coerced.
    assert envelope["details"] is details or envelope["details"] == details


@given(error=_error_text, message=_message_text, data=json_value(max_leaves=10))
def test_data_alias_is_honoured_when_details_is_none(error: str, message: str, data) -> None:
    # The ``data`` kwarg is documented as an alias for callers that prefer
    # JSON-RPC's "data" convention. When both are None this should yield
    # details=None; when only data is set it should land in details.
    envelope = make_error(error, message, None, data=data)
    assert envelope["details"] is data or envelope["details"] == data


@given(error=_error_text, message=_message_text, details=json_value(max_leaves=10), data=json_value(max_leaves=10))
def test_details_wins_over_data_alias(error: str, message: str, details, data) -> None:
    # If both are passed, ``details`` wins. This is the documented precedence
    # in make_error's docstring; pinning it stops a future refactor from
    # accidentally swapping the order.
    envelope = make_error(error, message, details, data=data)
    assert envelope["details"] is details or envelope["details"] == details


@given(error=_error_text, message=_message_text, details=json_value(max_leaves=8))
def test_envelope_is_json_serialisable_when_details_is_json(error: str, message: str, details) -> None:
    envelope = make_error(error, message, details)
    # The whole point of the envelope is wire-level interop. If JSON
    # serialisation can ever fail on JSON-shaped details, the SDK's
    # error-path is broken in a way unit tests would never notice.
    serialised = json.dumps(envelope)
    reparsed = json.loads(serialised)
    assert set(reparsed.keys()) == {"error", "message", "details"}
    assert reparsed["error"] == envelope["error"]
    assert reparsed["message"] == envelope["message"]
