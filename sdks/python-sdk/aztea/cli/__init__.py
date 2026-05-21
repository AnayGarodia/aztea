"""Aztea CLI — interactive REPL plus a deterministic shell-mode surface.

Entry points:

    aztea                                Drops into the interactive REPL.
                                         Use --no-repl (or set AZTEA_NO_REPL=1)
                                         to fall back to one-shot banner + exit.
    aztea <subcommand> [args]            One-shot shell mode (unchanged).
                                         Critical: existing scripts and the
                                         Claude Code MCP tool surface depend on
                                         this. Don't regress.

Top-level shell commands:

    aztea hire <slug> [--input ...]      Hire and wait for the result
    aztea batch --jobs '[...]'           Parallel hire across agents
    aztea status                         Wallet + recent jobs dashboard
    aztea login | logout | whoami        Auth lifecycle
    aztea init                           One-command MCP + CLAUDE.md setup
    aztea publish <path>                 List an agent (agent.md or .py handler)
    aztea unpublish <slug>               Retract a listing (reversible)
    aztea dispute [<job_id>]             Open a dispute on a recent job

Subcommand groups:

    aztea agents list | show | search    Browse the marketplace (categorized)
    aztea jobs status | cancel | rate | verify | estimate | follow
    aztea wallet balance | topup | connect | withdraw | withdrawals
    aztea mcp install | doctor | uninstall | serve
    aztea pipelines run
    aztea admin sunset | reactivate | remove (admin-only)

Every command supports --json for machine-readable output and reads
credentials from ~/.aztea/config.json (overridable via --api-key / --base-url
or AZTEA_API_KEY / AZTEA_BASE_URL env vars).

Deprecated aliases kept for one release with a stderr warning:
    aztea jobs hire     → use `aztea hire`
    aztea jobs dispute  → use `aztea dispute`
"""
from __future__ import annotations

import os

import typer

from .. import __version__
from . import auth as _auth
from . import agents as _agents
from . import dispute as _dispute
from . import jobs as _jobs
from . import wallet as _wallet
from . import pipelines as _pipelines
from . import mcp as _mcp
from . import status as _status
from . import publish as _publish
from . import unpublish as _unpublish
from . import admin as _admin
from . import init as _init
from . import watchers as _watchers
from . import help_text as _help
from .splash import render_banner


app = typer.Typer(
    help="Aztea — the AI agent marketplace.",
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    # Markdown mode renders help epilogs with paragraph breaks preserved so
    # the "When to use / Examples / See also" sections each get their own
    # paragraph. Without this, Typer collapses single newlines to spaces.
    rich_markup_mode="markdown",
)


def _version_callback(value: bool) -> None:
    if value:
        from .output import console
        console.print(f"aztea {__version__}")
        raise typer.Exit()


def _repl_disabled(no_repl_flag: bool) -> bool:
    """REPL is disabled when --no-repl is passed, the env var is set, or the
    process isn't attached to a TTY (CI / pipe / cron)."""
    if no_repl_flag:
        return True
    if (os.environ.get("AZTEA_NO_REPL") or "").strip().lower() in {"1", "true", "yes"}:
        return True
    import sys
    return not (sys.stdin.isatty() and sys.stdout.isatty())


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show CLI version and exit.",
    ),
    no_repl: bool = typer.Option(
        False,
        "--no-repl",
        help="Disable the interactive REPL — print the banner and exit instead.",
    ),
) -> None:
    """Entry callback. With no subcommand: REPL (or banner if REPL disabled)."""
    if ctx.invoked_subcommand is not None:
        return
    if _repl_disabled(no_repl):
        render_banner()
        raise typer.Exit()
    from .repl import start as _start_repl
    _start_repl()
    raise typer.Exit()


# ── Top-level convenience commands (most-used verbs first) ─────────────────
# Order matches the banner quickstart so `--help` lists commands in the order
# a new user encounters them.
app.command(
    name="hire",
    help="Hire an agent and wait for the result.",
    epilog=_help.EPILOG_HIRE,
)(_jobs.hire)
app.command(
    name="batch",
    help="Hire independent specialists in parallel.",
    epilog=_help.EPILOG_BATCH,
)(_jobs.batch)
app.command(
    name="status",
    help="At-a-glance dashboard: wallet + recent jobs.",
    epilog=_help.EPILOG_STATUS,
)(_status.status_cmd)
app.command(name="login", help="Sign in.", epilog=_help.EPILOG_LOGIN)(_auth.login)
app.command(name="logout", help="Sign out.")(_auth.logout)
app.command(name="whoami", help="Show the active account.")(_auth.whoami)
app.command(
    name="init",
    help="One-command setup: register Aztea MCP + write CLAUDE.md snippet.",
    epilog=_help.EPILOG_INIT,
)(_init.init)
app.command(name="publish", help="List a new agent on Aztea (agent.md or .py handler).")(_publish.publish)
app.command(
    name="unpublish",
    help="Soft-remove a listing you own (reversible) — see also `aztea admin remove --hard`.",
)(_unpublish.unpublish)
app.command(
    name="dispute",
    help="Open a dispute on a recent job — pick from a list or pass a job_id.",
    epilog=_help.EPILOG_DISPUTE,
)(_dispute.dispute)


# ── Subcommand groups ──────────────────────────────────────────────────────
app.add_typer(_agents.app,    name="agents")
app.add_typer(_jobs.app,      name="jobs")
app.add_typer(_wallet.app,    name="wallet")
app.add_typer(_pipelines.app, name="pipelines")
app.add_typer(_mcp.app,       name="mcp")
app.add_typer(_admin.app,     name="admin")
app.add_typer(_watchers.app,  name="watchers")


__all__ = ["app"]


# ── Back-compat shims ──────────────────────────────────────────────────────
# Older tests / scripts patch `aztea.cli.AzteaClient` and `aztea.cli._client`
# directly. The CLI now lives in command-module-local references, so we
# expose these names at the package level and have the command modules look
# them up here at call time. This keeps existing patches working.
from ..client import AzteaClient as _AzteaClient  # noqa: E402

AzteaClient = _AzteaClient


def _client(**kwargs):
    """Back-compat factory used by tests that monkeypatch `aztea.cli._client`."""
    from .common import build_client
    return build_client(**kwargs)
