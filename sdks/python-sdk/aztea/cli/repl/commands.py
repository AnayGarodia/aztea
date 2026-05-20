"""Slash command registry + handlers for the Aztea REPL.

Each handler forwards to the existing Typer command function. We do NOT
re-implement command logic here — the REPL is a thin parser + dispatcher
on top of the same code paths the shell-mode CLI uses, so behaviour stays
in lockstep between the two surfaces.

# OWNS: the slash-command vocabulary and the mapping from each slash name
#        to its underlying Typer function.
# NOT OWNS: the prompt loop (app.py), tab completion (completer.py), or
#            the underlying command bodies (auth.py, jobs.py, agents.py, ...).
"""
from __future__ import annotations

import argparse
import difflib
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

import typer

from .. import (
    agents as _agents,
    auth as _auth,
    dispute as _dispute,
    init as _init,
    jobs as _jobs,
    mcp as _mcp,
    publish as _publish,
    status as _status,
    unpublish as _unpublish,
    wallet as _wallet,
)
from ..output import console, error, info, success, warn


# ── Registry ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SlashCommand:
    """A registered slash command."""
    name: str               # canonical "/login"
    summary: str            # one-liner shown in /help and completer
    group: str              # grouping shown in /help ("auth" / "browse" / ...)
    handler: Callable[[list[str]], None]


_COMMANDS: dict[str, SlashCommand] = {}


def register(name: str, summary: str, group: str):
    """Decorator: register a handler under a slash command name."""
    def _wrap(handler: Callable[[list[str]], None]) -> Callable[[list[str]], None]:
        _COMMANDS[name] = SlashCommand(name, summary, group, handler)
        return handler
    return _wrap


def all_commands() -> list[SlashCommand]:
    """Return every registered slash command, in registration order."""
    return list(_COMMANDS.values())


def find(name: str) -> SlashCommand | None:
    """Look up a slash command by its full name (e.g. ``/login``)."""
    return _COMMANDS.get(name)


def suggest(typed: str, *, n: int = 3) -> list[str]:
    """Difflib-driven 'did you mean' for unknown slash inputs.

    Cutoff 0.6 lets one-character typos through (``/agent`` → ``/agents``)
    while filtering out wholly-unrelated tokens that would produce noisy
    suggestions.
    """
    return difflib.get_close_matches(typed, list(_COMMANDS.keys()), n=n, cutoff=0.6)


# ── Argparse helpers ──────────────────────────────────────────────────────


class _SilentArgParseError(Exception):
    """Raised when argparse encounters a usage error.

    Lets the dispatcher decide how to surface it instead of arsparse
    blowing up the whole REPL process via SystemExit.
    """


class _ReplArgParser(argparse.ArgumentParser):
    """ArgumentParser that raises instead of calling sys.exit."""
    def error(self, message: str) -> None:
        raise _SilentArgParseError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            raise _SilentArgParseError(message)
        raise _SilentArgParseError("")


def _parse(prog: str, argv: list[str], setup: Callable[[_ReplArgParser], None]):
    """Build a parser, parse, and surface any error as REPL output."""
    p = _ReplArgParser(prog=prog, add_help=False)
    setup(p)
    try:
        return p.parse_args(argv)
    except _SilentArgParseError as exc:
        msg = str(exc) or "Invalid arguments."
        error(msg, hint=f"Type `{prog} --help` from shell mode for full usage.")
        return None


def _run(fn: Callable[..., None], **kwargs) -> None:
    """Call an underlying Typer command, swallowing typer.Exit so the REPL
    survives non-zero exits (the inner command already printed the error)."""
    try:
        fn(**kwargs)
    except typer.Exit:
        # Inner Typer command printed its own error/success and raised Exit
        # to terminate the shell-mode process; in REPL mode we just return.
        pass
    except KeyboardInterrupt:
        warn("Cancelled.")


# ── Handlers (registered in display order via the @register decorator) ───


