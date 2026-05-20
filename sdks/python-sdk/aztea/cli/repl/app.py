"""Interactive REPL — full-screen alt-buffer with bottom-pinned input + overlay.

# OWNS: the persistent prompt loop launched by `aztea` (no subcommand),
#        the once-per-session banner, the dismissable browse overlay.
# NOT OWNS: slash command bodies (commands.py), tab completion (completer.py),
#            prompt visuals (prompt.py — left/right border characters).
#
# Architecture:
#   We use a full prompt_toolkit ``Application`` with ``full_screen=True``
#   so launching `aztea` takes over the terminal in an alt-buffer (vim /
#   less / htop style). On exit (Ctrl-C, Ctrl-D, /exit, /quit), the user's
#   previous bash session is restored exactly as it was.
#
#   Layout, top → bottom (main layer):
#     1. Banner ........... static, rendered once at start
#     2. Output history ... scrolls; appended to on each command
#     3. Input box ........ pinned at the bottom (Frame widget = rectangle)
#
#   Plus a Float overlay layer for browse-style commands (/agents, /help,
#   /show, /status, /jobs, /wallet). Their output opens in a centered
#   panel with "Press Esc to close" — Esc dismisses, output never lands
#   in history so the chat-style log stays focused on actions, not
#   browse dumps.
#
#   Slash command output is captured by redirecting Rich's stdout +
#   stderr consoles to a StringIO during dispatch. The captured string
#   (with ANSI escapes preserved) is appended to history OR shown in
#   the overlay, depending on the command class.
"""
from __future__ import annotations

import io
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from ...config import config_path
from .. import output as _output
from . import commands as _commands
from .completer import AzteaCompleter
from .login_modal import (
    build_login_modal,
    hide_login_modal,
    modal_is_visible,
)
from .register_modal import (
    build_register_modal,
    hide_register_modal,
    register_modal_is_visible,
)


# Module-level handle to the main input TextArea, set by _build_application.
# The login modal reads this via get_main_input_field() so it can restore
# focus on Esc / submit-close without app.py having to thread the ref
# through helper functions. Module state is acceptable because the
# Application is a singleton for the process lifetime.
_main_input_field: list[Any] = [None]


def get_main_input_field():
    """Return the main REPL input TextArea, or None if no app is running."""
    return _main_input_field[0]


# Pending subprocess for /claude-code (or any future shell-out command).
# We can't run a subprocess that owns the terminal (Claude Code, vim, etc.)
# from inside a running full_screen prompt_toolkit Application — the two
# alt-buffer renderers fight and leave the screen with Aztea-leftovers at
# the top and the subprocess output at the bottom. Instead, the handler
# sets this slot + calls app.exit(); start() then spawns the subprocess
# after Application.run() returns, so the subprocess owns the terminal
# cleanly. The user lands at their bash shell when the subprocess exits.
_pending_subprocess: list[Optional[list[str]]] = [None]


def request_subprocess_on_exit(cmd: list[str]) -> None:
    """Schedule a subprocess to run after the Application exits."""
    _pending_subprocess[0] = cmd


def _consume_pending_subprocess() -> Optional[list[str]]:
    """Pop the pending command; returns None if nothing was requested."""
    cmd = _pending_subprocess[0]
    _pending_subprocess[0] = None
    return cmd


# Commands whose output is best shown as a dismissable overlay panel
# rather than appended to the chat-history pane. These tend to be wide,
# vertical, or browse-style outputs that the user wants to scan once and
# clear. Action commands (login / hire / init / rate / etc.) keep posting
# to history because the user wants those receipts persistent.
_OVERLAY_COMMANDS = frozenset({
    "/agents",
    "/help",
    "/show",
    "/status",
    "/jobs",
    "/wallet",
})


def _history_path() -> Path:
    """REPL history persists next to ~/.aztea/config.json."""
    return Path(config_path()).expanduser().parent / "repl-history"


def _history_path_ensure() -> Path:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ── Banner ────────────────────────────────────────────────────────────────


