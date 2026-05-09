"""Interactive picker for `aztea dispute`.

Drops the user into a numbered menu of their recent disputable jobs when
`aztea dispute` is invoked with no job_id. Eligible jobs are numbered;
ineligible ones are listed with the reason (already disputed, already
rated, window expired, etc.) so the user immediately sees *why* they
can't dispute a particular row.

The disputability decision lives entirely on the server (each job
response carries `disputable` + `disputable_reason` fields) so this
wizard cannot drift from the predicate the dispute endpoint applies.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

import typer

from ..config import load_config
from .common import resolve_settings


def _open_client(**kwargs):
    """Defer to `aztea.cli._client` (patchable) so tests can monkeypatch it."""
    from . import _client as _factory

    return _factory(**kwargs)
from .output import (
    banner,
    console,
    err_console,
    info,
    spinner,
)


def run_wizard(
    *,
    api_key: str | None,
    base_url: str | None,
    json_mode: bool,
    limit: int,
) -> tuple[str, str, str | None]:
    """Run the interactive picker. Returns ``(job_id, reason, evidence)``.

    Refuses on `--json` mode and non-TTY input — those callers must pass
    the job_id and reason as flags.
    """
    if json_mode:
        err_console.print(
            "[error]✗[/error] The dispute wizard is interactive; rerun with "
            "<job_id> --reason \"...\" or drop --json."
        )
        raise typer.Exit(code=2)
    if not _is_tty():
        err_console.print(
            "[error]✗[/error] `aztea dispute` without a job_id requires an "
            "interactive terminal. Pass <job_id> + --reason to script."
        )
        raise typer.Exit(code=2)

    resolved_base, saved_key = resolve_settings(
        api_key=api_key, base_url=base_url, require_api_key=True
    )
    cfg = load_config() or {}
    username = cfg.get("username")
    _greet(resolved_base, username)

    with _open_client(api_key=api_key, base_url=base_url) as client:
        with spinner("Loading recent jobs", json_mode=False):
            payload = client.list_jobs(limit=limit, status="complete,failed")
        jobs = payload.get("jobs") if isinstance(payload, dict) else []
        owner_id = _own_owner_id_from(client, cfg)
        rows = [_classify(job, owner_id) for job in jobs or []]

        eligible_rows = [r for r in rows if r.eligible]
        if not rows:
            info("No recent jobs found. Hire something with `aztea hire <slug>` first.")
            raise typer.Exit(code=0)
        if not eligible_rows:
            console.print()
            console.print("[muted]None of your recent jobs are disputable:[/muted]")
            _render_picker(rows)
            console.print()
            info("Tip: disputes must be filed before rating, within the dispute window.")
            raise typer.Exit(code=0)

        _render_picker(rows)
        picked_row = _pick_row(eligible_rows)

    reason = _ask_reason()
    evidence = _ask_evidence()
    return picked_row.job_id, reason, evidence


# ---------------------------------------------------------------------------
# Picker rendering
# ---------------------------------------------------------------------------


class _Row:
    __slots__ = ("job_id", "eligible", "label", "subtitle", "reason_text")

    def __init__(
        self,
        job_id: str,
        eligible: bool,
        label: str,
        subtitle: str,
        reason_text: str | None,
    ) -> None:
        self.job_id = job_id
        self.eligible = eligible
        self.label = label
        self.subtitle = subtitle
        self.reason_text = reason_text


def _classify(job: dict[str, Any], owner_id: str | None) -> _Row:
    job_id = str(job.get("job_id") or "")
    agent_name = str(job.get("agent_name") or job.get("agent_id") or "—")
    when = _format_relative_time(job.get("completed_at") or job.get("created_at"))
    price = float(job.get("price_cents") or 0) / 100
    job_status = str(job.get("status") or "")
    side = "as caller" if owner_id == job.get("caller_owner_id") else "as worker"
    label = f"{agent_name:<22} {when:>10}  ${price:>5.2f}  {job_status:<8}  {side}"

    input_text = _summarize_input(job.get("input_payload"))
    subtitle = f'"{_truncate(input_text, 60)}"' if input_text else ""

    eligible = bool(job.get("disputable"))
    reason_text = job.get("disputable_reason") if not eligible else None
    return _Row(job_id, eligible, label, subtitle, reason_text)


def _render_picker(rows: list[_Row]) -> None:
    console.print()
    console.print("[label]Recent jobs[/label]")
    console.print(f"  [muted]{'─' * 70}[/muted]")
    eligible_index = 0
    for row in rows:
        if row.eligible:
            eligible_index += 1
            marker = f"[teal]{eligible_index:>2}.[/teal]"
        else:
            marker = "[muted] ─[/muted]"
        console.print(f"  {marker}  {row.label}")
        if row.subtitle:
            console.print(f"      [muted]{row.subtitle}[/muted]")
        if not row.eligible and row.reason_text:
            console.print(f"      [muted]└ {row.reason_text}[/muted]")
    console.print(f"  [muted]{'─' * 70}[/muted]")


def _pick_row(eligible_rows: list[_Row]) -> _Row:
    from rich.prompt import Prompt

    if len(eligible_rows) == 1:
        # Still confirm — implicit selection on a money path is risky.
        only = eligible_rows[0]
        console.print()
        console.print(
            f"[muted]Only one disputable job:[/muted] [code]{only.job_id}[/code]"
        )
        from rich.prompt import Confirm

        if not Confirm.ask(
            "[label]Dispute this job?[/label]", default=True, console=console
        ):
            info("Cancelled.")
            raise typer.Exit(code=0)
        return only

    while True:
        console.print()
        raw = Prompt.ask(
            "[label]Pick a disputable job[/label]", default="1", console=console
        ).strip()
        if not raw.isdigit():
            err_console.print("  [error]✗[/error] Enter a number from the list.")
            continue
        choice = int(raw)
        if 1 <= choice <= len(eligible_rows):
            return eligible_rows[choice - 1]
        err_console.print(
            f"  [error]✗[/error] Pick a number between 1 and {len(eligible_rows)}."
        )


# ---------------------------------------------------------------------------
# Reason / evidence prompts
# ---------------------------------------------------------------------------


def _ask_reason() -> str:
    from rich.prompt import Prompt

    console.print()
    console.print("[label]Reason for the dispute[/label]")
    console.print(
        "  [muted]Why did the result fail to meet your expectations? "
        "Be specific — judges will read this.[/muted]"
    )
    while True:
        text = Prompt.ask("[teal]>[/teal]", console=console).strip()
        if not text:
            err_console.print("  [error]✗[/error] Reason is required.")
            continue
        if len(text.split()) < 3:
            err_console.print(
                "  [error]✗[/error] Use at least three words so judges have context."
            )
            continue
        return text


def _ask_evidence() -> str | None:
    from rich.prompt import Prompt

    console.print()
    console.print("[label]Optional evidence[/label]")
    console.print(
        "  [muted]URL or text supporting your claim. Press Enter to skip.[/muted]"
    )
    text = Prompt.ask("[teal]>[/teal]", default="", console=console).strip()
    return text or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _greet(base_url: str, username: str | None) -> None:
    banner(
        "aztea dispute",
        subtitle="Open a dispute on a recent job. The agent's payout is held in escrow until judges decide.",
    )
    who = f" ([accent]{username}[/accent])" if username else ""
    console.print(
        f"  [muted]Filing against[/muted] [code]{base_url}[/code]{who}."
    )


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _format_relative_time(iso: Any) -> str:
    if not iso:
        return "—"
    try:
        text = str(iso).strip().replace("Z", "+00:00")
        when = datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "—"
    delta = datetime.now(timezone.utc) - when
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m ago"
    if total_seconds < 86400:
        return f"{total_seconds // 3600}h ago"
    return f"{total_seconds // 86400}d ago"


def _summarize_input(payload: Any) -> str:
    if not payload:
        return ""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("query", "task", "prompt", "q", "question", "input"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _own_owner_id_from(client: Any, cfg: dict[str, Any]) -> str | None:
    """Resolve the signed-in user's owner_id so the picker can label rows
    as caller vs worker. Best-effort; returns None on any failure."""
    cached = cfg.get("owner_id")
    if isinstance(cached, str) and cached:
        return cached
    try:
        me = client._request_json("GET", "/auth/me")
    except Exception:  # noqa: BLE001 — labelling is informational
        return None
    if isinstance(me, dict):
        for key in ("owner_id", "user_id", "id"):
            value = me.get(key)
            if isinstance(value, str) and value:
                return value
    return None


__all__ = ["run_wizard"]
