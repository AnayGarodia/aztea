# live_sandbox — operator runbook

Operational reference for the `live_sandbox` built-in agent. Read this before
deploying, scaling, or debugging a live_sandbox in production.

## What it is

`live_sandbox` is the agent that boots a Docker-backed clone of the user's
project (the real services + DB + env, not a sketch) and exposes ~60
typed verbs over MCP/HTTP. See `core/sandbox/` for the engine.

Dispatch surface as of the latest revision: **61 real handlers + 0 stubs**.

## Host prerequisites

Required for any non-trivial use:

- **Docker daemon**, reachable from the server process. macOS / Windows
  need Docker Desktop; Linux needs `dockerd` running and the server user
  in the `docker` group.
- **git** on PATH (only needed for `source.kind == "git"`).

Optional — each unlocks specific verbs and **degrades cleanly when absent**:

| CLI / library | Unlocks | Detection |
|---|---|---|
| `cloudflared` | `sandbox_tunnel_open` quick tunnels | `which cloudflared` |
| `ngrok` + `AZTEA_NGROK_TOKEN` | `sandbox_tunnel_open` via ngrok | env + `which ngrok` |
| `lighthouse` (npm global) | `sandbox_browser_lighthouse` | `which lighthouse` |
| `playwright` + chromium | full browser surface | python import + chromium |
| `libfaketime` | deterministic `clock.frozen_at` | shared-library probe |
| `rsync` | faster `sandbox_sync_from_local` | `which rsync` (else shutil) |
| `kind` + `kubectl` | `boot.strategy == "k8s_kind"` | both must be present |
| `helm` | `boot.strategy == "helm"` | `which helm` |
| `nix` | `boot.strategy == "nix"` | `which nix` |
| `devcontainer` CLI | devcontainer.json `features` | `which devcontainer` |
| `runsc` (gVisor) | `isolation_backend == "gvisor"` | `docker info` runtimes |

Privileged verbs are **off by default** and require explicit operator opt-in
via env vars. See "Privilege model" below.

## Environment variables

### Required

None. The agent boots without any sandbox-specific env, taking sane defaults.

### Boot / runtime tuning

| Env | Default | Effect |
|---|---|---|
| `AZTEA_SANDBOX_STATE_ROOT` | `/tmp/aztea-sandbox` | Root for per-sandbox state dirs |
| `AZTEA_SANDBOX_DOCKER_BIN` | `which docker` | Override the docker binary path |
| `AZTEA_SANDBOX_FAKETIME_LIB` | autodiscovered | Path to `libfaketime.so` |
| `AZTEA_AXE_CORE_JS` | autodiscovered | Local path to `axe.min.js` (avoids CDN fetch) |

### Tunneling

| Env | Effect |
|---|---|
| `AZTEA_CLOUDFLARE_TUNNEL_TOKEN` | Selects the production-grade cloudflared named-tunnel path. **Set this in production**; quick tunnels are rate-limited per host IP. |
| `AZTEA_NGROK_TOKEN` | Enables the ngrok fallback path. |
| `AZTEA_VCR_PROXY_HOST` | Override the host name compose containers use to reach the VCR proxy. Default: `host.docker.internal` (works on Docker Desktop). On native Linux set to your bridge gateway (commonly `172.17.0.1`). |

### Privileged actions (off by default)

| Env | Unlocks |
|---|---|
| `AZTEA_SANDBOX_ALLOW_NET_RAW=1` | `sandbox_network_capture` (tcpdump sidecar with `CAP_NET_RAW`) |
| `AZTEA_SANDBOX_ALLOW_PTRACE=1` | `sandbox_trace` (py-spy / strace with `CAP_SYS_PTRACE`) |

Both refuse with a structured envelope (not an error) when the env flag is
absent — operators can probe whether the action will work without granting
the privilege.

### BYOK LLM keys (from PR-61, bug #5)

| Env | Effect |
|---|---|
| `AZTEA_BYOK_<API_KEY_ID>_<PROVIDER>_API_KEY` | Per-caller LLM key overlay; replaces the platform default for that caller. |
| `AZTEA_BYOK_<API_KEY_ID>_<PROVIDER>_BASE_URL` | Optional base URL for the overlay. |