@register("/login", "Sign in to aztea.ai", group="auth")
def _login(argv: list[str]) -> None:
    """Route to the in-REPL modal when no credentials are passed inline.

    - `/login` (bare) inside the REPL → open the login modal dialog.
    - `/login --api-key az_xxx` → direct call to auth.login (modal skipped).
    - Outside the REPL (shell mode), behaviour is unchanged.

    The modal handles the interactive email/password and API-key flows
    without leaving the alt-screen. Without it, Rich's blocking prompts
    deadlock against the running Application that owns stdin.
    """
    def setup(p):
        p.add_argument("--api-key", dest="api_key", default=None)
        p.add_argument("--base-url", dest="base_url", default="https://aztea.ai")
        p.add_argument("--rotate", action="store_true")
        p.add_argument("--force", action="store_true")
        p.add_argument("--json", action="store_true", dest="json_mode")
    args = _parse("/login", argv, setup)
    if args is None:
        return

    # If we're inside the REPL Application and the user provided no
    # credentials, hand off to the modal. The modal will collect creds
    # and then call _auth.login itself (same end-state as the direct path).
    no_creds = (args.api_key is None) and not argv
    if no_creds and _inside_pt_application():
        from .login_modal import show_login_modal
        show_login_modal()
        return

    _run(
        _auth.login,
        email=None,
        password=None,
        api_key=args.api_key,
        base_url=args.base_url,
        rotate=args.rotate,
        force=args.force,
        json_mode=args.json_mode,
    )


def _inside_pt_application() -> bool:
    """True when we're being called from inside the running REPL."""
    try:
        from prompt_toolkit.application import get_app_or_none
        return get_app_or_none() is not None
    except Exception:
        return False


@register("/logout", "Forget the saved API key", group="auth")
def _logout(argv: list[str]) -> None:
    _run(_auth.logout, json_mode=False)


@register("/whoami", "Show the active account", group="auth")
def _whoami(argv: list[str]) -> None:
    _run(_auth.whoami, api_key=None, base_url=None, json_mode=False)


@register("/agents", "Browse agents (categorized by default)", group="browse")
def _agents_list(argv: list[str]) -> None:
    def setup(p):
        p.add_argument("search", nargs="?", default=None)
        p.add_argument("--max-price", type=float, default=None)
        p.add_argument("--min-trust", type=float, default=None)
        p.add_argument("--category", default=None)
        p.add_argument("--flat", action="store_true")
        p.add_argument("--free", action="store_true")
        p.add_argument("--json", action="store_true", dest="json_mode")
    args = _parse("/agents", argv, setup)
    if args is None:
        return
    _run(
        _agents.list_cmd,
        search=args.search,
        max_price=args.max_price,
        min_trust=args.min_trust,
        category=args.category,
        flat=args.flat,
        free=args.free,
        api_key=None,
        base_url=None,
        json_mode=args.json_mode,
    )


@register("/show", "Show an agent's full spec", group="browse")
def _show(argv: list[str]) -> None:
    if not argv:
        info("Usage: /show <slug>")
        return
    _run(_agents.show, slug=argv[0], api_key=None, base_url=None, json_mode=False)


@register("/hire", "Hire an agent and wait for the result", group="hire")
def _hire(argv: list[str]) -> None:
    if not argv:
        info("Usage: /hire <slug> [<json|@file|key=val>]")
        return
    slug, *rest = argv
    positional_input = " ".join(rest) if rest else None
    _run(
        _jobs.hire,
        slug=slug,
        positional_input=positional_input,
        input_value=None,
        api_key=None,
        base_url=None,
        json_mode=False,
    )


@register("/batch", "Hire many agents in parallel", group="hire")
def _batch(argv: list[str]) -> None:
    def setup(p):
        p.add_argument("--jobs", dest="jobs_value", required=True)
        p.add_argument("--intent", default=None)
        p.add_argument("--max-total-cents", type=int, default=None, dest="max_total_cents")
        p.add_argument("--json", action="store_true", dest="json_mode")
    args = _parse("/batch", argv, setup)
    if args is None:
        return
    _run(
        _jobs.batch,
        jobs_value=args.jobs_value,
        intent=args.intent,
        max_total_cents=args.max_total_cents,
        api_key=None,
        base_url=None,
        json_mode=args.json_mode,
    )


@register("/status", "Wallet + recent jobs dashboard", group="manage")
def _status_cmd(argv: list[str]) -> None:
    def setup(p):
        p.add_argument("--limit", type=int, default=5)
        p.add_argument("--json", action="store_true", dest="json_mode")
    args = _parse("/status", argv, setup)
    if args is None:
        return
    _run(
        _status.status_cmd,
        limit=args.limit,
        api_key=None,
        base_url=None,
        json_mode=args.json_mode,
    )


