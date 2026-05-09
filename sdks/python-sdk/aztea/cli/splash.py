"""Branded splash printed when `aztea` is invoked with no command.

Two modes:
    - Signed-out: hero wordmark + minimal "get started" call to action.
    - Signed-in:  compact wordmark + live status pill (user) plus the four
                  most-used shortcuts.

Both modes degrade gracefully when Rich is unavailable or stdout is not a TTY.
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


# Refined wordmark — narrower glyphs, asymmetric weight, kerned for ~60-col
# terminal. Tested in macOS Terminal, iTerm2, VS Code, Alacritty, kitty.
_LOGO = """\
      ▄▀█ ▀▀█ ▀█▀ █▀▀ ▄▀█
      █▀█ ▄▀░ ░█░ ██▄ █▀█"""

_TAGLINE = "the clearing house for agent commerce"
_SUBTAGLINE = "discovery  ·  escrow  ·  signed receipts  ·  recourse"


def _is_tty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _signed_in_meta() -> dict[str, Any] | None:
    cfg = load_config() or {}
    if not cfg.get("api_key"):
        return None
    return {
        "username": cfg.get("username") or "",
        "base_url": cfg.get("base_url") or "https://aztea.ai",
    }


def _render_rich(meta: dict[str, Any] | None) -> None:
    from rich.text import Text
    from rich.align import Align
    from rich.panel import Panel
    from rich import box

    console.print()
    console.print(Align.center(Text(_LOGO, style="bold #14B8A6")))
    console.print()

    tagline = Text(_TAGLINE, style="bold #5EEAD4")
    console.print(Align.center(tagline))
    console.print(Align.center(Text(_SUBTAGLINE, style="muted")))
    console.print()

    # Status strip — compact pill when signed in, ghost CTA when signed out.
    if meta:
        strip = Text()
        strip.append(f"  {BAR} ", style="success")
        strip.append("signed in", style="success")
        strip.append(f"   {DOT}   ", style="border")
        strip.append((meta.get("username") or "user"), style="bold")
        strip.append(f"   {DOT}   ", style="border")
        strip.append(meta.get("base_url") or "", style="muted")
    else:
        strip = Text()
        strip.append(f"  {BAR} ", style="warn")
        strip.append("signed out", style="warn")
        strip.append("   run ", style="muted")
        strip.append("aztea login", style="code")
        strip.append(" to begin", style="muted")
    console.print(Align.center(strip))
    console.print()

    # Action card — the highest-value shortcuts.
    if meta:
        rows = [
            ("aztea status",         f"{ARROW} balance + recent jobs at a glance"),
            ("aztea agents list",    f"{ARROW} browse the specialist marketplace"),
            ("aztea hire <slug>",    f"{ARROW} hire and stream the result"),
            ("aztea publish",        f"{ARROW} list a new agent (interactive wizard)"),
            ("aztea wallet balance", f"{ARROW} funds, escrow, Stripe payouts"),
        ]
    else:
        rows = [
            ("aztea login",          f"{ARROW} sign in and set up MCP"),
            ("aztea agents list",    f"{ARROW} browse the marketplace (no auth)"),
            ("aztea --help",         f"{ARROW} every command, every flag"),
            ("aztea mcp doctor",     f"{ARROW} verify your editor integration"),
        ]

    body = Text()
    for cmd, desc in rows:
        body.append(f"  {cmd:<22}", style="code")
        body.append(f"  {desc}\n", style="muted")
    panel = Panel(
        body,
        title=Text(" quickstart ", style="bold #0F2A2D on #5EEAD4"),
        title_align="left",
        border_style="border_dim",
        box=box.ROUNDED,
        padding=(1, 2),
        width=72,
    )
    console.print(Align.center(panel))

    # Footer — version + docs
    foot = Text()
    foot.append(f"  v{__version__}", style="muted")
    foot.append(f"   {DIAMOND}   ", style="border_dim")
    foot.append("docs.aztea.ai", style="muted")
    foot.append(f"   {DIAMOND}   ", style="border_dim")
    foot.append("aztea --help", style="code")
    console.print(Align.center(foot))
    console.print()


def _render_plain(meta: dict[str, Any] | None) -> None:
    console.print(_LOGO)
    console.print(_TAGLINE)
    console.print(_SUBTAGLINE)
    console.print()
    if meta:
        console.print(f"  {CHECK} signed in as {meta.get('username') or 'user'} ({meta.get('base_url')})")
        console.print("  aztea agents list      browse the marketplace")
        console.print("  aztea hire <slug>      hire a specialist")
        console.print("  aztea wallet balance   inspect funds")
        console.print("  aztea status           dashboard")
    else:
        console.print("  signed out — run `aztea login` to begin")
        console.print("  aztea login            sign in and set up MCP")
        console.print("  aztea agents list      browse the marketplace")
        console.print("  aztea --help           every command")
    console.print()
    console.print(f"v{__version__}")


def render_splash() -> None:
    meta = _signed_in_meta()
    if _HAS_RICH and _is_tty():
        _render_rich(meta)
        return
    _render_plain(meta)
