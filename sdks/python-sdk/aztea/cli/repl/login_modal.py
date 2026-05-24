"""In-REPL login modal dialog.

# OWNS: the interactive sign-in dialog shown when /login is typed without
#        any auth flags from inside the REPL.
# NOT OWNS: the actual sign-in network call (auth.login owns that); the
#            broader REPL prompt loop (app.py); slash-command routing
#            (commands.py).
# INVARIANTS:
#   - Never reimplements auth.login. The modal collects credentials and
#     calls auth.login under output-capture; auth.login owns save_config,
#     verify, error-hint logic. One source of truth.
#   - Esc at any step dismisses cleanly — no partial state survives, no
#     incomplete writes to ~/.aztea/config.json.
#   - On any close (success / failure / cancel), focus returns to the
#     main REPL input field.
#   - Singleton: only one modal can be open at a time. show_login_modal
#     while already-open is a no-op (resets state, refocuses method
#     picker).
# DECISIONS:
#   - State is module-level (mirrors the existing browse-overlay pattern
#     in app.py). It's a singleton dialog; per-instance state would
#     overcomplicate.
#   - All four step-sections live in the layout always; only one is
#     visible at a time via ConditionalContainer. Cheaper than rebuilding
#     the layout per state transition.
#   - Sync auth call. The Application freezes for ~1-2 s during the
#     verify round-trip; acceptable for a one-shot call. Going async
#     here would be significant scaffolding for one short network hop.
#   - On submission (success OR failure), the modal closes and the
#     captured output lands in the main history pane. Failure does NOT
#     keep the modal open with an inline error — the user retries by
#     typing /login again. Parallels every other slash-command shape
#     and keeps the dialog simple.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer, Float
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame, TextArea


# ── Step constants ────────────────────────────────────────────────────────


STEP_METHOD = "method"
STEP_EMAIL = "email"
STEP_PASSWORD = "password"
STEP_API_KEY = "api_key"


# ── Module-level state ────────────────────────────────────────────────────


_modal_visible: list[bool] = [False]
_modal_step: list[str] = [STEP_METHOD]
_modal_status: list[str] = [""]

# Captured between the email step and password step so we can call
# auth.login(email=..., password=...) with both at once.
_collected_email: list[str] = [""]

# Widget refs filled in by build_login_modal so the state-machine
# handlers can clear / focus them by name without passing them around.
_method_field: list[Optional[TextArea]] = [None]
_email_field: list[Optional[TextArea]] = [None]
_password_field: list[Optional[TextArea]] = [None]
_api_key_field: list[Optional[TextArea]] = [None]

# Callable that returns the widget to focus on modal close (the main
# REPL input field). Stored as a callable, not a direct ref, so app.py
# can bind it lazily and avoid circular imports.
_restore_focus_ref: list[Optional[Callable[[], Any]]] = [None]


# ── Public predicates ─────────────────────────────────────────────────────


@Condition
def modal_is_visible() -> bool:
    """prompt_toolkit Condition — True while the modal should be drawn."""
    return _modal_visible[0]


def _step_is(name: str) -> Condition:
    """Build a Condition that's True when the modal is at ``name``."""
    @Condition
    def _f() -> bool:
        return _modal_visible[0] and _modal_step[0] == name
    return _f


# ── Show / hide / focus helpers ───────────────────────────────────────────


def _focus(widget: Any) -> None:
    """Try to focus a widget. No-op if the Application isn't running."""
    if widget is None:
        return
    try:
        from prompt_toolkit.application import get_app
        get_app().layout.focus(widget)
    except Exception:
        pass


def _invalidate() -> None:
    """Trigger a redraw of the running Application."""
    try:
        from prompt_toolkit.application import get_app
        get_app().invalidate()
    except Exception:
        pass


def _reset_state() -> None:
    """Reset every modal-internal slot to its initial value."""
    _modal_step[0] = STEP_METHOD
    _modal_status[0] = ""
    _collected_email[0] = ""
    for ref in (_method_field, _email_field, _password_field, _api_key_field):
        if ref[0] is not None:
            ref[0].buffer.text = ""


def show_login_modal() -> None:
    """Open the modal — resets state and focuses the method picker.

    Idempotent: calling while already open just resets to the picker.
    Useful when the user re-issues /login after a failed attempt.
    """
    _reset_state()
    _modal_visible[0] = True
    _focus(_method_field[0])
    _invalidate()


def hide_login_modal() -> None:
    """Close the modal, reset state, restore focus to the main input."""
    _modal_visible[0] = False
    _reset_state()
    restorer = _restore_focus_ref[0]
    if restorer is not None:
        _focus(restorer())
    _invalidate()


# ── Status line rendering ─────────────────────────────────────────────────