## Boot strategies

The `boot.strategy` field selects how the sandbox comes up:

| Strategy | What it does | Detection signals (`auto`) |
|---|---|---|
| `auto` | Pick the first matching strategy from the signals column | falls through in priority order |
| `docker_compose` | `docker compose up` against the user's compose file(s) | `docker-compose.yml`, `compose.yml`, etc. |
| `dockerfile` | `docker build` + `docker run` | `Dockerfile` |
| `devcontainer` | Parse `devcontainer.json`; handles `image`, `dockerFile`, `dockerComposeFile`, `features`, `forwardPorts`, `postCreateCommand` | `.devcontainer/devcontainer.json` or `devcontainer.json` |
| `custom_commands` | Run `boot.custom_commands[]` inside a generic Ubuntu base | always applicable |
| `k8s_kind` | Create a `kind` cluster + `kubectl apply` user manifests | requires `boot.k8s_manifests[]` |
| `helm` | `helm upgrade --install` against a kind cluster | requires `boot.helm_chart` |
| `nix` | `nix develop` in a generic Nix container with the repo mounted | requires `flake.nix` at repo root |

## Ready checks

Every boot accepts a `boot.ready_checks[]` list of four kinds:

```jsonc
{
  "ready_checks": [
    { "kind": "http",      "target": "http://web:3000/health", "expect_status": 200 },
    { "kind": "tcp",       "target": "db:5432" },
    { "kind": "log_regex", "service": "worker", "pattern": "ready to process" },
    { "kind": "command",   "cmd": "pnpm db:status" }
  ],
  "ready_timeout_seconds": 600
}
```

All checks must pass. The boot raises `sandbox.boot_failed` if any check is
still unsatisfied after `ready_timeout_seconds` (default 600).

## Isolation backend

Set `isolation_backend` on the `sandbox_start` payload:

- `"docker"` (default) — vanilla Docker container per service.
- `"gvisor"` — applies `--runtime=runsc` to every container. Strong
  syscall-level isolation; refuses if `runsc` isn't registered with the
  Docker daemon.
- `"firecracker"`, `"kata"` — explicit not-implemented envelope. Both
  need host-level infra outside the agent module.

## Spending caps (gap #5)

Each sandbox carries an in-memory cap:

- Default: `5000` cents ($50).
- Hard ceiling: `50_000` cents ($500); caller requests above this are
  clamped, not refused.
- Override at boot: `"spending_cap_cents": <int>` in the `sandbox_start`
  payload.
- `sandbox_cost` returns the live `cap_cents` / `spent_cents` /
  `remaining_cents` triple plus the `actions` count.

`sandbox_batch_start` pre-holds the full matrix budget (per-cell cap × cells)
before any cell boots and refuses if it would exceed the default batch
ceiling (`20_000` cents / $200).

## Snapshots (COW where supported)

Each `sandbox_snapshot` writes:

- `fs.tar` — universal portable artifact (used by `sandbox_export_snapshot`).
- `fs.reflink/` — O(1) reflink mirror via `cp --reflink=auto` when the
  underlying filesystem supports it (btrfs / xfs reflink=1 / zfs).
  Restore prefers this; the tar stays the fallback.
- `services/<name>.tag` — `docker commit` tag per service.
- `db/<label>.pgdump` — when a Postgres service is detected.
- `manifest.json` — boot info + lifetime + network + size.

Check the filesystem your `AZTEA_SANDBOX_STATE_ROOT` lives on. Reflink works
out of the box on default Fedora installs, xfs with `mkfs.xfs -m reflink=1`,
and ZFS pools; otherwise you get the regular tar copy.

## Share viewer

`sandbox_share` mints a SHA-256-hashed join token bound to the sandbox.
The viewer listens on `127.0.0.1:<random_port>` by default and serves the
audit log + Merkle root for any caller presenting the right token.

To expose externally: open a tunnel against the viewer port. The viewer
runs in the same process as the engine — it's not a separate sidecar.

## Lifecycle eviction

