"""Branded splash printed when `aztea` is invoked with no command.

Two modes:
    - Signed-out: hero wordmark + minimal "get started" call to action.
    - Signed-in:  same hero plus a live status pill (user/host) and a
                  quickstart card pinned to the top-five shortcuts.

# OWNS: the visual hero shown by `aztea` (no subcommand).
# NOT OWNS: any subcommand body, network I/O, or persisted state.
# INVARIANTS:
#   - Pure layout: never makes a network call. Reads `load_config()` once.
#   - Width-stable: hero must fit a 72-col terminal (we don't probe size).
#   - Degrades cleanly when Rich is missing or stdout is not a TTY.
# DECISIONS:
#   - Hero wordmark uses ANSI Shadow glyphs. It's iconic, kerns predictably,
#     and renders identically across iTerm2 / Terminal.app / Alacritty / kitty.
#   - We render the wordmark with a vertical teal-gradient (mint → teal → ink)
#     so it reads as 3D-shaded rather than a flat block.
#   - The "ticker" strip below the wordmark mirrors the four product pillars
#     (discovery / escrow / receipts / recourse) and replaces the older
#     subtagline. It evokes a marketplace tape, fitting the brand promise.
"""
from __future__ import annotations

import sys
from typing import Any

from .. import __version__
from ..config import load_config
from .output import (
    ARROW,
    BAR,
    CHECK,
    DIAMOND,
    DOT,
    _HAS_RICH,
    console,
)


# ── Hero wordmark ──────────────────────────────────────────────────────────
# ANSI-Shadow style. Six rows × 42 cols. We render each row in a slightly
# different teal so the block reads as illuminated from above — the lightest
# row is the highlight, the darkest is the cast shadow.

_LOGO_ROWS: tuple[str, ...] = (
    " █████╗ ███████╗████████╗███████╗ █████╗ ",
    "██╔══██╗╚══███╔╝╚══██╔══╝██╔════╝██╔══██╗",
    "███████║  ███╔╝    ██║   █████╗  ███████║",
    "██╔══██║ ███╔╝     ██║   ██╔══╝  ██╔══██║",
    "██║  ██║███████╗   ██║   ███████╗██║  ██║",
    "╚═╝  ╚═╝╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝",
)
# Mint highlight → teal body → deep teal shadow. Index-aligned with _LOGO_ROWS.
_LOGO_GRADIENT: tuple[str, ...] = (
    "bold #99F6E4",
    "bold #5EEAD4",
    "bold #2DD4BF",
    "bold #14B8A6",
    "bold #0F766E",
    "bold #115E59",
)
_LOGO_WIDTH = 41

_TAGLINE = "the clearing house for agent commerce"

# Marketplace ticker — four product pillars separated by chevrons.
_TICKER_PILLARS: tuple[str, ...] = ("discovery", "escrow", "signed receipts", "recourse")

# Quickstart shortcuts. (cmd, description) tuples; cmd column is fixed-width.
_SHORTCUTS_AUTHED: tuple[tuple[str, str], ...] = (
    ("aztea status",         "balance + recent jobs at a glance"),
    ("aztea agents list",    "browse the specialist marketplace"),
    ("aztea hire <slug>",    "hire and stream the result"),
    ("aztea publish",        "list a new agent (interactive wizard)"),
    ("aztea wallet balance", "funds, escrow, Stripe payouts"),
)
_SHORTCUTS_GUEST: tuple[tuple[str, str], ...] = (
    ("aztea login",       "sign in and set up MCP"),
    ("aztea agents list", "browse the marketplace (no auth)"),
    ("aztea --help",      "every command, every flag"),
    ("aztea mcp doctor",  "verify your editor integration"),
)
_PANEL_WIDTH = 74
_CMD_COL_WIDTH = 22


def _is_tty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _signed_in_meta() -> dict[str, Any] | None:
    # Pulls just the display fields we need; never raises on a missing config.
    cfg = load_config() or {}
    if not cfg.get("api_key"):
        return None
    return {
        "username": cfg.get("username") or "",
        "base_url": cfg.get("base_url") or "https://aztea.ai",
    }


