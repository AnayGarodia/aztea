# Deploy & infrastructure runbook

The full operational reference: production deploy, nginx, env vars, packaging, Stripe webhook. Keep this current — when something here drifts from reality, fix it the same day you discover it.

---

## Cloudflare + EC2 (typical setup)

- **DNS:** Point the hostname to the EC2 public IP. With Cloudflare proxy (orange cloud) on, set SSL/TLS to **Full (strict)** — this requires a valid cert on the origin (certbot + nginx).
- **Client IP:** Terminate at nginx, forward `X-Forwarded-For` / `X-Real-IP`, configure `TRUSTED_PROXY_IPS` so `slowapi` and admin checks see the real client IP.
- **Env URLs:** `SERVER_BASE_URL`, `FRONTEND_BASE_URL`, and `CORS_ALLOW_ORIGINS` must use the public `https://` hostname, not the raw EC2 IP.

## Infrastructure

- **Server:** AWS EC2 Ubuntu — `/home/aztea/app`
- **Stack:** two systemd services — no Docker in production
  - `aztea.service` — Python/FastAPI (uvicorn, HTTP API + payments + auth)
  - `aztea-elixir.service` — Elixir/OTP (job GenServers + lease sweeper + Phoenix.PubSub)
- **Python process:** `/home/aztea/app/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000 --workers 2`
  - **Instance (verified 2026-05-30):** ~7.8 GB RAM, 2 vCPUs, **no swap** (`free -m`). The box was upgraded from the original t3.micro (1 GB); the memory-pressure tuning below was written for that smaller box and no longer binds. Re-check `free -m` over SSH before trusting any of it.
  - **Worker count:** still pinned to **2** via the systemd drop-in at `/etc/systemd/system/aztea.service.d/override.conf` (the base unit requests 3; the drop-in overrides down to 2) — verify with `sudo systemctl cat aztea | grep ExecStart`. The 2-worker cap was a t3.micro memory necessity (3 workers thrashed swap to 1 GB+ steady-state; each worker holds its own ~80 MB sentence-transformers copy). On the current ~8 GB box that constraint is gone — but the box only has 2 vCPUs, so raising worker count buys little. The hard limit on worker count is now backend correctness, not memory: see **Multi-worker correctness** below (Postgres allows any count; SQLite requires `--workers 1`).
  - **Embedding warmup:** currently **off** (`AZTEA_WARM_EMBEDDINGS` unset), so the first search per worker after a deploy lazy-loads the ~80 MB model and takes ~40s; later searches are sub-second. It was disabled on 2026-05-09 because on t3.micro both workers raced to load the model on startup and one always lost to OOM (crashloop, 15+ "Started server process"/min). On the current ~8 GB box that OOM risk is gone — setting `AZTEA_WARM_EMBEDDINGS=1` to remove the first-search latency is now safe; restart `aztea` and watch `journalctl -u aztea` for a single clean startup per worker.
- **Elixir process:** `/home/aztea/elixir-release/bin/aztea start` (beam.smp)
- **Database:** PostgreSQL 16 (`aztea_prod`) — `DATABASE_URL=postgresql://aztea:...@localhost/aztea_prod` in `.env`
- **Reverse proxy:** Caddy at `/etc/caddy/Caddyfile` reverse-proxies the catch-all to uvicorn on `127.0.0.1:8000`, **but serves the immutable hashed Vite bundles (`/assets/*`) directly from `/home/aztea/app/frontend/dist`** via a `handle /assets/* { file_server }` block placed before the catch-all (2026-06-02). This keeps the ~13 asset requests per page load off the 2-worker uvicorn pool — under concurrent load uvicorn was intermittently stalling and a page load fans out enough asset requests to hang the whole pool. The `caddy` user already has read+traverse on the dist tree; assets get `Cache-Control: public, max-age=31536000, immutable` + `X-Content-Type-Options: nosniff` (filenames are content-hashed, so immutable is safe). **`index.html` and every other path still proxy to uvicorn** — **FastAPI owns SPA fallback** via the catch-all `@app.get("/{full_path:path}")` in `server/application_parts/part_014.py`: maps existing `frontend/dist/*` files to themselves, returns `index.html` for everything else, and 404s anything matching `_SPA_API_PREFIXES`. The `/assets/*` Caddy block shadows that path for assets only; the fallback still handles `index.html`, images, and SPA routes.
  - **Reload gotcha (2026-06-02):** `sudo systemctl reload caddy` failed on this box with `status=226/NAMESPACE` (`PrivateTmp` mount-namespace setup error on `/tmp`) — it does NOT apply the new config and leaves the *old* process running. Use the admin-API hot reload instead: `sudo caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile` (zero-downtime, bypasses systemd). Always `caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile` first.
