# Otto Telemetry — Event Contract (v1)

Single source of truth for the events the Otto macOS app sends and the aztea
server ingests. Mirror of this lives in the Otto repo (`docs/telemetry-schema.md`).
Bump `schema_version` and update both repos together when this changes.

## Principles

- **Privacy first.** No raw task text, no file names, no argument values. Only
  category labels and structured numbers. Heavy per-action detail stays local on
  the device (`~/.otto/*.jsonl`); only aggregates per task leave the machine.
- **Opt-out respected.** Nothing is sent if the user disabled telemetry.
- **Anonymous.** `device_id` is a random UUID generated once on-device. No PII.
- **Sliceable, not vanity.** Every event carries dimensions you can group by
  (intent category, app, failure reason, path) — not just running totals.

## Transport

- `POST https://aztea.ai/otto/telemetry`
- Header: `Authorization: Bearer <OTTO_APP_TOKEN or user api key>` (same auth as
  `/otto/responses`, validated by `_otto_proxy_auth_ok`).
- Body: a **batch**: `{ "events": [ <event>, ... ] }` (max 100 per call).
- Each event carries a stable `event_id` (UUID) so the server can dedup and
  retried/offline-queued sends never double-count.

## Envelope (every event)

| field | type | notes |
|---|---|---|
| `event_id` | uuid string | unique per event; dedup key |
| `event` | string | `install\|launch\|task\|permission\|onboarding\|account\|error\|download` |
| `schema_version` | int | currently `1` |
| `device_id` | uuid string | anonymous, stable per install |
| `session_id` | uuid string | one per app launch |
| `ts_client` | ISO-8601 string | when it happened on-device |
| `app_version` | string | e.g. `0.5.4` |
| `os_version` | string | e.g. `macOS 15.5` |
| `mac_model` | string | chip + model, e.g. `Mac14,2 / arm64` |
| `props` | object | event-specific payload (below) |

Server stamps `ts_server` on receipt. Clients must not send it.

## Event payloads (`props`)

### `install` — once per device, first launch after install
```
install_source?: string   // "dmg" | "appstore" | unknown
```

### `launch` — every app start
```
kind: "cold" | "warm"
since_last_launch_s: number   // 0 on first ever launch
```

### `permission` — at each macOS permission gate
```
kind: "accessibility" | "screen_recording" | "automation" | "mic"
granted: bool
```

### `onboarding` — each setup step
```
step: string              // stable step id, e.g. "welcome", "grant_ax", "connect_accounts", "first_task"
status: "reached" | "completed" | "abandoned"
```

### `account` — connector linked/removed
```
provider: "gmail" | "gcal" | "drive" | "messages" | "notes" | "contacts" | "browser_history"
action: "connected" | "removed"
```

### `task` — one per task (the core event)
```
task_id: uuid
intent_category: string   // classified ON DEVICE; never raw goal text
                          // "form_fill" | "email" | "research" | "file_op"
                          // | "navigation" | "data_entry" | "scheduling" | "other"
app: string               // target app/site bundle or host
summon: "voice" | "typed"
outcome: "success" | "partial" | "failed" | "stopped"
failure_reason: "none" | "element_not_found" | "stale_ref" | "verify_failed"
              | "vision_timeout" | "repeat_loop" | "user_stopped"
              | "model_refused" | "app_error" | "network"
step_count: int
action_count: int
retries: int
intervened: bool          // user corrected mid-run
user_accepted: bool|null  // kept result vs undid/redid; null = unknown
from_recipe: bool         // ran from a learned open-loop recipe (a repeat)
latency_ms: {             // sums across the whole task
  ttfa: int               // ask -> first action (perceived responsiveness)
  total: int              // ask -> done (wall clock)
  perceive: int           // building AX tree / DOM / vision capture
  model: int              // LLM round trips
  act: int                // executing clicks/typing
  verify: int             // post-action checks
}
path: {                   // step counts by perception path
  ax: int
  dom: int
  vision: int             // the slow path
}
models: [ { name: string, ms: int, calls: int } ]
tokens: { input: int, output: int }
cost_usd: number
```

### `error` — failure not tied to a single task outcome
```
kind: "crash" | "hang" | "model_timeout" | "network" | "auth_expired" | "voice_glitch"
where: string             // coarse location, e.g. "act_loop", "voice", "startup"
fatal: bool
```

### `download` — sent by the WEBSITE (not the app) via the counting redirect
```
platform: string          // "mac"
referrer?: string
utm_source?: string
utm_campaign?: string
```

## Server storage

- Raw append-only table `otto_events` (one row per event, `event_id` UNIQUE for
  idempotency, `props` as JSON/JSONB via `core/db.py`).
- Aggregates computed by query/materialized views; see
  `core/otto_telemetry/` and the `/admin/otto/*` metrics API.

## Notes

- `heartbeat` from schema v0 is **retired**: daily/weekly actives are derived
  from `launch` + `task` events instead. The old `/otto/telemetry/event` single
  endpoint is replaced by the batch `/otto/telemetry`.
