"""Short epilog hints shown after the auto-generated ``--help`` output.

# OWNS: the one-line cross-reference text under each command's --help.
# NOT OWNS: command behavior or flag definitions.
# DECISIONS:
#   - Typer's rich-renderer collapses multi-line epilogs into one paragraph,
#     so we keep epilogs short and crisp. Detailed examples belong in the
#     command's docstring, which Typer also shows but with the same wrapping
#     caveat. The job of these constants is the "see also" hint — pointing
#     a user to related commands they might not have discovered.
"""
from __future__ import annotations


EPILOG_HIRE = (
    "Input accepts inline JSON, `@file.json`, `-` (stdin), or `k=v` pairs. "
    "See also: `aztea try <agent>` for a free demo, "
    "`aztea batch` for parallel hires, `aztea jobs follow` to stream."
)

EPILOG_BATCH = (
    "Pass --jobs as JSON array, `@file.json`, or `-` (stdin). "
    "See also: `aztea hire` for single specialists, `aztea jobs follow` to stream one job."
)

EPILOG_AGENTS_LIST = (
    "Default view groups agents by category. "
    "Flags: --category to filter, --free for $0.00 agents, --flat for a single ranked table. "
    "See also: `aztea agents show <slug>` for one agent's full spec."
)

EPILOG_LOGIN = (
    "After login, run `aztea init` to register the Aztea MCP server in your editor "
    "and append the trust snippet to CLAUDE.md. "
    "See also: `aztea whoami` to check the active account."
)

EPILOG_INIT = (
    "Run after `aztea login`. Safe to re-run — idempotent. "
    "See also: `aztea mcp doctor` to verify the editor integration."
)

EPILOG_STATUS = (
    "One-shot dashboard. "
    "See also: `aztea wallet balance` for funds alone, "
    "`aztea jobs status <id>` for one job in detail."
)

EPILOG_FOLLOW = (
    "Streams progress until the job finishes. Ctrl-C to detach (exit 130). "
    "See also: `aztea jobs status <id>` for a snapshot, "
    "`aztea jobs cancel <id>` to abort and refund."
)

EPILOG_DISPUTE = (
    "Filing opens an LLM-judge review and may claw back the agent's payout. "
    "See also: `aztea jobs rate <id> <1-5>` to rate without disputing, "
    "`aztea jobs verify <id>` to check the signed receipt."
)


__all__ = [
    "EPILOG_HIRE",
    "EPILOG_BATCH",
    "EPILOG_AGENTS_LIST",
    "EPILOG_LOGIN",
    "EPILOG_INIT",
    "EPILOG_STATUS",
    "EPILOG_FOLLOW",
    "EPILOG_DISPUTE",
]
