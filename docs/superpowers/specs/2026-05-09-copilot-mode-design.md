# Co-Pilot Mode — Steerable Calls + Stop-Conditions

**Status:** Approved design, ready for implementation plan.
**Date:** 2026-05-09
**Owner:** anay (with cofounder review)
**Effort estimate:** 1.5–2 days
**Worktree:** `.claude/worktrees/copilot-mode/` (branch `feat/copilot-mode`)

## Goal

Turn long agent calls into a bidirectional protocol:

1. The agent streams `partial_output` events while running.
2. The caller can `POST /jobs/{id}/steer` mid-flight to inject guidance.
3. The caller can preset `stop_when` JMESPath predicates that abort the call at the exact partial that matches, with billing settled to the precise unit count.
4. On any terminal transition the agent signs the entire transcript (input → partials → steers → output → stop reason) with its existing per-call Ed25519 key. The caller fetches a verifiable JWS receipt.

Two new MCP tools (`aztea_call_streaming`, `aztea_steer`) make this usable end-to-end. The first Cursor user who corrects a SAST scanner mid-run never goes back to one-shot.

## Non-goals (v1)

- SSE / WebSocket transport (long-poll only).
- Built-in agent steering — only hosted skills (`core/skill_executor.py`) and external workers honor steers in v1. Built-ins that run synchronously inside `_execute_builtin_agent()` are out of scope.
- Mutable `stop_when` after job creation.
- Platform co-signed receipts (would require `did:web:aztea.ai/.well-known/did.json`, which doesn't exist today).
- Frontend "live partial" UI. The Job Timeline gets a `stopped` pill and nothing more.

## Design

### Architecture (one paragraph)

Extend the existing poll-based `job_messages` protocol with two new message types (`partial_output`, `steer`). Add an optional `stop_when` array of JMESPath predicates set at job creation, plus an optional `billing_unit` of `'call'` or `'partial'`. Hosted skills and external workers cooperatively read the steer inbox between turns and emit partials via a new SDK helper. State transitions to terminal happen inside the messaging transaction, but **settlement and receipt-signing are decoupled**: the messaging tx writes a `pending_settlements` row; a settlement runner drains it idempotently — both synchronously after the request returns and asynchronously via the existing sweeper. Receipts are signed by the **agent's** existing per-call Ed25519 key over the canonical transcript. A long-poll variant of `GET /jobs/{id}/messages` keeps streaming traffic cheap.

### Data model — `migrations/0032_copilot_mode.sql`

```sql
ALTER TABLE jobs ADD COLUMN stop_when_json     TEXT;
ALTER TABLE jobs ADD COLUMN stop_reason_json   TEXT;
ALTER TABLE jobs ADD COLUMN partials_count     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN steer_count        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN billing_unit       TEXT
    CHECK (billing_unit IN ('call','partial') OR billing_unit IS NULL);
ALTER TABLE jobs ADD COLUMN receipt_jws        TEXT;
ALTER TABLE jobs ADD COLUMN terminal_at        TEXT;     -- ISO8601 stamp; set exactly once
ALTER TABLE jobs ADD COLUMN terminal_message_id INTEGER; -- max(job_messages.id) at terminal moment

CREATE TABLE pending_settlements (
    job_id            TEXT PRIMARY KEY,
    terminal_state    TEXT NOT NULL,    -- complete | failed | cancelled | stopped
    terminal_at       TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    settled_at        TEXT,
    receipt_built_at  TEXT
);
CREATE INDEX idx_pending_settlements_unsettled
    ON pending_settlements(settled_at) WHERE settled_at IS NULL;
```

`pending_settlements` is **not** copilot-specific — it absorbs the existing `complete`, `failed`, and `cancelled` paths too (boy-scout sweep, see below). This is a small generalization, not a new pattern.

### Message types

| `msg_type` | Direction | Lease effect | Notes |
|---|---|---|---|
| `partial_output` | agent → caller | extends lease (same as `progress`) | payload free-form `dict`. Insert increments `jobs.partials_count`. Server runs `stop_when` synchronously inside the same tx — see "Stop_when evaluation" below. |
| `steer` | caller → agent | **none** (no extend, no transition) | payload `{message: str, metadata?: dict}`. Capped at `STEER_MAX_PER_JOB=20` and `STEER_MAX_RATE_PER_CALLER=30/min`. |

`core/jobs/db.py::_LEASE_BEHAVIORS` adds:
- `partial_output` → `_LEASE_BEHAVIOR_EXTEND`
- `steer` → new `_LEASE_BEHAVIOR_NONE`

### Settlement out of the messaging transaction (Issue 1)

**Tx A** — inside `record_job_message`, short, no I/O:
1. Insert the message row.
2. If `partial_output` and any `stop_when` predicate matches → state→`stopped`, write `stop_reason_json`, write `terminal_at` and `terminal_message_id`, INSERT a `pending_settlements` row.
3. Commit.

**Tx B** — settlement runner, drains `pending_settlements`:
1. Lease one unsettled row (`SELECT … WHERE settled_at IS NULL FOR UPDATE SKIP LOCKED` on Postgres; row-lease pattern on SQLite).
2. Run the right `core/payments/` primitives based on `terminal_state` + `billing_unit`.
3. Build canonical transcript, agent-sign it, store JWS on `jobs.receipt_jws`.
4. Stamp `settled_at` and `receipt_built_at`. Idempotent on re-run — every step checks the corresponding stamp before acting.

**Drain triggers:**
- Synchronous: every terminal-transition request calls the runner once before returning, so callers see receipts immediately.
- Asynchronous: the existing sweeper in `part_006.py` re-tries any row whose `attempts < N` and `settled_at IS NULL`, with backoff.

This removes signing and ledger writes from the messaging tx entirely — no write-lock-held-during-Ed25519-signing, and `messaging.py` doesn't become a writer to the ledger.

### Receipt signing (Issue 2)

- **Signer:** the agent's existing per-call Ed25519 key, minted in `core/identity.py`. No new platform DID.
- **Transcript schema** (canonicalized via `core/crypto.canonical_json`):
  ```json
  {
    "schema": "aztea/copilot-receipt/1",
    "job_id": "...",
    "agent_id": "...",
    "caller_id": "...",
    "input": { ... },
    "messages": [
      {"id": 12, "type": "partial_output", "from": "agent", "payload": {...}, "ts": "..."},
      {"id": 13, "type": "steer",          "from": "caller","payload": {...}, "ts": "..."},
      {"id": 14, "type": "partial_output", "from": "agent", "payload": {...}, "ts": "..."}
    ],
    "output": { ... } | null,
    "terminal_state": "complete|stopped|failed|cancelled",
    "stop_reason": {"label": "...", "expr": "...", "matched_message_id": 14} | null,
    "terminal_at": "..."
  }
  ```
- **Wire format:** JWS-compact (`base64url(header) "." base64url(payload) "." base64url(sig)`), header `{"alg":"EdDSA","kid":"<agent did:web URL fragment>"}`.
- **Endpoint:** `GET /jobs/{id}/receipt` returns `{jws, transcript, public_jwk, kid}`. Returns `425 Too Early` if `pending_settlements.receipt_built_at IS NULL`.
- **Backfill in this PR:** the existing `complete` / `failed` / `cancelled` terminal paths also build and store this receipt. Today they emit a per-output agent signature only; that becomes the receipt's signature over the full transcript. Two formats must not coexist.

### Bounded JMESPath (Issue 3)

**Submit-time validation** in `POST /jobs`:
- `len(stop_when) ≤ 8`
- For each predicate: `len(expr) ≤ 500`, `len(label) ≤ 64`
- AST inspection: walk `jmespath.compile(expr).parsed`; reject any expression containing a wildcard projection (`[*]` / `*`) at depth > 2, or `||` chains > 4. Implementation lives in `core/copilot_predicates.py`.
- Parse failure or any of the above → `400 stop_when.invalid` with the predicate label.

**Runtime evaluation** (per partial, per predicate):
- 25ms wallclock budget enforced via a thread-pool `concurrent.futures.ThreadPoolExecutor` with `future.result(timeout=0.025)`.
- On timeout: log `{job_id, predicate_label, partial_id}`, increment `aztea_stop_when_timeouts_total`, **skip that predicate for this partial only**.
- Recompile per partial — JMESPath compile is microseconds; no cache.

### Race ordering (Issue 4)

- `job_messages.id` is the strict ordering authority (already an autoincrement / sequence on both backends).
- Once `terminal_at` is stamped on the job, `record_job_message` rejects further `partial_output` and `steer` with `409 job.terminal`.
- `read_steers(since_id)` filters `WHERE id <= jobs.terminal_message_id` so an agent that races a steer-read against a stop-fire never sees post-terminal steers.
- Transcript is built strictly by `id ASC`.

Tests — see "Tests" section: `test_steer_after_terminal_rejected`, `test_partial_after_terminal_rejected`, `test_concurrent_steer_and_stop_fire_orders_by_id`.

### Long-poll on the existing endpoint (Issue 5)

`GET /jobs/{id}/messages?since_id=N&wait_ms=2000` (cap `wait_ms ≤ 25_000`):
- If `since_id < max(id)` for the job → return immediately with the new messages.
- Else condition-wait on a per-job in-process `asyncio.Event`. New messages signal the event; cross-worker callers just time out and return empty (acceptable; they re-poll).
- Returns `200` with `{messages: []}` on timeout — **not** `408`. MCP client re-issues with the same `since_id`.

Postgres LISTEN/NOTIFY would scale across workers but is YAGNI for v1.

### Routes

All on existing `/jobs/{id}/*` paths. **Shard placement:** new routes go in a new shard `server/application_parts/part_014.py` rather than fattening `part_007.py` (1358 lines today). The shard owns:
- `POST /jobs/{id}/steer` — caller-scoped, must own job; `STEER_MAX_PER_JOB`, `STEER_MAX_RATE_PER_CALLER`.
- `GET /jobs/{id}/receipt` — returns `{jws, transcript, public_jwk, kid}`. `425` if not yet built.

Modifications to existing routes:
- `POST /jobs` — accept `stop_when: [{label, expr}]` and `billing_unit: 'call'|'partial'` in the request body. Validates per the bounds above.
- `GET /jobs/{id}/messages` — new optional `wait_ms` query param.

### Skill executor (`core/skill_executor.py`)

New helpers exposed to hosted skill code:

```python
aztea.emit_partial(payload: dict) -> None
    # POSTs partial_output for the current job. Raises SkillStoppedError if
    # the response indicates the job transitioned to 'stopped' (caller stop_when fired).

aztea.read_steers(since_id: int | None = None) -> tuple[list[dict], int]
    # Returns (steer messages with id > since_id and id <= terminal_message_id, new_cursor).
```

Skill execution loop:
1. Before each LLM/tool turn, call `read_steers()`; thread the latest steers into the next prompt.
2. After each meaningful intermediate result, call `emit_partial()`.
3. Catch `SkillStoppedError` as a clean terminal — do not log as an error.

### MCP tools (`scripts/aztea_mcp_server.py`)

```python
aztea_call_streaming(slug, input, stop_when=None, billing_unit=None,
                     wait_ms=2000, timeout_s=300)
    # Submits async job, long-polls /jobs/{id}/messages, yields each partial_output
    # via MCP progress notifications. Returns {output, receipt_jws, stop_reason}.

aztea_steer(job_id, message)
    # POSTs /jobs/{id}/steer. Returns {steer_count}.
```

Both registered as lazy tools alongside the existing seven (`scripts/aztea_mcp_server.py:_LAZY_TOOL_NAME_ALIASES` etc.).

### SDK addition (surgically minimal)

`sdks/python-sdk/aztea/agent_server.py`:

```python
class AgentServer:
    def emit_partial(self, job_id: str, payload: dict) -> None: ...
    def read_steers(self, job_id: str, since_id: int | None = None) -> tuple[list[dict], int]: ...
```

That is the entire SDK delta. No surrounding refactor.

### Boy-scout sweep — `stopped` as a new terminal state

Every "is this job done" call site must learn `stopped` in this PR. Greppable list:

- `core/jobs/db.py` — `_TERMINAL_STATES` (or equivalent set)
- `core/jobs/leases.py` — sweeper helpers
- `server/application_parts/part_006.py` — background sweeper
- `sdks/python-sdk/aztea/client.py` — `wait_for_completion`
- `sdks/python/...` — resource-oriented SDK
- `scripts/aztea_mcp_server.py` — `manage_job` resource handler
- `core/disputes.py` — dispute deadline calculator (terminal jobs only get rated)
- `core/reputation.py` — rating windows
- `tui/` — poll helpers
- `scripts/client_cli.py` — CLI poll
- Prometheus metric labels for `terminal_state`
- `frontend/src/features/jobs/JobTimeline.jsx` — render `stopped` pill (use `--color-warning` token)
- `frontend/src/api.js` — any client-side terminal-state assertions

Spec checklist — every line above gets a checkbox in the implementation plan.

## Tests

Must pass on both Postgres and SQLite. New file `tests/integration/test_copilot_mode.py`:

- `test_partial_output_extends_lease`
- `test_steer_does_not_extend_lease`
- `test_stop_when_aborts_at_exact_partial`
- `test_stop_when_invalid_jmespath_rejected_at_submit`
- `test_stop_when_complexity_rejected_at_submit`
- `test_stop_when_eval_timeout_skips_predicate_only`
- `test_billing_unit_partial_settles_proportionally`
- `test_billing_unit_call_full_charge_on_stop`
- `test_steer_rate_limit_per_job_429`
- `test_steer_rate_limit_per_caller_429`
- `test_receipt_signature_verifies_against_agent_jwk`
- `test_receipt_contains_full_transcript_in_id_order`
- `test_receipt_built_on_complete_failed_stopped_cancel` (parametrized over the four terminal states)
- `test_receipt_idempotent_under_double_drain`
- `test_steer_after_terminal_rejected`
- `test_partial_after_terminal_rejected`
- `test_concurrent_steer_and_stop_fire_orders_by_id`
- `test_long_poll_returns_immediately_on_new_message`
- `test_long_poll_times_out_with_empty_array`
- `test_pending_settlements_drained_by_sweeper_on_failure`

Plus extension to `tests/test_oss_mode_isolation.py` confirming no hosted call is added on the receipt path.

## Files touched

**New:**
1. `migrations/0032_copilot_mode.sql`
2. `core/copilot_predicates.py` — JMESPath compile, complexity walker, bounded eval
3. `core/receipts.py` — transcript builder, sign + JWS encode, store
4. `core/payments/copilot_settlement.py` — partial-unit settlement orchestration (calls existing `core/payments/` primitives, does not touch ledger directly)
5. `core/settlement_runner.py` — `pending_settlements` drain (sync + async paths)
6. `server/application_parts/part_014.py` — `/jobs/{id}/steer` and `/jobs/{id}/receipt`
7. `tests/integration/test_copilot_mode.py`

**Modified:**
1. `core/models/messages_ops.py` — `PartialOutputMessage`, `SteerMessage`, canonical type list
2. `core/jobs/db.py` — `_LEASE_BEHAVIORS` entries; `_LEASE_BEHAVIOR_NONE`; `partials_count` / `steer_count` increments; `terminal_at` / `terminal_message_id` stamping; `_TERMINAL_STATES` includes `stopped`
3. `core/jobs/messaging.py` — handle the two new types; integrate stop_when evaluation; emit pending-settlement row on stop; reject post-terminal sends with `409`
4. `core/jobs/leases.py` — confirm steer doesn't extend; integrate with terminal stamping
5. `core/skill_executor.py` — `emit_partial`, `read_steers`, `SkillStoppedError`
6. `server/application_parts/part_006.py` — settlement-runner drain in sweeper; learn `stopped`
7. `server/application_parts/part_008.py` — `POST /jobs` (`jobs_create`) accepts `stop_when` + `billing_unit`. *Note: this shard is already 2553 lines (over budget on `main`). Add only the minimum field-passthrough; all validation logic lives in `core/copilot_predicates.py`. Do not refactor the shard in this PR.*
8. `server/application_parts/part_010.py` — `GET /jobs/{id}/messages` learns `wait_ms`
9. `scripts/aztea_mcp_server.py` — register `aztea_call_streaming`, `aztea_steer`; `manage_job` learns `stopped`
10. `sdks/python-sdk/aztea/agent_server.py` — two new helpers
11. `sdks/python/...` — `stopped` in resource-oriented SDK terminal sets
12. `frontend/src/features/jobs/JobTimeline.jsx` — `stopped` pill
13. `frontend/src/api.js` — terminal-state assertions
14. `core/disputes.py`, `core/reputation.py` — rating/dispute windows include `stopped`
15. `tui/` and `scripts/client_cli.py` — poll helpers learn `stopped`
16. Existing `complete` / `failed` / `cancelled` paths in `core/jobs/leases.py` — migrate to `pending_settlements` enqueue + runner

## Risks / open issues

- **In-flight `.claude/worktrees/strict-rules-refactor/`** is touching shards (notably `part_008.py`, `part_009.py`, `part_011.py`). Co-pilot mode adds a *new* shard (`part_014.py`) and avoids modifying those, so conflict surface is low — but `part_006.py` (sweeper) and `core/jobs/db.py` are likely overlap points. Mitigation: rebase before merge; run the full integration suite on rebase.
- **Line-budget rule is currently broken on `main`** (47 files >1000). Co-pilot mode does not regress this — every new module stays well under 500 lines, and routes go in a fresh shard.
- **Long-poll across workers** is single-process; multi-worker callers fall back to short-poll on timeout. Acceptable for v1; revisit if the hosted deployment scales out.
- **Settlement runner failure modes:** synchronous drain may fail on transient DB errors; the sweeper picks it up. If both fail and `attempts ≥ N`, we surface a `pending_settlements` Prometheus alert.

## Implementation order

The implementation plan (next step, via `writing-plans`) will phase this as:

1. Migration + models + lease behaviors (foundation, no behavior change).
2. `pending_settlements` table + settlement runner; migrate existing terminal paths.
3. Receipt builder + signing + `GET /jobs/{id}/receipt` (works for non-stopped terminals first).
4. `partial_output` + `stop_when` evaluation + `stopped` terminal state.
5. `steer` message type + `POST /jobs/{id}/steer` + rate limits.
6. Long-poll on `GET /jobs/{id}/messages`.
7. Skill executor helpers + SkillStoppedError integration.
8. SDK additions.
9. MCP tools.
10. Frontend / SDK / TUI / CLI / disputes / reputation `stopped` sweep.
11. Tests pass on both backends.

## Acceptance criteria

- All tests in `tests/integration/test_copilot_mode.py` pass on Postgres and SQLite.
- Existing test suite (`pytest -q tests --ignore=tests/test_sdk_contract.py`) still passes (currently 723/2 skipped).
- `python scripts/check_file_line_budget.py` does not regress (no new file >1000 introduced; existing offenders untouched).
- A receipt fetched for a `stopped` job verifies offline against the agent's published JWK.
- An MCP `aztea_call_streaming` invocation against a hosted skill that emits 5 partials, with `stop_when=[{label:'safe',expr:'severity == \\'critical\\''}]` triggering on partial 3, returns `{output, receipt_jws, stop_reason: {label:'safe', matched_message_id: <id>}}` and bills `3 * unit_price` cents when `billing_unit='partial'`.
