"""Portable deference decision logic, shared across agent-harness adapters.

# OWNS: the pure, framework-agnostic classifiers that decide WHEN a coding
#   agent should defer to an Aztea specialist (auto_call_agent) — the PreToolUse
#   classifier, the prompt pre-filter, the auto-hire dry-run scout, and the
#   prompt-suggestion builder.
# NOT OWNS: any harness-specific wiring — Claude Code settings.json hooks and
#   the Claude-shaped deny-JSON live in cli/mcp_hooks.py; OpenClaw/Hermes
#   adapters live in their own modules and reuse the functions here.
# INVARIANTS: every function here is PURE or fail-open — no function raises on
#   bad input or network/IO failure (the scout swallows all errors to None), so
#   harness adapters can call them without defensive wrapping.
# DECISIONS: this module was split out of mcp_hooks.py (2026-06) so a second and
#   third harness (OpenClaw, Hermes) reuse the decision logic instead of forking
#   it. Adapters translate their native event shape into the dicts these
#   functions accept, and translate the returned Decision into their native
#   block/allow format.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Bash command classification ────────────────────────────────────────────
# Default is silent. We only nudge when a Bash command reaches the network,
# installs packages, or runs ad-hoc code — the cases where a specialist beats
# the model doing it itself. Plain file/git ops never trigger, so the hook
# stays quiet during normal coding.
_BASH_NETWORK_RE = re.compile(
    r"(?i)(\bcurl\b|\bwget\b|\bnc\b|\bncat\b|\btelnet\b|https?://)"
)
_BASH_INSTALL_RE = re.compile(
    r"(?i)("
    r"\b(pip|pip3|pipx)\s+install\b"
    r"|\bnpm\s+(i|install)\b|\bnpx\b|\byarn\s+add\b|\bpnpm\s+(i|install)\b"
    r"|\buv\s+pip\b"
    r"|\b(cargo|go|gem)\s+install\b"
    r"|\b(apt|apt-get|brew|yum|dnf)\s+install\b"
    r"|\|\s*(sh|bash)\b"  # `curl … | sh`
    r")"
)
_BASH_EXEC_RE = re.compile(
    r"(?i)(\b(python|python3|node|deno|bun|ruby|perl)\s+-(c|e)\b|\b(bash|sh)\s+-c\b)"
)

# Nudges name the canonical tool (`auto_call_agent`); the reflex CLAUDE.md rule
# is aligned to the same name (cli/mcp.py).
_NUDGE_WEB = (
    "Aztea: before fetching or searching the web yourself, consider "
    '`auto_call_agent(intent="<the task>")` — a live fetch/scrape specialist is '
    "usually more accurate than reconstructing page contents from memory."
)
_NUDGE_DEPS = (
    "Aztea: before installing or auditing dependencies by hand, consider "
    '`auto_call_agent(intent="<the task>")` — a dependency / CVE-audit specialist '
    "may do this more reliably."
)
_NUDGE_EXEC = (
    "Aztea: before reasoning about what this code would output, consider running "
    'it via `auto_call_agent(intent="<the task>")` — a sandboxed-execution '
    "specialist returns the real result."
)


@dataclass(frozen=True)
class Decision:
    action: str    # "warn" | "block"
    category: str  # "web" | "live_data" | "deps" | "exec"
    message: str


# Hook modes. "warn" and "block" are the production pair (only web ever
# escalates to block — Bash is too broad to deny by default). "block-all"
# escalates EVERY wedge category to block; it exists for controlled
# deference experiments where the treatment arm must be visible to models
# that ignore advisory warnings (most harnesses surface only hard blocks).
MODE_WARN = "warn"
MODE_BLOCK = "block"
MODE_BLOCK_ALL = "block-all"


def _tool_name(event: dict[str, Any]) -> str:
    # Tolerate snake_case and camelCase across Claude Code versions.
    return str(event.get("tool_name") or event.get("toolName") or "").strip()


def _tool_input(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("tool_input")
    if raw is None:
        raw = event.get("toolInput")
    return raw if isinstance(raw, dict) else {}


def classify_pretool_event(event: Any, *, allow_block: bool) -> Optional[Decision]:
    """Decide whether a PreToolUse event warrants a nudge. Returns None to stay
    silent. Pure — no IO — so it is exhaustively unit-testable.

    Only WebFetch/WebSearch can escalate to ``block`` (and only when
    ``allow_block``); Bash is always ``warn`` regardless, because the matcher
    is too broad to safely deny.
    """
    if not isinstance(event, dict):
        return None
    tool = _tool_name(event)
    if tool in ("WebFetch", "WebSearch"):
        return Decision("block" if allow_block else "warn", "web", _NUDGE_WEB)
    if tool == "Bash":
        command = str(_tool_input(event).get("command") or "")
        if not command.strip():
            return None
        if _BASH_NETWORK_RE.search(command):
            return Decision("warn", "live_data", _NUDGE_WEB)
        if _BASH_INSTALL_RE.search(command):
            return Decision("warn", "deps", _NUDGE_DEPS)
        if _BASH_EXEC_RE.search(command):
            return Decision("warn", "exec", _NUDGE_EXEC)
        return None
    return None


def classify_pretool_event_for_mode(event: Any, mode: str) -> Optional[Decision]:
    """Mode-string front door over classify_pretool_event — the single place
    the warn/block/block-all vocabulary is interpreted, so the CLI, the Claude
    adapter, and harness plugins cannot drift on what a mode means.

    Unknown modes degrade to "warn" (fail-open module invariant: bad input
    must never raise or block a hook)."""
    allow_block = mode in (MODE_BLOCK, MODE_BLOCK_ALL)
    decision = classify_pretool_event(event, allow_block=allow_block)
    if decision is None or mode != MODE_BLOCK_ALL:
        return decision
    if decision.action == "block":
        return decision
    return Decision("block", decision.category, decision.message)


# ── UserPromptSubmit scouting ──────────────────────────────────────────────
# Skip trivial turns so we don't round-trip to the backend on chatter.
_MIN_PROMPT_CHARS = 12
_TRIVIAL_PROMPTS = frozenset({
    "yes", "no", "y", "n", "ok", "okay", "continue", "go", "yep", "sure",
    "thanks", "thank you", "stop", "cancel", "resume", "retry", "again",
    "do it", "proceed", "next",
})


def prompt_should_scout(prompt: str) -> bool:
    """Local pre-filter: is this prompt substantive enough to look up a
    specialist for? Pure; keeps the network call off the trivial path."""
    text = (prompt or "").strip()
    if len(text) < _MIN_PROMPT_CHARS:
        return False
    return text.lower() not in _TRIVIAL_PROMPTS


# Registry-supplied agent name/slug get echoed into the model's prompt context
# on UserPromptSubmit. Anyone can register an agent, so the name is an
# attacker-controlled channel — collapse whitespace, drop non-printables, and
# length-cap to neutralize second-order prompt injection (newline + "IGNORE
# ABOVE INSTRUCTIONS" payloads riding in via a high-ranking malicious listing).
_MAX_LABEL_LEN = 80


def _sanitize_label(value: str) -> str:
    collapsed = re.sub(r"\s+", " ", str(value)).strip()
    cleaned = "".join(ch for ch in collapsed if ch.isprintable())
    return cleaned[:_MAX_LABEL_LEN]


def build_prompt_suggestion(response: Any) -> Optional[str]:
    """Turn an auto-hire dry-run response into a one-line named suggestion, or
    None when no specialist confidently matched (self-suppressing nudge).

    Sanitizes the registry-supplied name/slug — see _sanitize_label."""
    if not isinstance(response, dict) or response.get("would_invoke") is not True:
        return None
    agent = response.get("agent") if isinstance(response.get("agent"), dict) else {}
    name = _sanitize_label(agent.get("name") or agent.get("slug") or "a specialist")
    slug = _sanitize_label(agent.get("slug") or "")
    if not name:
        name = "a specialist"
    confidence = response.get("confidence")
    cost = response.get("estimated_cost_usd")
    detail_bits = []
    if isinstance(confidence, (int, float)):
        detail_bits.append(f"confidence {confidence}")
    if isinstance(cost, (int, float)):
        detail_bits.append(f"~${cost:.2f}")
    detail = f" ({', '.join(detail_bits)})" if detail_bits else ""
    named = f"{name}" + (f" (`{slug}`)" if slug else "")
    return (
        f"Aztea has a specialist for this: {named}{detail}. Prefer "
        '`auto_call_agent(intent="…")` over doing it yourself — it is bounded '
        "in cost and auto-refunds on failure."
    )


# ── prompt-scout network call (hot path: runs per substantive prompt) ───────
# Each hook invocation is a fresh short-lived subprocess, so the dead-key /
# backend-down circuit breaker lives ON DISK, not in memory: after any failure
# we skip the per-turn POST for _SCOUT_COOLDOWN_S so an expired key or a down
# backend doesn't tax every prompt with latency forever. Timeouts are split
# (connect, read) and redirects are not followed, to bound added per-turn time.
_SCOUT_CONNECT_TIMEOUT_S = 1.0
_SCOUT_READ_TIMEOUT_S = 1.5
_SCOUT_MAX_RESPONSE_BYTES = 64 * 1024  # a dry-run response is tiny; reject huge/hostile bodies
_SCOUT_MAX_COST_USD = 0.10  # dry-run only, but keep the request body well-formed
_SCOUT_COOLDOWN_S = 300  # after a failure, stop scouting for 5 min
_SCOUT_COOLDOWN_PATH = Path.home() / ".aztea" / ".scout-cooldown"


def _scout_in_cooldown(now: float) -> bool:
    """True if a recent failure put scouting in cooldown. Best-effort: any read
    error is treated as 'not in cooldown' so bookkeeping never blocks the hook."""
    try:
        return now < float(_SCOUT_COOLDOWN_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False


def _scout_set_cooldown(now: float) -> None:
    """Start a cooldown window. Best-effort; swallow IO errors by design —
    failing to write the breaker must never break the hook (documented).
    Atomic tmp+replace so concurrent prompt-hooks (parallel agents sharing
    $HOME) can't interleave into a garbled float that disables the breaker."""
    try:
        _SCOUT_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SCOUT_COOLDOWN_PATH.with_suffix(".tmp")
        tmp.write_text(str(now + _SCOUT_COOLDOWN_S), encoding="utf-8")
        os.replace(tmp, _SCOUT_COOLDOWN_PATH)
    except OSError:
        pass


