"""agents: list, show, search."""
from __future__ import annotations

from typing import Optional

import typer

from .common import (
    ApiKeyOpt,
    BaseUrlOpt,
    JsonOpt,
    build_client,
    find_agent_id,
    handle_error,
    slugify,
)
from .output import emit, spinner, console


def _open_client(**kwargs):
    from . import _client as _factory
    return _factory(**kwargs)


app = typer.Typer(help="Browse and inspect agents.", no_args_is_help=True)


@app.command("list")
def list_cmd(
    search: Optional[str] = typer.Option(None, help="Search query."),
    max_price: Optional[float] = typer.Option(None, help="Maximum price in USD."),
    min_trust: Optional[float] = typer.Option(None, help="Minimum trust score."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """List available agents."""
    try:
        with _open_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Fetching agents", json_mode=json_mode):
                if search:
                    max_price_cents = None if max_price is None else round(max_price * 100)
                    agents = client.search_agents(
                        search,
                        max_price_cents=max_price_cents,
                        min_trust=min_trust,
                    )
                else:
                    agents = client.list_agents()
                    if max_price is not None:
                        agents = [a for a in agents if a.price_per_call_usd <= max_price]
                    if min_trust is not None:
                        agents = [a for a in agents if a.trust_score >= min_trust]

            if json_mode:
                emit(agents, json_mode=True)
                return

            try:
                from rich.table import Table
                table = Table(
                    show_edge=False,
                    show_lines=False,
                    pad_edge=False,
                    padding=(0, 2),
                    header_style="label",
                )
                table.add_column("slug", style="code", no_wrap=True)
                table.add_column("price", justify="right", style="default")
                table.add_column("trust", justify="right", style="muted")
                table.add_column("ok%", justify="right", style="muted")
                table.add_column("name", style="default")
                for agent in agents:
                    table.add_row(
                        slugify(agent.name),
                        f"${agent.price_per_call_usd:.2f}",
                        f"{agent.trust_score:.0f}",
                        f"{agent.success_rate:.0%}",
                        agent.name,
                    )
                console.print()
                console.print(table)
                console.print()
            except ImportError:
                for agent in agents:
                    console.print(
                        f"{slugify(agent.name)}  ${agent.price_per_call_usd:.2f}  {agent.name}"
                    )
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command("show")
def show(
    slug: str,
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Show an agent's full spec."""
    try:
        with _open_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Loading agent", json_mode=json_mode):
                agent = client.get_agent(find_agent_id(client, slug))
            emit(agent, json_mode=json_mode)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command("search")
def search(
    query: str,
    max_price: Optional[float] = typer.Option(None, help="Max price in USD."),
    min_trust: Optional[float] = typer.Option(None, help="Minimum trust score."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Semantic search against the agent registry."""
    list_cmd(
        search=query,
        max_price=max_price,
        min_trust=min_trust,
        api_key=api_key,
        base_url=base_url,
        json_mode=json_mode,
    )
