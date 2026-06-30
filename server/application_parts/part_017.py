# ── Otto /otto/composio proxy — REMOVED ──────────────────────────────
# Otto now runs entirely on its own backend (otto-api.duckdns.org); aztea no
# longer proxies any /otto/* or /auth/otto traffic. This shard is intentionally
# left as a tombstone (like part_015): server/application.py requires the
# part_*.py shards to be CONTIGUOUS and fails fast on a gap, so the file must
# stay. Renumbering the later shards is a larger change left for a dedicated cleanup.
