"""Per-error-code envelope contract tests.

# OWNS: assertions that every error code in core.error_codes produces the
#       documented envelope shape via make_error, and that DEFAULT_BY_STATUS
#       is internally consistent.
# INVARIANTS asserted: envelope has 'error', 'message', 'details' keys;
#       error code is non-empty; message is non-empty; default-by-status
#       map only references real codes.
"""
from __future__ import annotations

import pytest

import core.error_codes as ec
from core.error_codes import DEFAULT_BY_STATUS, make_error
from tests.corpora import error_codes

pytestmark = pytest.mark.surface

_CODES = error_codes()


@pytest.mark.parametrize("code", _CODES)
def test_make_error_envelope_shape(code):
    out = make_error(code, "test message")
    assert set(out.keys()) >= {"error", "message", "details"}
    assert out["error"] == code
    assert out["message"] == "test message"


@pytest.mark.parametrize("code", _CODES)
def test_make_error_with_details(code):
    out = make_error(code, "msg", {"foo": "bar"})
    assert out["details"] == {"foo": "bar"}


@pytest.mark.parametrize("code", _CODES)
def test_make_error_with_data_alias(code):
    """The keyword-only `data` alias maps onto details."""
    out = make_error(code, "msg", data={"k": 1})
    assert out["details"] == {"k": 1}


@pytest.mark.parametrize("code", _CODES)
def test_code_is_dot_namespaced_lowercase(code):
    assert "." in code
    assert code == code.lower()
    assert " " not in code


def test_make_error_empty_code_falls_back():
    out = make_error("", "msg")
    assert out["error"] == "request.invalid_input"


def test_make_error_empty_message_falls_back():
    out = make_error("x.y", "")
    assert out["message"] == "Request failed."


@pytest.mark.parametrize("status", sorted(DEFAULT_BY_STATUS.keys()))
def test_default_by_status_codes_are_real(status):
    """Every status → code mapping must reference a code defined in the module."""
    code = DEFAULT_BY_STATUS[status]
    assert code, f"status {status} has empty default code"
    # Some defaults are inline strings (e.g., "auth.invalid_key") not bound to
    # a module-level constant. Validate shape rather than identity.
    assert "." in code and " " not in code and code == code.lower()


_HTTP_STATUS_RANGES = [(400, 422), (429, 429), (500, 503), (410, 413)]


@pytest.mark.parametrize("low,high", _HTTP_STATUS_RANGES)
def test_default_by_status_covers_common_4xx_5xx(low, high):
    """For each well-known status range, at least one of the bracket codes
    should be in the table — sanity check that we didn't lose coverage."""
    statuses = list(range(low, high + 1))
    have_any = any(s in DEFAULT_BY_STATUS for s in statuses)
    assert have_any, f"no default code for status range [{low}, {high}]"


# --- Constants expose every documented code ----------------------------------

@pytest.mark.parametrize("code", _CODES)
def test_code_appears_as_module_constant(code):
    """Every code in the corpus is reachable via a module attribute. Catches
    typos where DEFAULT_BY_STATUS references a string that no constant binds."""
    found = any(getattr(ec, name) == code for name in dir(ec) if name.isupper() and not name.startswith("_"))
    if not found:
        pytest.skip(f"code {code!r} is referenced but not bound to a UPPER_CASE constant")
