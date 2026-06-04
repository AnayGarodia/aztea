"""Tab completion for the Aztea REPL.

# OWNS: producing prompt_toolkit Completion objects for the current input.
# NOT OWNS: which commands exist (commands.py), how they render (app.py),
#            or the cached agent / job state (lives on this module so
#            invalidation is cheap and local).
# DECISIONS:
#   - Agent slugs are fetched lazily on the first completion attempt and
#     cached for 5 minutes. We never block the prompt on a fresh fetch
#     mid-keystroke; if the cache is empty we yield no completions and
#     the next /agents call will populate it.
#   - Recent job IDs are pushed into the cache by /status and /jobs
#     handlers. Completion never round-trips the network to fetch them.
"""
from __future__ import annotations

import time
from typing import Iterable

from prompt_toolkit.completion import Completer, Completion

from .commands import all_commands


_AGENT_TTL_S = 300.0
_agent_cache: dict[str, object] = {"ts": 0.0, "slugs": []}

# Categories — the canonical set lives in cli/agents.py. We mirror it here
# to keep the completer self-contained and avoid pulling the agents module
# at import time.
_CATEGORIES: tuple[str, ...] = (
    "Security",
    "Code Execution",
    "Quality",
    "Web",
    "Research",
    "Developer Tools",
    "QA",
)

# Commands that take a job_id as their first positional arg.
_JOB_ID_COMMANDS = frozenset({
    "/follow", "/cancel", "/rate", "/verify", "/dispute", "/jobs",
})

# Recent job IDs cache — populated by /status and /jobs handlers (best-effort).
_recent_jobs: list[str] = []


def remember_jobs(job_ids: Iterable[str]) -> None:
    """Called by /status and /jobs to refresh the completion cache."""
    _recent_jobs.clear()
    for jid in job_ids:
        if jid and jid not in _recent_jobs:
            _recent_jobs.append(jid)


def remember_agents(slugs: Iterable[str]) -> None:
    """Called when /agents runs successfully — caches the slugs we saw."""
    _agent_cache["slugs"] = list(slugs)
    _agent_cache["ts"] = time.time()


def _agent_slugs() -> list[str]:
    """Return cached agent slugs, ignoring entries older than the TTL."""
    if time.time() - float(_agent_cache["ts"]) > _AGENT_TTL_S:
        return []
    slugs = _agent_cache["slugs"]
    if isinstance(slugs, list):
        return [s for s in slugs if isinstance(s, str)]
    return []


