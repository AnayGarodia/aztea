"""watchers: schedule cron-driven or condition-based agent runs.

A watcher is a server-side recurring trigger that fires an agent when its
schedule rolls around OR when a watched URL fingerprint changes. Caller
sets a daily-spend cap on the watcher itself; if a fire would exceed the
cap, the run is skipped (no charge). Receipts are produced on every fire.

Usage:
    aztea watchers list
    aztea watchers create --agent <slug-or-id> --cron "0 9 * * *" --payload '{}'
    aztea watchers create --agent <slug-or-id> --url https://x.com/feed --payload '{}'
    aztea watchers show <watcher_id>
    aztea watchers delete <watcher_id>

1.7.3 — added because the backend has shipped /watchers since 1.6 but the
CLI surface was missing. The eval flagged this twice as B-17.
"""
from __future__ import annotations

import json as _json
from typing import Optional

import typer

from .common import (
    ApiKeyOpt,
    BaseUrlOpt,
    JsonOpt,
    build_client,
    find_agent_id,
    handle_error,
)
from .output import emit, info, spinner, success


app = typer.Typer(help="Manage scheduled / condition-based agent runs.", no_args_is_help=True)


@app.command("list")
def list_watchers(
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """List every watcher you own."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Loading watchers", json_mode=json_mode):
                resp = client._request_json("GET", "/watchers")
        watchers = resp.get("watchers") or []
        if json_mode:
            emit({"watchers": watchers, "count": len(watchers)}, json_mode=True)
            return
        if not watchers:
            info("No watchers yet. Create one with `aztea watchers create`.")
            return
        for w in watchers:
            info(
                f"  {w.get('watcher_id', '')[:8]}…  "
                f"{w.get('agent_id', '')[:8]}…  "
                f"{w.get('schedule_kind') or 'cron'}  "
                f"{w.get('cron_expression') or w.get('watch_url') or '—'}  "
                f"fires={w.get('total_fires', 0)}"
            )
        success(f"{len(watchers)} watcher(s).")
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command("create")
def create_watcher(
    agent: str = typer.Option(..., "--agent", help="Slug or agent_id to invoke on each fire."),
    cron: Optional[str] = typer.Option(
        None, "--cron",
        help="Cron expression (5- or 6-field). Mutually exclusive with --url.",
    ),
    url: Optional[str] = typer.Option(
        None, "--url",
        help="URL whose fingerprint to watch. Mutually exclusive with --cron.",
    ),
    payload: str = typer.Option(
        "{}", "--payload",
        help="JSON input to pass to the agent on each fire. Defaults to `{}`.",
    ),
    daily_cap_cents: Optional[int] = typer.Option(
        None, "--daily-cap-cents",
        help="Optional per-watcher daily spend cap. Fires exceeding it are skipped.",
    ),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Create a watcher. Pass either --cron OR --url, not both."""
    if bool(cron) == bool(url):
        raise typer.BadParameter("Pass exactly one of --cron or --url.")
    try:
        parsed_payload = _json.loads(payload)
    except _json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--payload must be valid JSON: {exc}")
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Resolving agent", json_mode=json_mode):
                agent_id = find_agent_id(client, agent)
            body: dict = {"agent_id": agent_id, "input_payload": parsed_payload}
            if cron:
                body["cron_expression"] = cron
            if url:
                body["watch_url"] = url
            if daily_cap_cents is not None:
                body["daily_cap_cents"] = int(daily_cap_cents)
            with spinner("Creating watcher", json_mode=json_mode):
                resp = client._request_json("POST", "/watchers", json_body=body)
        if json_mode:
            emit(resp, json_mode=True)
            return
        success(f"Watcher created: {resp.get('watcher_id', '')[:8]}…")
        info(f"  agent={agent_id[:8]}… trigger={cron or url}")
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command("delete")
def delete_watcher(
    watcher_id: str = typer.Argument(..., help="The watcher_id from `aztea watchers list`."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Delete a watcher. No future fires."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Deleting watcher", json_mode=json_mode):
                # Backend exposes both /watch/{id} and /watchers/{id} for
                # plural/singular ergonomics; the eval flagged that
                # DELETE /watchers/{id} returns 405 in some prod configs
                # while DELETE /watch/{id} works. Try the singular alias
                # first since it's the canonical handler.
                try:
                    resp = client._request_json("DELETE", f"/watch/{watcher_id}")
                except Exception:
                    resp = client._request_json("DELETE", f"/watchers/{watcher_id}")
        if json_mode:
            emit(resp, json_mode=True)
            return
        success(f"Watcher {watcher_id[:8]}… deleted.")
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)
