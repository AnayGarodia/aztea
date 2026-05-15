"""Hint-table regression for the SDK error layer.

The hint is free-form text shown to users; only ``error_code`` is the contract.
But the hint is the most-read string on a failure, so we pin its shape per
status code so PR 1's clarity work doesn't drift back to "Wait briefly, then
retry."
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aztea import errors as aztea_errors


class _FakeResponse:
    """Just enough of ``requests.Response`` for ``raise_for_error_response``."""

    def __init__(
        self,
        status_code: int,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._text = json.dumps(body) if body is not None else ""
        self.text = self._text
        self.content = self._text.encode("utf-8")
        self.ok = 200 <= status_code < 300

    def json(self) -> dict:
        return json.loads(self._text) if self._text else {}


def _capture(response: _FakeResponse) -> aztea_errors.APIError:
    with pytest.raises(aztea_errors.APIError) as info:
        aztea_errors.raise_for_error_response(response)  # type: ignore[arg-type]
    return info.value


def test_401_unrecognized_key_hint_mentions_aztea_login():
    err = _capture(_FakeResponse(401, {"message": "missing key"}))
    assert err.hint and "aztea login" in err.hint.lower()
    assert "not recognized" in err.hint.lower()


def test_401_revoked_key_hint_says_revoked():
    err = _capture(
        _FakeResponse(401, {"message": "revoked", "error": "api_key_revoked"})
    )
    assert err.hint and "revoked" in err.hint.lower()
    assert "aztea login" in err.hint.lower()


def test_429_with_retry_after_header_surfaces_seconds():
    err = _capture(_FakeResponse(429, {"message": "slow down"}, {"Retry-After": "7"}))
    assert err.hint == "Rate-limited. Retry after 7s."


def test_429_without_retry_after_falls_back_cleanly():
    err = _capture(_FakeResponse(429, {"message": "slow down"}))
    assert err.hint == "Rate-limited. Retry in a moment."


def test_500_hint_includes_request_id_when_surfaced():
    err = _capture(
        _FakeResponse(
            500,
            {"message": "boom", "details": {"request_id": "req_abc123"}},
        )
    )
    assert err.hint and "req_abc123" in err.hint
    assert "retry" in err.hint.lower()


def test_500_hint_without_request_id_stays_warm():
    err = _capture(_FakeResponse(500, {"message": "boom"}))
    assert err.hint and "retry" in err.hint.lower()
    assert "request_id" not in err.hint