- **SSL:** Managed by Caddy (automatic HTTPS via certbot or ACME). The "Recommended nginx config" section below is an alternative reference for deployments that use nginx instead of Caddy.

### Multi-worker correctness (2026-05-28)

- **SQLite backend:** `--workers 1` is the **only** supported configuration. The agent-catalog cache (`server/application_parts/part_007.py:_mcp_active_agents`) and the auto-hire decision cache use a process-local version counter; SQLite has no cross-process invalidation channel. A boot with `WEB_CONCURRENCY > 1` on the SQLite path **fails fast** at lifespan startup (see `server/application_parts/part_001.py`).
- **Postgres backend:** any worker count works. Cache invalidation flows over `LISTEN/NOTIFY` on the `aztea_catalog_version` channel — see `core/registry/catalog_broadcast.py`. The listener thread auto-reconnects with exponential backoff (max 30 s). Bump `DB_MAX_CONNECTIONS` by **+1 per worker** to budget the long-lived LISTEN connection alongside the request pool.
- **Auth caching:** Aztea does **not** cache `verify_api_key` across requests (intentionally — money-handling system). Key revocation is immediate. Inflight requests holding `request.state._caller` complete with the revoked key, but no subsequent request hits the cache.
- **Deferred writes:** observability-only writes (auto-hire decision audit, public work examples, receipts) drain through a process-local daemon queue (`core/deferred.py`). On crash, in-flight queue items are lost; loss is bounded to whatever was enqueued at the crash instant. Tunable via `AZTEA_DEFERRED_QUEUE_MAXSIZE` (default 8192). Wallet / ledger writes stay synchronous.
- **Outbound HTTP pool:** the agent-dispatch path uses a pooled `requests.Session` (`core/outbound_session.py`). DNS-rotation safety relies on a session recycle every `AZTEA_OUTBOUND_SESSION_RECYCLE_SECONDS` (default 300 s); shorten if upstream hostnames rotate IPs aggressively.
- **DNS-rebinding defense:** every outbound call through `core/outbound_session.post()` resolves the hostname ourselves, validates the IP against `core/url_security` policy, and pins that IP for the TCP connect via a context-var-scoped `socket.getaddrinfo` patch. A TTL=0 DNS responder cannot redirect a validated request to a private IP between validation and connect — the second resolution would raise `ValueError: ... DNS-rebinding attempt blocked` and the connection is refused.
- **Catalog-broadcast NOTIFY hardening:** the LISTEN handler ignores non-integer / non-positive payloads and clamps version advances of more than `_MAX_NOTIFY_VERSION_JUMP=1000` to `current+1`. A spoofed `NOTIFY aztea_catalog_version, '9999999999'` from a compromised DB user can at worst force one cache rebuild — not silently corrupt downstream decision-cache keys. The cache-invalidation callback still fires regardless of the value (defense in depth).
- **LISTEN keepalive:** the catalog-broadcast listener issues `SELECT 1` every 60 s on the LISTEN connection to defeat NAT / load-balancer idle timeouts that would otherwise silently freeze the listener thread.

**Verify what's actually deployed:** The canonical deploy host + SSH key live in `.env` as `DEPLOY_HOST` + `DEPLOY_SSH_KEY` — read those at deploy time, do NOT hardcode IPs in this runbook (they drift). As of 2026-05-19 the host is `ubuntu@3.13.141.122` (moved from the original `3.145.5.228`); future moves should just update `.env`. Compare `sha256sum /home/aztea/app/frontend/dist/assets/*.js` against your local `dist/` to confirm the build that was published. The release script (`scripts/release_publish_local.sh` — gitignored so tokens never leave the local checkout) does `git push origin HEAD:main` + EC2 `git fetch && git reset --hard origin/main` + frontend build + Python service restart + Elixir release rebuild, so **uncommitted local changes never deploy** — commit first, then run the script.

