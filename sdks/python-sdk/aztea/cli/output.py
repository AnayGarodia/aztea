"""Shared visual language for the Aztea CLI.

Every command imports its console + helpers from here so the look-and-feel
stays consistent. Brand-derived palette is mapped onto Rich named colors
that work across light + dark terminals.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from typing import Any, Iterator

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme
    _HAS_RICH = True
except ImportError:  # pragma: no cover - rich is a hard dep but be defensive
    _HAS_RICH = False

# ── Palette ────────────────────────────────────────────────────────────────
# Brand maps onto Rich-compatible color names. Hex values are commented for
# reference; Rich's theme system handles mapping to the terminal's actual
# capabilities (truecolor / 256 / 16).

_THEME = {
    # primary
    "accent":     "#063F43",   # deep ink-teal
    "terracotta": "#C65F3F",   # warm
    "gold":       "#A5863A",
    "ink":        "#102B2F",
    "ivory":      "#FBF7EF",
    # status
    "success":    "green3",
    "warn":       "yellow3",
    "error":      "red3",
    "info":       "cyan",
    "muted":      "grey50",
    # surfaces
    "border":     "#736953",
    "code":       "#C65F3F",
    "heading":    "bold #063F43",
    "label":      "bold #C65F3F",
}

if _HAS_RICH:
    _rich_theme = Theme(_THEME)
    console = Console(theme=_rich_theme, soft_wrap=False, highlight=False)
    err_console = Console(theme=_rich_theme, stderr=True, soft_wrap=False, highlight=False)
else:
    class _Fallback:
        def __init__(self, stderr: bool = False) -> None:
            self._s = sys.stderr if stderr else sys.stdout
        def print(self, *a: Any, **k: Any) -> None:
            print(*[str(x) for x in a], file=self._s)
        def print_json(self, value: str) -> None:
            print(value, file=self._s)
        def status(self, *_a: Any, **_k: Any):
            class _N:
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *_): return False
            return _N()
    console = _Fallback()           # type: ignore[assignment]
    err_console = _Fallback(stderr=True)  # type: ignore[assignment]


# ── Symbols ────────────────────────────────────────────────────────────────

CHECK = "✓"
CROSS = "✗"
ARROW = "→"
DOT   = "·"
BULLET = "•"


# ── Spinner ────────────────────────────────────────────────────────────────

@contextmanager
def spinner(label: str, *, json_mode: bool = False) -> Iterator[None]:
    """Show a Rich status spinner during a network call.

    Suppressed in --json mode (machine-readable output must stay clean) and
    when stdout is not a TTY (CI logs).
    """
    if json_mode or not _HAS_RICH or not sys.stdout.isatty():
        yield
        return
    with console.status(f"[muted]{label}…[/muted]", spinner="dots"):
        yield


# ── Display primitives ─────────────────────────────────────────────────────

def banner(title: str, subtitle: str | None = None) -> None:
    """Branded section header."""
    if not _HAS_RICH:
        console.print(title)
        if subtitle:
            console.print(subtitle)
        return
    console.print()
    console.print(Text(title, style="heading"))
    if subtitle:
        console.print(Text(subtitle, style="muted"))
    console.print()


def success(message: str, *, detail: str | None = None) -> None:
    if _HAS_RICH:
        line = Text.assemble((f"{CHECK} ", "success"), (message, "default"))
        console.print(line)
        if detail:
            console.print(Text(f"  {detail}", style="muted"))
    else:
        console.print(f"{CHECK} {message}")
        if detail:
            console.print(f"  {detail}")


def info(message: str) -> None:
    if _HAS_RICH:
        console.print(Text.assemble((f"{ARROW} ", "info"), (message, "default")))
    else:
        console.print(f"{ARROW} {message}")


def warn(message: str) -> None:
    if _HAS_RICH:
        err_console.print(Text.assemble(("! ", "warn"), (message, "default")))
    else:
        err_console.print(f"! {message}")


def error(message: str, *, hint: str | None = None, code: str | None = None) -> None:
    """Print an error to stderr in a branded panel.

    `hint` is a one-line remediation suggestion ("run `aztea login`").
    `code` is a machine-readable error code if available.
    """
    if not _HAS_RICH:
        err_console.print(f"{CROSS} {message}")
        if hint:
            err_console.print(f"  → {hint}")
        return
    body = Text(message, style="default")
    if code:
        body = Text.assemble((f"[{code}] ", "muted"), body)
    if hint:
        body.append("\n")
        body.append(Text.assemble((f"{ARROW} ", "info"), (hint, "muted")))
    panel = Panel(
        body,
        title=Text("aztea", style="error"),
        title_align="left",
        border_style="error",
        padding=(0, 1),
    )
    err_console.print(panel)


# ── JSON / data emission ───────────────────────────────────────────────────

def emit(data: Any, *, json_mode: bool, pretty: bool = True) -> None:
    """Print structured data. JSON mode stays line-clean for piping."""
    if json_mode:
        console.print_json(json.dumps(_plain(data), ensure_ascii=True))
        return
    if pretty:
        # Try a friendly object → table view; fall back to pretty repr.
        if hasattr(data, "__rich__") or hasattr(data, "__rich_console__"):
            console.print(data)
            return
        console.print(_plain(data) if not _HAS_RICH else data)
    else:
        console.print(data)


def kv_table(rows: list[tuple[str, str]], *, title: str | None = None) -> None:
    """Two-column key/value display, used by `whoami`, `mcp doctor`, etc."""
    if not _HAS_RICH:
        for k, v in rows:
            console.print(f"{k}: {v}")
        return
    table = Table(
        show_header=False,
        show_edge=False,
        box=None,
        padding=(0, 2),
        title=title,
        title_style="heading",
        title_justify="left",
    )
    table.add_column(justify="left", style="muted", no_wrap=True)
    table.add_column(justify="left", style="default")
    for key, val in rows:
        table.add_row(key, val)
    console.print(table)


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {
            f.name: _plain(getattr(value, f.name))
            for f in fields(value)
            if not f.name.startswith("_")
        }
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return {k: _plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return value


# Re-export Rich primitives for advanced use inside command modules.
if _HAS_RICH:
    __all__ = [
        "console", "err_console", "spinner",
        "banner", "success", "info", "warn", "error",
        "emit", "kv_table",
        "CHECK", "CROSS", "ARROW", "DOT", "BULLET",
        "Panel", "Table", "Text",
    ]
else:
    __all__ = [
        "console", "err_console", "spinner",
        "banner", "success", "info", "warn", "error",
        "emit", "kv_table",
        "CHECK", "CROSS", "ARROW", "DOT", "BULLET",
    ]