@register("/jobs", "Inspect one job (pass job_id)", group="manage")
def _jobs_status(argv: list[str]) -> None:
    if not argv:
        info("Usage: /jobs <job-id>  ·  for the dashboard use /status")
        return
    _run(
        _jobs.status,
        job_id=argv[0],
        full=False,
        api_key=None,
        base_url=None,
        json_mode=False,
    )


@register("/follow", "Stream live progress for a running job", group="manage")
def _follow(argv: list[str]) -> None:
    if not argv:
        info("Usage: /follow <job-id>")
        return
    _run(
        _jobs.follow,
        job_id=argv[0],
        api_key=None,
        base_url=None,
        json_mode=False,
    )


@register("/cancel", "Abort an in-flight job (refunds the pre-charge)", group="manage")
def _cancel(argv: list[str]) -> None:
    def setup(p):
        p.add_argument("job_id")
        p.add_argument("--reason", default=None)
        p.add_argument("--json", action="store_true", dest="json_mode")
    args = _parse("/cancel", argv, setup)
    if args is None:
        return
    _run(
        _jobs.cancel,
        job_id=args.job_id,
        reason=args.reason,
        api_key=None,
        base_url=None,
        json_mode=args.json_mode,
    )


@register("/rate", "Rate a completed job 1-5 stars", group="manage")
def _rate(argv: list[str]) -> None:
    if len(argv) < 2:
        info("Usage: /rate <job-id> <1-5>")
        return
    try:
        rating = int(argv[1])
    except ValueError:
        error("Rating must be an integer 1-5.")
        return
    _run(
        _jobs.rate,
        job_id=argv[0],
        rating=rating,
        api_key=None,
        base_url=None,
        json_mode=False,
    )


@register("/verify", "Cryptographically verify a job's signed receipt", group="manage")
def _verify(argv: list[str]) -> None:
    if not argv:
        info("Usage: /verify <job-id>")
        return
    _run(
        _jobs.verify,
        job_id=argv[0],
        api_key=None,
        base_url=None,
        json_mode=False,
    )


@register("/dispute", "File a dispute on a recent job", group="manage")
def _dispute_cmd(argv: list[str]) -> None:
    def setup(p):
        p.add_argument("job_id", nargs="?", default=None)
        p.add_argument("--reason", default=None)
        p.add_argument("--evidence", default=None)
        p.add_argument("--status", action="store_true", dest="status_flag")
        p.add_argument("--dry-run", action="store_true", dest="dry_run")
        p.add_argument("--yes", action="store_true", default=False)
        p.add_argument("--limit", type=int, default=10)
        p.add_argument("--json", action="store_true", dest="json_mode")
    args = _parse("/dispute", argv, setup)
    if args is None:
        return
    _run(
        _dispute.dispute,
        job_id=args.job_id,
        reason=args.reason,
        evidence=args.evidence,
        status=args.status_flag,
        dry_run=args.dry_run,
        yes=args.yes,
        limit=args.limit,
        api_key=None,
        base_url=None,
        json_mode=args.json_mode,
    )


@register("/wallet", "Wallet balance + payouts", group="manage")
def _wallet_cmd(argv: list[str]) -> None:
    if not argv or argv[0] == "balance":
        _run(_wallet.balance, api_key=None, base_url=None, json_mode=False)
        return
    sub = argv[0]
    if sub == "topup":
        if len(argv) < 2:
            info("Usage: /wallet topup <amount-usd>")
            return
        try:
            amount = float(argv[1])
        except ValueError:
            error("Amount must be a number (USD).")
            return
        _run(
            _wallet.topup,
            amount=amount,
            open_browser=True,
            api_key=None,
            base_url=None,
            json_mode=False,
        )
        return
    if sub == "connect":
        _run(
            _wallet.connect,
            return_url=None,
            open_browser=True,
            api_key=None,
            base_url=None,
            json_mode=False,
        )
        return
    info("Usage: /wallet [balance | topup <amount> | connect]")


@register("/init", "Register Aztea MCP in Claude Code + write CLAUDE.md", group="setup")
def _init_cmd(argv: list[str]) -> None:
    def setup(p):
        p.add_argument("--client", default="claude")
        p.add_argument("--no-mcp", action="store_true")
        p.add_argument("--no-claude-md", action="store_true")
        p.add_argument("--json", action="store_true", dest="json_mode")
    args = _parse("/init", argv, setup)
    if args is None:
        return
    _run(
        _init.init,
        client=args.client,
        api_key=None,
        base_url=None,
        no_mcp=args.no_mcp,
        no_claude_md=args.no_claude_md,
        json_mode=args.json_mode,
    )


