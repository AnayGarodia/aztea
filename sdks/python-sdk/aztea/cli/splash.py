"""Banner printed once at REPL start (and by ``aztea --no-repl``).

# OWNS: the visual hero shown on REPL entry and by the legacy --no-repl path.
# NOT OWNS: REPL behavior, slash-command dispatch, prompt loop.
# INVARIANTS:
#   - Pure layout. Reads `load_config()` once. Never makes a network call.
#   - Width-stable: the wordmark fits a 72-col terminal.
#   - Degrades cleanly when Rich is missing or stdout is not a TTY.
# DECISIONS:
#   - V3 visual = V1 visual minus the tagline and the four-pillar capability
#     ticker. The user explicitly preferred V1's gradient + rounded quickstart
#     panel; only the chatter was over the top.
#   - Theme-adaptive gradient: bright mint→teal on dark terminals (V1
#     original); deep teal→ink on light terminals so the wordmark stays
#     legible on cream / white backgrounds. Detection precedence:
#     ``AZTEA_TERMINAL_THEME=light|dark`` env, then ``COLORFGBG`` parsing,
#     then default dark.
#   - The quickstart panel lists *slash commands* (the V3 REPL surface), not
#     shell commands. From the REPL prompt the user types ``/login``, not
#     ``aztea login``.
"""
from __future__ import annotations

import os
import sys
from typing import Any

from ..config import load_config
from .output import (
    ARROW,
    DOT,
    _HAS_RICH,
    console,
)


# ── Hero wordmark ─────────────────────────────────────────────────────────
# ANSI Shadow. Six rows × 41 cols. Rendered with a six-tier vertical gradient
# so the block reads as illuminated from above.

_LOGO_ROWS: tuple[str, ...] = (
    " █████╗ ███████╗████████╗███████╗ █████╗ ",
    "██╔══██╗╚══███╔╝╚══██╔══╝██╔════╝██╔══██╗",
    "███████║  ███╔╝    ██║   █████╗  ███████║",
    "██╔══██║ ███╔╝     ██║   ██╔══╝  ██╔══██║",
    "██║  ██║███████╗   ██║   ███████╗██║  ██║",
    "╚═╝  ╚═╝╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝",
)
_LOGO_WIDTH = 41


# ── Theme-adaptive gradients ──────────────────────────────────────────────
# Six values, top-to-bottom. Dark = V1 original (mint highlight to ink-teal
# shadow). Light = Aztea brand deep-teal range so the wordmark sits dark
# enough to stay readable on cream / white terminals.

_GRADIENT_DARK: tuple[str, ...] = (
    "bold #99F6E4",   # mint highlight
    "bold #5EEAD4",   # bright mint
    "bold #2DD4BF",   # vivid teal
    "bold #14B8A6",   # teal body
    "bold #0F766E",   # deep teal
    "bold #115E59",   # ink-teal shadow
)
_GRADIENT_LIGHT: tuple[str, ...] = (
    "bold #0F766E",
    "bold #115E59",
    "bold #063F43",   # Aztea light-mode --accent
    "bold #053336",
    "bold #042F33",   # Aztea light-mode --accent-hover
    "bold #021F22",   # Aztea light-mode --accent-press
)


# ── Single-accent values for non-gradient chrome ──────────────────────────
# Quickstart panel chip, command-code highlight, prompt symbol. The chrome
# uses one solid accent (matched to the terminal mode) rather than picking
# from the gradient. These are Aztea's actual brand --accent values from
# tokens.css, not Tailwind teals.

_ACCENT_DARK = "#7EB9B0"
_ACCENT_LIGHT = "#063F43"
_CHIP_BG_DARK = "#5EEAD4"     # quickstart chip background on dark terminals
_CHIP_BG_LIGHT = "#0F766E"
_CHIP_FG_DARK = "#0C1F22"     # chip text colour on dark terminals
_CHIP_FG_LIGHT = "#F5EEDF"


# ── Quickstart panel content (slash commands) ─────────────────────────────
# Guest mode leads with /login; authed mode leads with the marketplace
# operations the user is most likely to want. /claude-code is the bridge
# between the Aztea REPL and the smart-routing surface (Claude Code with
# Aztea loaded as MCP).

_CMD_COL_WIDTH = 18

_SHORTCUTS_GUEST: tuple[tuple[str, str], ...] = (
    ("/login",       "Sign in to aztea.ai"),
    ("/register",    "Create a new Aztea account"),
    # /agents is intentionally NOT shown to unauth users — the server
    # rejects /registry/agents without a key, so showing it as the
    # second-most-discoverable command sets a bad first-run expectation.
    # Users get to /agents from /help once they're signed in.
    ("/claude-code", "Open Claude Code with Aztea"),
    ("/ask",         "Ask the Aztea troubleshooter anything"),
    ("/help",        "All slash commands"),
)

