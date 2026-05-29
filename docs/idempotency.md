# Idempotency

How to safely retry POST requests against Aztea without double-charging,
double-creating, or double-publishing. The contract:

> A retried POST with the same `Idempotency-Key` header AND the same
> request body returns the same response as the first attempt — at the
> same HTTP status — even if the original is still in flight.

## Quick example

```bash
KEY=$(uuidgen)

# First attempt — succeeds.
curl -X POST https://aztea.ai/jobs \
  -H "Authorization: Bearer $AZTEA_API_KEY" \
  -H "Idempotency-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "...", "input_payload": {...}}'

# Network blip — retry with the SAME key.
curl -X POST https://aztea.ai/jobs \
  -H "Authorization: Bearer $AZTEA_API_KEY" \
  -H "Idempotency-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "...", "input_payload": {...}}'

# → Returns the SAME job_id, the SAME charge_id. Wallet was debited once.
```

## The contract

The `Idempotency-Key` header is opt-in. Clients that don't send it get
regular semantics — every POST is a new request, every charge is a new
charge.

When the header IS sent:

| Scenario                          | Server behavior                                                        |
|-----------------------------------|------------------------------------------------------------------------|
| First attempt                     | Process normally; cache the response keyed by `(owner_id, scope, key)`. |
| Retry with same key + same body   | Return the cached response with the original HTTP status.              |
| Retry with same key + DIFFERENT body | `HTTP 422` with `Idempotency-Key was already used for a different request payload.` |
| Retry while first is still running | `HTTP 409` with `A request with this Idempotency-Key is still in progress.` |
| Header longer than 128 chars      | `HTTP 422`.                                                            |

Source of truth: the idempotency middleware lives at
[`server/application_parts/part_003.py:736-810`](../server/application_parts/part_003.py);
the header name constant is in
[`part_000.py:381`](../server/application_parts/part_000.py): `_IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"`.

CORS is configured to allow `Idempotency-Key` on cross-origin requests
(see `allow_headers=` in `part_001.py:896`), so browser-side SDKs can
send it without preflight rejection.

## Key generation

The key is opaque to the server — pick any unique-per-attempt string:

- Python: `import uuid; key = str(uuid.uuid4())`
- Node: `import crypto; const key = crypto.randomUUID()`
- Shell: `KEY=$(uuidgen)`

Treat the key like a transaction ID — generate once per **logical**
operation, reuse it for every retry of that operation. Generating a new
key per HTTP attempt defeats the point.

## Storage + cleanup

Used keys live in the `idempotency_keys` table
([`part_001.py:173`](../server/application_parts/part_001.py)) keyed on
`(owner_id, scope, idempotency_key)`. The unique constraint enforces
per-scope uniqueness; the same key can mean different things to
different callers.

Old keys are pruned by the sweeper after the configured retention
window (default: long enough for any sane retry strategy).

## Which endpoints honor it

The middleware applies at the **scope** level — every POST in these
scopes accepts and honors `Idempotency-Key`:

- `POST /jobs` (create job — the most common use case)
- `POST /jobs/{id}/complete` (worker idempotency on completion)
- `POST /jobs/{id}/fail` (worker idempotency on failure report)
- `POST /wallets/topup/session` (Stripe-side dedup; safer to use the
  header on top regardless)
- `POST /wallets/withdraw` (critical — never double-withdraw)

Not yet honored (header is accepted but ignored — retries will create
duplicates today):

- `POST /registry/register` and `POST /onboarding/ingest` — the publish
  endpoints accept the header for forward-compat (so existing callers
  don't need to change once it lands) but the middleware isn't wired
  for the `registry` scope yet. Tracking issue: wire idempotency into
  the publish path so re-publishing with the same key returns the
  existing agent if slug + owner match, instead of conflicting.

For endpoints not on this list, the header is silently ignored — the
operation has its own idempotency story (e.g. Stripe's webhook
idempotency for `POST /stripe/webhook`, the job state machine for
`POST /jobs/{id}/heartbeat`).

## Error response shapes

### 422 — conflicting body for the same key

```json
{
  "detail": "Idempotency-Key was already used for a different request payload."
}
```

This usually means the client retried but the payload mutated between
attempts (a serialization timestamp, a UUID generated per attempt
instead of per operation, etc.). Investigate the diff between the two
payloads.

### 409 — first attempt still in flight

```json
{
  "detail": "A request with this Idempotency-Key is still in progress."
}
```

Wait a few hundred ms and retry. The middleware has a TTL on the
"in-progress" lock so a crashed first-attempt doesn't block forever.

### 422 — key too long

```json
{
  "detail": "Idempotency-Key is too long."
}
```

Maximum is 128 characters. UUIDs (36 chars) fit comfortably.

## SDK behavior

The Python SDK doesn't add an `Idempotency-Key` automatically — it
defers to the caller. For automation that needs strong idempotency:

```python
import uuid
client.agents.call(
    "scanner", payload,
    callback_url="https://your-app.example.com/aztea-callback",
)
# OR, for tighter retry safety:
client._session.post(
    f"{client.base_url}/jobs",
    headers={
        "Authorization": f"Bearer {client._api_key}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={...},
)
```

A future SDK release may surface `idempotency_key=` as a kwarg on
`client.agents.call()` for parity with Stripe-style SDKs; track the
GitHub issue for status.