def _render_banner_to_ansi() -> str:
    """Render the splash banner to an ANSI-encoded string for the top pane.

    Rich's ``Align.center`` bakes left-padding spaces into the captured
    text based on ``console.width`` at the moment of capture. So when
    the terminal is resized (or the user zooms, which shrinks the column
    count), text rendered at the old width appears shifted in the
    narrower viewport. The fix is to re-render on each redraw — see
    ``_banner_ansi`` below for the cache that keeps this cheap.
    """
    from ..splash import render_banner
    with _output.console.capture() as capture:
        render_banner()
    return capture.get().rstrip("\n")


# Composite-key cache: re-runs Rich rendering whenever any of the inputs
# that affect banner content change. Width alone (V10) wasn't enough —
# logging in or out, or registering MCP via /init, also change the
# rendered text, and those need to invalidate too.
#   key = (terminal_width, auth_username_or_None, mcp_registered_bool)
_banner_cache: dict = {"key": None, "text": ""}


def _banner_ansi():
    """FormattedTextControl callable — returns the banner at current state.

    Invoked by prompt_toolkit on every redraw. Cheap when the cache key
    is unchanged (a couple of file reads + a dict comparison). Re-renders
    when any input differs:
      - terminal width changed (zoom / resize)
      - user signed in or out (cf. ~/.aztea/config.json)
      - MCP got registered or unregistered (cf. ~/.claude.json)
    """
    import shutil
    from ..splash import _signed_in_meta
    from ..mcp import is_mcp_registered

    width = shutil.get_terminal_size().columns
    meta = _signed_in_meta()
    username = (meta or {}).get("username") if meta else None
    mcp = is_mcp_registered() if meta else False
    key = (width, username, mcp)

    if key != _banner_cache["key"]:
        _banner_cache["key"] = key
        _banner_cache["text"] = _render_banner_to_ansi()
    return ANSI(_banner_cache["text"])


# ── Output history pane state ─────────────────────────────────────────────


_output_text: list[str] = []


def _output_ansi():
    """Hook called every render to assemble the accumulated history."""
    return ANSI("".join(_output_text))


def _append_output(text: str) -> None:
    if not text:
        return
    if not text.endswith("\n"):
        text = text + "\n"
    _output_text.append(text)


def _clear_output() -> None:
    _output_text.clear()


# ── Overlay state ─────────────────────────────────────────────────────────


_overlay_visible: list[bool] = [False]
_overlay_text: list[str] = [""]
_overlay_title: list[str] = [""]
# Scroll offset in lines into _overlay_text. 0 = top. Increases as the
# user pages down. Reset to 0 whenever a new overlay opens so the user
# always starts at the top of the result.
_overlay_scroll: list[int] = [0]
_overlay_total_lines: list[int] = [0]


def _overlay_ansi():
    """Slice the captured text to start at the current scroll offset.

    The Window renders only as many rows as fit in its viewport, so
    "slicing from offset N" effectively scrolls — rows 0..N-1 are
    hidden, and rows N..N+viewport are visible. Storing the slice
    length on _overlay_total_lines lets the title bar display the
    "X / Y" position.
    """
    text = _overlay_text[0]
    if not text:
        _overlay_total_lines[0] = 0
        return ANSI("")
    lines = text.split("\n")
    _overlay_total_lines[0] = len(lines)
    offset = max(0, min(_overlay_scroll[0], max(0, len(lines) - 1)))
    return ANSI("\n".join(lines[offset:]))


def _overlay_label():
    title = _overlay_title[0] or "result"
    total = _overlay_total_lines[0]
    offset = _overlay_scroll[0]
    # Show position when there's something to scroll. Hides the scroll
    # hint for short outputs that fit in one screen.
    if total > 0:
        return (
            f" {title}  ·  line {offset + 1}/{total}  ·  "
            f"↑↓ PgUp/PgDn to scroll  ·  Esc to close "
        )
    return f" {title}  ·  press Esc to close "


def _show_overlay(title: str, text: str) -> None:
    _overlay_visible[0] = True
    _overlay_title[0] = title
    _overlay_text[0] = text
    # Always start at the top of a fresh overlay — last session's scroll
    # position has nothing to do with this one.
    _overlay_scroll[0] = 0


def _hide_overlay() -> None:
    _overlay_visible[0] = False
    _overlay_text[0] = ""
    _overlay_title[0] = ""
    _overlay_scroll[0] = 0
    _overlay_total_lines[0] = 0