def scout_specialist(prompt: str, key: str, url: str, *, now: float) -> Optional[str]:
    """Ask the auto-hire endpoint (free dry-run) whether a specialist matches
    ``prompt``; return a one-line suggestion or None. Hardened for the hot path:
    split connect/read timeouts, no redirect following, a response-size guard,
    and the on-disk cooldown above. NEVER raises — callers depend on fail-open.
    ``now`` is injected (time.time()) so the cooldown is unit-testable."""
    if _scout_in_cooldown(now):
        return None
    import requests

    try:
        resp = requests.post(
            f"{url}/registry/agents/auto-hire",
            headers={"Authorization": f"Bearer {key}"},
            json={"intent": prompt, "dry_run": True, "max_cost_usd": _SCOUT_MAX_COST_USD},
            timeout=(_SCOUT_CONNECT_TIMEOUT_S, _SCOUT_READ_TIMEOUT_S),
            allow_redirects=False,
        )
        content_length = str(resp.headers.get("Content-Length") or "")
        if content_length.isdigit() and int(content_length) > _SCOUT_MAX_RESPONSE_BYTES:
            return None  # implausibly large for a dry-run — don't materialize it
        if not (200 <= resp.status_code < 300):
            _scout_set_cooldown(now)  # dead key / error → stop hammering every turn
            return None
        data = resp.json()
    except (requests.RequestException, ValueError, OSError):
        _scout_set_cooldown(now)
        return None
    return build_prompt_suggestion(data)


