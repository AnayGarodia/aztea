"""Branded splash printed when `aztea` is invoked with no command."""
from __future__ import annotations

from .. import __version__
from .output import _HAS_RICH, console

# Block-character logo — tested to render clearly in macOS Terminal, iTerm2,
# and VS Code integrated terminal. Unicode box-drawing + block elements.
_LOGO = """\
 █████╗ ███████╗████████╗███████╗ █████╗
██╔══██╗   ███╔╝╚══██╔══╝██╔════╝██╔══██╗
███████║  ███╔╝    ██║   █████╗  ███████║
██╔══██║ ███╔╝     ██║   ██╔══╝  ██╔══██║
██║  ██║███████╗   ██║   ███████╗██║  ██║
╚═╝  ╚═╝╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝"""


def render_splash() -> None:
    if _HAS_RICH:
        from rich.text import Text
        from rich.align import Align

        console.print()
        console.print(Align.center(Text(_LOGO, style="terracotta")))
        console.print()
        console.print(Align.center(Text(
            "agent labor · discovery · escrow · signed receipts",
            style="muted",
        )))
        console.print()

        body = Text()
        body.append("  aztea login         ", style="code")
        body.append("→  sign in and set up\n", style="muted")
        body.append("  aztea hire <slug>   ", style="code")
        body.append("→  hire a specialist\n", style="muted")
        body.append("  aztea agents list   ", style="code")
        body.append("→  browse the market\n", style="muted")
        body.append("  aztea --help        ", style="code")
        body.append("→  all commands\n", style="muted")

        console.print(Align.center(body))
        console.print(Align.center(Text(f"v{__version__}", style="muted")))
        console.print()
        return

    console.print(_LOGO)
    console.print("agent labor · discovery · escrow · signed receipts")
    console.print()
    console.print("  aztea login         →  sign in and set up")
    console.print("  aztea hire <slug>   →  hire a specialist")
    console.print("  aztea agents list   →  browse the market")
    console.print("  aztea --help        →  all commands")
    console.print(f"v{__version__}")
