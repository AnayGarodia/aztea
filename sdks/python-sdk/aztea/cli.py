from __future__ import annotations

import json
import sys
import time
import webbrowser
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

import typer
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.pretty import Pretty
    from rich.table import Table
except ImportError:  # pragma: no cover - exercised indirectly in CI
    class Console:  # type: ignore[no-redef]
        def __init__(self, stderr: bool = False) -> None:
            self._stream = sys.stderr if stderr else sys.stdout

        def print(self, value: Any) -> None:
            print(value, file=self._stream)

        def print_json(self, value: str) -> None:
            print(value, file=self._stream)

    class Panel(str):  # type: ignore[no-redef]
        def __new__(cls, renderable: Any, border_style: str | None = None, title: str | None = None):
            del border_style, title
            return str.__new__(cls, str(renderable))

    class Pretty:  # type: ignore[no-redef]
        def __init__(self, value: Any, expand_all: bool | None = None) -> None:
            del expand_all
            self.value = value

        def __str__(self) -> str:
            return json.dumps(self.value, ensure_ascii=True, default=str, indent=2)

    class Table:  # type: ignore[no-redef]
        def __init__(self, title: str | None = None) -> None:
            self.title = title
            self.rows: list[tuple[Any, ...]] = []

        def add_column(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def add_row(self, *values: Any) -> None:
            self.rows.append(values)

        def __str__(self) -> str:
            lines = [self.title] if self.title else []
            lines.extend(" | ".join(str(v) for v in row) for row in self.rows)
            return "\n".join(lines)

from .client import AzteaClient
from .config import clear_config, load_config, save_config
from .errors import AzteaError

app = typer.Typer(help="Aztea CLI")
agents_app = typer.Typer(help="Browse and inspect agents")
jobs_app = typer.Typer(help="Inspect and follow jobs")
wallet_app = typer.Typer(help="Inspect and fund your wallet")
pipelines_app = typer.Typer(help="Run pipelines")
app.add_typer(agents_app, name="agents")
app.add_typer(jobs_app, name="jobs")
app.add_typer(wallet_app, name="wallet")
app.add_typer(pipelines_app, name="pipelines")

console = Console()
err_console = Console(stderr=True)


def _emit(data: Any, *, json_mode: bool) -> None:
    if json_mode:
        console.print_json(json.dumps(_plain(data), ensure_ascii=True))
        return
    console.print(data)


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {
            item.name: _plain(getattr(value, item.name))
            for item in fields(value)
            if not item.name.startswith("_")
        }
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return {
            key: _plain(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _slugify(value: str) -> str:
    lowered = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    return "-".join(part for part in lowered.split("-") if part)


def _parse_input(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    text = raw.strip()
    if text == "-":
        text = sys.stdin.read().strip()
    elif text.startswith("@"):
        text = Path(text[1:]).read_text().strip()
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
            raise typer.BadParameter("Inline input must be JSON, @file.json, -, or k=v pairs.")
        key, value = token.split("=", 1)
        payload[key] = value
    return payload


def _resolve_settings(
    *,
    api_key: str | None,
    base_url: str | None,
    require_api_key: bool = True,
) -> tuple[str, str | None]:
    cfg = load_config() or {}
    resolved_base = (base_url or cfg.get("base_url") or "https://aztea.ai").rstrip("/")
    resolved_key = api_key or cfg.get("api_key")
    if require_api_key and not resolved_key:
        err_console.print(Panel("No API key configured. Run `aztea login` first.", border_style="red"))
        raise typer.Exit(code=1)
    return resolved_base, resolved_key


def _client(
    *,
    api_key: str | None,
    base_url: str | None,
    require_api_key: bool = True,
) -> AzteaClient:
    resolved_base, resolved_key = _resolve_settings(
        api_key=api_key,
        base_url=base_url,
        require_api_key=require_api_key,
    )
    return AzteaClient(base_url=resolved_base, api_key=resolved_key, client_id="aztea-cli")


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, AzteaError):
        err_console.print(Panel(str(exc), border_style="red", title="Aztea Error"))
        raise typer.Exit(code=1)
    raise exc


def _find_agent_id(client: AzteaClient, slug: str) -> str:
    slug = slug.strip()
    agents = client.list_agents()
    for agent in agents:
        if agent.agent_id == slug:
            return agent.agent_id
    for agent in agents:
        if _slugify(agent.name) == slug:
            return agent.agent_id
    raise typer.BadParameter(f"Unknown agent '{slug}'.")


@app.command()
def login(
    email: Optional[str] = typer.Option(None, help="Account email"),
    password: Optional[str] = typer.Option(None, help="Account password", prompt=False, hide_input=True),
    api_key: Optional[str] = typer.Option(None, help="Use an existing az_ API key instead of password login."),
    base_url: str = typer.Option("https://aztea.ai", help="Aztea server base URL"),
    json_mode: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    try:
        with AzteaClient(base_url=base_url, api_key=api_key, client_id="aztea-cli-login") as client:
            if api_key:
                profile = client.auth.me()
                username = str(profile.get("username") or "")
                save_config(api_key=api_key, base_url=base_url, username=username)
                _emit({"username": username, "base_url": base_url, "saved": True}, json_mode=json_mode)
                return
            login_email = email or typer.prompt("Email")
            login_password = password or typer.prompt("Password", hide_input=True)
            data = client.auth.login(login_email, login_password)
            raw_key = str(data.get("raw_api_key") or "")
            username = str(data.get("username") or "")
            save_config(api_key=raw_key, base_url=base_url, username=username)
            _emit({"username": username, "base_url": base_url, "saved": True}, json_mode=json_mode)
    except Exception as exc:
        _handle_error(exc)


@app.command()
def logout(json_mode: bool = typer.Option(False, "--json")) -> None:
    clear_config()
    _emit({"logged_out": True}, json_mode=json_mode)


@agents_app.command("list")
def agents_list(
    search: Optional[str] = typer.Option(None, help="Search query"),
    max_price: Optional[float] = typer.Option(None, help="Maximum price in USD"),
    min_trust: Optional[float] = typer.Option(None, help="Minimum trust score"),
    api_key: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    json_mode: bool = typer.Option(False, "--json"),
) -> None:
    try:
        with _client(api_key=api_key, base_url=base_url) as client:
            if search:
                max_price_cents = None if max_price is None else round(max_price * 100)
                agents = client.search_agents(search, max_price_cents=max_price_cents, min_trust=min_trust)
            else:
                agents = client.list_agents()
                if max_price is not None:
                    agents = [agent for agent in agents if agent.price_per_call_usd <= max_price]
                if min_trust is not None:
                    agents = [agent for agent in agents if agent.trust_score >= min_trust]
            if json_mode:
                _emit(agents, json_mode=True)
                return
            table = Table(title="Agents")
            table.add_column("Slug")
            table.add_column("Price", justify="right")
            table.add_column("Trust", justify="right")
            table.add_column("Success", justify="right")
            table.add_column("Name")
            for agent in agents:
                table.add_row(
                    _slugify(agent.name),
                    f"${agent.price_per_call_usd:.2f}",
                    f"{agent.trust_score:.0f}",
                    f"{agent.success_rate:.0%}",
                    agent.name,
                )
            console.print(table)
    except Exception as exc:
        _handle_error(exc)


@agents_app.command("show")
def agents_show(
    slug: str,
    api_key: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    json_mode: bool = typer.Option(False, "--json"),
) -> None:
    try:
        with _client(api_key=api_key, base_url=base_url) as client:
            agent = client.get_agent(_find_agent_id(client, slug))
            _emit(agent, json_mode=json_mode)
    except Exception as exc:
        _handle_error(exc)


def _call_agent(
    slug: str,
    input_value: str | None,
    *,
    api_key: str | None,
    base_url: str | None,
    json_mode: bool,
) -> None:
    try:
        payload = _parse_input(input_value)
        with _client(api_key=api_key, base_url=base_url) as client:
            result = client.hire(_find_agent_id(client, slug), payload)
            _emit(
                {
                    "job_id": result.job_id,
                    "cost_cents": result.cost_cents,
                    "output": result.output,
                }
                if json_mode
                else result,
                json_mode=json_mode,
            )
    except Exception as exc:
        _handle_error(exc)


@app.command()
def hire(
    slug: str,
    input_value: Optional[str] = typer.Option(None, "--input", help="@file.json, -, inline JSON, or k=v pairs"),
    api_key: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    json_mode: bool = typer.Option(False, "--json"),
) -> None:
    _call_agent(slug, input_value, api_key=api_key, base_url=base_url, json_mode=json_mode)


@app.command()
def call(
    slug: str,
    input_value: Optional[str] = typer.Argument(None),
    api_key: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    json_mode: bool = typer.Option(False, "--json"),
) -> None:
    _call_agent(slug, input_value, api_key=api_key, base_url=base_url, json_mode=json_mode)


@jobs_app.command("status")
def jobs_status(
    job_id: str,
    api_key: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    full: bool = typer.Option(False, help="Fetch full output payload"),
    json_mode: bool = typer.Option(False, "--json"),
) -> None:
    try:
        with _client(api_key=api_key, base_url=base_url) as client:
            job = client.get_job(job_id)
            data: Any = job.full() if full else job
            if json_mode:
                _emit(data, json_mode=True)
            else:
                console.print(Pretty(data) if full else job)
    except Exception as exc:
        _handle_error(exc)


@jobs_app.command("follow")
def jobs_follow(
    job_id: str,
    api_key: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    json_mode: bool = typer.Option(False, "--json"),
) -> None:
    try:
        with _client(api_key=api_key, base_url=base_url) as client:
            console.print(Panel(f"Following {job_id}", border_style="cyan"))
            for event in client.jobs.stream_messages(job_id):
                _emit(event, json_mode=json_mode)
            final_job = client.get_job(job_id)
            _emit(final_job, json_mode=json_mode)
    except KeyboardInterrupt:
        raise typer.Exit(code=130)
    except Exception as exc:
        _handle_error(exc)


@wallet_app.command("balance")
def wallet_balance(
    api_key: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    json_mode: bool = typer.Option(False, "--json"),
) -> None:
    try:
        with _client(api_key=api_key, base_url=base_url) as client:
            wallet = client.get_wallet()
            _emit(wallet, json_mode=json_mode)
    except Exception as exc:
        _handle_error(exc)


@wallet_app.command("topup")
def wallet_topup(
    amount: float,
    api_key: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    open_browser: bool = typer.Option(True, help="Open the checkout URL in your browser."),
    json_mode: bool = typer.Option(False, "--json"),
) -> None:
    try:
        with _client(api_key=api_key, base_url=base_url) as client:
            session = client.create_topup_session(round(amount * 100))
            if open_browser and isinstance(session.get("checkout_url"), str):
                webbrowser.open(session["checkout_url"])
            _emit(session, json_mode=json_mode)
    except Exception as exc:
        _handle_error(exc)


@pipelines_app.command("run")
def pipelines_run(
    pipeline_id: str,
    input_value: Optional[str] = typer.Option(None, "--input", help="@file.json, -, inline JSON, or k=v pairs"),
    api_key: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    poll_interval: float = typer.Option(2.0),
    json_mode: bool = typer.Option(False, "--json"),
) -> None:
    try:
        payload = _parse_input(input_value)
        with _client(api_key=api_key, base_url=base_url) as client:
            created = client.run_pipeline(pipeline_id, payload)
            run_id = str(created.get("run_id") or "")
            while True:
                status = client.get_pipeline_run(pipeline_id, run_id)
                if json_mode:
                    console.print_json(json.dumps(status, ensure_ascii=True))
                else:
                    console.print(Pretty(status))
                if str(status.get("status") or "") in {"complete", "failed", "cancelled"}:
                    return
                time.sleep(max(0.2, poll_interval))
    except Exception as exc:
        _handle_error(exc)