def _status_text():
    """Hook for the FormattedTextControl above the input fields."""
    text = _modal_status[0]
    if not text:
        # Reserve the line so the layout doesn't reflow when status appears.
        return FormattedText([("", " ")])
    # Show in a warm-warn color so it's clearly an inline message,
    # not part of the form labels.
    return FormattedText([("italic #C4A858", f"  {text}")])


# ── Step-specific accept handlers ─────────────────────────────────────────


def _on_method_accept(buff) -> bool:
    """Method picker: route to email or api_key step."""
    choice = buff.text.strip().lower()
    buff.text = ""
    if choice in ("", "1", "e", "email"):
        _modal_status[0] = ""
        _modal_step[0] = STEP_EMAIL
        _focus(_email_field[0])
    elif choice in ("2", "k", "key", "api", "api-key"):
        _modal_status[0] = ""
        _modal_step[0] = STEP_API_KEY
        _focus(_api_key_field[0])
    else:
        _modal_status[0] = "Pick 1 (email + password) or 2 (API key)."
    _invalidate()
    return False


def _on_email_accept(buff) -> bool:
    """Email step: stash and advance to password."""
    email = buff.text.strip()
    if not email:
        _modal_status[0] = "Email required."
        _invalidate()
        return False
    buff.text = ""
    _collected_email[0] = email
    _modal_status[0] = ""
    _modal_step[0] = STEP_PASSWORD
    _focus(_password_field[0])
    _invalidate()
    return False


def _on_password_accept(buff) -> bool:
    """Password step: submit email + password to auth.login."""
    password = buff.text
    buff.text = ""
    if not password:
        _modal_status[0] = "Password required."
        _invalidate()
        return False
    email = _collected_email[0]
    _submit(email=email, password=password)
    return False


def _on_api_key_accept(buff) -> bool:
    """API-key step: validate prefix and submit."""
    key = buff.text.strip()
    buff.text = ""
    if not key:
        _modal_status[0] = "API key required."
        _invalidate()
        return False
    if not key.startswith("az_"):
        _modal_status[0] = "API keys start with `az_`. Try again."
        _invalidate()
        return False
    _submit(api_key=key)
    return False


# ── Submission ────────────────────────────────────────────────────────────


_DEFAULT_AUTH_KWARGS = {
    "email": None,
    "password": None,
    "api_key": None,
    "base_url": "https://aztea.ai",
    "rotate": False,
    "force": False,
    "json_mode": False,
}


def _submit(**creds: Any) -> None:
    """Hand the collected credentials to auth.login and route the result.

    On success: close the modal, push the success message to the main
    history pane (so the chat-style log records the sign-in), plus a
    welcome line in brand teal naming a sensible next step.
    On failure: close the modal, push the error to history too. The
    user retries by re-issuing /login.
    """
    captured = _do_login(**creds)
    _append_to_history(captured)
    _push_welcome_if_signed_in(captured)
    hide_login_modal()


def _push_welcome_if_signed_in(captured: str) -> None:
    """Emit a brand-teal welcome line when login succeeded.

    Detected via the saved config — if a username is now readable it
    means ``auth.login`` reached the success path and called
    ``save_config``. Skipped when the captured text contains an Aztea
    error sigil (sign-in actually failed) even if the config still
    holds a stale username from a previous session.
    """
    if "Error" in captured[:80] and "✗" in captured[:80]:
        return
    from ..splash import _signed_in_meta
    meta = _signed_in_meta()
    if not meta:
        return
    name = meta.get("username") or "there"
    # ANSI-encoded teal so it survives the chat-history append path
    # (history stores raw ANSI strings, not Rich Text objects).
    teal = "\x1b[1;38;2;126;185;176m"
    reset = "\x1b[0m"
    line = (
        f"\n{teal}Welcome, {name}!{reset}  "
        f"Try {teal}/claude-code{reset} to open Claude Code with Aztea.\n"
    )
    _append_to_history(line)


def _do_login(**creds: Any) -> str:
    """Call auth.login under output-capture; return the captured text."""
    import typer
    from .. import auth as _auth_module
    from .app import _capture_command_output

    kwargs = dict(_DEFAULT_AUTH_KWARGS)
    kwargs.update(creds)

    with _capture_command_output() as cap:
        try:
            _auth_module.login(**kwargs)
        except typer.Exit:
            # auth.login raises Exit on success AND on failure — both
            # already printed their result to the captured stream.
            pass
        except Exception as exc:  # defensive — auth.login normally uses Exit
            _surface_login_error(exc)
    return cap.getvalue()


def _surface_login_error(exc: Exception) -> None:
    """Render a friendly error for client-side login failures.

    The AzteaClient default request timeout is 30s; if aztea.ai is slow
    or unreachable, ``auth.login`` bubbles up a raw urllib3/requests
    timeout. The shared ``render_network_error`` helper turns those into
    actionable copy instead of the raw pool URL.
    """
    from ..output import error as _err, render_network_error

    if render_network_error(exc, code_prefix="login"):
        return
    _err(f"Sign-in failed: {exc}")


