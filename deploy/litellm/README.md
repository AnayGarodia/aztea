# LiteLLM gateway for the Otto proxies

LiteLLM sits **privately** behind aztea on the VM (`127.0.0.1:4001`, never public) and is the
upstream for the GPT-5.5 (`/otto/responses`) and realtime voice (`/otto/realtime`) paths.
aztea stays the public front door: it validates the app's `OTTO_APP_TOKEN`, then forwards to
LiteLLM, which holds the Azure keys, does provider routing/fallback, and enforces budgets.

```
Otto app ──Bearer OTTO_APP_TOKEN──▶ aztea (auth) ──▶ LiteLLM (127.0.0.1:4001) ──▶ Azure
```

The whole cutover is gated by **`OTTO_USE_LITELLM`** in aztea's env: `1` = route via LiteLLM,
unset/`0` = legacy direct-Azure path. Rollback is one env toggle + an aztea restart.

## 1. First boot (on the VM)

```bash
cp deploy/litellm/.env.example deploy/litellm/.env
# edit deploy/litellm/.env — set LITELLM_MASTER_KEY, LITELLM_DB_PASSWORD, AZURE_* creds
docker compose -f deploy/litellm/docker-compose.yml --env-file deploy/litellm/.env up -d
docker compose -f deploy/litellm/docker-compose.yml logs -f litellm   # wait for healthy
curl -s http://127.0.0.1:4001/health/liveliness                       # -> "I'm alive!"
```

## 2. Mint the two budgeted virtual keys

The `$150` responses cap and the `$300` realtime backstop live on these keys' `max_budget`.

```bash
MASTER=sk-...   # = LITELLM_MASTER_KEY

# Responses key — $150 pool, scoped to the otto-responses model alias.
curl -s http://127.0.0.1:4001/key/generate \
  -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d '{"models":["otto-responses"],"max_budget":150,"key_alias":"otto-responses"}'

# Realtime key — $300 backstop, scoped to the otto-realtime model alias.
curl -s http://127.0.0.1:4001/key/generate \
  -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d '{"models":["otto-realtime"],"max_budget":300,"key_alias":"otto-realtime"}'
```

Put the returned keys into aztea's env (see step 3). Re-running `/key/generate` makes a new
key; to inspect/raise a budget later use `/key/info` and `/key/update`.

> Realtime note: aztea **also** meters realtime spend from the relayed `response.done`
> frames (see `part_016.py`), so the $300 voice cap is enforced by aztea regardless of
> whether LiteLLM tracks audio spend. The realtime key budget is a secondary backstop.

## 3. aztea env to add (systemd/env), then flip the flag

```bash
OTTO_USE_LITELLM=1
OTTO_RESPONSES_LITELLM_URL=http://127.0.0.1:4001
OTTO_RESPONSES_LITELLM_KEY=<responses key from step 2>
OTTO_REALTIME_LITELLM_URL=ws://127.0.0.1:4001
OTTO_REALTIME_LITELLM_KEY=<realtime key from step 2>
# Optional alias overrides (defaults shown):
# OTTO_RESPONSES_LITELLM_MODEL=otto-responses
# OTTO_REALTIME_LITELLM_MODEL=otto-realtime
```

Then restart aztea in a **foreground** SSH session with plain `systemctl` and watch health
from outside the box (per the aztea-ops runbook — never deploy via a backgrounded SSH).

## 4. Smoke tests before trusting it

- **Responses wire-format:** capture one real Otto payload (`OTTO_TRACE_RAW=1` in the app),
  replay it: `curl http://127.0.0.1:4001/v1/responses -H "Authorization: Bearer <resp key>"
  -H "Content-Type: application/json" -d @payload.json` → expect a valid Responses object.
- **Responses budget:** mint a throwaway `max_budget:0.01` key, call twice → second call must
  return a budget error; confirm aztea maps it to HTTP **402 `payment.spend_limit_exceeded`**.
- **Through aztea:** `POST https://aztea.ai/otto/responses` with a valid `OTTO_APP_TOKEN` →
  GPT-5.5 responds; bad token → 401.
- **Realtime:** connect a WS client to `wss://aztea.ai/otto/realtime` → voice works; confirm
  no added audio lag and that aztea's `otto_rt_budget` increments.

## Rollback

Set `OTTO_USE_LITELLM=0` (or unset) in aztea's env and restart aztea. The routes fall back to
the direct-Azure path immediately; LiteLLM can keep running or be torn down.
