"""agents: list, show, search."""
from __future__ import annotations

from typing import Optional

import typer

from .common import (
    ApiKeyOpt,
    BaseUrlOpt,
    JsonOpt,
    find_agent_id,
    handle_error,
    slugify,
)
from .output import (
    BAR,
    DOT,
    _HAS_RICH,
    console,
    emit,
    money,
    spinner,
    trust_gauge,
    price_tier,
)


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

            _render_agent_table(agents, query=search)
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


def _render_agent_table(agents, *, query: Optional[str] = None) -> None:
    """Premium agent table — trust gauges, price tiers, last-active, totals row."""
    if not _HAS_RICH:
        for agent in agents:
            console.print(
                f"{slugify(agent.name)}  ${agent.price_per_call_usd:.2f}  {agent.name}"
            )
        return

    from rich.table import Table
    from rich.text import Text as _Text
    from rich.panel import Panel
    from rich import box

    if not agents:
        body = _Text()
        body.append("no specialists matched", style="muted")
        if query:
            body.append("\nquery: ", style="muted")
            body.append(query, style="code")
        console.print()
        console.print(Panel(
            body, border_style="border_dim", box=box.ROUNDED,
            title=_Text(" marketplace ", style="bold #0F2A2D on #5EEAD4"),
            title_align="left", padding=(1, 2),
        ))
        console.print()
        return

    # Header strip — count, query, sort
    header = _Text()
    header.append(f"  {len(agents)} specialist{'s' if len(agents) != 1 else ''}", style="bold")
    if query:
        header.append("   matching ", style="muted")
        header.append(f'"{query}"', style="code")
    header.append(f"   {DOT}   sorted by ", style="muted")
    header.append("trust", style="default")

    table = Table(
        show_edge=False,
        show_lines=False,
        pad_edge=False,
        padding=(0, 1),
        header_style="label",
        box=box.SIMPLE_HEAD,
        border_style="border_dim",
    )
    table.add_column("",          width=1, no_wrap=True)
    table.add_column("SLUG",      style="code", no_wrap=True)
    table.add_column("NAME",      style="default", no_wrap=False, max_width=32)
    table.add_column("PRICE",     justify="right", no_wrap=True)
    table.add_column("",          width=4, no_wrap=True)  # price tier marker
    table.add_column("TRUST",     justify="left", no_wrap=True)
    table.add_column("SUCCESS",   justify="right", style="muted", no_wrap=True)

    sorted_agents = sorted(
        agents,
        key=lambda a: (-(getattr(a, "trust_score", 0) or 0), getattr(a, "price_per_call_usd", 0) or 0),
    )

    for agent in sorted_agents:
        price_usd = float(getattr(agent, "price_per_call_usd", 0) or 0)
        trust = float(getattr(agent, "trust_score", 0) or 0)
        success = float(getattr(agent, "success_rate", 0) or 0)

        if trust >= 80:
            mark_style = "success"
        elif trust >= 50:
            mark_style = "gold"
        elif trust >= 25:
            mark_style = "warn"
        else:
            mark_style = "muted"

        table.add_row(
            _Text(BAR, style=mark_style),
            slugify(agent.name),
            agent.name,
            money(round(price_usd * 100)),
            price_tier(price_usd),
            trust_gauge(trust),
            f"{success:.0%}",
        )

    console.print()
    console.print(header)
    console.print(table)

    # Footer hint
    foot = _Text()
    foot.append(f"  {DOT} ", style="border")
    foot.append("hire any specialist with ", style="muted")
    foot.append("aztea hire <slug>", style="code")
    console.print(foot)
    console.print()