def _render_hero() -> None:
    """Print the gradient-shaded ANSI-Shadow wordmark, centered."""
    from rich.text import Text
    from rich.align import Align
    console.print()
    for row, style in zip(_LOGO_ROWS, _LOGO_GRADIENT):
        console.print(Align.center(Text(row, style=style)))
    console.print()


def _render_tagline() -> None:
    """Print the tagline + a marketplace ticker of product pillars."""
    from rich.text import Text
    from rich.align import Align
    console.print(Align.center(Text(_TAGLINE, style="bold #5EEAD4")))
    ticker = Text()
    for i, pillar in enumerate(_TICKER_PILLARS):
        if i:
            ticker.append(f"  {DIAMOND}  ", style="border_dim")
        ticker.append(pillar, style="muted")
    console.print(Align.center(ticker))
    console.print()


def _render_status_strip(meta: dict[str, Any] | None) -> None:
    """Print the signed-in pill (or the signed-out CTA) below the tagline."""
    from rich.text import Text
    from rich.align import Align
    strip = Text()
    if meta:
        strip.append(f"  {BAR} ", style="success")
        strip.append("signed in", style="success")
        strip.append(f"   {DOT}   ", style="border")
        strip.append(meta.get("username") or "user", style="bold")
        strip.append(f"   {DOT}   ", style="border")
        strip.append(meta.get("base_url") or "", style="muted")
    else:
        strip.append(f"  {BAR} ", style="warn")
        strip.append("signed out", style="warn")
        strip.append("   run ", style="muted")
        strip.append("aztea login", style="code")
        strip.append(" to begin", style="muted")
    console.print(Align.center(strip))
    console.print()


def _render_quickstart(meta: dict[str, Any] | None) -> None:
    """Print the bordered quickstart panel with code + description columns."""
    from rich.text import Text
    from rich.align import Align
    from rich.panel import Panel
    from rich import box
    rows = _SHORTCUTS_AUTHED if meta else _SHORTCUTS_GUEST
    body = Text()
    for i, (cmd, desc) in enumerate(rows):
        if i:
            body.append("\n")
        body.append(f"  {cmd:<{_CMD_COL_WIDTH}}", style="code")
        body.append(f"  {ARROW} {desc}", style="muted")
    panel = Panel(
        body,
        title=Text(" quickstart ", style="bold #0F2A2D on #5EEAD4"),
        title_align="left",
        border_style="border_dim",
        box=box.ROUNDED,
        padding=(1, 2),
        width=_PANEL_WIDTH,
    )
    console.print(Align.center(panel))


def _render_footer() -> None:
    """Print the tape-style version + docs footer below the panel."""
    from rich.text import Text
    from rich.align import Align
    foot = Text()
    foot.append(f"  v{__version__}", style="muted")
    foot.append(f"   {DIAMOND}   ", style="border_dim")
    foot.append("docs.aztea.ai", style="muted")
    foot.append(f"   {DIAMOND}   ", style="border_dim")
    foot.append("aztea --help", style="code")
    console.print(Align.center(foot))
    console.print()


def _render_rich(meta: dict[str, Any] | None) -> None:
    _render_hero()
    _render_tagline()
    _render_status_strip(meta)
    _render_quickstart(meta)
    _render_footer()


def _render_plain(meta: dict[str, Any] | None) -> None:
    # Plain mode: no Rich, no colour, no centering. Used when piped or no TTY.
    for row in _LOGO_ROWS:
        console.print(row)
    console.print(_TAGLINE)
    console.print("  ·  ".join(_TICKER_PILLARS))
    console.print()
    shortcuts = _SHORTCUTS_AUTHED if meta else _SHORTCUTS_GUEST
    if meta:
        console.print(f"  {CHECK} signed in as {meta.get('username') or 'user'} ({meta.get('base_url')})")
    else:
        console.print("  signed out — run `aztea login` to begin")
    for cmd, desc in shortcuts:
        console.print(f"  {cmd:<{_CMD_COL_WIDTH}}  {desc}")
    console.print()
    console.print(f"v{__version__}  ·  docs.aztea.ai  ·  aztea --help")


def render_splash() -> None:
    meta = _signed_in_meta()
    if _HAS_RICH and _is_tty():
        _render_rich(meta)
        return
    _render_plain(meta)
