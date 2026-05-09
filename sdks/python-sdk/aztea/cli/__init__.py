"""Aztea CLI — single binary surface, branded output, scriptable JSON.

Entry point: `aztea`. Commands:
    aztea login | logout | whoami
    aztea agents list | show | search
    aztea hire <slug> [--input ...]            (jobs.hire alias at top level)
    aztea jobs status | cancel | rate | dispute | verify | estimate | follow
    aztea wallet balance | topup | connect | withdraw | withdrawals
    aztea mcp install | doctor | uninstall | serve
    aztea pipelines run

Every command supports --json for machine-readable output and reads
credentials from ~/.aztea/config.json (overridable via --api-key / --base-url
or AZTEA_API_KEY / AZTEA_BASE_URL env vars).
"""
from __future__ import annotations

import typer

from .. import __version__
from . import auth as _auth
from . import agents as _agents
from . import jobs as _jobs
from . import wallet as _wallet
from . import pipelines as _pipelines
from . import mcp as _mcp
from . import status as _status
from .splash import render_splash


app = typer.Typer(
    help="Aztea — the clearing house for agent-to-agent commerce.",
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        from .output import console
        console.print(f"aztea {__version__}")
        raise typer.Exit()


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
) -> None:
    """Show splash when invoked with no subcommand."""
    if ctx.invoked_subcommand is None:
        render_splash()
        raise typer.Exit()


# ── Top-level convenience commands (auth + hire are most-used) ─────────────
app.command(name="login", help="Sign in.")(_auth.login)
app.command(name="logout", help="Sign out.")(_auth.logout)
app.command(name="whoami", help="Show the active account.")(_auth.whoami)
app.command(name="hire", help="Hire an agent and wait for the result.")(_jobs.hire)
app.command(name="batch", help="Hire independent specialists in parallel.")(_jobs.batch)


# ── Subcommand groups ──────────────────────────────────────────────────────
app.add_typer(_agents.app,    name="agents")
app.add_typer(_jobs.app,      name="jobs")
app.add_typer(_wallet.app,    name="wallet")
app.add_typer(_pipelines.app, name="pipelines")
app.add_typer(_mcp.app,       name="mcp")
app.command(name="status", help="At-a-glance dashboard: wallet + recent jobs.")(_status.status_cmd)


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
