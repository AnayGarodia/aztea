"""In-REPL registration modal dialog.

# OWNS: the interactive sign-up dialog shown when /register is typed from
#        inside the REPL.
# NOT OWNS: the actual registration network call (client.auth.register
#            owns that); the broader REPL prompt loop (app.py); slash-
#            command routing (commands.py).
# INVARIANTS:
#   - Never reimplements client.auth.register or password hashing — we
#     send plaintext, server (PBKDF2-HMAC-SHA256, 100k iters) does the
#     rest. One source of truth.
#   - Esc at any step dismisses cleanly. No partial state survives. No
#     incomplete writes to ~/.aztea/config.json.
#   - On any close (success / failure / cancel), focus returns to the
#     main REPL input field.
#   - Singleton: only one modal can be open at a time.
#   - Validation happens client-side BEFORE the server call so the user
#     sees the rule violation inline rather than as a server error.
#     The exact rules mirror server-side validation
#     (core/models/core_types.py:UserRegisterRequest).
# DECISIONS:
#   - State is module-level, mirroring login_modal.py.
#   - Linear 4-step flow (no method picker like login). Username → email
#     → password → confirm password → submit.
#   - Inline status line shows the next-step rule when the field is
#     about to be entered, or a validation error when one fires.
#   - On success, save the returned raw_api_key to ~/.aztea/config.json
#     so the user is immediately signed in. Same effect as /login.
#   - Submission runs on a background thread so the modal keeps painting
#     "Creating your account…" instead of freezing for up to 30s (the
#     AzteaClient default request timeout). The done-callback marshals
#     back to the prompt_toolkit loop via call_soon_threadsafe so all
#     output capture + history append happens on the UI thread.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Callable, Optional

from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer, Float
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame, TextArea


# ── Step constants ────────────────────────────────────────────────────────


STEP_USERNAME = "username"
STEP_EMAIL = "email"
STEP_PASSWORD = "password"
STEP_CONFIRM = "confirm"


# ── Validation rules (mirror server-side core_types.py) ───────────────────


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_USERNAME_MIN, _USERNAME_MAX = 3, 32
_PASSWORD_MIN, _PASSWORD_MAX = 8, 1024


def _validate_username(value: str) -> str | None:
    """Return an error message or None if valid."""
    if len(value) < _USERNAME_MIN:
        return f"Username must be at least {_USERNAME_MIN} characters."
    if len(value) > _USERNAME_MAX:
        return f"Username must be at most {_USERNAME_MAX} characters."
    if not _USERNAME_RE.match(value):
        return "Username may only contain letters, numbers, _ and -."
    return None


def _validate_email(value: str) -> str | None:
    if not value:
        return "Email required."
    if not _EMAIL_RE.match(value):
        return "That doesn't look like a valid email address."
    return None


def _validate_password(value: str) -> str | None:
    if len(value) < _PASSWORD_MIN:
        return f"Password must be at least {_PASSWORD_MIN} characters."
    if len(value) > _PASSWORD_MAX:
        return f"Password is too long (max {_PASSWORD_MAX})."
    if not re.search(r"[A-Za-z]", value):
        return "Password must contain at least one letter."
    if not re.search(r"\d", value):
        return "Password must contain at least one digit."
    return None


# ── Module-level state ────────────────────────────────────────────────────


_modal_visible: list[bool] = [False]
_modal_step: list[str] = [STEP_USERNAME]
_modal_status: list[str] = [""]

# True while the network round-trip is in flight. Used to (a) ignore
# extra Enter presses so the user can't fire two registrations and
# (b) render the inline "Creating your account…" hint via _status_text.
_submitting: list[bool] = [False]

# Stash collected values between steps so the final submit has everything.
_collected = {"username": "", "email": "", "password": ""}

# Widget refs filled in by build_register_modal.
_username_field: list[Optional[TextArea]] = [None]
_email_field: list[Optional[TextArea]] = [None]
_password_field: list[Optional[TextArea]] = [None]
_confirm_field: list[Optional[TextArea]] = [None]

_restore_focus_ref: list[Optional[Callable[[], Any]]] = [None]


# ── Public predicates ─────────────────────────────────────────────────────


@Condition
def register_modal_is_visible() -> bool:
    return _modal_visible[0]


def _step_is(name: str) -> Condition:
    @Condition
    def _f() -> bool:
        return _modal_visible[0] and _modal_step[0] == name
    return _f


# ── Show / hide / focus ───────────────────────────────────────────────────


def _focus(widget: Any) -> None:
    if widget is None:
        return
    try:
        from prompt_toolkit.application import get_app
        get_app().layout.focus(widget)
    except Exception:
        pass


def _invalidate() -> None:
    try:
        from prompt_toolkit.application import get_app
        get_app().invalidate()
    except Exception:
        pass


def _reset_state() -> None:
    _modal_step[0] = STEP_USERNAME
    _modal_status[0] = ""
    _submitting[0] = False
    _collected.update(username="", email="", password="")
    for ref in (_username_field, _email_field, _password_field, _confirm_field):
        if ref[0] is not None:
            ref[0].buffer.text = ""


def show_register_modal() -> None:
    """Open the modal — resets state and focuses the username field."""
    _reset_state()
    _modal_visible[0] = True
    _focus(_username_field[0])
    _invalidate()


def hide_register_modal() -> None:
    """Close the modal and restore focus to the main REPL input."""
    _modal_visible[0] = False
    _reset_state()
    restorer = _restore_focus_ref[0]
    if restorer is not None:
        _focus(restorer())
    _invalidate()


# ── Status line ───────────────────────────────────────────────────────────


def _status_text():
    text = _modal_status[0]
    if not text:
        return FormattedText([("", " ")])
    return FormattedText([("italic #C4A858", f"  {text}")])


# ── Step accept handlers ──────────────────────────────────────────────────


def _on_username_accept(buff) -> bool:
    """Username step: validate format, advance to email."""
    value = buff.text.strip()
    err = _validate_username(value)
    if err:
        _modal_status[0] = err
        _invalidate()
        return False
    buff.text = ""
    _collected["username"] = value
    _modal_status[0] = ""
    _modal_step[0] = STEP_EMAIL
    _focus(_email_field[0])
    _invalidate()
    return False


def _on_email_accept(buff) -> bool:
    """Email step: validate format, advance to password."""
    value = buff.text.strip().lower()
    err = _validate_email(value)
    if err:
        _modal_status[0] = err
        _invalidate()
        return False
    buff.text = ""
    _collected["email"] = value
    _modal_status[0] = ""
    _modal_step[0] = STEP_PASSWORD
    _focus(_password_field[0])
    _invalidate()
    return False


def _on_password_accept(buff) -> bool:
    """Password step: validate complexity, advance to confirm."""
    value = buff.text
    err = _validate_password(value)
    if err:
        _modal_status[0] = err
        buff.text = ""  # don't keep an invalid password in the buffer
        _invalidate()
        return False
    buff.text = ""
    _collected["password"] = value
    _modal_status[0] = ""
    _modal_step[0] = STEP_CONFIRM
    _focus(_confirm_field[0])
    _invalidate()
    return False


def _on_confirm_accept(buff) -> bool:
    """Confirm step: ensure match, then submit."""
    if _submitting[0]:
        # Already mid-flight — swallow Enter so the user can't fire a
        # second registration while the first is still in the network.
        buff.text = ""
        return False
    value = buff.text
    buff.text = ""
    if value != _collected["password"]:
        _modal_status[0] = "Passwords don't match. Re-enter password."
        # Bounce back to the password step so the user re-enters BOTH
        # — typing only the confirm with no way to fix the original
        # password feels broken.
        _collected["password"] = ""
        _modal_step[0] = STEP_PASSWORD
        _focus(_password_field[0])
        _invalidate()
        return False
    _submit()
    return False


# ── Submission ────────────────────────────────────────────────────────────


_SUBMITTING_STATUS = "Creating your account…  this can take up to 30 seconds."

# Base URL is named so both the worker (which calls the API) and the
# UI-thread renderer (which writes save_config) agree on a single value.
_REGISTER_BASE_URL = "https://aztea.ai"


def _submit() -> None:
    """Send collected credentials to client.auth.register, route the result.

    The HTTP call goes through `client.auth.register`, which can block for
    up to 30s (the AzteaClient default request timeout). Running it inline
    would freeze the prompt_toolkit event loop — the modal would appear
    dead until the call returned. Instead:

      1. Mark the modal as submitting and paint the status line.
      2. Run the blocking HTTP call on a worker thread.
      3. Marshal the result back to the asyncio loop via
         ``call_soon_threadsafe`` and render there. Output capture +
         ``save_config`` + modal hide all happen on the UI thread to
         avoid touching shared globals (``sys.stdout``, prompt_toolkit
         state) from the worker.
    """
    _submitting[0] = True
    _modal_status[0] = _SUBMITTING_STATUS
    _invalidate()

    # Snapshot creds before the modal state gets cleared on hide. The
    # worker thread reads only these locals, never the module dicts.
    username = _collected["username"]
    email = _collected["email"]
    password = _collected["password"]

    loop = _get_running_loop()
    if loop is None:
        # No running asyncio loop (tests, head-less use). Fall back to
        # the original sync behavior so tests don't need an event loop.
        outcome = _perform_register_call(username, email, password)
        _finalize_submit(username, email, outcome)
        return

    def _worker() -> None:
        # Runs off the UI thread. Do NOT touch prompt_toolkit state or
        # capture output here — both reach into shared globals. The
        # work this thread owns is exactly the blocking HTTP call; the
        # outcome dict is marshaled back to the loop for rendering.
        outcome = _perform_register_call(username, email, password)
        loop.call_soon_threadsafe(
            _finalize_submit,
            username,
            email,
            outcome,
        )

    threading.Thread(target=_worker, daemon=True).start()


def _get_running_loop():
    """Return the running asyncio loop, or None if we aren't inside one."""
    import asyncio
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _perform_register_call(username: str, email: str, password: str) -> dict:
    """Make the actual HTTP call. Safe to run off-thread — no globals touched.

    Returns ``{"ok": True, "result": <server-payload>}`` on success or
    ``{"ok": False, "exc": <exception>}`` on any failure. Never raises.
    """
    from ...client import AzteaClient
    try:
        client = AzteaClient(
            base_url=_REGISTER_BASE_URL, client_id="aztea-cli-register",
        )
        result = client.auth.register(
            username=username, email=email, password=password,
        )
        return {"ok": True, "result": result}
    except Exception as exc:  # all errors flow through _surface_register_error
        return {"ok": False, "exc": exc}


