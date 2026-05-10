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
  - **Worker count:** 2 on t3.micro (1GB RAM). Each worker holds its own copy of FastAPI + the lazily-loaded sentence-transformers model (~80MB once warmed). 3 workers thrashed swap hard enough to run swap usage at 1GB+ steady-state; dropping to 2 freed ~600MB of swap and stabilized memory at ~760MB used / 911MB. Move to 3+ only after upgrading to t3.small (2GB) or larger. Configured via the systemd drop-in at `/etc/systemd/system/aztea.service.d/override.conf` — verify with `sudo systemctl cat aztea | grep ExecStart`.
  - **Embedding warmup:** **Disabled** on t3.micro. Tried `AZTEA_WARM_EMBEDDINGS=1` on 2026-05-09 — both workers race to load ~80MB simultaneously on startup and one always loses to OOM, producing a crashloop (15+ "Started server process" entries per minute). Tradeoff: first search per worker after a deploy lazy-loads the model and takes ~40s; subsequent searches are sub-second. The only safe way to enable warmup on this instance class is to drop to `--workers 1`, which serializes concurrent requests. Do not re-enable warmup until upgraded to t3.small or larger.
- **Elixir process:** `/home/aztea/elixir-release/bin/aztea start` (beam.smp)
- **Database:** PostgreSQL 16 (`aztea_prod`) — `DATABASE_URL=postgresql://aztea:...@localhost/aztea_prod` in `.env`
- **Reverse proxy:** Caddy at `/etc/caddy/Caddyfile` is a thin reverse proxy to uvicorn on `127.0.0.1:8000`. It does NOT serve static files. **FastAPI owns SPA fallback** via the catch-all `@app.get("/{full_path:path}")` in `server/application_parts/part_013.py`: maps existing `frontend/dist/*` files to themselves, returns `index.html` for everything else, and 404s anything matching `_SPA_API_PREFIXES`.
- **SSL:** Managed by certbot on the host; nginx handles termination.

**Verify what's actually deployed:** SSH key is `~/Downloads/aztea_key.pem`, user is `ubuntu@3.145.5.228` (per `.env` `DEPLOY_SSH_KEY` / `DEPLOY_HOST`). Compare `sha256sum /home/aztea/app/frontend/dist/assets/*.js` against your local `dist/` to confirm the build that was published. The release script does `git push` + EC2 `git fetch && git reset --hard origin/main`, so **uncommitted local changes never deploy** — commit first, then run the script.

## Deploying a new version

```bash
cd /home/aztea/app

# 1. Pull as the service user — NEVER sudo git pull (makes files root-owned,
#    breaks the systemd unit that runs as `aztea`).
sudo -u aztea git fetch origin main
sudo -u aztea git reset --hard origin/main

# 2. Rebuild the React frontend
cd frontend && npm ci && npm run build && cd ..

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

    # SPA fallback for client-side routes
    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

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

## Package distribution (PyPI + npm)

Publish order matters: `aztea-tui` first, then `aztea` (which depends on it).

```bash
# 1) TUI (PyPI)
cd tui
python3 -m venv .release-venv && source .release-venv/bin/activate
python -m pip install -U pip build twine
python -m build
python -m twine upload dist/aztea_tui-*

# 2) SDK (PyPI)
cd ../sdks/python-sdk
source ../../tui/.release-venv/bin/activate
python -m build
python -m twine upload dist/aztea-*

# 3) npm wrapper
cd ../../tui/npm
npm publish --access public --otp <code>
```

Verification:

```bash
python3 -m venv /tmp/aztea-check && source /tmp/aztea-check/bin/activate
pip install -U aztea
python -c "import aztea; print(aztea.__version__)"
which aztea-tui
```
