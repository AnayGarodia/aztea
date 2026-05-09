"""Interactive `aztea publish` wizard.

Drops the user into a guided flow when `aztea publish` is invoked with no
path argument. Handles the three publishing paths in plain English:

    1. Hosted skill (*.skill.md)        — runs on Aztea, no infra
    2. External webhook (agent.md)      — manifest pointing at your URL
    3. Python handler (.py)             — scaffold + your endpoint URL

Generates a starter file in the current working directory, then dispatches
into the existing `publish` flow so the safety scanner and registration
logic stay identical to the path-given case.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from string import Template
from typing import Any, Callable

import typer

from ..config import load_config
from .common import resolve_settings, slugify
from .output import (
    ARROW,
    BULLET,
    CHECK,
    banner,
    console,
    err_console,
    info,
    success,
    warn,
)
from . import prompts as _p

# Template files live alongside this module; loaded lazily on first use so
# import is cheap.
_TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# Public entry point — called from publish.py when path is None.
# ---------------------------------------------------------------------------


def run_wizard(
    *,
    api_key: str | None,
    base_url: str | None,
    json_mode: bool,
    from_template_only: bool = False,
) -> Path:
    """Run the wizard and return the path to the generated file.

    The caller (`publish.publish`) hands the returned path back into the
    normal publish pipeline (`detect()` + `_static_scan` + `_register`),
    so the wizard does NOT call the safety scanner itself.

    If `from_template_only=True`, generate the file and exit without
    publishing. Returning the path lets the caller decide.
    """
    if json_mode:
        err_console.print(
            "[error]✗[/error] The wizard is interactive; rerun with a file "
            "path or drop --json to use the questionnaire."
        )
        raise typer.Exit(code=2)
    if not _p._is_tty():
        err_console.print(
            "[error]✗[/error] `aztea publish` without a path requires an "
            "interactive terminal. Pipe a file path or use a TTY."
        )
        raise typer.Exit(code=2)

    resolved_base, saved_key = resolve_settings(
        api_key=api_key, base_url=base_url, require_api_key=False
    )
    if not saved_key:
        err_console.print(
            "[error]✗[/error] You're not signed in.\n"
            "  Run [code]aztea login[/code] first — that points the CLI at "
            "https://aztea.ai and saves your API key.\n"
            "  Or set [code]AZTEA_BASE_URL=http://localhost:8000[/code] for a "
            "local sandbox."
        )
        raise typer.Exit(code=2)

    cfg = load_config() or {}
    username = cfg.get("username") or None
    _greet(resolved_base, username)

    kind = _ask_kind()
    if kind == 1:
        path = _wizard_skill_md()
    elif kind == 2:
        path = _wizard_agent_md()
    else:
        path = _wizard_python_handler()

    success(f"Saved {path}")
    if from_template_only:
        info(
            "Template ready. Edit it as you like, then run "
            f"[code]aztea publish {path}[/code] when you're done."
        )
    return path


# ---------------------------------------------------------------------------
# Greeting + kind selector
# ---------------------------------------------------------------------------


def _greet(base_url: str, username: str | None) -> None:
    banner(
        "aztea publish",
        subtitle="Let's get your agent listed. I'll ask a few questions.",
    )
    console.print()
    who = f" (signed in as [accent]{username}[/accent])" if username else ""
    console.print(
        f"  [muted]Publishing to[/muted] [code]{base_url}[/code]{who}."
    )
    if "aztea.ai" in base_url:
        console.print(
            "  [muted]Override with[/muted] "
            "[code]AZTEA_BASE_URL=http://localhost:8000[/code] "
            "[muted]for a local sandbox.[/muted]"
        )
    console.print()


def _ask_kind() -> int:
    return _p.select_numeric(
        "What kind of agent are you publishing?",
        options=[
            (
                "Hosted skill",
                "markdown-only, runs on Aztea (easiest)",
            ),
            (
                "External webhook",
                "manifest pointing at a URL you host",
            ),
            (
                "Python handler",
                ".py file with def handler(payload)",
            ),
        ],
        default=1,
    )


# ---------------------------------------------------------------------------
# Path 1 — hosted SKILL.md
# ---------------------------------------------------------------------------


def _wizard_skill_md() -> Path:
    name = _p.ask(
        "Name (lowercase, dashes ok)",
        default=_default_name_from_cwd(),
        validator=_p.slug_validator,
    )
    description = _p.ask(
        "One-sentence description",
        validator=_p.description_validator,
        hint="What does your agent do, in plain English?",
    )
    emoji = _p.ask(
        "Emoji (optional, press Enter to skip)",
        default="",
        validator=_p.emoji_validator,
    )
    body = _p.multiline_or_editor(
        "Now write your skill body — the prompt the LLM sees.",
        initial=_skill_body_seed(name, description),
        suffix=".skill.md",
    )
    if not body:
        body = "Describe what your agent should do here."

    rendered = Template(_load_template("skill_md.template")).substitute(
        name=name,
        description=description,
        body=body,
        emoji_line=f"\nemoji: {emoji}" if emoji else "",
    )
    return _write_file(f"{name}.skill.md", rendered)


def _skill_body_seed(name: str, description: str) -> str:
    return (
        f"# {name}\n\n"
        f"{description}\n\n"
        "## Instructions\n"
        "Describe how the LLM should respond. Keep it short and concrete.\n"
    )


# ---------------------------------------------------------------------------
# Path 2 — agent.md manifest
# ---------------------------------------------------------------------------


def _wizard_agent_md() -> Path:
    name = _p.ask(
        "Name (lowercase, dashes ok)",
        default=_default_name_from_cwd(),
        validator=_p.slug_validator,
    )
    description = _p.ask(
        "One-sentence description",
        validator=_p.description_validator,
    )
    endpoint_url = _p.ask(
        "Endpoint URL (https://…)",
        validator=_p.url_validator,
        hint="The HTTPS URL where your agent receives POST requests.",
    )
    input_field = _p.ask(
        "Input field name",
        default="task",
        validator=_p.identifier_validator,
    )
    input_desc = _p.ask(
        "Input field description",
        default="What you want the agent to do.",
        validator=_p.description_validator,
    )
    output_field = _p.ask(
        "Output field name",
        default="result",
        validator=_p.identifier_validator,
    )
    output_desc = _p.ask(
        "Output field description",
        default="The agent's response.",
        validator=_p.description_validator,
    )
    price = float(
        _p.ask(
            "Price per call (USD)",
            default="0.05",
            validator=_p.price_validator,
        )
    )
    tags = _parse_tags(
        _p.ask("Tags (comma-separated, optional)", default="", validator=_p.optional)
    )

    metadata = {
        "name": name,
        "description": description,
        "endpoint_url": endpoint_url,
        "price_per_call_usd": price,
        "tags": tags,
        "input_schema": {
            "type": "object",
            "properties": {
                input_field: {
                    "type": "string",
                    "title": "Input",
                    "description": input_desc,
                }
            },
            "required": [input_field],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                output_field: {
                    "type": "string",
                    "description": output_desc,
                }
            },
            "required": [output_field],
        },
    }
    rendered = Template(_load_template("agent_md.template")).substitute(
        name=name,
        metadata_json=json.dumps(metadata, indent=2),
    )
    return _write_file(f"{name}.agent.md", rendered)


# ---------------------------------------------------------------------------
# Path 3 — Python handler
# ---------------------------------------------------------------------------


def _wizard_python_handler() -> Path:
    name = _p.ask(
        "Name (lowercase, dashes ok)",
        default=_default_name_from_cwd(),
        validator=_p.slug_validator,
    )
    description = _p.ask(
        "One-sentence description",
        validator=_p.description_validator,
    )
    rendered = Template(_load_template("handler_py.template")).substitute(
        name=name,
        description=description,
    )
    body_choice = _p.confirm(
        "Open the starter handler in your editor?", default=True
    )
    if body_choice:
        edited = _p.multiline_or_editor(
            "Edit the handler.",
            initial=rendered,
            suffix=".py",
            editor_default_yes=True,
        )
        if edited:
            rendered = edited
    file_name = name.replace("-", "_") + ".py"
    path = _write_file(file_name, rendered)
    info(
        f"Deploy this file at the URL you'll provide next, then we'll list it "
        "as an external endpoint."
    )
    return path


# ---------------------------------------------------------------------------
# Friendly error mapping for safety-scanner blocks
# ---------------------------------------------------------------------------

_REMEDIATION: dict[str, str] = {
    "skill.prompt_injection": (
        "Skill bodies are prompts the LLM treats as instructions. The phrase "
        "we matched looks like an attempt to override safety scaffolding. "
        "Rephrase as a description of behavior rather than a directive — "
        'e.g. change "ignore previous instructions" → "this skill summarizes '
        'the previous user turn".'
    ),
    "skill.embedded_api_key": (
        "We found a string that looks like an API key in the skill body. "
        "Hardcoded keys leak immediately. Take secrets via the caller's input "
        "or your own backend instead."
    ),
    "python.blocked_import": (
        "Your handler imports a module we don't allow for in-process "
        "listings (subprocess, socket, pickle, …). If you genuinely need it, "
        "host the handler yourself and register an external-endpoint "
        "listing instead."
    ),
    "python.blocked_builtin": (
        "Your handler calls eval/exec/compile or similar dynamic code. We "
        "block these to keep the in-process worker safe. Replace with "
        "explicit logic, or self-host the agent."
    ),
    "python.blocked_os_call": (
        "Your handler calls os.system / os.popen / os.exec*. Shell-out paths "
        "aren't allowed in-process; self-host the agent if you need them."
    ),
    "manifest.endpoint_is_aztea": (
        "Your endpoint URL points at an Aztea-owned host. Third-party agents "
        "must run on a host you control."
    ),
}


def remediation_for(code: str) -> str | None:
    return _REMEDIATION.get(code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_name_from_cwd() -> str:
    raw = Path.cwd().name or "my-agent"
    cleaned = slugify(raw)
    if not re.match(r"^[a-z]", cleaned):
        cleaned = "my-" + cleaned
    return cleaned[:64] or "my-agent"


def _load_template(name: str) -> str:
    path = _TEMPLATES_DIR / name
    return path.read_text(encoding="utf-8")


def _write_file(name: str, content: str) -> Path:
    target = Path.cwd() / name
    if target.exists():
        if not _p.confirm(
            f"{target.name} already exists. Overwrite?", default=False
        ):
            err_console.print(
                f"[error]✗[/error] Refusing to overwrite {target}; "
                "rename it or move into a fresh directory."
            )
            raise typer.Exit(code=1)
    target.write_text(content, encoding="utf-8")
    return target


def _parse_tags(raw: str) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


__all__ = ["run_wizard", "remediation_for"]