def _finalize_submit(username: str, email: str, outcome: dict) -> None:
    """Render the registration outcome on the UI thread and close the modal."""
    try:
        captured = _render_register_outcome(
            username=username, email=email, outcome=outcome,
        )
    finally:
        _submitting[0] = False
    _append_to_history(captured)
    _push_welcome_if_signed_in(captured)
    hide_register_modal()


def _render_register_outcome(
    *, username: str, email: str, outcome: dict,
) -> str:
    """Turn a worker-thread outcome into captured panel text.

    On success: save the returned raw_api_key to ~/.aztea/config.json so
    the user is signed in immediately. On failure: surface the error via
    Aztea's standard error() panel, friendly-ing common network failures.

    Must run on the UI thread — uses ``_capture_command_output`` which
    swaps ``sys.stdout`` globally.
    """
    from ...config import save_config
    from ..output import error, success
    from .app import _capture_command_output

    with _capture_command_output() as cap:
        if not outcome.get("ok"):
            _surface_register_error(outcome.get("exc") or RuntimeError("Registration failed."))
            return cap.getvalue()

        result = outcome.get("result") or {}
        raw_key = str(result.get("raw_api_key") or "").strip()
        if not raw_key:
            error(
                "Registration succeeded but no API key was returned.",
                hint=(
                    "Run `/login` with the email + password you just "
                    "registered to mint a fresh key."
                ),
                code="register.no_raw_key",
            )
            return cap.getvalue()

        save_config(api_key=raw_key, base_url=_REGISTER_BASE_URL, username=username)
        success(
            f"Account created — signed in as {username}",
            detail=email,
        )

    return cap.getvalue()