def _scroll_overlay(delta: int) -> None:
    """Adjust the overlay's scroll offset by ``delta`` lines.

    Negative = up, positive = down. Clamps to [0, total - 1] so the
    user can't scroll past the ends. Called by the key-binding
    handlers in _build_application.
    """
    total = _overlay_total_lines[0]
    if total <= 1:
        return
    _overlay_scroll[0] = max(0, min(total - 1, _overlay_scroll[0] + delta))


@Condition
def _overlay_is_visible():
    return _overlay_visible[0]


# ── Submit handler ────────────────────────────────────────────────────────


def _make_accept_handler():
    """Build the Enter-key callback that dispatches slash commands.

    Constructed as a top-level factory (rather than nested inside
    ``_build_application``) so it can be passed to TextArea at
    construction time — that's the only place prompt_toolkit reliably
    wires accept_handler onto the underlying Buffer. Setting the
    attribute later does nothing.

    The handler reaches the running Application via ``get_app()`` so we
    don't need a forward reference to it at definition time.
    """
    from prompt_toolkit.application import get_app

    def on_accept(buff) -> bool:
        line = buff.text
        buff.text = ""

        if not line.strip():
            return False

        # If an overlay is open, the next command should auto-dismiss it
        # (the user is moving on).
        _hide_overlay()

        stripped = line.strip()
        head = stripped.split(" ", 1)[0]

        # Echo the typed line into the output history so the chat-style
        # log reads as a transcript.
        _append_output(f"\x1b[2;37m›\x1b[0m {line}\n")

        # /clear nukes history before dispatch would do anything visible.
        if stripped == "/clear":
            _clear_output()
            return False

        with _capture_command_output() as cap:
            try:
                _commands.dispatch(line)
            except EOFError:
                get_app().exit()
                return False
            except Exception as exc:
                from ..output import error as _err
                _err(f"Command failed: {exc}")
        captured = cap.getvalue()

        if not captured.strip():
            return False

        # Errors always go to the persistent history — even for browse
        # commands like /agents. The overlay's Frame around captured
        # output that itself contains a Rich error Panel produced the
        # frame-inside-a-frame mess seen in V11. History rendering has
        # no outer frame so the error Panel reads cleanly.
        if "Error" in captured[:80] and "✗" in captured[:80]:
            _append_output(captured)
            return False

        # /logout wipes the previous session's history before posting
        # its own success line — gives the user a fresh-launch feel
        # (banner re-renders to unauth via the cache, history below it
        # shows only the logout receipt + the "sign back in" hint).
        if stripped == "/logout":
            _clear_output()
            _append_output(captured)
            return False

        # Route browse-style output to the overlay; everything else lands
        # in the persistent history.
        if head in _OVERLAY_COMMANDS:
            _show_overlay(head, captured)
        else:
            _append_output(captured)
        return False

    return on_accept


# ── Output capture during command dispatch ────────────────────────────────


@contextmanager
def _capture_command_output():
    """Capture both Rich consoles + sys.stdout during command execution.

    Rich's ``Console.file`` is a property that resolves to ``sys.stdout``
    when its private ``_file`` attribute is None. Saving + restoring the
    *resolved* value freezes the console to whatever sys.stdout was at
    capture time — subsequent tests (e.g. CliRunner-based ones) that
    swap sys.stdout would silently lose Rich output. Save the raw
    ``_file`` slot instead so the dynamic lookup survives the round-trip.
    """
    buf = io.StringIO()
    original_stdout = sys.stdout
    original_console_file = getattr(_output.console, "_file", None)
    original_err_file = getattr(_output.err_console, "_file", None)
    try:
        sys.stdout = buf
        _output.console.file = buf
        _output.err_console.file = buf
        yield buf
    finally:
        sys.stdout = original_stdout
        _output.console.file = original_console_file
        _output.err_console.file = original_err_file


# ── Application factory ───────────────────────────────────────────────────


