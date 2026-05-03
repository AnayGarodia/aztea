"""Branded splash printed when `aztea` is invoked with no command."""
from __future__ import annotations

from .. import __version__
from .output import _HAS_RICH, console

_LOGO = r"""
       _
   __ |/| _________  ___ _
  / _` |  /_  / __/ -_) _` |
  \__,_| /___\__/\__/\__,_|
"""


def render_splash() -> None:
    if _HAS_RICH:
        from rich.text import Text
        from rich.panel import Panel
        from rich.align import Align

        title = Text(_LOGO.strip("\n"), style="terracotta")
        body = Text()
        body.append("the clearing house for agent-to-agent commerce\n\n", style="muted")
        body.append("aztea login         ", style="code")
        body.append("sign in or create an account\n", style="muted")
        body.append("aztea hire <slug>   ", style="code")
        body.append("hire an agent and wait for the result\n", style="muted")
        body.append("aztea mcp install   ", style="code")
        body.append("add Aztea to Claude Code or Cursor\n", style="muted")
        body.append("aztea --help        ", style="code")
        body.append("see every command\n", style="muted")

        console.print()
        console.print(Align.center(title))
        console.print(Align.center(body))
        console.print(Align.center(Text(f"v{__version__}", style="muted")))
        console.print()
        return

    console.print(_LOGO)
    console.print("aztea login        sign in or create an account")
    console.print("aztea hire <slug>  hire an agent and wait for the result")
    console.print("aztea mcp install  add Aztea to Claude Code or Cursor")
    console.print("aztea --help       see every command")
    console.print(f"v{__version__}")
