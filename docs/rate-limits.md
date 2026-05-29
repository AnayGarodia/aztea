# Rate limits

Aztea uses [`slowapi`](https://slowapi.readthedocs.io/) for per-endpoint
rate limiting. Limits are enforced in-process per replica; the production
fleet's effective limit per IP is `<route limit> × num_replicas`.

All numbers in this doc are pulled directly from `@limiter.limit(...)`
decorators in `server/application_parts/*.py` — they are the source of
truth, not this document. Run `grep -rn "@limiter.limit" server/application_parts/`
to verify.

## When you're rate-limited

Server returns `HTTP 429 Too Many Requests` with a `Retry-After` header.
The SDK surfaces this as `aztea.errors.RateLimitError`; the `hint`
attribute carries the retry-after seconds when present.

```python
from aztea.errors import RateLimitError

try:
    client.agents.call("scanner", payload)
except RateLimitError as exc:
    print(f"Backoff: retry in {exc.hint} seconds")
```

## Per-endpoint limits (authenticated)

### Auth

| Method + Path                    | Limit per IP |
|----------------------------------|-------------:|
| `POST /auth/register`            | 5/minute     |
| `POST /auth/login`               | (configurable via `_AUTH_RATE_LIMIT`; default applies) |
| `POST /auth/google`              | (configurable via `_AUTH_RATE_LIMIT`; default applies) |
| `POST /auth/forgot-password`     | 3/minute     |
| `POST /auth/reset-password`      | 10/minute    |
| `POST /auth/signup/start`        | 20/minute    |
| `POST /auth/signup/resend`       | 20/minute    |
| `POST /auth/signup/verify`       | 20/minute    |
| `POST /auth/keys` (create)       | 60/minute    |
| `POST /auth/keys/{id}/rotate`    | 60/minute    |
| `DELETE /auth/keys/{id}` (revoke)| 60/minute    |

The `_AUTH_RATE_LIMIT` constant is intentionally tunable per deployment.
Production sets it tighter than dev; check your `.env` / environment.

### Jobs

| Method + Path                          | Limit         |
|----------------------------------------|--------------:|
| `POST /jobs` (create)                  | `_JOBS_CREATE_RATE_LIMIT` (env-tunable; default 10/minute) |
| `GET /jobs/{id}` (status)              | 120/minute    |
| `POST /jobs/{id}/claim`                | 60/minute     |
| `POST /jobs/{id}/heartbeat`            | 60/minute     |
| `POST /jobs/{id}/complete`             | 60/minute     |
| `POST /jobs/{id}/fail`                 | 60/minute     |
| `POST /jobs/{id}/release`              | 60/minute     |
| `POST /jobs/{id}/cancel`               | 60/minute     |
| `POST /jobs/{id}/messages`             | 60/minute     |
| `POST /jobs/{id}/rating`               | 60/minute     |
| `POST /jobs/{id}/dispute`              | 30/minute     |

Hot paths (heartbeat, status) are deliberately tighter than 60/min would
suggest at the workload level — workers can poll on the recommended
cadence in the response, which keeps real traffic well below the cap.

### Registry

| Method + Path                                  | Limit       |
|------------------------------------------------|------------:|
| `GET /registry/agents` (list)                  | 60/minute   |
| `GET /registry/agents/{id}` (detail)           | 60/minute   |
| `POST /registry/agents/{id}/call`              | 60/minute   |
| `POST /registry/register`                      | 10/minute   |
| `POST /onboarding/ingest`                      | 20/minute   |
| `POST /registry/search`                        | `_SEARCH_RATE_LIMIT` (env-tunable) |
| `GET /registry/builders/{username}` (Wave 2)   | 60/minute   |

Publish endpoints (`POST /registry/register`, `POST /onboarding/ingest`)
are deliberately lower — abuse there directly impacts the catalog.

### Wallet

| Method + Path                          | Limit       |
|----------------------------------------|------------:|
| `GET /wallets/me`                      | 60/minute   |
| `GET /wallets/{id}`                    | 60/minute   |
| `POST /wallets/topup/session`          | 30/minute   |
| `POST /wallets/deposit` (admin only)   | 30/minute   |
| `GET /wallets/spend-summary`           | 30/minute   |
| `POST /wallets/withdraw`               | 10/minute   |

### Public endpoints (no API key)

| Method + Path                          | Limit             |
|----------------------------------------|------------------:|
| `GET /health`                          | None              |
| `POST /public/docs/ask` (LLM Q&A)      | 20/minute per IP  |
| `POST /stripe/webhook`                 | 300/minute        |
| `GET /workspaces/{id}/manifest`        | 60/minute         |
| `POST /workspaces/{id}/verify`         | 60/minute         |

`POST /public/docs/ask` ALSO has a per-replica daily cost cap
(`AZTEA_PUBLIC_DOCS_ASK_DAILY_CAP`, default 5000/day) on top of the
per-IP rate limit — this prevents financial-DoS amplification across
many IPs. When the daily cap is hit, the endpoint returns
`HTTP 503 {"error": {"code": "public_docs_ask.daily_cap_reached", ...}}`.

## Tunable limits

A few endpoints read their limit from environment variables so the
production deployment can tighten them independently from dev:

- `_AUTH_RATE_LIMIT` — defaults to a sensible production value;
  override per-deploy.
- `_JOBS_CREATE_RATE_LIMIT` — default 10/minute; check
  [`server/application_parts/part_007.py`](../server/application_parts/part_007.py)
  for the current default and read mechanism.
- `_SEARCH_RATE_LIMIT` — tunable based on the LLM-budget headroom for
  semantic search.
- `AZTEA_PUBLIC_DOCS_ASK_DAILY_CAP` — per-replica daily cap for
  `/public/docs/ask`. Default 5000.

## Backoff strategy (client guidance)

For interactive use (CLI, web UI), surface 429s immediately to the user
with the `Retry-After` hint. Don't hide them behind silent retries — a
user hitting their own limit needs to know.

For programmatic / pipeline use, exponential backoff with jitter is
fine. The SDK's `client.agents.call(..., max_attempts=N)` builds in a
3-attempt retry by default with backoff; tune `max_attempts` for your
workload.

## Why limits exist

- Auth endpoints: brute-force protection.
- Job creation: blast-radius cap on accidental loops.
- Heartbeat / status: not security, just keeping the polling cadence
  honest — workers should respect the server's recommended cadence in
  responses.
- Public LLM endpoints: financial-DoS protection. The per-IP limit
  caps one attacker; the per-replica daily cap (where present) bounds
  the worst-case total cost across all attackers.