# Default authed Quickstart — used when MCP is already wired into Claude
# Code (the steady state). Leads with /claude-code because that's where
# most users actually want to go after sign-in (Aztea CLI is positioned
# as the marketplace control room; Claude Code is the chat surface).
_SHORTCUTS_AUTHED_DEFAULT: tuple[tuple[str, str], ...] = (
    ("/claude-code", "Open Claude Code with Aztea"),
    ("/agents",      "Browse 35 agents by category"),
    ("/hire <slug>", "Run an agent on your input"),
    ("/status",      "Wallet + recent jobs"),
    ("/ask",         "Ask the troubleshooter for help"),
    ("/help",        "All slash commands"),
)

# Authed Quickstart variant shown when the user is signed in but Aztea
# MCP is NOT yet registered in Claude Code. /init is the recommended
# next step, so it leads the panel; /status is bumped out of the panel
# (still discoverable via /help) to keep the rendered height stable.
_SHORTCUTS_AUTHED_NEEDS_INIT: tuple[tuple[str, str], ...] = (
    ("/init",        "Wire Aztea into Claude Code"),
    ("/claude-code", "Open Claude Code with Aztea"),
    ("/agents",      "Browse 35 agents by category"),
    ("/hire <slug>", "Run an agent on your input"),
    ("/ask",         "Ask the troubleshooter for help"),
    ("/help",        "All slash commands"),
)
# Back-compat alias for tests that reference the old single-tuple name.
# New code should call _authed_shortcuts() which picks the right variant
# based on MCP-registered state.
_SHORTCUTS_AUTHED = _SHORTCUTS_AUTHED_DEFAULT

_PANEL_WIDTH = 74


def _mcp_registered_now() -> bool:
    """Defer import to keep splash.py free of cli.mcp at import time
    (avoids a circular load on certain orderings)."""
    try:
        from .mcp import is_mcp_registered
        return is_mcp_registered("claude")
    except Exception:
        return False


def _authed_shortcuts() -> tuple[tuple[str, str], ...]:
    """Pick the Quickstart variant based on whether /init has been run.

    When MCP isn't yet registered, /init is the recommended next step
    and gets the top slot. Once it's registered, the default variant
    (lead with /claude-code) takes over.
    """
    if _mcp_registered_now():
        return _SHORTCUTS_AUTHED_DEFAULT
    return _SHORTCUTS_AUTHED_NEEDS_INIT


# ── Terminal-mode detection ───────────────────────────────────────────────


def _detect_terminal_mode() -> str:
    """Return ``"light"`` or ``"dark"`` for terminal background.

    Precedence:
      1. ``AZTEA_TERMINAL_THEME=light|dark`` — explicit user override.
      2. ``COLORFGBG`` env var (rxvt, urxvt, a few others) — format
         ``fg;bg`` where ``bg`` in ``{0..7}`` = dark, ``{8..15}`` = light.
         Some terminals emit ``fg;variant;bg``; we read the last field.
      3. Default to ``"dark"`` — the majority of developer terminals.
    """
    override = (os.environ.get("AZTEA_TERMINAL_THEME") or "").strip().lower()
    if override in ("light", "dark"):
        return override
    fgbg = os.environ.get("COLORFGBG")
    if fgbg:
        parts = fgbg.split(";")
        try:
            bg = int(parts[-1])
        except ValueError:
            return "dark"
        return "light" if bg >= 8 else "dark"
    return "dark"


def _theme_palette() -> dict[str, Any]:
    """Resolve every theme-dependent color in one place. Returned dict:

        gradient    six-tier wordmark gradient (top to bottom)
        accent      single accent for command codes + prompt + state hints
        chip_bg     quickstart panel title-chip background color
        chip_fg     quickstart panel title-chip text color
    """
    if _detect_terminal_mode() == "light":
        return {
            "gradient": _GRADIENT_LIGHT,
            "accent": _ACCENT_LIGHT,
            "chip_bg": _CHIP_BG_LIGHT,
            "chip_fg": _CHIP_FG_LIGHT,
        }
    return {
        "gradient": _GRADIENT_DARK,
        "accent": _ACCENT_DARK,
        "chip_bg": _CHIP_BG_DARK,
        "chip_fg": _CHIP_FG_DARK,
    }


def _is_tty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _signed_in_meta() -> dict[str, Any] | None:
    """Read the saved config once. Returns None when no API key is stored.

    The banner uses this only to choose which quickstart shortcut list to
    show — the verbose "signed in as X · wallet · host" line lives on the
    REPL bottom toolbar, not in the banner.
    """
    cfg = load_config() or {}
    if not cfg.get("api_key"):
        return None
    return {
        "username": cfg.get("username") or "",
        "base_url": cfg.get("base_url") or "https://aztea.ai",
    }


# ── Rich renderer ─────────────────────────────────────────────────────────


def _render_wordmark_rich(palette: dict[str, Any]) -> None:
    """Six-row ANSI Shadow wordmark, gradient top-to-bottom, centered."""
    from rich.text import Text
    from rich.align import Align
    console.print()
    for row, style in zip(_LOGO_ROWS, palette["gradient"]):
        console.print(Align.center(Text(row, style=style)))
    console.print()