class AzteaCompleter(Completer):
    """Context-aware completer for the Aztea REPL prompt."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # Split conservatively — we want word boundaries, not shlex semantics
        # (shlex.split would choke on an unterminated quote mid-typing).
        tokens = text.split()
        trailing_space = text.endswith(" ") if text else False

        # ── Top-level: empty input or typing a slash command ──
        if not tokens or (len(tokens) == 1 and not trailing_space):
            yield from self._complete_slash(tokens[0] if tokens else "")
            return

        head = tokens[0]
        last = "" if trailing_space else tokens[-1]
        # ``prev`` is the token *behind* the user's caret — equal to the last
        # token when the line ends in whitespace, else the token before it.
        if trailing_space:
            prev = tokens[-1]
        else:
            prev = tokens[-2] if len(tokens) >= 2 else ""

        # ── Per-command arg completion ──
        if head in ("/hire", "/show") and (trailing_space or len(tokens) >= 2):
            yield from self._complete_agent_slug(last)
            return

        if head == "/agents" and prev == "--category":
            # User typed `--category ` (or `--category Sec`) and is filling in
            # a value. Complete the canonical category names.
            yield from self._complete_category(last)
            return

        if head in _JOB_ID_COMMANDS and (trailing_space or len(tokens) >= 2):
            yield from self._complete_job_id(last)
            return

        # ── Flag completion ──
        # Fires when the user starts typing a flag (`-`) OR when the user
        # just typed the command name + space and is about to pick an arg.
        # The second branch is the "show me what args this command takes"
        # surface — the [--api-key] / [--base-url] menu after `/login `.
        starting_flag = last.startswith("-")
        ready_for_args = trailing_space and len(tokens) == 1
        if starting_flag or ready_for_args:
            yield from self._complete_flags(head, last)
            return

    # ── Helpers ──

    def _complete_slash(self, prefix: str) -> Iterable[Completion]:
        """Match slash commands, including a bare prefix without the slash.

        Typing ``l`` is treated as ``/l`` so the dropdown surfaces
        ``/login`` from the very first keystroke. The same logic lets the
        user type the command body without a leading slash — useful for
        muscle memory carried over from other CLIs.
        """
        bare_prefix = prefix.lstrip("/").lower()
        seen: set[str] = set()
        for cmd in all_commands():
            name = cmd.name
            bare_name = name.lstrip("/").lower()
            matches = (
                not prefix
                or name.lower().startswith(prefix.lower())
                or (bare_prefix and bare_name.startswith(bare_prefix))
            )
            if matches and name not in seen:
                seen.add(name)
                yield Completion(
                    name,
                    start_position=-len(prefix),
                    display_meta=cmd.summary,
                )

    def _complete_agent_slug(self, prefix: str) -> Iterable[Completion]:
        for slug in _agent_slugs():
            if slug.startswith(prefix):
                yield Completion(slug, start_position=-len(prefix))

    def _complete_category(self, prefix: str) -> Iterable[Completion]:
        for cat in _CATEGORIES:
            if cat.lower().startswith(prefix.lower()):
                yield Completion(cat, start_position=-len(prefix))

    def _complete_job_id(self, prefix: str) -> Iterable[Completion]:
        for jid in _recent_jobs:
            if jid.startswith(prefix):
                yield Completion(jid, start_position=-len(prefix))

    def _complete_flags(self, head: str, prefix: str) -> Iterable[Completion]:
        """Per-command flag map with a square-bracket display per user
        request ("after /login is written completely, it should show
        argument in square brackets"). The inserted text is just
        ``--flag`` — the brackets are display-only."""
        flags = _FLAGS_BY_COMMAND.get(head, ())
        for flag in flags:
            if flag.startswith(prefix):
                hint = _flag_hint(head, flag)
                yield Completion(
                    flag,
                    start_position=-len(prefix),
                    display=f"[{flag}]",
                    display_meta=hint,
                )


_FLAGS_BY_COMMAND: dict[str, tuple[str, ...]] = {
    "/agents":  ("--category", "--free", "--flat", "--max-price", "--min-trust", "--json"),
    "/hire":    ("--input", "--json"),
    "/batch":   ("--jobs", "--intent", "--max-total-cents", "--json"),
    "/dispute": ("--reason", "--evidence", "--status", "--dry-run", "--yes", "--limit", "--json"),
    "/cancel":  ("--reason", "--json"),
    "/login":   ("--api-key", "--base-url", "--rotate", "--force", "--json"),
    "/init":    ("--client", "--no-mcp", "--no-claude-md", "--json"),
    "/status":  ("--limit", "--json"),
    "/wallet":  ("--json",),
}


# Flag hints shown to the right of each completion in the dropdown. Common
# flags (shared across many commands) have a default; per-command entries
# override when the meaning differs.
_FLAG_HINTS_COMMON: dict[str, str] = {
    "--json":       "Machine-readable output",
    "--base-url":   "Override the server URL",
    "--api-key":    "Override the saved API key",
    "--limit":      "Number of rows",
    "--reason":     "One-line explanation",
}
_FLAG_HINTS_PER_COMMAND: dict[str, dict[str, str]] = {
    "/login": {
        "--api-key":  "Sign in with a pre-existing az_ API key",
        "--rotate":   "Mint a fresh per-machine key",
        "--force":    "Re-prompt even if a saved key works",
    },
    "/agents": {
        "--category":   "Filter to one bucket (e.g. Security)",
        "--free":       "Show only $0.00 agents",
        "--flat":       "Single ranked table (no grouping)",
        "--max-price":  "Cap by price (USD)",
        "--min-trust":  "Cap by trust score",
    },
    "/hire": {
        "--input":  "Inline JSON, @file, '-', or k=v pairs",
    },
    "/batch": {
        "--jobs":             "JSON array of {slug, input_payload}",
        "--intent":           "One-line goal for the batch",
        "--max-total-cents":  "Hard cap before any charge",
    },
    "/dispute": {
        "--evidence":  "URL or note supporting the claim",
        "--status":    "Show existing dispute state",
        "--dry-run":   "Preview deposit without filing",
        "--yes":       "Skip confirmation",
    },
    "/init": {
        "--client":         "claude | cursor | vscode | windsurf | codex",
        "--no-mcp":         "Skip MCP registration",
        "--no-claude-md":   "Skip CLAUDE.md snippet",
    },
}


def _flag_hint(head: str, flag: str) -> str:
    """Return the right-hand description for a flag, falling back to common."""
    per_cmd = _FLAG_HINTS_PER_COMMAND.get(head, {})
    return per_cmd.get(flag) or _FLAG_HINTS_COMMON.get(flag, "")