def _build_application() -> Application:
    """Build the full-screen Application with main layout + overlay layer."""
    # Seed the cache so banner_lines below sees a real captured string.
    # ``_banner_ansi`` will refresh per redraw; this initial capture is
    # only used to size the Window height once at construction.
    banner_text = _render_banner_to_ansi()
    # Force a re-render on first redraw so the cache picks up the live
    # state (banner_lines below uses this initial capture only to size
    # the Window height).
    _banner_cache["key"] = None
    # Banner height accommodates the longest possible variant (authed +
    # init tip + 6-row Quickstart). Computed off the worst-case rendering
    # so the Window doesn't grow/shrink as the user logs in or runs /init.
    banner_lines = max(banner_text.count("\n") + 1, 22)

    # ── Top: banner (re-rendered on resize) ──
    #
    # The FormattedTextControl callable runs on every redraw, so the
    # banner re-centers to the current terminal width whenever the user
    # zooms or resizes. Height is preferred=banner_lines, max=banner_lines
    # (no ``min``) — clip from the bottom rather than refuse to draw.
    banner_control = FormattedTextControl(_banner_ansi, focusable=False)
    banner_window = Window(
        banner_control,
        height=Dimension(preferred=banner_lines, max=banner_lines),
    )

    # ── Middle: scrolling output history ──
    output_control = FormattedTextControl(_output_ansi, focusable=False)
    output_window = Window(
        output_control,
        wrap_lines=True,
        always_hide_cursor=True,
    )

    # ── Bottom: boxed input, pinned ──
    #
    # The accept_handler MUST be passed at construction time (it's stored
    # on the underlying Buffer, not as an attribute on TextArea). Setting
    # `input_field.accept_handler = ...` after the fact creates a stray
    # Python attribute that prompt_toolkit never reads — the symptom is
    # that pressing Enter on /login does nothing. The handler closes over
    # `application` via prompt_toolkit's get_app() so we avoid the forward
    # reference to the Application object that hasn't been created yet.
    input_field = TextArea(
        height=1,
        multiline=False,
        prompt="",
        completer=AzteaCompleter(),
        complete_while_typing=True,
        history=FileHistory(str(_history_path_ensure())),
        wrap_lines=False,
        accept_handler=_make_accept_handler(),
    )
    boxed_input = Frame(input_field)
    # Publish the input field reference so the login modal can restore
    # focus on close. Must happen before build_login_modal so the modal's
    # restore_focus callback can resolve it lazily.
    _main_input_field[0] = input_field

    # ── Login modal (Float) ──
    login_modal_float, _login_fields = build_login_modal(
        restore_focus=get_main_input_field,
    )

    # ── Register modal (Float) ──
    register_modal_float, _register_fields = build_register_modal(
        restore_focus=get_main_input_field,
    )

    # ── Overlay (Float) ──
    #
    # Anchored below the banner so the AZTEA wordmark and the Quickstart
    # panel stay visible while an overlay is open. Earlier positioning
    # (top=2) covered rows 2+ of the layout — i.e. all but the top two
    # rows of the wordmark.
    overlay_control = FormattedTextControl(_overlay_ansi, focusable=False)
    overlay_window = Window(overlay_control, wrap_lines=True, always_hide_cursor=True)
    overlay_frame = Frame(overlay_window, title=_overlay_label)
    overlay_float = Float(
        content=ConditionalContainer(
            content=overlay_frame,
            filter=_overlay_is_visible,
        ),
        # Full screen width below the banner. Earlier positioning
        # (left=6, right=6) left vertical strips at columns 0-5 and
        # right-5..right where the output_window underneath bled
        # through — chat history fragments visible alongside the
        # overlay. Float positions are screen-absolute; with
        # left=0/right=0 the Frame border IS the visual edge.
        top=banner_lines + 1,
        left=0,
        right=0,
        bottom=4,
    )

    # ── Key bindings ──
    kb = KeyBindings()

    @kb.add("c-c", eager=True)
    @kb.add("c-d", eager=True)
    def _exit(event) -> None:
        event.app.exit()

    @kb.add("escape", filter=_overlay_is_visible, eager=True)
    def _close_overlay(event) -> None:
        _hide_overlay()
        event.app.invalidate()

    # Overlay scroll bindings. Only fire while the overlay is open so
    # they don't shadow the main input's text-editing behavior (arrow
    # keys for buffer navigation, etc.).
    @kb.add("up", filter=_overlay_is_visible, eager=True)
    def _overlay_scroll_up(event) -> None:
        _scroll_overlay(-1)
        event.app.invalidate()

    @kb.add("down", filter=_overlay_is_visible, eager=True)
    def _overlay_scroll_down(event) -> None:
        _scroll_overlay(+1)
        event.app.invalidate()

    @kb.add("pageup", filter=_overlay_is_visible, eager=True)
    def _overlay_page_up(event) -> None:
        _scroll_overlay(-10)
        event.app.invalidate()

    @kb.add("pagedown", filter=_overlay_is_visible, eager=True)
    def _overlay_page_down(event) -> None:
        _scroll_overlay(+10)
        event.app.invalidate()

    @kb.add("home", filter=_overlay_is_visible, eager=True)
    def _overlay_home(event) -> None:
        _overlay_scroll[0] = 0
        event.app.invalidate()

    @kb.add("end", filter=_overlay_is_visible, eager=True)
    def _overlay_end(event) -> None:
        # Jump to the bottom; clamped inside _scroll_overlay anyway.
        _scroll_overlay(_overlay_total_lines[0])
        event.app.invalidate()

    @kb.add("escape", filter=modal_is_visible, eager=True)
    def _close_login_modal(event) -> None:
        """Esc inside the login dialog: dismiss + refocus main input."""
        hide_login_modal()
        event.app.layout.focus(input_field)
        event.app.invalidate()

    @kb.add("escape", filter=register_modal_is_visible, eager=True)
    def _close_register_modal(event) -> None:
        """Esc inside the register dialog: dismiss + refocus main input."""
        hide_register_modal()
        event.app.layout.focus(input_field)
        event.app.invalidate()

    @kb.add(
        "escape",
        filter=~(_overlay_is_visible | modal_is_visible | register_modal_is_visible),
        eager=True,
    )
    def _esc_exits_repl(event) -> None:
        """Esc with nothing else open: leave the REPL entirely.

        Per user: pressing Esc at the bare prompt should return to the
        bash shell, same as Ctrl-D / Ctrl-C. Without this binding, Esc
        falls through to prompt_toolkit's default (the start of a Meta
        sequence) and leaves the renderer in a half-exited state — the
        symptom was an "alt-screen shrink" with the banner clipping
        and "Window too small" errors on subsequent Ctrl-D presses.
        """
        event.app.exit()

    # ── Style ──
    from .prompt import _accent as _accent_fn
    accent = _accent_fn()
    style = Style.from_dict({
        "frame.border": accent,
        "frame.label":  accent,
    })

    # ── Layout ──
    main_body = HSplit([
        banner_window,
        output_window,
        boxed_input,
    ])
    # Login modal renders ABOVE the browse overlay (insertion order = stacking
    # order; later = drawn on top). In practice only one is ever visible at
    # once because both their Esc bindings hide on dismiss, so the order
    # only matters for the rare race where /login is typed while an overlay
    # is open. The dispatcher hides the overlay before opening the modal.
    root = FloatContainer(
        content=main_body,
        floats=[overlay_float, login_modal_float, register_modal_float],
    )

    application = Application(
        layout=Layout(root, focused_element=input_field),
        key_bindings=kb,
        full_screen=True,
        mouse_support=True,
        style=style,
    )
    return application


