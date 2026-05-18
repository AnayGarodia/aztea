# Runbook: Workspaces

**Owner:** Anyone with `admin` scope and SSH access to the server.

**Reference:** Buyer-facing docs at [`docs/workspaces.md`](../workspaces.md). Architecture details in [`CLAUDE.md`](../../CLAUDE.md) under the "Workspaces" section once added.

---

## What lives where

| Asset | Location | Notes |
|---|---|---|
| `workspaces` table | DB | Schema in `migrations/0053_workspaces.sql`. |
| `workspace_artifacts` table | DB | Content stored inline as `BLOB`/`bytea`. |
| `pipeline_runs.workspace_id` | DB | Column from `migrations/0054_pipeline_runs_workspace_id.sql`. Nullable. |
| Workspace seal signing key | `data/workspace_signing_key.pem` (mode `0o600`) | Generated on first `seal_workspace()` call. Override path with `AZTEA_WORKSPACE_SIGNING_KEY_PATH`. Format: private PEM + `\n---PUBLIC---\n` + public PEM. **Secret.** |
| Sealer DID | `https://<host>/workspaces/sealer/did.json` | Public. Resolves to the JWK form of the public key. |
| Module | `core/workspaces.py` | All lifecycle / CRUD / seal logic. Sibling exceptions in `core/workspaces_errors.py`. |

The signing key is `.gitignore`d. Losing it means existing manifests still verify (the public key in the DID doc is unchanged), but new seals will be invalid until you restore the file or accept a key rotation.

---

## Storage growth monitoring

### Quick check

```sql
-- Per-status row counts and total bytes
SELECT status, COUNT(*) AS rows, SUM(total_bytes) AS bytes
  FROM workspaces
 GROUP BY status;

-- Top 10 active workspaces by size
SELECT workspace_id, owner_user_id, total_bytes, artifact_count, created_at
  FROM workspaces
 WHERE status = 'active'
 ORDER BY total_bytes DESC
 LIMIT 10;

-- Artifact-count distribution
SELECT artifact_count, COUNT(*) AS workspaces
  FROM workspaces
 GROUP BY artifact_count
 ORDER BY artifact_count DESC
 LIMIT 20;
```

### Thresholds

| Metric | Healthy | Watch | Action |
|---|---|---|---|
| Total `workspace_artifacts.content` size | < 50 GB | 50–200 GB | Monitor; v0.1 sweeper will null expired content |
| Per-`workspaces` row in `total_bytes` | < 64 MiB | At quota | Default quota; raise on per-workspace basis via `quota_bytes` |
| Single artifact `size_bytes` | < 1 MiB typical | 1–8 MiB | Within v0 cap; design unchanged |
| `expired` rows older than 30 days | 0 (after v0.1) | Any | v0 doesn't null content yet; future cleanup will |

### Postgres bytea sanity

`SELECT pg_total_relation_size('workspace_artifacts');` gives the on-disk size including TOAST overflow. Above ~500 GB the table starts costing real money on autovacuum + dump/restore; that's the signal to plan the S3 migration (schema reserves `external_store_uri`).

---

## TTL sweeper

The workspace sweeper runs inside the same background loop as the watcher / job sweepers (`server/application_parts/part_006.py`). It calls `core.workspaces.run_sweeper()` on each tick.

What it does:
- Marks `active` workspaces past `expires_at` as `expired`.

What it does NOT do (v0):
- Null `content` on `expired` rows. Plan for v0.1: a second pass that sets `workspace_artifacts.content = NULL` for expired workspaces older than the audit retention window (default 30 days).
- Drop `sealed` workspaces. Sealed workspaces are retained indefinitely so external verifiers can fetch the manifest. If a sealed workspace's content grows costly, **don't drop it manually** without checking with the owner first — they may be relying on the audit trail.

### Manually sweep

```bash
python -c "from core import workspaces as w; print(w.run_sweeper(), 'rows marked expired')"
```

Safe to run any time. Idempotent. Returns the number of rows it changed.

### When the sweeper isn't running

If the background loop is down, you'll see `active` workspaces with `expires_at < now()` piling up:

```sql
SELECT COUNT(*) FROM workspaces
 WHERE status = 'active' AND expires_at < now();
```

Anything > 0 for more than 10 minutes during normal operation means the loop has stopped. Check `journalctl -u aztea -n 200 | grep sweeper`.

---

## Signing key rotation

The per-server workspace signing key lives at `data/workspace_signing_key.pem` (or whatever path `AZTEA_WORKSPACE_SIGNING_KEY_PATH` points to). It's generated on first `seal_workspace()` call.

### When to rotate

- **Compromise.** Suspected key leak. Rotate immediately.
- **Yearly hygiene.** Even without compromise, rotate annually.
- **Hardware migration.** Moving to a different host. Decide whether to bring the key along (preserves verifiability of old seals on the new host) or rotate (clean break).

### How to rotate (preserves old manifests)

Old manifests stay verifiable as long as the **old** public key is still resolvable somewhere. The simplest pattern is "publish both keys":

1. Generate the new keypair locally:
   ```bash
   python -c "from core.crypto import generate_signing_keypair; p, q = generate_signing_keypair(); open('/tmp/new_key.pem','w').write(p+'\n---PUBLIC---\n'+q)"
   ```
