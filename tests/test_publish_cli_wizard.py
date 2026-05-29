# SPDX-License-Identifier: Apache-2.0
"""Wave 2 (2026-05-26): tests for `aztea publish` wizard inference path.

# OWNS: regression coverage for the third option in the publish wizard's
#       kind selector — "AI-inferred publish" — which reads an existing
#       .py handler, runs core.publish_inference, prompts the user with
#       each inferred value pre-filled, and POSTs to /registry/register.
# INVARIANTS:
#   - The wizard's POST body has the SAME shape as the /publish_agent MCP
#     tool's POST body (both go through core.publish_inference and post to
#     /registry/register). Anything that the MCP tool gets right by
#     construction, the CLI wizard must also get right.
#   - User overrides win over inferred defaults (the prompt loop returns
#     whatever the user typed; Enter returns the default).
#   - On user-cancel (Ctrl-C or 'n' to the final confirm), zero HTTP calls
#     are made.

All prompts are stubbed via monkeypatch; no real TTY needed. The HTTP
layer is also stubbed — assertions on the captured POST body verify the
wire shape end-to-end without depending on a live backend.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SDK_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "sdks" / "python-sdk"
if str(SDK_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_PYTHON_ROOT))

import typer
from aztea.cli import wizard as _wizard  # noqa: E402


_HANDLER_SOURCE = '''"""Validate a Stripe webhook signature."""

from pydantic import BaseModel


class Input(BaseModel):
    signature: str
    body: str
    secret: str


class Output(BaseModel):
    valid: bool


def handler(payload: Input) -> Output:
    """Verify the HMAC signature on a Stripe webhook payload."""
    return Output(valid=True)
'''


@pytest.fixture
def handler_file(tmp_path: Path) -> Path:
    """Write a realistic handler.py to a tmp dir and return its path."""
    p = tmp_path / "stripe_webhook_validator.py"
    p.write_text(_HANDLER_SOURCE, encoding="utf-8")
    return p


def _scripted_ask_factory(answers: dict[str, str]):
    """Build an `ask` replacement that returns answers[question_text].

    The wizard calls `_p.ask("Agent name", default=...)`; we match on the
    leading prompt text (case-insensitive starts-with) so the test is
    robust to default-value formatting.
    """
    def _ask(question: str, *, default: str | None = None, validator=None):
        for prefix, answer in answers.items():
            if question.lower().startswith(prefix.lower()):
                # Empty answer means "user pressed Enter" → return default.
                return answer if answer else (default or "")
        if default is not None:
            return default
        raise AssertionError(
            f"Wizard prompted an un-scripted question: {question!r}"
        )
    return _ask


def _fake_post_capture():
    """Build (capture_dict, fake_post_fn) for monkeypatching requests.post."""
    captured: dict = {}

    class _Resp:
        status_code = 201
        text = ""

        def json(self):
            return {
                "agent_id": "agent_abc123",
                "slug": "stripe-webhook-validator",
                "review_status": "probation",
            }

    def _post(url, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["json"] = json or {}
        return _Resp()

    return captured, _post


def test_wizard_inferred_publish_accepts_all_defaults(monkeypatch, handler_file):
    """User presses Enter on every prompt → POST body must use inferred values
    verbatim (except endpoint_url which has no inferred default)."""
    answers = {
        "path to your handler": str(handler_file),
        "agent name": "",            # Enter ⇒ accept inferred
        "slug": "",                  # Enter
        "description": "",           # Enter
        "category": "",              # Enter
        "price per call": "",        # Enter
        "public https endpoint": "https://example.com/agent",
        "tags": "",                  # Enter
    }
    monkeypatch.setattr(_wizard._p, "ask", _scripted_ask_factory(answers))
    monkeypatch.setattr(_wizard._p, "confirm", lambda *_a, **_kw: True)

    captured, fake_post = _fake_post_capture()
    import requests as _requests
    monkeypatch.setattr(_requests, "post", fake_post)

    with pytest.raises(typer.Exit) as exc_info:
        _wizard._wizard_inferred_publish(
            resolved_base="https://aztea.test",
            api_key="az_worker_test",
        )
    assert exc_info.value.exit_code == 0
    sent = captured["json"]
    # Inferred fields came through.
    assert sent["name"] == "Stripe Webhook Validator"  # filename-based
    assert sent["slug"] == "stripe-webhook-validator"
    assert "stripe" in sent["description"].lower() or "webhook" in sent["description"].lower()
    assert sent["category"] == "security"               # keyword-based
    assert sent["price_per_call_usd"] == 0.05           # default
    # Pydantic-inferred schemas.
    assert sent["input_schema"]["type"] == "object"
    assert "signature" in sent["input_schema"]["properties"]
    # User-supplied endpoint.
    assert sent["endpoint_url"] == "https://example.com/agent"
    # Auth header passes through.
    assert captured["headers"]["Authorization"] == "Bearer az_worker_test"


def test_wizard_inferred_publish_overrides_one_field(monkeypatch, handler_file):
    """User overrides just the name; everything else is the inferred default."""
    answers = {
        "path to your handler": str(handler_file),
        "agent name": "My Custom Webhook Validator",
        "slug": "",
        "description": "",
        "category": "",
        "price per call": "",
        "public https endpoint": "https://example.com/agent",
        "tags": "",
    }
    monkeypatch.setattr(_wizard._p, "ask", _scripted_ask_factory(answers))
    monkeypatch.setattr(_wizard._p, "confirm", lambda *_a, **_kw: True)

    captured, fake_post = _fake_post_capture()
    import requests as _requests
    monkeypatch.setattr(_requests, "post", fake_post)

    with pytest.raises(typer.Exit) as exc_info:
        _wizard._wizard_inferred_publish(
            resolved_base="https://aztea.test",
            api_key="az_worker_test",
        )
    assert exc_info.value.exit_code == 0
    assert captured["json"]["name"] == "My Custom Webhook Validator"
    # slug still came from inference.
    assert captured["json"]["slug"] == "stripe-webhook-validator"


def test_wizard_user_cancel_at_final_confirm_makes_zero_http_calls(
    monkeypatch, handler_file,
):
    """When the user answers 'n' to 'Ready to publish?', no HTTP must be sent."""
    answers = {
        "path to your handler": str(handler_file),
        "agent name": "",
        "slug": "",
        "description": "",
        "category": "",
        "price per call": "",
        "public https endpoint": "https://example.com/agent",
        "tags": "",
    }
    monkeypatch.setattr(_wizard._p, "ask", _scripted_ask_factory(answers))
    monkeypatch.setattr(_wizard._p, "confirm", lambda *_a, **_kw: False)

    captured, fake_post = _fake_post_capture()
    import requests as _requests
    monkeypatch.setattr(_requests, "post", fake_post)

    with pytest.raises(typer.Exit) as exc_info:
        _wizard._wizard_inferred_publish(
            resolved_base="https://aztea.test",
            api_key="az_worker_test",
        )
    assert exc_info.value.exit_code == 130
    assert "url" not in captured, (
        "User cancelled at final confirm — wizard must NOT have hit the backend"
    )


def test_wizard_refuses_when_no_api_key(monkeypatch, handler_file):
    """An unauthenticated wizard run must exit cleanly with code 2 and tell
    the user to run `aztea login`."""
    # No prompts should even be reached.
    monkeypatch.setattr(_wizard._p, "ask", lambda *_a, **_kw: "should not be called")
    with pytest.raises(typer.Exit) as exc_info:
        _wizard._wizard_inferred_publish(
            resolved_base="https://aztea.test",
            api_key=None,
        )
    assert exc_info.value.exit_code == 2


def test_wizard_refuses_when_handler_path_does_not_exist(monkeypatch, tmp_path):
    """The path validator must reject non-existent files. The validator is
    called by `_p.ask` in the real flow; we test it in isolation here."""
    ok, msg = _wizard._handler_path_validator(str(tmp_path / "nope.py"))
    assert ok is False
    assert "no file" in msg.lower()


def test_handler_path_validator_rejects_non_python_file(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    ok, msg = _wizard._handler_path_validator(str(txt))
    assert ok is False
    assert ".py" in msg.lower()


def test_handler_path_validator_rejects_directory(tmp_path):
    ok, msg = _wizard._handler_path_validator(str(tmp_path))
    assert ok is False
    assert "directory" in msg.lower()


def test_handler_path_validator_accepts_existing_python_file(handler_file):
    ok, value = _wizard._handler_path_validator(str(handler_file))
    assert ok is True
    assert value == str(handler_file)
