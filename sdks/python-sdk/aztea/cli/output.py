"""Shared visual language for the Aztea CLI.

Every command imports its console + helpers from here so the look-and-feel
stays consistent. Brand-derived palette is mapped onto Rich named colors
that work across light + dark terminals.

Design language:
    Palette: deep ink-teal accent, vivid teal primary, mint-gold for value,
             muted slate for chrome.
    Glyphs:  ✓ ✗ → · • ◆ ▎  used as semantic markers, not decoration.
    Hierarchy: HERO (display) → HEADING (small-caps) → label → body → muted.

Backwards-compat: every name from the previous iteration of this module is
still exported. New helpers are additive.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from typing import Any, Iterator, Sequence

try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme
    from rich.padding import Padding
    from rich.align import Align
    from rich import box as _box
    _HAS_RICH = True
except ImportError:  # pragma: no cover - rich is a hard dep but be defensive
    _HAS_RICH = False

# ── Palette ────────────────────────────────────────────────────────────────
# Brand maps onto Rich-compatible color names. Hex values are the source of
# truth; Rich's theme system handles mapping to the terminal's actual
# capabilities (truecolor / 256 / 16).

_THEME = {
    # primary — teal-led palette
    "accent":     "#063F43",
    "teal":       "#14B8A6",
    "teal_dim":   "#0F766E",
    "gold":       "#7DD3C4",
    "mint":       "#5EEAD4",
    "ink":        "#102B2F",
    "ivory":      "#FBF7EF",
    # status
    "success":    "#22C55E",
    "warn":       "#EAB308",
    "error":      "#EF4444",
    "info":       "#38BDF8",
    "muted":      "grey50",
    "dim":        "grey39",
    "subtle":     "grey62",
    # surfaces
    "border":     "#334155",
    "border_dim": "#1F2937",
    "code":       "#5EEAD4",
    "heading":    "bold #14B8A6",
    "label":      "bold #7DD3C4",
    "hero":       "bold #5EEAD4",
    "kbd":        "#7DD3C4 on #0F2A2D",
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
DIAMOND = "◆"
BAR = "▎"
SPARK_FULL = "█"
SPARK_HALF = "▌"
SPARK_EMPTY = "░"
CHEVRON = "›"
EM_DASH = "—"


def _is_tty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


# ── Layout primitives ──────────────────────────────────────────────────────

def divider(width: int = 56, *, style: str = "border_dim") -> None:
    """Quiet horizontal rule between sections."""
    if not _HAS_RICH:
        console.print("─" * width)
        return
    console.print(Text("─" * width, style=style))


def section(title: str, subtitle: str | None = None) -> None:
    """Small-caps section header. Use to introduce a logical group."""
    if not _HAS_RICH:
        console.print(title.upper())
        if subtitle:
            console.print(subtitle)
        return
    console.print()
    head = Text()
    head.append(title.upper(), style="heading")
    head.append("  ")
    head.append(EM_DASH, style="border")
    if subtitle:
        head.append("  ")
        head.append(subtitle, style="muted")
    console.print(head)
    console.print(Text("─" * 56, style="border_dim"))


def step(n: int, total: int, label: str) -> None:
    """Numbered step indicator for multi-step flows."""
    if not _HAS_RICH:
        console.print(f"[{n}/{total}] {label}")
        return
    counter = Text(f"  {n}", style="bold #7DD3C4")
    counter.append(f" / {total}", style="muted")
    counter.append(f"   {label}", style="default")
    console.print(counter)


def setup_complete(rows: list[tuple[str, str]]) -> None:
    """Receipt-style summary at the end of a setup flow."""
    if not _HAS_RICH:
        for label, value in rows:
            console.print(f"  {label}: {value}")
        return
    for label, value in rows:
        line = Text(f"  {label}  ", style="muted")
        line.append(value, style="default")
        console.print(line)


def login_intro() -> None:
    """Branded panel printed at the top of `aztea login`."""
    if not _HAS_RICH or not _is_tty():
        return
    title = Text("welcome to aztea", style="hero")
    body = Text()
    pieces = [
        ("agent labor", "default"),
        ("discovery", "default"),
        ("escrow", "default"),
        ("signed receipts", "default"),
    ]
    for i, (txt, sty) in enumerate(pieces):
        if i:
            body.append(f"  {DOT}  ", style="border")
        body.append(txt, style=sty)
    panel = Panel(
        Align.center(body),
        title=title,
        title_align="left",
        border_style="teal_dim",
        box=_box.ROUNDED,
        padding=(1, 3),
        width=64,
    )
    console.print()
    console.print(Align.center(panel))
    console.print()


def styled_prompt(label: str, *, password: bool = False, default: str | None = None) -> str:
    """Branded input prompt. Falls back to typer.prompt without Rich."""
    if not _HAS_RICH or not _is_tty():
        import typer
        return typer.prompt(label, default=default or None, hide_input=password)
    from rich.prompt import Prompt
    arrow = Text(f"  {ARROW}  ", style="teal")
    label_text = Text(label, style="bold")
    prompt_text = arrow + label_text
    return Prompt.ask(prompt_text, password=password, default=default, show_default=bool(default))


# ── Spinner ────────────────────────────────────────────────────────────────

@contextmanager
def spinner(label: str, *, json_mode: bool = False) -> Iterator[None]:
    """Show a Rich status spinner during a network call."""
    if json_mode or not _HAS_RICH or not _is_tty():
        yield
        return
    with console.status(f"[muted]{label}…[/muted]", spinner="dots", spinner_style="teal"):
        yield


# ── Display primitives ─────────────────────────────────────────────────────

def banner(title: str, subtitle: str | None = None) -> None:
    """Branded section header (legacy — prefer `section`)."""
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
    """Print an error to stderr in a branded panel."""
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
        title=Text("aztea error", style="error"),
        title_align="left",
        border_style="error",
        box=_box.ROUNDED,
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
        if hasattr(data, "__rich__") or hasattr(data, "__rich_console__"):
            console.print(data)
            return
        console.print(_plain(data) if not _HAS_RICH else data)
    else:
        console.print(data)


def kv_table(rows: list[tuple[str, str]], *, title: str | None = None) -> None:
    """Two-column key/value display."""
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
    table.add_column(justify="right", style="muted", no_wrap=True)
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


# ── Money + time formatters ────────────────────────────────────────────────

def money(cents: int | float | None, *, dim_zero: bool = True):
    """Format integer cents as a USD string with brand-tier coloring."""
    if cents is None:
        return Text("—", style="muted") if _HAS_RICH else "—"
    amount = float(cents) / 100.0
    txt = f"${amount:,.2f}"
    if not _HAS_RICH:
        return txt
    if amount == 0 and dim_zero:
        return Text(txt, style="muted")
    if amount >= 1.00:
        return Text(txt, style="bold #7DD3C4")
    if amount >= 0.10:
        return Text(txt, style="gold")
    return Text(txt, style="default")


def hero_money(cents: int | float | None, *, currency: str = "USD") -> None:
    """Render a hero-sized balance as the visual centerpiece of a card."""
    if cents is None:
        amount_str = "—"
    else:
        amount_str = f"${float(cents) / 100:,.2f}"
    if not _HAS_RICH:
        console.print(f"balance  {amount_str}  {currency}")
        return
    line = Text()
    line.append(amount_str, style="hero")
    line.append("  ")
    line.append(currency, style="muted")
    console.print()
    console.print(Padding(line, (0, 0, 0, 2)))
    console.print()


def big_balance(amount_str: str) -> None:
    """Legacy: prominent gold balance line for wallet display."""
    if not _HAS_RICH:
        console.print(f"balance  {amount_str}")
        return
    line = Text(amount_str, style="hero")
    console.print()
    console.print(Padding(line, (0, 0, 0, 2)))
    console.print()


def relative_time(iso_or_epoch: Any) -> str:
    """Best-effort 'just now / 3m ago / 2h ago / 1d ago' formatter."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    when: _dt.datetime | None = None
    if isinstance(iso_or_epoch, (int, float)):
        try:
            when = _dt.datetime.fromtimestamp(float(iso_or_epoch), _dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return "—"
    elif isinstance(iso_or_epoch, str) and iso_or_epoch:
        try:
            when = _dt.datetime.fromisoformat(iso_or_epoch.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return iso_or_epoch
    if when is None:
        return "—"
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 5:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 30:
        return f"{secs // 86400}d ago"
    if secs < 86400 * 365:
        return f"{secs // (86400 * 30)}mo ago"
    return f"{secs // (86400 * 365)}y ago"


# ── Status pills + gauges ──────────────────────────────────────────────────

_STATUS_PALETTE: dict[str, str] = {
    "pending":                "warn",
    "queued":                 "warn",
    "running":                "info",
    "claimed":                "info",
    "complete":               "success",
    "completed":              "success",
    "failed":                 "error",
    "cancelled":              "muted",
    "canceled":               "muted",
    "awaiting_clarification": "warn",
    "verified":               "success",
    "unverified":             "muted",
}


def status_pill(status: str | None):
    """Coloured left-bar badge: ` ▎running `."""
    label = (status or "—").lower()
    palette = _STATUS_PALETTE.get(label, "muted")
    pretty = label.replace("_", " ")
    if not _HAS_RICH:
        return pretty
    return Text.assemble((f"{BAR} ", palette), (pretty, palette))


def trust_gauge(score: float | int | None, *, segments: int = 5):
    """Render a 5-segment ▰▱ gauge sized to the trust score (0–100)."""
    if score is None:
        score = 0.0
    pct = max(0.0, min(100.0, float(score)))
    filled = int(round(pct / (100.0 / segments)))
    if not _HAS_RICH:
        return ("█" * filled) + ("░" * (segments - filled))
    if pct >= 80:
        style = "success"
    elif pct >= 50:
        style = "gold"
    elif pct >= 25:
        style = "warn"
    else:
        style = "muted"
    out = Text()
    for i in range(segments):
        if i < filled:
            out.append(SPARK_FULL, style=style)
        else:
            out.append(SPARK_EMPTY, style="border_dim")
    out.append(f" {pct:>3.0f}", style="muted")
    return out


def price_tier(price_usd: float | None):
    """Compact $/$$/$$$ tier marker."""
    if price_usd is None:
        return "—"
    if price_usd <= 0:
        return Text("free", style="success") if _HAS_RICH else "free"
    if price_usd < 0.05:
        return Text("$", style="muted") if _HAS_RICH else "$"
    if price_usd < 0.50:
        return Text("$$", style="gold") if _HAS_RICH else "$$"
    return Text("$$$", style="bold #7DD3C4") if _HAS_RICH else "$$$"


def mini_bar(fraction: float, width: int = 12, *, style: str = "teal"):
    """Tiny progress bar: █████░░░░░ used inline next to numbers."""
    f = max(0.0, min(1.0, float(fraction or 0.0)))
    filled = int(round(f * width))
    if not _HAS_RICH:
        return ("█" * filled) + ("░" * (width - filled))
    out = Text()
    out.append(SPARK_FULL * filled, style=style)
    out.append(SPARK_EMPTY * (width - filled), style="border_dim")
    return out


# ── Receipt panel ──────────────────────────────────────────────────────────

def receipt_panel(
    title: str,
    rows: Sequence[tuple[str, Any]],
    *,
    footer: str | None = None,
    seal: bool = False,
    border_style: str = "teal_dim",
) -> None:
    """Bordered receipt-style summary used after a hire / settle / withdraw."""
    if not _HAS_RICH:
        console.print(title)
        for label, value in rows:
            console.print(f"  {label}: {value}")
        if footer:
            console.print(footer)
        if seal:
            console.print(f"  {CHECK} signed receipt verified")
        return
    table = Table(
        show_header=False, show_edge=False, box=None, padding=(0, 2),
    )
    table.add_column(justify="right", style="muted", no_wrap=True)
    table.add_column(justify="left", style="default")
    for label, value in rows:
        cell = value if isinstance(value, Text) else Text(str(value))
        table.add_row(label, cell)
    pieces: list[Any] = [table]
    if seal:
        seal_line = Text()
        seal_line.append(f"  {CHECK} ", style="success")
        seal_line.append("signed receipt", style="bold #7DD3C4")
        seal_line.append("  ·  ", style="border")
        seal_line.append("ed25519 verified", style="muted")
        pieces.append(Text(""))
        pieces.append(seal_line)
    if footer:
        pieces.append(Text(""))
        pieces.append(Text(f"  {footer}", style="muted"))
    panel = Panel(
        Group(*pieces),
        title=Text(title, style="heading"),
        title_align="left",
        border_style=border_style,
        box=_box.ROUNDED,
        padding=(1, 1),
    )
    console.print()
    console.print(panel)
    console.print()


def hint_card(lines: Sequence[tuple[str, str]]) -> None:
    """Two-column shortcut card: command on the left, description on the right."""
    if not _HAS_RICH:
        for cmd, desc in lines:
            console.print(f"  {cmd}  {desc}")
        return
    table = Table(
        show_header=False, show_edge=False, box=None, padding=(0, 2),
    )
    table.add_column(style="code", no_wrap=True)
    table.add_column(style="muted")
    for cmd, desc in lines:
        table.add_row(cmd, desc)
    console.print(table)


def kbd(label: str):
    """Inline key-cap-styled chip."""
    if not _HAS_RICH:
        return f"[{label}]"
    return Text(f" {label} ", style="kbd")


# ── Re-exports ─────────────────────────────────────────────────────────────

if _HAS_RICH:
    __all__ = [
        "console", "err_console", "spinner",
        "banner", "section", "success", "info", "warn", "error",
        "emit", "kv_table",
        "divider", "step", "setup_complete", "big_balance", "hero_money",
        "money", "relative_time",
        "status_pill", "trust_gauge", "price_tier", "mini_bar",
        "receipt_panel", "hint_card", "kbd",
        "login_intro", "styled_prompt",
        "CHECK", "CROSS", "ARROW", "DOT", "BULLET", "DIAMOND", "BAR", "CHEVRON",
        "Panel", "Table", "Text", "Group", "Padding", "Align",
    ]
else:
    __all__ = [
        "console", "err_console", "spinner",
        "banner", "section", "success", "info", "warn", "error",
        "emit", "kv_table",
        "divider", "step", "setup_complete", "big_balance", "hero_money",
        "money", "relative_time",
        "status_pill", "trust_gauge", "price_tier", "mini_bar",
        "receipt_panel", "hint_card", "kbd",
        "login_intro", "styled_prompt",
        "CHECK", "CROSS", "ARROW", "DOT", "BULLET", "DIAMOND", "BAR", "CHEVRON",
    ]