def _append_to_history(text: str) -> None:
    """Push captured submit output into the main history pane."""
    if not text:
        return
    from .app import _append_output
    _append_output(text)


# ── Builder ───────────────────────────────────────────────────────────────


def build_login_modal(
    restore_focus: Callable[[], Any],
) -> tuple[Float, dict[str, TextArea]]:
    """Construct the modal Float + per-step TextArea fields.

    ``restore_focus`` is a zero-arg callable returning the widget to
    focus when the modal closes (typically a lambda that returns the
    main REPL input field). We use a callable rather than a direct ref
    so app.py can bind it after constructing the input.

    Returns ``(float_container, {"method": ..., "email": ..., ...})``.
    The dict is provided so app.py / tests can introspect the fields.
    """
    _restore_focus_ref[0] = restore_focus

    # ── Method picker ──
    method_instr = Window(
        FormattedTextControl(FormattedText([
            ("", "\n"),
            ("bold", "  Choose how to sign in:\n"),
            ("", "    1) Email and password\n"),
            ("", "    2) Paste an existing API key (starts with az_)\n"),
            ("", "\n"),
        ])),
        height=5,
    )
    method_field = TextArea(
        height=1,
        multiline=False,
        prompt="  →  Choice [1]: ",
        accept_handler=_on_method_accept,
        wrap_lines=False,
    )
    method_step = ConditionalContainer(
        HSplit([method_instr, method_field]),
        filter=_step_is(STEP_METHOD),
    )

    # ── Email step ──
    email_field = TextArea(
        height=1,
        multiline=False,
        prompt="  →  Email: ",
        accept_handler=_on_email_accept,
        wrap_lines=False,
    )
    email_step = ConditionalContainer(
        HSplit([
            Window(FormattedTextControl(FormattedText([("", "\n")])), height=1),
            email_field,
        ]),
        filter=_step_is(STEP_EMAIL),
    )

    # ── Password step ──
    password_field = TextArea(
        height=1,
        multiline=False,
        prompt="  →  Password: ",
        accept_handler=_on_password_accept,
        wrap_lines=False,
        password=True,
    )
    password_step = ConditionalContainer(
        HSplit([
            Window(
                FormattedTextControl(_email_caption_text),
                height=2,
            ),
            password_field,
        ]),
        filter=_step_is(STEP_PASSWORD),
    )

    # ── API key step ──
    api_key_field = TextArea(
        height=1,
        multiline=False,
        prompt="  →  API key: ",
        accept_handler=_on_api_key_accept,
        wrap_lines=False,
        password=True,
    )
    api_key_step = ConditionalContainer(
        HSplit([
            Window(FormattedTextControl(FormattedText([("", "\n")])), height=1),
            api_key_field,
        ]),
        filter=_step_is(STEP_API_KEY),
    )

    # ── Top status line ──
    status_window = Window(
        FormattedTextControl(_status_text),
        height=1,
    )

    # ── Footer hint ──
    footer = Window(
        FormattedTextControl(FormattedText([
            ("", "\n"),
            ("italic #948D81", "  Enter to continue  ·  Esc to cancel"),
        ])),
        height=2,
    )

    # ── Frame ──
    body = HSplit([
        status_window,
        method_step,
        email_step,
        password_step,
        api_key_step,
        footer,
    ])
    frame = Frame(body, title=" Sign in to Aztea ")

    modal_float = Float(
        content=ConditionalContainer(content=frame, filter=modal_is_visible),
        # Bottom-anchored so the AZTEA banner at the top of the layout
        # stays fully visible while the modal is open. `top=4` (earlier
        # iteration) clipped the wordmark because Float positions are
        # screen-absolute — the banner sits at rows 0..banner_lines, and
        # the modal drawn at row 4 covered everything below.
        # Bottom=4 → 1-row margin above the 3-row input frame.
        bottom=4,
        left=8,
        right=8,
        # `Float.height` MUST be an int (or None) — passing a Dimension
        # raises TypeError inside the renderer ('>' not supported between
        # instances of 'Dimension' and 'int'). 14 lines is enough for
        # the frame borders + status line + method-step instructions
        # (the widest step) + input field + footer.
        height=14,
    )

    # Store widget refs for the state-machine handlers.
    _method_field[0] = method_field
    _email_field[0] = email_field
    _password_field[0] = password_field
    _api_key_field[0] = api_key_field

    return modal_float, {
        "method": method_field,
        "email": email_field,
        "password": password_field,
        "api_key": api_key_field,
    }


def _email_caption():
    """Render the captured email (or a placeholder) for the password step."""
    return _collected_email[0] or "(no email captured)"


def _email_caption_text():
    """Return prompt_toolkit fragments for the dynamic password-step caption."""
    return FormattedText([
        ("italic #948D81", "  Signing in as: "),
        ("italic", _email_caption()),
        ("", "\n"),
    ])