2. On the server, archive the old key as `data/workspace_signing_key.archived-YYYY-MM-DD.pem`. Keep `mode 0o600`.
3. Replace `data/workspace_signing_key.pem` with the new key file.
4. Restart `aztea.service` so the in-process key cache (none today, but defensive) picks it up.
5. Extend `GET /workspaces/sealer/did.json` (in `server/application_parts/part_013.py:workspaces_sealer_did_document`) to advertise **both** verification methods. The new one signs new seals; the old one continues to verify pre-rotation manifests. (This is a one-time code edit; document the cut-over date in the DID document's `id` suffix.)

### How to rotate (clean break)

Accept that pre-rotation manifests will read as `valid: false` after rotation because the published public key changed:

1. `rm data/workspace_signing_key.pem` on the server.
2. Restart. The next `seal_workspace()` call regenerates a fresh keypair.

Reserve this for cases where the old key is known compromised AND no third party is verifying old manifests.

---

## Backup

The signing key is the only piece of workspace infrastructure that isn't in the database. Back it up alongside `.env`:

```bash
sudo cat /home/aztea/app/data/workspace_signing_key.pem | gpg --encrypt -r ops@example.com > workspace_signing_key.pem.gpg
# Store the encrypted copy somewhere off-host (1Password / S3 with strict ACL).
```

Permissions check:

```bash
sudo stat -c '%a %U:%G %n' /home/aztea/app/data/workspace_signing_key.pem
# Expected: 600 aztea:aztea data/workspace_signing_key.pem
```

If the mode is anything other than `600`, fix immediately: `sudo chmod 600 /home/aztea/app/data/workspace_signing_key.pem`.

---

## Debugging

### "Workspace not found" but I just created it

Most likely: testing path. The workspace ID is correct but the request went to a different DB.

- Check `DATABASE_URL` in the calling process (curl from local dev hits a different DB than prod).
- Check the workspace ID format: must start with `ws_` and be exactly 25 chars.

### `POST /verify` returns `valid: false` for a workspace I just sealed

The verifier re-hashes every artifact and compares against the manifest. If even one byte changed since seal, `valid` is false.

```sql
-- See if any artifact's stored sha256 doesn't match the manifest's recorded sha256.
SELECT a.name, a.sha256 AS current_sha
  FROM workspace_artifacts a
 WHERE workspace_id = 'ws_XXX';
```

Then compare with the manifest:

```bash
curl -s https://aztea.ai/workspaces/ws_XXX/manifest | jq '.manifest.artifacts[]|{name,sha256}'
```

Mismatches: someone (or something) wrote directly to the `workspace_artifacts` table after sealing. Sealed workspaces should be immutable; if you find drift, audit recent migrations / admin SQL for direct UPDATEs.

### `POST /workspaces/.../seal` returns 500 `workspace.seal.signing_failed`

The signing keypair couldn't be loaded or used. Check:

1. File exists: `ls -l /home/aztea/app/data/workspace_signing_key.pem`
2. Process can read it: `sudo -u aztea cat /home/aztea/app/data/workspace_signing_key.pem >/dev/null`
3. File format: should be `-----BEGIN PRIVATE KEY-----`, then `-----END PRIVATE KEY-----`, then the marker line `---PUBLIC---`, then `-----BEGIN PUBLIC KEY-----`, etc.

If the file is malformed, delete it; the next seal call will regenerate. **Existing sealed manifests will fail to verify** because the public key changed; restore from backup first if any external verifier depends on the prior key.

### Sandbox-backed workspace stuck in `sandbox_evicted`

There's no "un-evict" — the sandbox is gone. The workspace's metadata + sealed manifest are still valid as evidence; new reads/writes are not possible. Options:

1. Build the seal manifest one last time (if not already sealed): `python -c "from core import workspaces as w; print(w.seal_workspace('ws_XXX'))"` — produces a manifest flagged `partial: true` based on whatever was committed.
2. Clean up: `python -c "from core import workspaces as w; w.cleanup_workspace('ws_XXX')"`.

### High `held_cents` after a workspace-heavy day

Unrelated. Workspaces don't touch wallet holds. See [`docs/runbooks/ledger-drift.md`](ledger-drift.md) if reconciliation is reporting drift.

---

## CI test coverage

| Suite | Path | What it asserts |
|---|---|---|
| Unit: CRUD | `tests/test_workspaces_crud.py` | create, get, write, read, list, delete, quota, name validation, CAS |
| Unit: seal | `tests/test_workspaces_seal.py` | manifest shape, signature round-trip, tamper detection |
| Unit: sweeper | `tests/test_workspaces_sweeper.py` | TTL marks `active` → `expired`; leaves `sealed` alone |
| Unit: sandbox backing | `tests/test_workspaces_sandbox_backing.py` | filesystem routing, eviction handling |
| Integration: HTTP | `tests/integration/test_workspaces_http.py` | full REST CRUD + seal/verify + DID doc + auth |
| Integration: dispatch | `tests/integration/test_workspaces_dispatch.py` | `_workspace_id` envelope + `_artifact_ref` resolution + auto-write |
| Integration: pipeline e2e | `tests/integration/test_workspaces_pipeline_e2e.py` | `auto_workspace: true` recipe creates + seals + verifies |

Run them all:

```bash
pytest -q tests/test_workspaces_*.py tests/integration/test_workspaces_*.py
```
