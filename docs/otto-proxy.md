# Otto proxy — `POST /otto/chat`

A **self-contained** authenticated passthrough to Anthropic's Messages API for
the **Otto** desktop app (`server/application_parts/part_015.py`). It deliberately
does **not** use aztea's user/wallet/payments framework — it's a standalone
shortcut so no Anthropic key ships in the downloadable app.

- **Auth:** the app sends `Authorization: Bearer <T>`; the endpoint checks `T`
  against the `OTTO_APP_TOKEN` env var (constant-time). The token is baked into
  the app and is therefore extractable — that's expected; the **budget** below is
  the real protection, not the token's secrecy.
- **Upstream:** forwards the `/v1/messages` body unchanged to Anthropic using the
  **server-side `ANTHROPIC_API_KEY`**, and returns the response verbatim.
- **Budget:** a single **shared spend pool**, tracked in a tiny SQLite counter and
  priced at real Anthropic rates. Each call reserves an upper-bound estimate, then
  reconciles to actual token cost, so the cap maps to real dollars. When the pool
  is exhausted → **HTTP 402** for every caller until it's reset/raised.

## Server config (env vars)

| Var | Purpose | Default |
|---|---|---|
| `OTTO_APP_TOKEN` | shared bearer secret — **must equal the app's baked-in token** | (required) |
| `ANTHROPIC_API_KEY` | the real Anthropic key (already used by aztea) | (required) |
| `OTTO_BUDGET_CAP_CENTS` | spend cap in cents | `20000` ($200) |
| `OTTO_BUDGET_DB` | sqlite path for the counter | `~/.otto-proxy-budget.sqlite3` |

**No provisioning, no DB migration, no aztea account needed.** Set
`OTTO_APP_TOKEN` (same value the app ships with) and `ANTHROPIC_API_KEY`, deploy,
done.

## Operating the cap

- **Raise the cap:** set `OTTO_BUDGET_CAP_CENTS` (e.g. `50000` = $500) and restart.
- **Reset / top up the pool:** `sqlite3 ~/.otto-proxy-budget.sqlite3 \
  'UPDATE otto_budget SET spent_cents = 0;'`
- **Check current spend:** `sqlite3 ~/.otto-proxy-budget.sqlite3 \
  'SELECT spent_cents FROM otto_budget;'`

## Notes

- Otto currently requests `claude-opus-4-8`. Switching the app to
  `claude-sonnet-4-6` roughly halves cost (same proxy, no server change).
- Rate limit: `120/minute` per client (slowapi). Tune in `part_015.py`.
- The budget is a flat shared total (no daily reset). Add a periodic
  `UPDATE … SET spent_cents = 0` (cron) if you want it to refill.