## Deploying a new version

> **Dep pinning.** `requirements.txt` and `requirements-dev.txt` are generated
> from `*.in` source files via `make lockfile`. Every dep is fully pinned
> (incl. transitives). Deploy installs via `pip install --no-deps -r
> requirements.txt` so pip cannot silently resolve an additional package at
> install time. If you change `requirements.in`, you MUST commit the
> regenerated lockfile in the same PR — `make lockfile-verify` enforces
> this gate. Hash-verified installs (`--require-hashes`) are the next
> hardening step and require regenerating the lockfile inside
> `python:3.11-slim` (matches the prod image). The runbook command is:
>
> ```bash
> docker run --rm -v "$PWD":/app -w /app python:3.11-slim bash -c \
>   'pip install pip-tools && pip-compile --generate-hashes --strip-extras \
>     -o requirements.txt requirements.in'
> ```

```bash
cd /home/aztea/app

# 1. Pull as the service user — NEVER sudo git pull (makes files root-owned,
#    breaks the systemd unit that runs as `aztea`).
sudo -u aztea git fetch origin main
sudo -u aztea git reset --hard origin/main

# 2. Rebuild the React frontend — MUST run as the `aztea` user so it
#    can write to its own node_modules. Pre-fix the runbook ran npm as
#    `ubuntu` and EACCES'd on every deploy because node_modules/.bin
#    is owned by aztea. Wrap in `sudo -u aztea bash -c '...'` so PATH
#    is set up correctly inside the sub-shell. `npm ci` is load-bearing
#    here — it pins to the committed package-lock.json and fails if the
#    lockfile drifted from package.json. Never `npm install` in deploy.
sudo -u aztea bash -c 'cd /home/aztea/app/frontend && npm ci && npm run build'

# 3. Restart the API (migrations run automatically on startup).
# Force-kill ALL uvicorn processes before starting — `systemctl restart`
# alone leaves zombie workers from the old deploy because graceful
# shutdown takes 60+ seconds while the embedding model lazy-loads.
# Zombie workers serve a fraction of incoming requests with stale code,
# producing inconsistent ranking results that look like "the deploy
# half-took". Discovered 2026-05-09 — only 1 of 3 workers had the new
# routing keyword overlay because the other 2 were still ~3 minutes old.
sudo pkill -9 -f "uvicorn server:app" || true
sudo pkill -9 -f "spawn_main" || true
sleep 2
sudo systemctl start aztea

# 4. Verify
sudo systemctl status aztea
sudo journalctl -u aztea -n 50
```

If the service stops cleanly: `sudo systemctl restart aztea`. Migrations run automatically on startup via `core/migrate.py`.

Two-worker startup is race-safe under Postgres. `core/migrate.py` takes a
session-level Postgres advisory lock (`MIGRATION_ADVISORY_LOCK_ID =
4297493287`) before reading `schema_migrations`, so only one worker
applies pending migrations at a time. Other workers wait up to
`MIGRATION_LOCK_TIMEOUT_SECONDS` (60s) for the lock to release; once they
get it the schema is already current and they iterate zero times. If the
lock-holder crashes, Postgres releases the lock when the connection
drops — no manual cleanup required. SQLite path is unchanged (already
serialised via `BEGIN IMMEDIATE`).

A timed-out lock acquire raises `RuntimeError` and emits a
`migrations.lock.timeout` event. If you see this in journalctl, find the
worker still holding the lock (`SELECT pid FROM pg_locks WHERE
locktype = 'advisory' AND objid = 4297493287`), confirm it's actually
making progress, and `kill -9` it if not.

## Post-deploy rails verification

The 2026-05-09 rails-to-A pass moved every "rich" platform behavior — audit
aggregation, search ranking, off-catalog detection, error envelope, worker-pool
telemetry — to **server-side endpoints**. The `aztea-cli` package is now a dumb
HTTP passthrough. This means the right place to check that a rails fix shipped
is `https://aztea.ai` itself, **not** a local MCP. If you skip this check after
a deploy you'll repeat the bug class that produced eight prior "fix the rails"
commits without lifting any eval grades.

After every deploy that touches rails code, run these four probes against
production. Each one should return the new shape; if any returns the old shape,
**the deploy didn't ship and you need to debug the deploy lane before celebrating**.

