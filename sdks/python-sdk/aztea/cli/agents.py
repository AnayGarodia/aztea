"""agents: list, show, search.

The default ``aztea agents list`` view groups agents by category so a new
user can scan by intent ("security stuff", "code execution") instead of by
name. Pass ``--flat`` to get the legacy alphabetic table (kept for scripts).
"""
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
)


# Order in which category buckets render. Anything not on this list is
# bucketed under "Other" and rendered last. Security leads because it's
# the headline use case (see CLAUDE.md "Top use cases" — security audits
# are the documented #1).
_CATEGORY_ORDER: tuple[str, ...] = (
    "Security",
    "Code Execution",
    "Quality",
    "Web",
    "Research",
    "Developer Tools",
    "QA",
)
_OTHER_BUCKET = "Other"
_UNCATEGORIZED = "Uncategorized"


def _open_client(**kwargs):
    """Open a registry client without forcing a saved API key.

    Browsing the marketplace is a public action on aztea.ai — listing
    agents, viewing specs, searching the catalogue all work without
    sign-in. Requiring an API key client-side blocked these commands from
    the REPL for unauthenticated users, even though the server would
    have happily answered them. Pass ``require_api_key=False`` through to
    the build_client factory so the call goes out and the server decides.
    """
    from . import _client as _factory
    kwargs.setdefault("require_api_key", False)
    return _factory(**kwargs)


app = typer.Typer(help="Browse and inspect agents.", no_args_is_help=True)


_AGENTS_LIST_EPILOG = (
    "Default view groups agents by category (Security, Code Execution, Quality, "
    "Web, Research, Developer Tools). Pass --flat for the legacy single table, "
    "--category to filter to one bucket, --free to see only $0.00 agents."
)


