# Otto proxies — `/otto/*`

Authenticated proxies for the **Otto** desktop app, so no provider keys ship in the
downloadable app. aztea is the public front door (validates the app's baked
`OTTO_APP_TOKEN`); the LLM paths forward to a **private LiteLLM gateway** on the VM that
holds the real Azure keys, routes/falls back across providers, and enforces budgets.

```
Otto app ──Bearer OTTO_APP_TOKEN──▶ aztea (auth) ──▶ LiteLLM (127.0.0.1:4001) ──▶ Azure
```

| Route | Purpose | Upstream |
|---|---|---|
| `POST /otto/responses` | GPT-5.5 acting (Azure Responses API) — `part_018.py` | LiteLLM `/v1/responses` |
| `WS /otto/realtime` | Voice (Azure realtime) — `part_016.py` | LiteLLM `/v1/realtime` (aztea relays the socket) |
| `POST /otto/composio/{path}` | Composio connector relay — `part_017.py` | Composio (direct; not an LLM call) |
| ~~`POST /otto/chat`~~ | **Removed** — the app no longer ships an Anthropic client | — |

The LLM cutover is gated by **`OTTO_USE_LITELLM`**: `1` routes via LiteLLM, unset/`0` uses the
legacy direct-Azure path (rollback = one env toggle + aztea restart). Standing up LiteLLM and
minting the budgeted virtual keys is documented in **[`deploy/litellm/README.md`](../deploy/litellm/README.md)**.

## Server config (env vars)

Shared:

| Var | Purpose | Default |
|---|---|---|
| `OTTO_APP_TOKEN` | shared bearer secret — **must equal the app's baked-in token** | (required) |
| `OTTO_USE_LITELLM` | `1` → route LLM paths via LiteLLM; else legacy direct-Azure | unset |

LiteLLM gateway path (`OTTO_USE_LITELLM=1`):

| Var | Purpose | Default |
|---|---|---|
| `OTTO_RESPONSES_LITELLM_URL` | LiteLLM base for responses | `http://127.0.0.1:4001` |
| `OTTO_RESPONSES_LITELLM_KEY` | LiteLLM virtual key, `max_budget=$150` | (required) |
| `OTTO_RESPONSES_LITELLM_MODEL` | model alias to pin | `otto-responses` |
| `OTTO_REALTIME_LITELLM_URL` | LiteLLM ws base for realtime | `ws://127.0.0.1:4001` |
| `OTTO_REALTIME_LITELLM_KEY` | LiteLLM virtual key, `max_budget=$300` (backstop) | (required) |
| `OTTO_REALTIME_LITELLM_MODEL` | model alias to pin | `otto-realtime` |

Azure creds (`AZURE_RESPONSES_*`, `AZURE_REALTIME_*`) move **off aztea** and into the LiteLLM
stack (`deploy/litellm/.env`) on this path. They remain on aztea only for the legacy/rollback
path below.

Legacy / rollback path (`OTTO_USE_LITELLM` off):

| Var | Purpose | Default |
|---|---|---|
| `AZURE_RESPONSES_URL` / `AZURE_RESPONSES_KEY` | Azure Responses upstream (server-side) | (required) |
| `AZURE_RESPONSES_API_VERSION` / `AZURE_RESPONSES_MODEL` | api-version / deployment pin | `2025-04-01-preview` / — |
| `AZURE_REALTIME_URL` / `AZURE_REALTIME_KEY` | Azure realtime upstream (server-side) | (required) |
| `OTTO_RESPONSES_BUDGET_CAP_CENTS` | SQLite responses cap | `15000` ($150) |
| `OTTO_RT_BUDGET_CAP_CENTS` | realtime cap (always active — see below) | `30000` ($300) |
| `OTTO_BUDGET_DB` | sqlite path for the counters | `~/.otto-proxy-budget.sqlite3` |

## Budgets

- **Responses ($150):** on the LiteLLM path the cap is the responses virtual key's
  `max_budget`; LiteLLM rejects over-budget calls and aztea maps that to the app's
  `402 payment.spend_limit_exceeded` contract. On the legacy path it's the SQLite
  `otto_responses_budget` counter.
- **Realtime ($300):** aztea **always** meters realtime spend itself from the relayed
  `response.done` usage frames (`part_016.py`), independent of upstream — so the voice cap
  holds whether or not LiteLLM tracks audio spend. The realtime virtual key's `max_budget`
  is a secondary backstop.
- **Inspect/reset legacy SQLite counters:**
  `sqlite3 ~/.otto-proxy-budget.sqlite3 'SELECT * FROM otto_responses_budget; SELECT * FROM otto_rt_budget;'`
  The retired `otto_budget` (Anthropic/chat) table is now orphaned and can be dropped.

## Notes

- Rate limit: `120/minute` per client (slowapi) on `/otto/responses`. Tune in `part_018.py`.
- `part_015.py` is kept as a tombstone (no route) because `server/application.py` requires the
  `part_*.py` shards to be contiguous; renumbering is left for a dedicated cleanup.
