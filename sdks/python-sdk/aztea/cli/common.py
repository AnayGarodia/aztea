"""Shared helpers for every CLI command module."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from ..client import AzteaClient
from ..config import load_config
from ..errors import (
    AzteaError,
    AuthenticationError,
    InsufficientFundsError,
    NotFoundError,
    RateLimitError,
)
from .output import error


# ── Input parsing ──────────────────────────────────────────────────────────

def parse_input(raw: str | None) -> dict[str, Any]:
    """Parse `--input` value: JSON literal, @file.json, '-' for stdin, or k=v pairs."""
    if raw is None:
        return {}
    text = raw.strip()
    if text == "-":
        text = sys.stdin.read().strip()
    elif text.startswith("@"):
        text = Path(text[1:]).expanduser().read_text().strip()
    if not text:
        return {}
    if text.startswith("{"):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise typer.BadParameter("Input JSON must be an object.")
        return parsed
    payload: dict[str, Any] = {}
    for token in text.split():
        if "=" not in token:
            raise typer.BadParameter(
                "Inline input must be JSON, @file.json, '-', or k=v pairs."
            )
        key, value = token.split("=", 1)
        payload[key] = value
    return payload


def slugify(value: str) -> str:
    lowered = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    return "-".join(part for part in lowered.split("-") if part)


def find_agent_id(client: AzteaClient, slug: str) -> str:
    """Resolve `slug` to an agent_id. Accepts UUID or kebab-cased name."""
    slug = slug.strip()
    agents = client.list_agents()
    for agent in agents:
        if agent.agent_id == slug:
            return agent.agent_id
    for agent in agents:
        if slugify(agent.name) == slug:
            return agent.agent_id
    raise typer.BadParameter(f"Unknown agent '{slug}'. Try `aztea agents list`.")


# ── Settings / client construction ─────────────────────────────────────────

def resolve_settings(
    *,
    api_key: str | None,
    base_url: str | None,
    require_api_key: bool = True,
) -> tuple[str, str | None]:
    cfg = load_config() or {}
    resolved_base = (base_url or cfg.get("base_url") or "https://aztea.ai").rstrip("/")
    resolved_key = api_key or cfg.get("api_key")
    if require_api_key and not resolved_key:
        error(
            "No API key configured.",
            hint="Run `aztea login` to sign in, or pass --api-key.",
            code="auth.no_key",
        )
        raise typer.Exit(code=1)
    return resolved_base, resolved_key


def build_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    require_api_key: bool = True,
) -> AzteaClient:
    resolved_base, resolved_key = resolve_settings(
        api_key=api_key, base_url=base_url, require_api_key=require_api_key
    )
    # Resolve AzteaClient via the package namespace so tests that patch
    # `aztea.cli.AzteaClient` see their mock.
    from . import AzteaClient as _AzteaClient  # noqa: PLC0415
    return _AzteaClient(
        base_url=resolved_base,
        api_key=resolved_key,
        client_id="aztea-cli",
    )


# ── Error funnel ───────────────────────────────────────────────────────────

def handle_error(exc: Exception) -> None:
    """Convert SDK errors into a friendly panel + exit code, then re-raise."""
    if isinstance(exc, AuthenticationError):
        error(
            str(exc) or "Authentication failed.",
            hint="Run `aztea login` to refresh your key.",
            code="auth.invalid",
        )
        raise typer.Exit(code=1)
    if isinstance(exc, InsufficientFundsError):
        error(
            str(exc) or "Insufficient wallet balance.",
            hint="Run `aztea wallet topup <amount>` to add credits.",
            code="wallet.insufficient",
        )
        raise typer.Exit(code=1)
    if isinstance(exc, NotFoundError):
        error(str(exc) or "Resource not found.", code="not_found")
        raise typer.Exit(code=1)
    if isinstance(exc, RateLimitError):
        error(
            str(exc) or "Rate limited.",
            hint="Wait a moment and retry.",
            code="rate_limit",
        )
        raise typer.Exit(code=1)
    if isinstance(exc, AzteaError):
        error(str(exc), code="aztea")
        raise typer.Exit(code=1)
    raise exc


# ── Standard CLI options reused across commands ────────────────────────────

ApiKeyOpt = typer.Option(None, "--api-key", help="Override the saved API key.", show_default=False)
BaseUrlOpt = typer.Option(None, "--base-url", help="Override the API base URL.", show_default=False)
JsonOpt = typer.Option(False, "--json", help="Emit machine-readable JSON.")
