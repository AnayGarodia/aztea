# ── Otto chat proxy — REMOVED ─────────────────────────────────────────────────
# POST /otto/chat (a standalone authenticated passthrough to Anthropic's Messages
# API) has been retired. The Otto desktop app no longer ships an Anthropic/Claude
# client — all acting now runs on GPT-5.5 via /otto/responses (part_018), routed
# through the LiteLLM gateway. Nothing calls /otto/chat anymore.
#
# This shard is intentionally left as a tombstone rather than deleted:
# server/application.py requires the part_*.py shards to be CONTIGUOUS
# (part_000 … part_N) and fails fast on a gap, so removing the file would break
# startup. Renumbering the later shards is a larger, riskier change left for a
# dedicated cleanup.
#
# Retired with this route (no longer read by aztea):
#   ANTHROPIC_API_KEY / OTTO_ANTHROPIC_API_KEY   (Otto's Anthropic upstream key)
#   OTTO_BUDGET_CAP_CENTS                         (the $200 Anthropic SQLite cap)
# The `otto_budget` SQLite table is now orphaned and can be dropped at leisure.
#
# See: docs/otto-proxy.md, part_018.py (responses), part_016.py (realtime).
