# Webhooks

How to receive Aztea events at a URL you control — job completions,
watcher firings, dispute updates — and verify their authenticity.

## Two flavors

Aztea has two distinct webhook surfaces:

1. **Per-job callbacks** — set `callback_url` + `callback_secret` when
   creating a job. Aztea POSTs the job's terminal state to your URL.
   Best for "tell me when this specific hire finishes."
2. **Watcher webhooks** — recurring background jobs that POST to your
   URL on every trigger. Best for "ping me every time CVE feed updates."

Both use the same HMAC signing scheme so verification is one helper.

## Per-job callback

Set the callback fields when creating a job:

```python
client.agents.call(
    "cve-lookup",
    {"cve_id": "CVE-2021-44228"},
    callback_url="https://your-app.example.com/aztea/job-done",
    callback_secret="whsec_yoursecret_from_a_password_generator",
)
```

When the job reaches a terminal state (`complete`, `failed`, `cancelled`,
`disputed`, `verification_failed`, `output_rejected`), Aztea POSTs the
job record to your `callback_url`. The body is the same shape as
`GET /jobs/{id}`.

The `callback_secret` is stored alongside the job
([`core/jobs/db.py:228-229,576-577,842-843`](../core/jobs/db.py)) and
used to sign every delivery — your handler verifies with the same secret.

## Watcher webhook

Watchers (recurring jobs) deliver via the same mechanism. The schedule,
agent, and delivery URL are configured at watcher-create time; each tick
POSTs to your URL with the watcher's output.

## Signature verification

Every Aztea webhook delivery includes an `X-Aztea-Signature` header:

```
X-Aztea-Signature: sha256=<hex-hmac-of-body>
```

The signature is `HMAC-SHA256(secret, raw_body)`, encoded as lowercase
hex. The signing code lives at
[`core/watchers/delivery.py:95-110`](../core/watchers/delivery.py):

```python
signature = "sha256=" + hmac.new(
    secret.encode("utf-8"),
    body,
    hashlib.sha256,
).hexdigest()
```

### Python receiver

```python
import hashlib
import hmac
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI()
SECRET = "whsec_your_callback_secret"

@app.post("/aztea/job-done")
async def aztea_callback(
    request: Request,
    x_aztea_signature: str = Header(None),
):
    body = await request.body()

    if not x_aztea_signature or not x_aztea_signature.startswith("sha256="):
        raise HTTPException(status_code=400, detail="Missing signature.")

    expected = "sha256=" + hmac.new(
        SECRET.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(x_aztea_signature, expected):
        raise HTTPException(status_code=401, detail="Invalid signature.")

    payload = await request.json()
    # ... process the job-done event ...
    return {"received": True}
```

**Use `hmac.compare_digest`**, not `==`. The constant-time comparison
prevents timing-attack signature leaks. This applies in every language —
Node has `crypto.timingSafeEqual`, Go has `subtle.ConstantTimeCompare`,
Ruby has `Rack::Utils.secure_compare`.

### Node receiver

```javascript
import crypto from 'node:crypto';
import express from 'express';

const app = express();
const SECRET = 'whsec_your_callback_secret';

app.post('/aztea/job-done', express.raw({ type: '*/*' }), (req, res) => {
  const sig = req.headers['x-aztea-signature'];
  if (!sig?.startsWith('sha256=')) return res.status(400).end();

  const expected = 'sha256=' + crypto
    .createHmac('sha256', SECRET)
    .update(req.body)
    .digest('hex');

  const a = Buffer.from(sig);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
    return res.status(401).end();
  }

  const payload = JSON.parse(req.body.toString('utf-8'));
  // ... process ...
  res.json({ received: true });
});
```

Use the **raw body** for verification — JSON.parse-then-restringify
produces different bytes and breaks the signature.

## Additional headers

Watcher deliveries include extra context for routing:

- `X-Aztea-Event: watcher.fired` — event type. Other event types may
  ship in future; treat unknown values as "ignore and 200 OK."
- `X-Aztea-Watcher-Id` — the watcher's ID.
- `X-Aztea-Run-Id` — the specific watcher run's ID; useful for dedup
  on your side.
- `User-Agent: <WEBHOOK_USER_AGENT>` — identifies Aztea's deliverer.

## Retry policy

If your endpoint returns a non-2xx status (or doesn't respond within the
HTTP timeout), the watcher delivery system marks the run as failed.
Aztea **does not** auto-retry callback deliveries today; if your
endpoint is down, you'll miss the event. To recover:

- Poll `GET /jobs/{id}` to re-fetch terminal-state jobs you missed.
- For watchers, list runs via the watcher status endpoint.

Auto-retry with exponential backoff is on the roadmap; track the
GitHub issue for status.

## SSRF guarantee on the deliver side

Aztea validates every outbound webhook URL through
[`core/url_security.py`](../core/url_security.py) before POSTing.
Localhost, private IPs, tunneling services (ngrok, lhr.life, cfargotunnel,
etc.) are blocked at the registration boundary — you cannot register a
callback URL pointing at infrastructure Aztea couldn't safely reach.
This protects publishers from being weaponized against Aztea's internal
network and stops legitimate publishers from accidentally pointing
production callbacks at a forgotten test stub.

## Secret rotation

Treat `callback_secret` like a password. To rotate:

1. Generate a new secret with `openssl rand -hex 32`.
2. Have your handler accept BOTH the old and the new for a grace window.
3. Update Aztea-side: re-create new jobs with the new secret. Existing
   jobs in-flight keep the old secret until they terminate.
4. After the grace window, retire the old secret.

There is no "rotate this watcher's secret" endpoint today — recreate the
watcher with a new secret. Track the GitHub issue if this becomes
operationally painful.

## What's not in scope (yet)

- Idempotent delivery IDs across retries (use `X-Aztea-Run-Id` plus
  your own dedup table in the meantime).
- Per-event-type subscriptions (everything fires on the same URL today;
  filter on `X-Aztea-Event`).
- Replay endpoint (`POST /watchers/{id}/runs/{run_id}/replay` is on
  the roadmap).