`sandbox_stop` runs the following teardown:

1. Optional final snapshot (`lifetime.snapshot_on_stop` or
   `final_snapshot=true`).
2. Browser session pool eviction (chromium children).
3. Tunnel subprocess eviction (cloudflared / ngrok).
4. Webhook inbox sidecar shutdown.
5. Share token registry purge.
6. VCR proxy shutdown.
7. Compose `down --remove-orphans --volumes` (or label-filtered `rm -f`
   for non-compose strategies).
8. Spending budget eviction.
9. State registry remove.

## Idempotency (gap #11)

Every mutating action accepts an `idempotency_key`. The engine caches
the first successful response under `(action, idempotency_key)` for one
hour. Retries return the cached response with `idempotency_replayed: true`.

This is in-memory only — restart of the server process loses the cache.

## Performance bar

The original demand spec asked for these targets. Run
`scripts/sandbox_perf_bench.py` to measure on your host:

| Metric | Target |
|---|---|
| Cold boot (Node + Postgres compose) | < 60 s |
| Warm boot from snapshot fork | < 15 s |
| Exec round-trip (`sandbox_exec "true"`) | < 200 ms |
| Snapshot (10 GB workspace) | < 5 s |
| Fork from snapshot | < 10 s |

The benchmark prints measured vs. target and exits non-zero on a >25%
miss. Wire into CI for regression alerts.

## Troubleshooting

### "sandbox.docker_unavailable" at start
Docker daemon isn't reachable. `docker info` from the server user; check
the Unix socket permissions on Linux.

### "sandbox.boot_failed: ready_checks did not pass"
Ready-check details surface in the error's `unsatisfied` list. Inspect
container logs with `sandbox_logs(service=...)` after-the-fact (the boot
leaves the failing containers in place for triage).

### Tunnels timing out with "Cloudflare 429"
Quick tunnels are throttled per host IP. Set
`AZTEA_CLOUDFLARE_TUNNEL_TOKEN` to use a named tunnel.

### VCR proxy connection refused from container
Set `AZTEA_VCR_PROXY_HOST` to the bridge gateway on native Linux. Default
`host.docker.internal` resolves on Docker Desktop but not on bare Linux
without the `--add-host host.docker.internal:host-gateway` flag.

### "sandbox.quota_exceeded" on a long-running session
The default $50 sandbox cap was hit. Either bump it at start
(`spending_cap_cents`) or stop and restart with a higher value.

### Browser tests OOM during `sandbox_browser_screenshot`
Chromium full-page screenshots can blow past container memory. Pass
`size: { "memory_gb": 8 }` at start, or use `full_page: false`.

### `sandbox_lighthouse` says CLI missing
`npm install -g lighthouse` on the host. The agent never auto-installs.

## Single-flag operations cheat sheet

```
# Spin up the most common compose-backed sandbox
sandbox_start {
  source: { kind: "git", url: "<repo>", shallow: true },
  boot:   { strategy: "auto" },
  lifetime: { max_minutes: 30 },
  isolation_backend: "gvisor",
  spending_cap_cents: 1000
}

# Reproduce a webhook + verify
sandbox_outbound_record { sandbox_id, cassette: "stripe-prod" }
# ... do work ...
sandbox_outbound_replay { sandbox_id, cassette: "stripe-prod" }

# Snapshot before a risky migration, fork to try alternatives in parallel
sandbox_snapshot { sandbox_id, reason: "pre-migration" }
sandbox_fork    { source_sandbox_id: sandbox_id, snapshot_id: <id> }
```

## Honest gaps

The full surface is implemented, but a few items remain genuinely deferred:

- **Hardware-virtualized isolation** (Firecracker, Kata Containers) is
  host-infra work outside the agent module. gVisor is the v0 strong
  isolation path.
- **Wallet-backed billing** (atomic pre-hold against the buyer's wallet)
  layers on top of the spending cap once the `caller_api_keys` table
  lands.
- **Multi-host overlay networking** (Docker Swarm / k8s service mesh)
  for `sandbox_link` is a cluster follow-up; v0 is single-daemon.

See PR descriptions for the historical context.
