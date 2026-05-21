"""LLM Q&A — the Aztea troubleshooter, powered by the Anthropic API.

# OWNS: the /ask slash command. Sends user questions to a Claude model
#        whose system prompt positions it as *Aztea* — the user sees
#        responses labelled "Aztea", not "Claude". Internally Claude is
#        only the inference engine.
# NOT OWNS: the broader REPL loop (app.py) or any live Aztea data —
#            /ask is stateless. The model gets a static facts dump in
#            the system prompt and reasons from there.
# DECISIONS:
#   - User-facing branding: "Aztea" everywhere. The fact that Claude is
#     the model is an implementation detail; mixing the names in the
#     output ("Asking Claude..." then a "Claude" answer block about
#     Aztea) confused users (V16). The Anthropic API key still has its
#     usual env-var name (ANTHROPIC_API_KEY) — that's a tool-chain
#     constant, not a user-facing label.
#   - Key lookup precedence: ANTHROPIC_API_KEY env var first, then
#     ``~/.aztea/anthropic.key`` (single line, mode 0600). Never
#     hardcoded in source.
#   - Model: claude-sonnet-4-6 — fast enough for interactive REPL use,
#     smart enough for non-trivial troubleshooting. max_tokens=800
#     keeps responses terminal-friendly.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

from ...config import config_path
from ..output import console, error, info


_MODEL = "claude-sonnet-4-6"
_API_ENDPOINT = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_TIMEOUT_S = 60
_MAX_TOKENS = 800


# Static facts the model needs to play "Aztea" usefully. Kept short on
# purpose — the model fills in the conversational glue. We name slash
# commands explicitly so the response cites the right next step.
_SYSTEM_PROMPT = """\
You are Aztea — the in-CLI troubleshooter for users of the Aztea
marketplace. Respond as Aztea, in first person ("I can help with...",
not "Claude can help..."). Never refer to yourself as Claude, an
assistant, or an AI model — to the user you are Aztea.

Aztea is an AI agent marketplace at aztea.ai. The Aztea CLI is an
interactive REPL with these slash commands:

- /login — sign in. A picker offers email+password OR pasting an API key.
- /logout — clear saved credentials.
- /whoami — show the current account.
- /agents — browse the current specialist catalog grouped by category
  (Security, Code Execution, Quality, Web, Research, Developer Tools).
- /show <slug> — show one agent's full spec.
- /hire <slug> — run a specialist on your input. Blocks until done.
- /batch — hire many specialists in parallel.
- /status — wallet balance + recent jobs dashboard.
- /jobs <id> — one job's details. /follow <id> streams progress.
- /cancel, /rate, /verify, /dispute — per-job operations.
- /wallet — balance, top-up via Stripe, withdraw, Stripe Connect.
- /init — register Aztea MCP in Claude Code + write the CLAUDE.md
  snippet. Run once after sign-in.
- /publish — list a new agent on the marketplace.
- /claude-code — open Claude Code in the current directory with Aztea
  loaded as MCP. The recommended path for natural-language work.
- /ask — you (the troubleshooter).
- /help — list every slash command, grouped.
- /clear — clear the chat history pane.
- /exit, /quit, Ctrl-D, Ctrl-C, Esc — leave the REPL.

Aztea agents do things LLMs can't do alone: live CVE lookups (NIST
NVD), sandboxed code execution, browser automation, Dockerfile linting,
security header grading, dependency audits, signed receipts. Buying a
call charges your wallet; failures auto-refund.

Common issues and the precise fix to suggest:

- "API key revoked" / 403 + API_KEY_REVOKED — the user's saved key was
  invalidated on the server. Tell them: run /login again to mint a
  fresh key. The saved key will be overwritten.
- "No API key configured" — they aren't signed in yet. Tell them: run
  /login first.
- "Aztea MCP isn't registered" — Claude Code can't see Aztea. Tell
  them: run /init.
- /agents shows an auth error even after the V5 require_api_key=False
  fix — the server still requires a valid key for /registry/agents.
  Tell them: run /login first.
- Login is interactive and would prompt for email/password — inside the
  REPL it opens a modal. Outside the REPL (regular shell), it asks at
  the terminal.
- Esc at the empty REPL prompt exits the REPL.

Tone and length:
1. Be CONCISE. Terminal output. Aim for ≤5 sentences.
2. Cite specific slash commands the user should run. Use backticks.
3. If unsure of a specific fact (agent prices, exact rate limits), tell
   them to run /help, /status, or check docs.aztea.ai.
4. Never invent agent names, prices, or features that aren't in this
   prompt.
5. Default to actionable next-step advice.
"""


def _get_api_key() -> str | None:
    """Read the Anthropic key, env var first then ~/.aztea/anthropic.key."""
    env_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if env_key:
        return env_key
    key_file = Path(config_path()).expanduser().parent / "anthropic.key"
    if key_file.is_file():
        try:
            content = key_file.read_text(encoding="utf-8").strip()
            return content or None
        except OSError:
            return None
    return None


def _call_anthropic(query: str, *, key: str) -> tuple[int, dict | str]:
    """POST one user turn to the Messages API. Returns (status_code, body)."""
    try:
        resp = requests.post(
            _API_ENDPOINT,
            headers={
                "x-api-key": key,
                "anthropic-version": _API_VERSION,
                "Content-Type": "application/json",
            },
            json={
                "model": _MODEL,
                "max_tokens": _MAX_TOKENS,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": query}],
            },
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        return -1, f"Network error: {exc}"

    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:400]


def _extract_text(body: dict) -> str:
    """Pull the assistant's response out of an Anthropic Messages payload."""
    blocks = body.get("content") or []
    pieces: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            pieces.append(str(block.get("text") or ""))
    return "\n".join(pieces).strip()


def ask(query: str) -> None:
    """Send a question to the Aztea troubleshooter (Claude under the hood)."""
    query = (query or "").strip()
    if not query:
        info("Usage: /ask <your question>")
        info("Example: `/ask How do I top up my wallet?`")
        return

    key = _get_api_key()
    if not key:
        error(
            "ANTHROPIC_API_KEY is not configured.",
            hint=(
                "Set it once via:  export ANTHROPIC_API_KEY=sk-ant-...\n"
                "Or save your key to ~/.aztea/anthropic.key (file mode 0600)."
            ),
            code="ask.no_key",
        )
        return

    # No spinner: under output capture (REPL mode) Rich's status renderer
    # writes ANSI cursor-movement escapes into the buffer that come out
    # as garbage when the captured text is later displayed in history.
    # The blocking POST takes ~1-3s on Sonnet — acceptable without a
    # visible indicator.
    info("Asking Aztea…")
    status, body = _call_anthropic(query, key=key)

    if status == -1:
        # Body holds the exception message in this case.
        error(str(body), code="ask.network")
        return
    if status != 200:
        hint: str
        if isinstance(body, dict):
            hint = str(body.get("error", {}).get("message") or "").strip()
            if not hint:
                hint = str(body)[:400]
        else:
            hint = str(body)
        error(
            f"Anthropic API returned {status}.",
            hint=hint,
            code="ask.api_error",
        )
        return

    if not isinstance(body, dict):
        error("Unexpected response shape.", code="ask.parse")
        return

    response_text = _extract_text(body)
    if not response_text:
        info("(empty response)")
        return

    # Print with a styled "Aztea" label prefix so the conversational
    # nature is obvious. Brand teal matches the wordmark. The model is
    # Claude under the hood, but to the user the troubleshooter is Aztea.
    from rich.text import Text
    console.print()
    console.print(Text("Aztea", style="bold #7EB9B0"))
    console.print(response_text)
    console.print()
