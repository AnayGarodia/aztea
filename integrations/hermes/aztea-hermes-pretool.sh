#!/usr/bin/env bash
# Aztea deference adapter for Hermes (REFERENCE).
#
# Wire as a `pre_tool_call` shell hook in ~/.hermes/config.yaml:
#
#   hooks:
#     pre_tool_call:
#       - command: /path/to/aztea-hermes-pretool.sh
#
# Hermes fires this on every tool call with a JSON payload on stdin:
#   {"tool_name": "terminal", "args": {"command": "..."}, ...}
# That shape differs from Aztea's PreToolUse event ({tool_name, tool_input}),
# and Hermes' tool names differ (its shell tool is "terminal", not "Bash"), so
# this adapter TRANSLATES the payload, then defers the decision to the single
# source of truth: `aztea mcp pretool-hook --format json` (which runs the same
# classifier the OpenClaw plugin and the Python hook use).
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

# Translate Hermes -> Aztea event shape with jq (tool_name passthrough; map the
# shell tool to "Bash" so the classifier's Bash rules apply; args -> tool_input).
event="$(printf '%s' "$payload" | jq -c '
  {
    tool_name: (if (.tool_name // "") == "terminal" then "Bash" else (.tool_name // "") end),
    tool_input: (.args // {})
  }' 2>/dev/null || echo '{}')"

# Bound the call so a stalled hook (e.g. slow/stale $HOME mount during the
# deference-log write) can't hang the agent's tool loop. Use timeout/gtimeout
# when present; degrade gracefully if neither exists.
_TIMEOUT=""
command -v timeout  >/dev/null 2>&1 && _TIMEOUT="timeout 2"
command -v gtimeout >/dev/null 2>&1 && _TIMEOUT="gtimeout 2"

# Defer to the shared classifier. Fail-open: any error/timeout -> allow.
printf '%s' "$event" | ${_TIMEOUT} aztea mcp pretool-hook --format json 2>/dev/null || echo "$ALLOW"
