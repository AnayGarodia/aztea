# Otto proxy — `POST /otto/chat`

A thin authenticated passthrough to Anthropic's Messages API for the **Otto**
desktop app (`server/application_parts/part_015.py`).

The app authenticates with a shared **"Otto app"** API key
(`Authorization: Bearer az_...`). The endpoint forwards the request to Anthropic
using the **server-side `ANTHROPIC_API_KEY`**, so no Anthropic key ships inside
the downloadable app. The standard `/v1/messages` body (tools included) is passed
through unchanged; the response is returned verbatim.

## Spend cap (metered on the wallet balance)

Spend is metered against the Otto service wallet's **balance**, reconciled to the
**actual** token cost of each call — so the balance is the true running spend, and
**$200 of balance = $200 of real spend**. When the balance can't cover the next
call, `pre_call_charge` raises `InsufficientBalanceError` → **HTTP 402** for every
caller sharing the key ("at capacity"). The cap is a **single shared pool** — all
users draw from the same balance.

> We meter on balance, not the per-key `max_spend_cents`, because
> `post_call_refund` does not carry `charged_by_key_id`, so a refund would not net
> against the per-key cap (it counts gross charges). Balance nets correctly.

## Prerequisites (server)

- `ANTHROPIC_API_KEY` must be set in the production environment (the endpoint
  reads it via `os.environ`). aztea already uses Anthropic, so this is likely
  already present — confirm it's set for the API service.

## One-time provisioning (the $200 "Otto app" key)

Create, via your existing admin/signup path (or a one-off script using
`core.auth` + `core.payments`):

1. A service **user** (e.g. `otto-app`).
2. An **API key** for that user with scope **`caller`** and **no** per-key
   `max_spend_cents` (leave it NULL — the wallet balance is the cap).
3. **Credit that user's wallet to `20000` cents ($200)** via
   `payments.get_or_create_wallet(owner_id)` + your credit/top-up function. This
   balance is the cap; **to raise the cap, top up the balance.**
4. Hand the resulting `az_...` key to the Otto build as `OTTO_APP_TOKEN` (it gets
   baked into `AppToken.swift` at build time and never committed).

When the $200 is spent, calls return 402 until the wallet is topped up.

## Notes

- Otto currently requests `claude-opus-4-8`. Switching the app to
  `claude-sonnet-4-6` roughly halves cost (same proxy, no server change).
- Rate limit: `120/minute` per client (slowapi). Tune in `part_015.py`.
