#!/usr/bin/env bash
# Aztea deference adapter for Hermes (REFERENCE).
#
# Wire as a `pre_tool_call` shell hook in ~/.hermes/config.yaml:
#
#   hooks:
#     pre_tool_call:
#       - command: /path/to/aztea-hermes-pretool.sh
#
# Hermes fires this on every tool call with a JSON payload on stdin
# (agent/shell_hooks.py::_serialize_payload):
#   {"hook_event_name": "pre_tool_call", "tool_name": "terminal",
#    "tool_input": {"command": "..."}, "session_id": ..., "cwd": ..., ...}
# Hermes' tool names differ from the canonical classifier names (its shell
# tool is "terminal", its web tools are "web_search"/"web_extract"), so this
# adapter TRANSLATES the payload, then defers the decision to the single
# source of truth: `aztea mcp pretool-hook --format json` (which runs the same
# classifier the OpenClaw plugin and the Python hook use).
#
# Mode: set AZTEA_DEFERENCE_MODE=warn|block|block-all in the agent's
# environment (default warn). block-all is the experiment treatment arm.
#
# Output: the neutral decision JSON on stdout — {"decision":"block|warn|allow"}.
# Finalize how Hermes consumes a block (shell-hook block convention vs the
# plugin `get_pre_tool_call_block_message` path) against your Hermes version;
# the classification contract here is stable, the block-wiring is the part to
# confirm. For a hard block, the Hermes plugin path is the verified mechanism
# (agent/tool_executor.py honors get_pre_tool_call_block_message).
set -euo pipefail

ALLOW='{"decision":"allow","reason":null}'

# Hard deps: both fail-open to allow, but jq's absence would silently and
# permanently disable deference, so warn once to stderr (not just swallow).
command -v aztea >/dev/null 2>&1 || { echo "$ALLOW"; exit 0; }
command -v jq >/dev/null 2>&1 || {
  echo "aztea-hermes-pretool: jq not found — deference disabled (install jq to enable)" >&2
  echo "$ALLOW"; exit 0
}

# Size-cap the read (mirror the Python 1 MiB stdin guard) so a huge tool arg
# can't balloon shell memory on every tool call.
payload="$(head -c 1048576)"

# Translate Hermes -> canonical event shape with jq. Hermes sends the args
# under `tool_input` (older builds used `args`; accept both). Map the wedge
# tool names so the classifier's rules apply; pass everything else through.
event="$(printf '%s' "$payload" | jq -c '
  {
    tool_name: ({terminal: "Bash", web_search: "WebSearch", web_extract: "WebFetch"}[.tool_name // ""] // (.tool_name // "")),
    tool_input: (.tool_input // .args // {})
  }' 2>/dev/null || echo '{}')"

# Bound the call so a stalled hook (e.g. slow/stale $HOME mount during the
# deference-log write) can't hang the agent's tool loop. Use timeout/gtimeout
# when present; degrade gracefully if neither exists.
_TIMEOUT=""
command -v timeout  >/dev/null 2>&1 && _TIMEOUT="timeout 2"
command -v gtimeout >/dev/null 2>&1 && _TIMEOUT="gtimeout 2"

# Defer to the shared classifier. Fail-open: any error/timeout -> allow.
# AZTEA_CLIENT_ID tags the deference-log row; this adapter only ever runs
# under Hermes, so default the harness identity here (the MCP server entry's
# env does not reach hook subprocesses).
_MODE="${AZTEA_DEFERENCE_MODE:-warn}"
export AZTEA_CLIENT_ID="${AZTEA_CLIENT_ID:-hermes}"
printf '%s' "$event" | ${_TIMEOUT} aztea mcp pretool-hook --mode "$_MODE" --format json 2>/dev/null || echo "$ALLOW"