```bash
API_KEY="<a caller-scope key>"

# 1. Audit endpoint exists with the rich shape (since/until/digest/bulk-verify).
curl -s -H "Authorization: Bearer $API_KEY" \
  "https://aztea.ai/wallets/audit?period=1d&verify_all=true" \
  | jq '{has_digest: (.receipts_digest != null),
         has_aggregates: (.receipts_aggregate != null),
         has_options: (.available_options != null),
         has_bulk_verify: (.bulk_verification != null)}'
# Expect: every key true.

# 2. Search empty-result mode fires for off-catalog queries.
curl -s -H "Authorization: Bearer $API_KEY" -X POST https://aztea.ai/registry/search \
  -H "Content-Type: application/json" -d '{"query":"tell me a joke","limit":5}' \
  | jq '{count, off_catalog, has_note: (.note != null)}'
# Expect: count == 0, off_catalog == true, has_note == true.

# 3. Worker_pool snapshot has the collapsed shape (no in_flight_global_raw at top).
# Replace <BATCH_ID> with any batch you can poll. ?debug=1 should still show the
# diagnostic fields under a `debug` sub-dict.
curl -s -H "Authorization: Bearer $API_KEY" \
  "https://aztea.ai/jobs/batch/<BATCH_ID>" \
  | jq '.parallel_hire_trace.worker_pool | keys'
# Expect: ["capacity_remaining","configured_parallelism","in_flight_global",
#          "interval_seconds","platform_queue_depth",
#          "this_batch_pending","this_batch_running"]
# Specifically: NO "in_flight_global_raw", NO "last_worker_summary", NO "hint".

# 4. Failed batch jobs carry the structured error envelope alongside the legacy
# error_message string. Submit any batch with one intentionally bad job, poll
# batch_status, and inspect a failed entry.
# Expect: jobs[].error to be a dict with {error, message, details};
# jobs[].error_message to remain present as a string.
```

If any probe fails, do NOT bump the `aztea-cli` package — the CLI is a thin
wrapper. The grades only move when the **server** ships the new shape, which
happens via `git fetch origin main && git reset --hard origin/main` followed by
`sudo systemctl restart aztea`. The CLI release exists for hygiene (smaller
client codebase, less duplication), not for grade improvements.

## Recommended nginx config