def _surface_register_error(exc: Exception) -> None:
    """Map common registration failures to actionable messages.

    Client-side network failures (timeout, DNS, unreachable host) flow
    through the shared ``render_network_error`` helper so every surface
    in the CLI tells the same story. Server-emitted Aztea errors get
    specific copy for the cases the user can act on (taken username,
    rate limited); everything else falls through to a generic panel.
    """
    from ..output import error, render_network_error
    from ...errors import AzteaError

    # Check network shapes first — they come back as plain requests/urllib3
    # exceptions, NOT AzteaError, and the raw urllib3 message is hostile.
    if render_network_error(exc, code_prefix="register"):
        return

    msg = str(exc).strip() or "Registration failed."

    if isinstance(exc, AzteaError):
        lower = msg.lower()
        if "already" in lower and ("exist" in lower or "taken" in lower):
            error(
                "Username or email is already taken.",
                hint=(
                    "Pick a different username, or run `/login` if this "
                    "is your account."
                ),
                code="register.taken",
            )
            return
        if "rate" in lower or "429" in lower:
            error(
                "Too many sign-up attempts.",
                hint="Wait a minute and try `/register` again.",
                code="register.rate_limited",
            )
            return

    error(msg, code="register.failed")


def _push_welcome_if_signed_in(captured: str) -> None:
    """Emit a brand-teal welcome line for the new account."""
    if "Error" in captured[:80] and "✗" in captured[:80]:
        return
    from ..splash import _signed_in_meta
    meta = _signed_in_meta()
    if not meta:
        return
    name = meta.get("username") or "there"
    teal = "\x1b[1;38;2;126;185;176m"
    reset = "\x1b[0m"
    line = (
        f"\n{teal}Welcome to Aztea, {name}!{reset}  "
        f"Try {teal}/init{reset} next to wire Aztea into Claude Code.\n"
    )
    _append_to_history(line)