@register("/publish", "List a new agent on Aztea", group="setup")
def _publish_cmd(argv: list[str]) -> None:
    if not argv:
        info("Usage: /publish <path-to-agent.md or .py>")
        return
    # Defer to publish.publish — it has a complex Typer signature; the
    # safest path is shell-mode for this one. Direct shell users can also
    # `aztea publish <path>` from outside the REPL.
    info("Tip: `/publish` chains a verification gate that's easier to read")
    info("in shell mode. Try: aztea publish " + argv[0])


@register("/claude-code", "Open Claude Code in this directory (Aztea wired up)", group="bridge")
def _claude_code(argv: list[str]) -> None:
    """Bridge: hand off to Claude Code with Aztea MCP available.

    Aztea CLI is a marketplace control room; Claude Code is the
    natural-language surface. /claude-code passes the user from one to
    the other — when they exit Claude Code, they land back at their
    original bash shell (NOT in Aztea), because nesting two alt-screen
    apps caused render conflicts (V12: Aztea's leftovers stayed at the
    top of the screen while Claude Code rendered at the bottom).

    Implementation: schedule the subprocess via
    ``request_subprocess_on_exit`` and exit the Application. The
    Aztea ``start()`` function runs the subprocess after its
    ``Application.run()`` returns — with the alt-buffer fully closed,
    Claude Code owns the terminal cleanly.
    """
    if not shutil.which("claude"):
        error(
            "Claude Code (`claude`) was not found on PATH.",
            hint="Install from https://claude.com/claude-code, then re-run /claude-code.",
        )
        return

    # Warn (don't block) when Aztea MCP isn't registered yet — `claude` will
    # still launch fine, the user just won't have Aztea agents available.
    try:
        from ..mcp import _claude_path, _read_config
        cfg = _read_config(_claude_path())
        registered = "aztea" in (cfg.get("mcpServers") or {})
    except Exception:
        registered = False
    if not registered:
        warn("Aztea MCP is not yet registered with Claude Code.")
        info("Run /init first if you want Claude to be able to call Aztea agents.")

    info(f"Opening Claude Code in {os.getcwd()}…")
    info("To verify Aztea is connected, type `/mcp` inside Claude Code")
    info("— Aztea should appear under 'Connected MCP servers'.")
    info("Or just ask Claude to do something Aztea handles, e.g.")
    info('  "audit my requirements.txt for known CVEs"')
    info("— Claude will call `do_specialist_task` automatically.")
    info("When you exit Claude Code, you'll be back at your shell.")

    # Schedule + exit. The subprocess runs after Application.run() returns
    # from start(), which guarantees the alt-screen is fully closed before
    # Claude Code starts drawing.
    from .app import request_subprocess_on_exit
    from prompt_toolkit.application import get_app
    request_subprocess_on_exit(["claude"])
    get_app().exit()


@register("/register", "Create a new Aztea account", group="auth")
def _register_cmd(argv: list[str]) -> None:
    """Open the in-REPL sign-up dialog.

    Routes /register to a modal that collects username + email + password
    + confirm in four steps. The actual server call goes through the
    existing SDK method ``client.auth.register`` so any validation,
    rate-limiting, or error-mapping stays in one place. On success the
    returned API key is written to ``~/.aztea/config.json`` and the
    user is immediately signed in — same effect as completing /login.

    The modal only opens from inside the REPL (full-screen Application);
    if /register were ever called from shell mode, we surface a clear
    "drop into the REPL with `aztea`" hint instead of trying to nest
    blocking input prompts.
    """
    from prompt_toolkit.application import get_app_or_none
    inside_repl = get_app_or_none() is not None
    if not inside_repl:
        info(
            "Sign-up is currently REPL-only — run `aztea` to drop into "
            "the interactive prompt, then type `/register`."
        )
        return
    from .register_modal import show_register_modal
    show_register_modal()


@register("/ask", "Ask Claude (Aztea troubleshooter)", group="ask")
def _ask_cmd(argv: list[str]) -> None:
    """Forward an arbitrary question to Claude with Aztea-troubleshooter context.

    Lives in its own ``ask`` group so /help can call it out separately —
    it's the only command that talks to an LLM (every other slash command
    is deterministic). Argv is joined back into one string so the user
    doesn't have to quote multi-word questions: ``/ask why is my key
    revoked`` works the same as ``/ask "why is my key revoked"``.
    """
    from .ask import ask
    query = " ".join(argv)
    ask(query)