```nginx
server {
    listen 443 ssl http2;
    server_name aztea.ai www.aztea.ai;

    root /home/aztea/app/frontend/dist;
    index index.html;

    # Hashed Vite assets — long cache
    location ~* ^/assets/.*\.(js|css|woff2?|ttf|eot|svg|png|jpg|jpeg|gif|webp|ico|map)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
        try_files $uri =404;
    }

    # API + server routes → uvicorn (strip the /api prefix)
    location ~ ^/(api|auth|admin|agents|jobs|registry|wallets|ops|mcp|public|config|stripe|llm|health|metrics|onboarding|disputes|reputation|runs|webhooks|skills|openapi.json)(/|$) {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    # Elixir realtime sidecar — WebSocket upgrade for /elixir/socket/*
    location /elixir/socket {
        proxy_pass http://127.0.0.1:4000/socket;
        proxy_http_version 1.1;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        "upgrade";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # WebSocket idle timeout — Phoenix heartbeat is 30s, give buffer.
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
    }

    # SPA fallback for client-side routes
    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

### Caddy alternative (if you're on Caddy instead of nginx)

```caddy
aztea.ai {
    # …existing reverse_proxy directives…

    # Phoenix mounts the socket at /socket (see elixir/lib/aztea_web/endpoint.ex);
    # strip the /elixir prefix before forwarding or you'll get 404s from Cowboy.
    handle /elixir/socket* {
        uri strip_prefix /elixir
        reverse_proxy 127.0.0.1:4000 {
            header_up Connection {http.request.header.Connection}
            header_up Upgrade    {http.request.header.Upgrade}
        }
    }

    # Immutable hashed Vite bundles — served from disk so they never consume
    # a uvicorn worker. Must come BEFORE the catch-all `handle { reverse_proxy }`
    # (handle blocks are exclusive, evaluated in written order). Content-hashed
    # filenames make the immutable cache safe. index.html + SPA routes still
    # fall through to uvicorn.
    handle /assets/* {
        root * /home/aztea/app/frontend/dist
        header Cache-Control "public, max-age=31536000, immutable"
        header X-Content-Type-Options nosniff
        file_server
    }

    handle {
        reverse_proxy localhost:8000
    }
}
```

**Caddy gotcha (2026-05-17):** the earlier example used `@elixir_socket` matcher + bare `reverse_proxy` and did not strip the path prefix. Phoenix mounts at `/socket`, so it returned `404 Not Found` for `/elixir/socket/websocket`. The `uri strip_prefix /elixir` directive is required. `handle_path /elixir/socket*` does NOT work either — it strips too much (`/elixir/socket`), leaving `/websocket`, which Phoenix also rejects.

## Realtime job events (Elixir sidecar)

Step 1 of the strangle-fig migration to Elixir. When enabled, job state
transitions land in the web UI in <1 s instead of waiting on the SSE / 5 s poll.

**Components:**

- `aztea-elixir.service` — Phoenix endpoint on `127.0.0.1:4000`
- `POST /elixir/socket/*` (proxied by nginx/Caddy) — WebSocket the FE connects to
- `POST /internal/job-events` (loopback only) — fired by the Python app on every state change

**Required env vars on the Python service AND the Elixir service** (both read
the same `.env`):

```
AZTEA_ELIXIR_EVENTS=1                          # FEATURE FLAG. Off by default.
ELIXIR_HTTP_URL=http://127.0.0.1:4000          # default; override only for testing
ELIXIR_HTTP_PORT=4000                          # consumed by the Elixir release
ELIXIR_INTERNAL_SHARED_SECRET=<openssl rand -hex 32>
```

Both services trust the same secret: it authenticates the Python → Elixir
`POST /internal/job-events`, and it signs the short-lived HMAC tokens the
frontend uses on `wss://aztea.ai/elixir/socket`. Rotating the secret
invalidates both directions; restart both services after rotation.

**Cold deploy procedure:**

1. Land the code with `AZTEA_ELIXIR_EVENTS` UNSET — verify the FE behaves
   identically to before (5 s poll still drives updates).
2. Set `ELIXIR_INTERNAL_SHARED_SECRET` on both services; restart Elixir.
3. `curl http://127.0.0.1:4000/health` → `{"status":"ok"}`.
4. Flip `AZTEA_ELIXIR_EVENTS=1` on the Python service; restart uvicorn.
5. Watch journalctl on both: Python should log `elixir.notify_post_failed`
   only when Elixir is genuinely down. The web UI should slow its
   reconciliation poll from 5 s to 60 s once the socket is up.

**Rollback** is one env-var flip (`AZTEA_ELIXIR_EVENTS=0`) + restart uvicorn.
Caddy / nginx config can stay; the proxied path just stops being used.

## Self-improving hosted skills (`AZTEA_SELF_IMPROVEMENT`)

Migration 0077. When on, the job sweeper distils a hosted skill's recent
failures (low-rated `output_examples` + caller-filed dispute text + judge
reasoning) into short corrective "learnings", the skill owner accepts/rejects
them in **My Agents**, and accepted learnings are injected as a delimited DATA
block at execution time (`core/skill_executor.py`). The stored `system_prompt`
is never mutated — reversal is the owner rejecting, or deleting the skill. A
Level-1 `trust_trend` (improving / flat / declining) also lights up and adds a
small, bounded ranking nudge in auto-hire + search.

This is a **hosted-only** feature. Leave it off in OSS: the distiller spends
platform LLM credits, and `tests/test_oss_mode_isolation.py` assumes no
credit-spending background work runs by default.

**Why default-off (do not flip globally yet):** v1 is a demand probe. The
metric that justifies the feature is **owner acceptance rate of proposed
learnings** — turn it on, watch that rate and the ranking delta, and only then
consider making it the default.

**Cold deploy procedure:**

1. Land the code with `AZTEA_SELF_IMPROVEMENT` UNSET — verify behavior is
   identical to before (no distill log lines, owner learnings routes return 404).
2. Confirm at least one LLM provider key is set (the distiller uses
   `run_with_fallback`; with no provider it soft-fails to a no-op).
3. Flip `AZTEA_SELF_IMPROVEMENT=1` on the Python service; restart uvicorn.
4. Within `AZTEA_SELF_IMPROVEMENT_INTERVAL_S` (default 24h) watch journalctl for
   `learning_distillation.swept` — `learnings_proposed > 0` means owners now
   have proposals waiting in My Agents. Force an earlier first run by lowering
   the interval temporarily.
5. Track owner accept-rate before considering wider rollout.

**Rollback** is one env-var flip (`AZTEA_SELF_IMPROVEMENT=0`) + restart uvicorn.
Already-active learnings stop being injected immediately (the executor reads the
flag at call time); the `skill_learnings` rows remain for when it is re-enabled.

## Useful server commands

```bash
# Live logs
sudo journalctl -u aztea -f

# Last 100 lines
sudo journalctl -u aztea -n 100

# Restart API
sudo systemctl restart aztea

# Force kill if stuck (background threads blocking shutdown)
sudo systemctl kill -s SIGKILL aztea && sudo systemctl start aztea

# Service status
sudo systemctl status aztea

# Manual DB backup
sqlite3 /path/to/registry.db ".backup /path/to/registry.db.bak"

# Python shell with app context
cd /home/aztea/app && source venv/bin/activate && python

# Manual reconciliation
curl -H "Authorization: Bearer $API_KEY" -X POST https://aztea.ai/ops/payments/reconcile
```

## Production environment variables

Stored in `.env` on the server (never committed). Key vars:

```
# Core
ENVIRONMENT=production
API_KEY=                        # master key — openssl rand -hex 32
SERVER_BASE_URL=https://aztea.ai
FRONTEND_BASE_URL=https://aztea.ai
CORS_ALLOW_ORIGINS=https://aztea.ai
AZTEA_FRONTEND_URL=https://aztea.ai

# Stripe (use live keys in prod)
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PUBLISHABLE_KEY=pk_live_...

# LLM (at least one required)
GROQ_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
AZTEA_LLM_DEFAULT_CHAIN=groq,openai,anthropic

# Optional features
AZTEA_ENABLE_LIVE_DISPUTE_JUDGES=1
AZTEA_ENABLE_LIVE_QUALITY_JUDGE=1

# Probation auto-graduation thresholds (sweeper-driven, defaults shown).
# Listings clear probation when ALL gates pass: ≥5 successful calls, ≥80%
# success rate, ≥3.5 avg quality rating, no open disputes, ≥24h since
# created_at. Tighten in prod if abuse appears; loosen for friendlier onboarding.
# AZTEA_PROBATION_MIN_SUCCESSES=5
# AZTEA_PROBATION_MIN_SUCCESS_RATE=0.80
# AZTEA_PROBATION_MIN_QUALITY=3.5
# AZTEA_PROBATION_MIN_AGE_HOURS=24
# AZTEA_PROBATION_SWEEP_INTERVAL_S=300

# Self-improving hosted skills (migration 0077). OFF by default and intended
# to stay off in OSS — the distiller spends platform LLM credits and the
# injected learnings block changes skill behavior. With the flag unset the
# sweep never runs, no block is injected, the trust_trend ranking nudge is
# inert, and the owner routes 404 (byte-identical to pre-0077). Turn on for
# hosted aztea.ai only after validating owner accept-rate. See the rollout
# note below.
# AZTEA_SELF_IMPROVEMENT=1
# AZTEA_SELF_IMPROVEMENT_INTERVAL_S=86400       # distill cadence (default 24h)
# AZTEA_SELF_IMPROVEMENT_MAX_SKILLS_PER_RUN=25  # per-sweep LLM-cost cap
# AZTEA_SELF_IMPROVEMENT_MAX_PENDING=10         # skip skills with this many un-reviewed proposals

# Email (if unset, all email silently no-ops)
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=noreply@aztea.ai
```

## Stripe webhook

- Endpoint: `POST https://aztea.ai/stripe/webhook`
- Register in the Stripe dashboard; set `STRIPE_WEBHOOK_SECRET` to the signing secret.
- Required events: `checkout.session.completed`, `payment_intent.succeeded`.

## Package distribution (PyPI)

```bash
cd sdks/python-sdk
python3 -m venv .release-venv && source .release-venv/bin/activate
python -m pip install -U pip build twine
python -m build
python -m twine upload dist/aztea-*
```

Verification:

```bash
python3 -m venv /tmp/aztea-check && source /tmp/aztea-check/bin/activate
pip install -U aztea
python -c "import aztea; print(aztea.__version__)"
which aztea
```