def _render_status_line_rich(meta: dict[str, Any]) -> None:
    """One-line account caption above the Quickstart panel.

    Only emitted when signed in — for unauth users the panel itself
    (which leads with /login) tells the story. Username + host with
    no wallet balance, because a wallet read would mean a network
    call on every banner redraw.
    """
    from rich.text import Text
    from rich.align import Align
    host = (meta.get("base_url") or "").replace("https://", "").replace("http://", "")
    line = Text()
    line.append("Signed in as ", style="muted")
    line.append(meta.get("username") or "user", style="bold")
    line.append("  ·  ", style="border_dim")
    line.append(host, style="muted")
    console.print(Align.center(line))
    console.print()


def _render_init_tip_rich(palette: dict[str, Any]) -> None:
    """A one-line hint nudging the user to /init when MCP isn't wired.

    Sits between the status line and the Quickstart panel. Styled in
    Aztea copper (warmth without 'this is an error') so it reads as a
    helpful suggestion. The Quickstart panel also leads with /init when
    MCP isn't registered — this tip is the explanation for *why* /init
    is at the top.
    """
    from rich.text import Text
    from rich.align import Align
    tip = Text()
    tip.append("Aztea MCP isn't wired into Claude Code yet — run ", style="muted")
    tip.append("/init", style=f"bold {palette['accent']}")
    tip.append(" to enable it.", style="muted")
    console.print(Align.center(tip))
    console.print()


def _render_quickstart_rich(meta: dict[str, Any] | None, palette: dict[str, Any]) -> None:
    """Rounded-border panel with a title chip and command/description rows.

    The panel itself is centered under the wordmark; rows inside the panel
    stay left-aligned so the command + description columns line up.
    """
    from rich.text import Text
    from rich.align import Align
    from rich.panel import Panel
    from rich import box

    rows = _authed_shortcuts() if meta else _SHORTCUTS_GUEST
    accent = palette["accent"]
    chip_style = f"bold {palette['chip_fg']} on {palette['chip_bg']}"

    body = Text()
    for i, (cmd, desc) in enumerate(rows):
        if i:
            body.append("\n")
        body.append(f"  {cmd:<{_CMD_COL_WIDTH}}", style=accent)
        body.append(f"  {ARROW} {desc}", style="muted")

    panel = Panel(
        body,
        title=Text(" Quickstart ", style=chip_style),
        title_align="left",
        border_style="border_dim",
        box=box.ROUNDED,
        padding=(1, 2),
        width=_PANEL_WIDTH,
    )
    console.print(Align.center(panel))


def _render_footer_rich() -> None:
    """One-line footer pointing to /help and Ctrl-D, centered."""
    from rich.text import Text
    from rich.align import Align
    foot = Text()
    foot.append("Type ", style="muted")
    foot.append("/help", style="code")
    foot.append(" for commands  ", style="muted")
    foot.append(DOT, style="border_dim")
    foot.append("  ", style="muted")
    foot.append("Ctrl-D", style="code")
    foot.append(" to exit", style="muted")
    console.print()
    console.print(Align.center(foot))
    console.print()


def _render_rich(meta: dict[str, Any] | None) -> None:
    """Wordmark → (status line + init tip when relevant) → Quickstart → footer.

    The status line + init tip only render when signed in. The init tip
    further requires that Aztea MCP is NOT already registered with
    Claude Code — once /init has been run, the tip vanishes.
    """
    palette = _theme_palette()
    _render_wordmark_rich(palette)
    if meta:
        _render_status_line_rich(meta)
        if not _mcp_registered_now():
            _render_init_tip_rich(palette)
    _render_quickstart_rich(meta, palette)
    _render_footer_rich()


# ── Plain-text renderer (no TTY, no Rich) ─────────────────────────────────


def _render_plain(meta: dict[str, Any] | None) -> None:
    """No-Rich fallback. Centers visually against a 72-col reference width
    (most piped / non-TTY readers wrap at 72-80 cols)."""
    rows = _authed_shortcuts() if meta else _SHORTCUTS_GUEST
    pad = max(0, (_PANEL_WIDTH - _LOGO_WIDTH) // 2)
    indent = " " * pad
    for row in _LOGO_ROWS:
        console.print(f"{indent}{row}")
    console.print()
    if meta:
        host = (meta.get("base_url") or "").replace("https://", "").replace("http://", "")
        console.print(f"{indent}Signed in as {meta.get('username') or 'user'}  ·  {host}")
        if not _mcp_registered_now():
            console.print(f"{indent}Aztea MCP isn't wired into Claude Code yet — run /init.")
        console.print()
    console.print(f"{indent}Quickstart")
    for cmd, desc in rows:
        console.print(f"{indent}  {cmd:<{_CMD_COL_WIDTH}}  {desc}")
    console.print()
    console.print(f"{indent}Type /help for commands  ·  Ctrl-D to exit")
    console.print()


def render_banner() -> None:
    """Print the V3 banner. Called once at REPL start and by ``--no-repl``."""
    meta = _signed_in_meta()
    if _HAS_RICH and _is_tty():
        _render_rich(meta)
        return
    _render_plain(meta)


# Back-compat alias for callers (tests, old shim references).
render_splash = render_banner