@register("/help", "List every slash command, grouped", group="meta")
def _help_cmd(argv: list[str]) -> None:
    """List every slash command, with a Common-workflows cheat sheet first.

    Newcomers reading /help for the first time benefit more from "what
    can I actually do?" than from an alphabetic command list, so the
    cheat sheet at the top names the 5 most common workflows in slash-
    command shorthand. The full grouped list follows for reference.
    """
    from rich.text import Text
    from rich.table import Table

    # ── Common workflows cheat sheet ──
    _COMMON_WORKFLOWS: tuple[tuple[str, str], ...] = (
        ("Wire Aztea into Claude Code",    "/init"),
        ("Open Claude Code with Aztea",    "/claude-code"),
        ("Browse and hire",                "/agents → /hire <slug>"),
        ("Parallel work",                  "/batch --jobs @batch.json"),
        ("Stream a long job",              "/follow <job-id>"),
        ("Stuck? Ask the troubleshooter",  "/ask <your question>"),
    )
    console.print()
    console.print(Text("  Common workflows", style="heading"))
    workflow_table = Table(
        show_header=False, show_edge=False, box=None, padding=(0, 2),
    )
    workflow_table.add_column(style="muted")
    workflow_table.add_column(style="code", no_wrap=True)
    for desc, cmd in _COMMON_WORKFLOWS:
        workflow_table.add_row(desc, cmd)
    console.print(workflow_table)
    console.print()

    # ── Full grouped list ──
    grouped: dict[str, list[SlashCommand]] = {}
    for cmd in all_commands():
        grouped.setdefault(cmd.group, []).append(cmd)

    order = ["auth", "browse", "hire", "manage", "setup", "bridge", "ask", "meta"]
    pretty = {
        "auth":   "Auth",
        "browse": "Browse",
        "hire":   "Hire",
        "manage": "Manage",
        "setup":  "Setup",
        "bridge": "Bridge",
        "ask":    "Ask",
        "meta":   "Meta",
    }

    for key in order:
        if key not in grouped:
            continue
        console.print(Text(f"  {pretty[key]}", style="heading"))
        table = Table(
            show_header=False, show_edge=False, box=None, padding=(0, 2),
        )
        table.add_column(style="code", no_wrap=True)
        table.add_column(style="muted")
        for cmd in grouped[key]:
            table.add_row(cmd.name, cmd.summary)
        console.print(table)
        console.print()


@register("/clear", "Clear the screen", group="meta")
def _clear(argv: list[str]) -> None:
    console.clear()


@register("/exit", "Leave the REPL", group="meta")
def _exit(argv: list[str]) -> None:
    raise EOFError


@register("/quit", "Leave the REPL", group="meta")
def _quit(argv: list[str]) -> None:
    raise EOFError


# ── Top-level dispatcher ──────────────────────────────────────────────────


def dispatch(line: str) -> None:
    """Parse a REPL line and run the matching slash command (or redirect)."""
    line = line.strip()
    if not line:
        return
    if not line.startswith("/"):
        _handle_free_text(line)
        return

    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        error(f"Could not parse command: {exc}")
        return
    name, *argv = tokens

    cmd = find(name)
    if cmd is None:
        _handle_unknown(name)
        return

    cmd.handler(argv)


def _handle_free_text(line: str) -> None:
    """Friendly redirect — Aztea CLI never tries to chat. Claude Code does.

    Avoids cannibalizing the MCP surface where Aztea actually adds value
    inside a smart agent. From the REPL the user gets a deterministic
    control room; for natural language they're one slash away from the
    surface that does it well.
    """
    info("Aztea is a marketplace control room — type / for commands.")
    info("For natural-language tasks: /claude-code")


def _handle_unknown(name: str) -> None:
    """Print a 'did you mean' line if difflib finds close matches."""
    matches = suggest(name)
    if matches:
        formatted = " · ".join(matches)
        warn(f"Unknown command {name}.  Did you mean: {formatted}?")
        info("Type /help to see every command.")
        return
    # No good matches → fall through to the standard redirect.
    warn(f"Unknown command {name}.")
    _handle_free_text(name)
