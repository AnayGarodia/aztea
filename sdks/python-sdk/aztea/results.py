from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from typing import Any

try:
    from rich.console import Group
    from rich.panel import Panel
    from rich.pretty import Pretty
    from rich.table import Table
    from rich.text import Text
    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised indirectly in CI
    Group = Panel = Pretty = Table = Text = None  # type: ignore[assignment]
    _RICH_AVAILABLE = False


def _to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return {
            item.name: _to_plain(getattr(value, item.name))
            for item in fields(value)
            if not item.name.startswith("_")
        }
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_plain(item) for key, item in value.items()}
    return value


def summarize_value(value: Any, *, max_chars: int = 240, max_items: int = 8) -> str:
    plain = _to_plain(value)
    if isinstance(plain, dict):
        keys = list(plain.keys())
        preview = ", ".join(str(key) for key in keys[:max_items])
        suffix = " ..." if len(keys) > max_items else ""
        return "{" + preview + suffix + "}"
    if isinstance(plain, list):
        suffix = " ..." if len(plain) > max_items else ""
        return f"[{len(plain)} items{suffix}]"
    if isinstance(plain, str):
        text = plain.strip().replace("\n", " ")
        if len(text) > max_chars:
            return text[: max_chars - 1] + "…"
        return text
    rendered = json.dumps(plain, ensure_ascii=True) if isinstance(plain, (bool, int, float, type(None))) else str(plain)
    if len(rendered) > max_chars:
        return rendered[: max_chars - 1] + "…"
    return rendered


def record_table(title: str, rows: list[tuple[str, Any]]) -> Any:
    if not _RICH_AVAILABLE:
        return {
            "title": title,
            "rows": [(label, summarize_value(value)) for label, value in rows],
        }
    table = Table.grid(padding=(0, 1))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(style="white")
    for label, value in rows:
        table.add_row(label, summarize_value(value))
    return Panel(table, title=title, border_style="cyan")


def job_payload_panel(title: str, payload: Any) -> Any:
    if not _RICH_AVAILABLE:
        return {"title": title, "payload": _to_plain(payload)}
    return Pretty(_to_plain(payload), expand_all=True)


def stack_renderables(*items: Any) -> Any:
    if not _RICH_AVAILABLE:
        return list(items)
    return Group(*items)


def render_status(status: str) -> Any:
    normalized = str(status or "unknown").replace("_", " ")
    if not _RICH_AVAILABLE:
        return normalized.upper()
    styles = {
        "complete": "green",
        "failed": "red",
        "running": "yellow",
        "pending": "blue",
        "awaiting clarification": "cyan",
    }
    return Text(normalized.upper(), style=styles.get(normalized, "white"))
