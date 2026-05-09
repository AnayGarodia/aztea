"""status: at-a-glance dashboard — wallet + recent jobs + connect health.

Single-pane summary you'd want to glance at before kicking off real work.
Designed to be the default landing screen for an active user — call it
manually or wire it into your shell prompt.
"""
from __future__ import annotations

from typing import Optional

import typer

from .common import ApiKeyOpt, BaseUrlOpt, JsonOpt, build_client, handle_error
from .output import (
    BAR,
    DOT,
    _HAS_RICH,
    console,
    emit,
    money,
    relative_time,
    section,
    spinner,
    status_pill,
)


def status_cmd(
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
    limit: int = typer.Option(5, "--limit", min=1, max=20, help="Recent jobs to show."),
) -> None:
    """Wallet, last jobs, and connect health on one screen."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Loading dashboard", json_mode=json_mode):
                profile = client.auth.me()
                wallet = None
                try:
                    wallet = client.get_wallet()
                except Exception:
                    pass
                recent_jobs = []
                try:
                    listing = client.list_jobs(limit=limit) if hasattr(client, "list_jobs") else None
                    if isinstance(listing, list):
                        recent_jobs = listing
                    elif isinstance(listing, dict):
                        recent_jobs = listing.get("jobs") or []
                except Exception:
                    recent_jobs = []
                connect = None
                try:
                    connect = client.get_connect_status() if hasattr(client, "get_connect_status") else None
                except Exception:
                    connect = None

            payload = {
                "username": profile.get("username"),
                "balance_cents": getattr(wallet, "balance_cents", None),
                "escrow_cents": getattr(wallet, "escrow_cents", None),
                "connect_charges_enabled": (connect or {}).get("charges_enabled") if isinstance(connect, dict) else None,
                "recent_jobs": [_job_summary(j) for j in recent_jobs[:limit]],
            }
            if json_mode:
                emit(payload, json_mode=True)
                return

            _render(profile, wallet, recent_jobs[:limit], connect)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


def _job_summary(j) -> dict:
    if isinstance(j, dict):
        return {
            "job_id": j.get("job_id"),
            "agent_slug": j.get("agent_slug") or j.get("agent_name"),
            "status": j.get("status"),
            "cost_cents": j.get("cost_cents") or j.get("total_charge_cents"),
            "updated_at": j.get("updated_at") or j.get("created_at"),
        }
    return {
        "job_id": getattr(j, "job_id", None),
        "agent_slug": getattr(j, "agent_slug", None) or getattr(j, "agent_name", None),
        "status": getattr(j, "status", None),
        "cost_cents": getattr(j, "cost_cents", None),
        "updated_at": getattr(j, "updated_at", None) or getattr(j, "created_at", None),
    }


def _render(profile: dict, wallet, jobs: list, connect) -> None:
    if not _HAS_RICH:
        console.print(f"signed in as {profile.get('username') or 'user'}")
        if wallet is not None:
            bc = getattr(wallet, "balance_cents", 0) or 0
            console.print(f"balance ${bc/100:.2f}")
        for j in jobs:
            d = _job_summary(j)
            console.print(f"  {d.get('status')}  {d.get('agent_slug')}  ${(d.get('cost_cents') or 0)/100:.2f}  {d.get('job_id')}")
        return

    from rich.text import Text
    from rich.table import Table
    from rich.columns import Columns
    from rich import box

    username = profile.get("username") or "user"
    balance_cents = int(getattr(wallet, "balance_cents", 0) or 0)
    escrow_cents = int(getattr(wallet, "escrow_cents", 0) or 0)
    charges_enabled = bool((connect or {}).get("charges_enabled")) if isinstance(connect, dict) else False

    # ── Top hero strip
    head = Text()
    head.append(f"  {BAR} ", style="success")
    head.append("hello, ", style="muted")
    head.append(username, style="bold #5EEAD4")
    head.append(f"   {DOT}   ", style="border")
    head.append(relative_time((profile or {}).get("last_seen_at") or ""), style="muted") if (profile or {}).get("last_seen_at") else None
    console.print()
    console.print(head)
    console.print()

    # ── Wallet card + connect card side-by-side
    wallet_card = _wallet_card(balance_cents, escrow_cents)
    connect_card = _connect_card(charges_enabled, connect or {})
    console.print(Columns([wallet_card, connect_card], equal=False, expand=False, padding=(0, 1)))
    console.print()

    # ── Recent jobs
    section("recent jobs", f"last {len(jobs)}" if jobs else "none yet")
    if not jobs:
        empty = Text()
        empty.append(f"  {DOT}  ", style="border")
        empty.append("nothing yet — try ", style="muted")
        empty.append("aztea hire <slug>", style="code")
        console.print(empty)
        console.print()
        return

    table = Table(
        show_edge=False, show_lines=False, pad_edge=False, padding=(0, 1),
        header_style="label", box=box.SIMPLE_HEAD, border_style="border_dim",
    )
    table.add_column("STATUS", no_wrap=True)
    table.add_column("SPECIALIST", style="code", no_wrap=True)
    table.add_column("JOB", style="muted", no_wrap=True)
    table.add_column("CHARGED", justify="right", no_wrap=True)
    table.add_column("WHEN", style="muted", justify="right", no_wrap=True)

    for j in jobs:
        d = _job_summary(j)
        job_id = (d.get("job_id") or "—")
        short_id = job_id[:12] + ("…" if len(job_id) > 12 else "")
        table.add_row(
            status_pill(d.get("status") or "—"),
            d.get("agent_slug") or "—",
            short_id,
            money(d.get("cost_cents")),
            relative_time(d.get("updated_at") or ""),
        )
    console.print(table)
    console.print()


def _wallet_card(balance_cents: int, escrow_cents: int):
    from rich.text import Text
    from rich.panel import Panel
    from rich.console import Group
    from rich.padding import Padding
    from rich import box

    hero = Text()
    hero.append(f"${balance_cents/100:,.2f}", style="hero")
    hero.append("  USD", style="muted")

    sub = Text()
    sub.append("available", style="muted")
    if escrow_cents:
        sub.append(f"   {DOT}   ", style="border")
        sub.append(f"${escrow_cents/100:,.2f} escrow", style="muted")

    return Panel(
        Group(Padding(hero, (0, 0, 0, 0)), sub),
        title=Text(" wallet ", style="bold #0F2A2D on #5EEAD4"),
        title_align="left",
        border_style="border_dim",
        box=box.ROUNDED,
        padding=(1, 2),
        width=42,
    )


def _connect_card(charges_enabled: bool, connect: dict):
    from rich.text import Text
    from rich.panel import Panel
    from rich.console import Group
    from rich import box

    head = Text()
    if charges_enabled:
        head.append(f"{BAR} ", style="success")
        head.append("payouts enabled", style="success")
    else:
        head.append(f"{BAR} ", style="warn")
        head.append("not connected", style="warn")

    sub = Text()
    if charges_enabled:
        sub.append("withdraw any time:  ", style="muted")
        sub.append("aztea wallet withdraw <amount>", style="code")
    else:
        sub.append("connect Stripe:  ", style="muted")
        sub.append("aztea wallet connect", style="code")

    return Panel(
        Group(head, Text(""), sub),
        title=Text(" payouts ", style="bold #0F2A2D on #5EEAD4"),
        title_align="left",
        border_style="border_dim",
        box=box.ROUNDED,
        padding=(1, 2),
        width=42,
    )