# ── Deference decision log (minimum-viable observability) ──────────────────
# On a no-human agent surface, nothing tells the operator whether deference
# fired. We append one JSONL line per WEDGE decision (pass-throughs — the
# Read/Edit/git common case — are NOT logged, so the hot path stays quiet and
# the file stays small). This is the push side ("what the hook decided");
# jobs.client_id is the settled side ("what actually got called"). Writing is
# fail-open: a log error must NEVER break a hook or block a tool call.
DEFERENCE_LOG_PATH = Path.home() / ".aztea" / "deference.jsonl"
_DEFERENCE_LOG_RING_CAP = 2000  # keep the last N lines; bound the file on disk
_DEFAULT_CLIENT_ID = "claude-code"  # matches mcp/server.py when AZTEA_CLIENT_ID unset


def _resolve_client(client: Optional[str]) -> str:
    """Harness identity for a log row. Explicit arg wins; else the same
    AZTEA_CLIENT_ID env the MCP server reads (so an OpenClaw-spawned hook tags
    'openclaw'); else the default."""
    if client:
        return client
    return os.environ.get("AZTEA_CLIENT_ID") or _DEFAULT_CLIENT_ID


def record_deference_decision(
    *,
    tool: str,
    category: str,
    action: str,
    redirected: bool,
    now: float,
    client: Optional[str] = None,
    job_id: Optional[str] = None,
    path: Optional[Path] = None,
) -> bool:
    """Append one decision row to the deference log. Fail-open: returns False on
    any IO error (never raises). ``now`` is injected for deterministic tests.

    redirected = the hook blocked/escalated (vs warn-only). job_id is best-effort
    (the push side rarely knows the settled job id)."""
    target = path or DEFERENCE_LOG_PATH
    row = {
        "ts": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "client": _resolve_client(client),
        "tool": tool,
        "category": category,
        "action": action,
        "redirected": bool(redirected),
        "job_id": job_id,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        _trim_deference_log(target)
        return True
    except OSError:
        return False  # fail-open by design — observability must never block a hook


def _trim_deference_log(path: Path) -> None:
    """Ring-buffer the log to the last _DEFERENCE_LOG_RING_CAP lines. Best-effort;
    only rewrites when meaningfully over cap to keep the common append cheap.
    The rewrite is atomic (tmp + os.replace) so a concurrent appender or a second
    trimmer can never observe a half-written/truncated file — the worst case under
    parallel hook subprocesses is a few lost observability rows, never corruption."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= int(_DEFERENCE_LOG_RING_CAP * 1.1):
        return  # slack before rewriting so we don't rewrite on every append
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines[-_DEFERENCE_LOG_RING_CAP:]) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return


def record_pretool_decision(
    stdin_text: str, *, mode: str, now: float,
    client: Optional[str] = None, path: Optional[Path] = None,
) -> Optional[Decision]:
    """Classify a raw PreToolUse event and log it IF it is a wedge task. Returns
    the Decision (or None). Side effect (the log append) is fail-open; the
    classification itself reuses the single source of truth. Callers wire this
    alongside run_pretool_hook so the live hook path is observable."""
    try:
        event = json.loads(stdin_text) if stdin_text.strip() else {}
    except (ValueError, TypeError):
        return None
    decision = classify_pretool_event_for_mode(event, mode)
    if decision is None:
        return None
    record_deference_decision(
        tool=_tool_name(event) or "unknown",
        category=decision.category,
        action=decision.action,
        redirected=(decision.action == "block"),
        now=now,
        client=client,
        path=path,
    )
    return decision


def read_deference_log(
    *, limit: int = 50, path: Optional[Path] = None
) -> list[dict[str, Any]]:
    """Return the most recent decision rows (newest last), best-effort. Skips
    malformed lines rather than raising — a partial/corrupt log still reads."""
    target = path or DEFERENCE_LOG_PATH
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def summarize_deference_log(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up decision rows into {total, redirected, by_category}. Pure."""
    by_category: dict[str, int] = {}
    redirected = 0
    for row in rows:
        cat = str(row.get("category") or "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
        if row.get("redirected"):
            redirected += 1
    return {"total": len(rows), "redirected": redirected, "by_category": by_category}


# ── Neutral cross-harness contract (for non-Claude plugins) ────────────────
# Claude Code's PreToolUse hook wants a specific deny-JSON shape (see
# mcp_hooks.run_pretool_hook). OpenClaw/Hermes plugins want a neutral decision
# they can map to their own block API. This is THE stable contract a harness
# plugin reads off `aztea mcp pretool-hook --format json` stdout.

def pretool_decision_json(stdin_text: str, *, mode: str) -> str:
    """Classify a raw PreToolUse event into a neutral, versioned JSON decision:
    ``{"decision": "block"|"warn"|"allow", "reason": str|None}``. Always safe
    (fail-open → allow). The plugin maps block→block, warn→allow+surface-reason,
    allow→no-op."""
    try:
        event = json.loads(stdin_text) if stdin_text.strip() else {}
    except (ValueError, TypeError):
        return json.dumps({"decision": "allow", "reason": None})
    decision = classify_pretool_event_for_mode(event, mode)
    if decision is None:
        return json.dumps({"decision": "allow", "reason": None})
    return json.dumps({"decision": decision.action, "reason": decision.message})


def deference_self_check() -> tuple[bool, str]:
    """Deterministic self-test for `aztea mcp doctor`: confirm the classifier
    fires on each of the three wedge categories. Pure (no network), so a green
    here means 'deference logic would fire on a real wedge task'."""
    probes = (
        ({"tool_name": "WebFetch"}, "web"),
        ({"tool_name": "Bash", "tool_input": {"command": "pip install requests"}}, "deps"),
        ({"tool_name": "Bash", "tool_input": {"command": "python -c 'print(1)'"}}, "exec"),
    )
    for event, expected in probes:
        decision = classify_pretool_event(event, allow_block=True)
        if decision is None or decision.category != expected:
            return False, f"deference classifier did not fire on a {expected} wedge task"
    return True, "deference classifier fires on web / deps / exec wedge tasks"