@app.command("list", epilog=_AGENTS_LIST_EPILOG)
def list_cmd(
    search: Optional[str] = typer.Option(None, help="Search query."),
    max_price: Optional[float] = typer.Option(None, help="Maximum price in USD."),
    min_trust: Optional[float] = typer.Option(None, help="Minimum trust score."),
    category: Optional[str] = typer.Option(
        None,
        "--category",
        help="Filter to a single category (e.g. Security, Code Execution).",
    ),
    flat: bool = typer.Option(
        False,
        "--flat",
        help="Render one ranked table instead of grouping by category.",
    ),
    free: bool = typer.Option(
        False,
        "--free",
        help="Show only $0.00 agents — the free-tier gateway demos.",
    ),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """List available agents, grouped by category by default."""
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
                if free:
                    agents = [a for a in agents if (a.price_per_call_usd or 0) <= 0]
                if category:
                    wanted = category.strip().lower()
                    agents = [
                        a for a in agents
                        if (getattr(a, "category", "") or "").lower() == wanted
                    ]

            if json_mode:
                # Inject the kebab-cased slug into each row so programmatic
                # consumers can hire by slug. The server response only has
                # agent_id + name; we derive slug client-side via slugify(name).
                # `Agent` is a slots dataclass — dict(a) raises, so use asdict.
                from dataclasses import asdict, is_dataclass
                from .common import slugify
                rows = []
                for a in agents:
                    if hasattr(a, "model_dump"):
                        raw = a.model_dump()
                    elif is_dataclass(a):
                        raw = asdict(a)
                    elif isinstance(a, dict):
                        raw = dict(a)
                    else:
                        raw = {k: v for k, v in vars(a).items() if not k.startswith("_")}
                    name = str(raw.get("name") or "")
                    derived = slugify(name)
                    if derived and not raw.get("slug"):
                        raw["slug"] = derived
                    rows.append(raw)
                emit(rows, json_mode=True)
                return

            # Categorized view is the default unless the caller passed --flat,
            # passed --search (results are already a ranked list), or filtered
            # to one category (no point grouping a single bucket).
            if flat or search or category:
                _render_agent_table(agents, query=search)
            else:
                _render_agent_categories(agents)
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
                # 1.7.4 — :.4f then strip trailing zeros so sub-cent prices
                # render honestly ($0.004 not $0.00).
                f"{slugify(agent.name)}  "
                f"${agent.price_per_call_usd:.4f}".rstrip('0').rstrip('.')
                + f"  {agent.name}"
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
            title=_Text(" marketplace ", style="bold #0C1F22 on #7EB9B0"),
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
            # 1.7.4 — pass raw cents (with sub-cent precision) so the
            # money() formatter can render values like 0.4¢ as $0.004.
            # Pre-1.7.4 `round(price_usd * 100)` collapsed all sub-cent
            # prices to 0 cents BEFORE the formatter saw them, defeating
            # the 1.7.3 B-15 fix at the wrong layer.
            money(price_usd * 100),
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


def _bucket_for(agent) -> str:
    """Return the display bucket name for an agent.

    Spec metadata uses canonical bucket names (Security, Code Execution, …).
    Anything not in ``_CATEGORY_ORDER`` falls into "Other" so unknown
    categories still surface — silently dropping them would hide new agents.
    """
    raw = (getattr(agent, "category", None) or "").strip()
    if not raw:
        return _UNCATEGORIZED
    for known in _CATEGORY_ORDER:
        if known.lower() == raw.lower():
            return known
    return _OTHER_BUCKET


def _group_by_category(agents) -> dict[str, list]:
    """Group agents into category buckets in render order. Empty buckets dropped."""
    buckets: dict[str, list] = {name: [] for name in _CATEGORY_ORDER}
    buckets[_OTHER_BUCKET] = []
    buckets[_UNCATEGORIZED] = []
    for agent in agents:
        buckets[_bucket_for(agent)].append(agent)
    # Drop empties so the output doesn't have lonely headers.
    return {name: items for name, items in buckets.items() if items}


def _render_agent_categories(agents) -> None:
    """Bucketed marketplace view: one mini-table per category, in fixed order."""
    if not _HAS_RICH:
        # Plain text fallback — still grouped, just no Rich formatting.
        for bucket, items in _group_by_category(agents).items():
            console.print(f"\n{bucket} ({len(items)})")
            for agent in items:
                price_usd = float(getattr(agent, "price_per_call_usd", 0) or 0)
                console.print(
                    f"  {slugify(agent.name):<28}  "
                    f"${price_usd:.4f}".rstrip('0').rstrip('.')
                    + f"  {agent.name}"
                )
        return

    from rich.table import Table
    from rich.text import Text as _Text
    from rich.panel import Panel
    from rich import box

    grouped = _group_by_category(agents)
    if not grouped:
        body = _Text("no agents available", style="muted")
        console.print()
        console.print(Panel(
            body, border_style="border_dim", box=box.ROUNDED,
            title=_Text(" marketplace ", style="bold #0C1F22 on #7EB9B0"),
            title_align="left", padding=(1, 2),
        ))
        console.print()
        return

    total = sum(len(items) for items in grouped.values())
    header = _Text()
    header.append(f"  {total} specialist{'s' if total != 1 else ''}", style="bold")
    header.append("   across   ", style="muted")
    header.append(f"{len(grouped)}", style="default")
    header.append(" categories", style="muted")
    console.print()
    console.print(header)

    for bucket, items in grouped.items():
        sub_header = _Text()
        sub_header.append(f"\n  {bucket}", style="heading")
        sub_header.append(f"   ({len(items)})", style="muted")
        console.print(sub_header)

        table = Table(
            show_edge=False,
            show_lines=False,
            pad_edge=False,
            padding=(0, 1),
            header_style="label",
            box=box.SIMPLE_HEAD,
            border_style="border_dim",
        )
        table.add_column("",        width=1, no_wrap=True)
        table.add_column("SLUG",    style="code", no_wrap=True)
        table.add_column("NAME",    style="default", no_wrap=False, max_width=36)
        table.add_column("PRICE",   justify="right", no_wrap=True)
        table.add_column("TRUST",   justify="left", no_wrap=True)

        sorted_items = sorted(
            items,
            key=lambda a: (
                -(getattr(a, "trust_score", 0) or 0),
                getattr(a, "price_per_call_usd", 0) or 0,
            ),
        )
        for agent in sorted_items:
            price_usd = float(getattr(agent, "price_per_call_usd", 0) or 0)
            trust = float(getattr(agent, "trust_score", 0) or 0)
            mark_style = (
                "success" if trust >= 80
                else "gold" if trust >= 50
                else "warn" if trust >= 25
                else "muted"
            )
            table.add_row(
                _Text(BAR, style=mark_style),
                slugify(agent.name),
                agent.name,
                money(price_usd * 100),
                trust_gauge(trust),
            )
        console.print(table)

    # Footer hint mirrors the flat view.
    foot = _Text()
    foot.append(f"  {DOT} ", style="border")
    foot.append("hire any specialist with ", style="muted")
    foot.append("aztea hire <slug>", style="code")
    foot.append("   ·   ", style="border")
    foot.append("aztea agents list --flat", style="code")
    foot.append(" for one ranked table", style="muted")
    console.print()
    console.print(foot)
    console.print()
