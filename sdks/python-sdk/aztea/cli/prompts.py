"""Interactive prompt helpers for the Aztea CLI wizards.

Thin wrappers over Rich's Prompt/Confirm so wizard call sites stay readable.
Falls back to plain `input()` when Rich is unavailable. Validators are simple
predicates returning (ok, error_message); the prompt re-asks until valid.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterable

try:
    from rich.prompt import Confirm, Prompt
    _HAS_RICH = True
except ImportError:  # pragma: no cover — rich is a hard dep but be defensive
    _HAS_RICH = False

from .output import console, err_console

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

Validator = Callable[[str], tuple[bool, str]]


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ---------------------------------------------------------------------------
# Core prompts
# ---------------------------------------------------------------------------


def ask(
    question: str,
    *,
    default: str | None = None,
    validator: Validator | None = None,
    hint: str | None = None,
) -> str:
    """Ask a free-form text question. Loops on validator failure."""
    while True:
        if hint:
            console.print(f"  [muted]{hint}[/muted]")
        if _HAS_RICH:
            answer = Prompt.ask(
                f"[label]?[/label] {question}",
                default=default,
                show_default=default is not None,
                console=console,
            )
        else:
            prompt = f"? {question}"
            if default is not None:
                prompt += f" [{default}]"
            prompt += ": "
            raw = input(prompt)
            answer = raw.strip() or (default or "")
        answer = (answer or "").strip()
        if validator is not None:
            ok, err = validator(answer)
            if not ok:
                err_console.print(f"  [error]✗[/error] {err}")
                continue
        return answer


def confirm(question: str, *, default: bool = True) -> bool:
    if _HAS_RICH:
        return Confirm.ask(
            f"[label]?[/label] {question}",
            default=default,
            console=console,
        )
    suffix = " [Y/n]" if default else " [y/N]"
    raw = input(f"? {question}{suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def select_numeric(
    question: str,
    options: Iterable[tuple[str, str]],
    *,
    default: int | None = None,
) -> int:
    """Ask the user to pick one of a numbered list of (label, description) pairs.

    Returns the 1-based index of the selected option.
    """
    opts = list(options)
    if not opts:
        raise ValueError("select_numeric requires at least one option")
    console.print(f"[label]?[/label] {question}")
    for idx, (label, description) in enumerate(opts, start=1):
        marker = "→" if (default == idx) else " "
        console.print(f"  {marker} [accent]{idx}.[/accent] [bold]{label}[/bold]")
        if description:
            console.print(f"     [muted]{description}[/muted]")
    while True:
        raw = ask(
            "Choose",
            default=str(default) if default else None,
        )
        if not raw.isdigit():
            err_console.print("  [error]✗[/error] Enter a number from the list.")
            continue
        choice = int(raw)
        if 1 <= choice <= len(opts):
            return choice
        err_console.print(
            f"  [error]✗[/error] Pick a number between 1 and {len(opts)}."
        )


def multiline_or_editor(
    question: str,
    *,
    initial: str = "",
    suffix: str = ".md",
    editor_default_yes: bool = True,
) -> str:
    """Ask the user to provide a multi-line block of text.

    Offers an editor (`$EDITOR`, falling back to `nano`/`vi`) or a paste-then-EOF
    fallback for environments without one. `initial` seeds the editor buffer.
    """
    use_editor = confirm(
        f"{question} Open your editor?",
        default=editor_default_yes,
    )
    if use_editor:
        return _spawn_editor(initial, suffix=suffix)
    console.print(
        f"  [muted]Paste your content below. End with a single line containing "
        "EOF and press Enter.[/muted]"
    )
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "EOF":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _spawn_editor(initial: str, *, suffix: str) -> str:
    editor = (
        os.environ.get("AZTEA_EDITOR")
        or os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
    )
    if not editor:
        for candidate in ("nano", "vi", "vim"):
            if _which(candidate):
                editor = candidate
                break
    if not editor:
        err_console.print(
            "  [warn]![/warn] No editor found ($EDITOR unset). Falling back "
            "to paste-with-EOF mode."
        )
        return multiline_or_editor(
            "Provide your content.",
            initial=initial,
            suffix=suffix,
            editor_default_yes=False,
        )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(initial)
        tmp_path = Path(tmp.name)
    try:
        subprocess.run([editor, str(tmp_path)], check=False)
        return tmp_path.read_text(encoding="utf-8").strip()
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _which(cmd: str) -> bool:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(d) / cmd
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return True
    return False


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


def slug_validator(value: str) -> tuple[bool, str]:
    if not value:
        return False, "Name is required."
    if not _SLUG_RE.match(value):
        return False, (
            "Use lowercase letters, digits, and dashes only. Start with a "
            "letter; 2–64 chars."
        )
    return True, ""


def description_validator(value: str) -> tuple[bool, str]:
    if not value:
        return False, "Description is required."
    if len(value.split()) < 3:
        return False, "Use at least three words so callers know what your agent does."
    return True, ""


def price_validator(value: str) -> tuple[bool, str]:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False, "Enter a number, e.g. 0.02."
    if price < 0:
        return False, "Price must be non-negative."
    if price > 25:
        return False, "Price cap is $25.00 per call. Lower the number."
    return True, ""


def url_validator(value: str) -> tuple[bool, str]:
    if not value:
        return False, "URL is required."
    if not (value.startswith("http://") or value.startswith("https://")):
        return False, "URL must start with http:// or https://."
    if value.startswith("http://") and not os.environ.get(
        "ALLOW_PRIVATE_OUTBOUND_URLS"
    ):
        return False, "Use https:// (set ALLOW_PRIVATE_OUTBOUND_URLS=1 to allow http for local testing)."
    return True, ""


def identifier_validator(value: str) -> tuple[bool, str]:
    if not value:
        return False, "Field name is required."
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]{0,47}$", value):
        return False, "Use a Python-style identifier (letters/digits/underscore, start with a letter)."
    return True, ""


def emoji_validator(value: str) -> tuple[bool, str]:
    # Empty (skip) is fine; otherwise just trim.
    if value and len(value) > 8:
        return False, "Emoji is too long; pick a single character."
    return True, ""


def optional(value: str) -> tuple[bool, str]:
    return True, ""


__all__ = [
    "ask",
    "confirm",
    "select_numeric",
    "multiline_or_editor",
    "slug_validator",
    "description_validator",
    "price_validator",
    "url_validator",
    "identifier_validator",
    "emoji_validator",
    "optional",
]