# ── Entry point ───────────────────────────────────────────────────────────


def start() -> None:
    """Open the full-screen REPL. Returns when the user exits."""
    _clear_output()
    _hide_overlay()
    application = _build_application()
    try:
        application.run()
    except KeyboardInterrupt:
        return

    # If a slash command queued a subprocess via request_subprocess_on_exit
    # (today: only /claude-code), run it now — the Application's alt-screen
    # is closed and the subprocess can own the terminal cleanly. When the
    # subprocess returns, the user is back at their original bash shell.
    pending = _consume_pending_subprocess()
    if pending:
        import os
        import subprocess
        import sys
        # Wipe the screen before handing off. Without this, bash
        # scrollback from previous `aztea` invocations stays visible at
        # the top of the terminal (we saw 5 echoed prompt lines before
        # Claude Code's banner in V18). ANSI 2J clears the visible
        # screen, H homes the cursor. Most modern terminals also have
        # \x1b[3J which clears scrollback too — adding it for thoroughness.
        sys.stdout.write("\x1b[3J\x1b[2J\x1b[H")
        sys.stdout.flush()
        try:
            subprocess.run(pending, cwd=os.getcwd(), check=False)
        except FileNotFoundError:
            # Already validated in the slash handler; if it disappeared
            # between the check and now, fall through silently.
            pass
