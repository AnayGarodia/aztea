# Reflex eval harness

Measures `reflex_rate` — the fraction of canonical "Aztea should fire"
prompts for which Claude Code (or any host agent) actually calls
`do_specialist_task`. This is the SLI for the routing reflex work.

## Layout

```
tests/eval/reflex/
  fixtures/             *.json — one fixture per canonical prompt
  schema.json           JSON Schema all fixtures conform to
  runner.py             Loads fixtures + (when wired) drives Claude Code SDK
  README.md             This file
```

## Fixture format

See `schema.json`. Minimal example:

```json
{
  "id": "cve-lookup-bare-id",
  "intent": "look up CVE-2021-44228 details",
  "expected_specialist_slug": "cve_lookup",
  "failure_bucket_if_wrong": "agent_called_wrong_specialist"
}
```

`failure_bucket_if_wrong` is one of:
- `agent_used_native_tool` — Claude used Bash/Read/WebFetch instead
- `agent_synthesized_from_training` — Claude answered from training cutoff
- `agent_called_wrong_specialist` — Claude called Aztea but picked wrong
- `aztea_refused` — Aztea correctly refused but should have fired

Each fixture should be runnable N≥5 times to surface flake. The runner
flags fixtures with flake rate ≥20%.

## Wiring

The runner is currently a scaffold — `pytest tests/eval/reflex/` exercises
the schema validation and fixture loader. The actual Claude Code SDK
driver lands when the SDK exposes a stable headless trace API.

Until then, use the harness for:
1. Fixture authoring + schema enforcement
2. Manual playback (load a fixture, paste the intent into your local
   Claude Code session, record what happened)
3. Forward-compat — when the SDK ships, the driver slots in without
   changing the fixture format.
