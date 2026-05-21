"""Aztea interactive REPL.

# OWNS: the persistent prompt invoked by `aztea` with no subcommand.
# NOT OWNS: any slash command's behavior (those forward to existing Typer
#            commands), one-shot shell-mode dispatch (lives in cli/__init__.py).
# DECISIONS:
#   - Positioning: Aztea CLI is a marketplace control room. Slash commands
#     only. Free text gets a friendly redirect to /claude-code, which is the
#     bridge to the smart-routing surface (Claude Code with Aztea MCP loaded).
#     The REPL never tries to be smart — that's Claude Code's job.
#   - Powered by prompt_toolkit (Python). Not Ink. Aztea CLI is already
#     Python; adding a Node runtime dep buys us nothing prompt_toolkit
#     doesn't already deliver (tab completion, history, bottom toolbar,
#     multi-line, async-safe rendering).
"""
from .app import start


__all__ = ["start"]