def _append_to_history(text: str) -> None:
    if not text:
        return
    from .app import _append_output
    _append_output(text)


# ── Builder ───────────────────────────────────────────────────────────────


def build_register_modal(
    restore_focus: Callable[[], Any],
) -> tuple[Float, dict[str, TextArea]]:
    """Construct the modal Float + per-step TextArea fields.

    Mirrors build_login_modal: same Float positioning, same height,
    same focus-restoration callable pattern.
    """
    _restore_focus_ref[0] = restore_focus

    # ── Username step ──
    username_instr = Window(
        FormattedTextControl(FormattedText([
            ("", "\n"),
            ("bold", "  Create your Aztea account\n"),
            ("", "\n"),
            ("italic #948D81", "  3-32 chars · letters, numbers, _ and -\n"),
        ])),
        height=4,
    )
    username_field = TextArea(
        height=1,
        multiline=False,
        prompt="  →  Username: ",
        accept_handler=_on_username_accept,
        wrap_lines=False,
    )
    username_step = ConditionalContainer(
        HSplit([username_instr, username_field]),
        filter=_step_is(STEP_USERNAME),
    )

    # ── Email step ──
    email_instr = Window(
        FormattedTextControl(FormattedText([
            ("", "\n"),
            ("italic #948D81", "  We'll send job receipts and disputes here.\n"),
        ])),
        height=2,
    )
    email_field = TextArea(
        height=1,
        multiline=False,
        prompt="  →  Email: ",
        accept_handler=_on_email_accept,
        wrap_lines=False,
    )
    email_step = ConditionalContainer(
        HSplit([email_instr, email_field]),
        filter=_step_is(STEP_EMAIL),
    )

    # ── Password step ──
    password_instr = Window(
        FormattedTextControl(FormattedText([
            ("", "\n"),
            ("italic #948D81", "  8+ chars · at least one letter and one digit\n"),
        ])),
        height=2,
    )
    password_field = TextArea(
        height=1,
        multiline=False,
        prompt="  →  Password: ",
        accept_handler=_on_password_accept,
        wrap_lines=False,
        password=True,
    )
    password_step = ConditionalContainer(
        HSplit([password_instr, password_field]),
        filter=_step_is(STEP_PASSWORD),
    )

    # ── Confirm password step ──
    confirm_instr = Window(
        FormattedTextControl(FormattedText([
            ("", "\n"),
            ("italic #948D81", "  Type your password once more to confirm.\n"),
        ])),
        height=2,
    )
    confirm_field = TextArea(
        height=1,
        multiline=False,
        prompt="  →  Confirm: ",
        accept_handler=_on_confirm_accept,
        wrap_lines=False,
        password=True,
    )
    confirm_step = ConditionalContainer(
        HSplit([confirm_instr, confirm_field]),
        filter=_step_is(STEP_CONFIRM),
    )

    # ── Status + footer ──
    status_window = Window(FormattedTextControl(_status_text), height=1)
    footer = Window(
        FormattedTextControl(FormattedText([
            ("", "\n"),
            ("italic #948D81", "  Enter to continue  ·  Esc to cancel"),
        ])),
        height=2,
    )

    body = HSplit([
        status_window,
        username_step,
        email_step,
        password_step,
        confirm_step,
        footer,
    ])
    frame = Frame(body, title=" Create your Aztea account ")

    modal_float = Float(
        content=ConditionalContainer(content=frame, filter=register_modal_is_visible),
        # Same anchoring + height as the login modal — keeps the AZTEA
        # banner at the top fully visible.
        bottom=4,
        left=8,
        right=8,
        height=14,
    )

    _username_field[0] = username_field
    _email_field[0] = email_field
    _password_field[0] = password_field
    _confirm_field[0] = confirm_field

    return modal_float, {
        "username": username_field,
        "email": email_field,
        "password": password_field,
        "confirm": confirm_field,
    }
