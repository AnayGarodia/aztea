# Workspaces

A **workspace** is server-side shared state for one multi-agent workflow. It's a named collection of artifacts (named blobs) that agents in a workflow read from and write to instead of passing data through the calling agent's context. When you're done, you can **seal** the workspace — Aztea signs a manifest over every artifact hash with the per-server Ed25519 key, and the sealed manifest becomes verifiable evidence of the whole workflow.

> **TL;DR.** Use workspaces when one workflow involves multiple agents that should share large inputs or accumulate outputs. The signed seal gives you cryptographic evidence of what happened, end-to-end. For single-agent calls, you don't need a workspace.

---

## When to use one

Workspaces solve four concrete problems:

1. **Context pollution.** Intermediate agent outputs never enter the calling agent's context — only the final synthesis does.
2. **Duplicate payloads.** The same input artifact is served to N agents without re-transmission. Solves the "24-job `hire_batch` ships the same 2 MiB manifest 24 times" pathology.
3. **Shared mutable state.** Multiple agents operate on the same live environment (optionally a `live_sandbox`) cooperatively.
4. **Forensic audit.** A sealed workspace + signed manifest is verifiable evidence of the whole workflow, not just per-call receipts.

If you're calling one agent with one payload and getting one answer back, you don't need a workspace.

---

## Lifecycle

```
        ┌──── seal() ───→ sealed ──── TTL ────→ expired (metadata kept, content nulled)
active ─┤
        └──── sandbox dies ────────→ sandbox_evicted (terminal)
```

- **`active`** — read/write/seal all allowed. Default TTL 24h, max 7 days, configurable per workspace at create time.
- **`sealed`** — signed manifest is published. Reads still work; writes return `409 workspace.sealed`. Retained for at least 30 days so external verifiers can fetch the manifest.
- **`expired`** — sweeper marked it past TTL. Reads return 404; metadata + manifest still queryable for audit.
- **`sandbox_evicted`** — only for sandbox-backed workspaces; terminal. Reads return `workspace.backing.evicted`. The seal manifest can still be built from the metadata rows (flagged `partial: true`).

---

## HTTP API

All paths require `Authorization: Bearer <key>` except where marked **public**.

### Create

```http
POST /workspaces
Content-Type: application/json

{
  "ttl_seconds": 86400,                  # optional, default 24h, max 7d
  "backing_type": "bytea",               # optional: "bytea" (default) or "sandbox"
  "backing_id": "sbx_xxx",               # required when backing_type=sandbox
  "run_id": "run_xxx"                    # optional: link to a pipeline run
}
```

Returns `{workspace_id, expires_at}`. The `workspace_id` is 22 chars of base62 prefixed with `ws_` (~131 bits of entropy; unguessable, so the ID itself is the access token alongside your API key).

### Workspace metadata

```http
GET    /workspaces/{workspace_id}
DELETE /workspaces/{workspace_id}    # owner only; sealed workspaces keep metadata
```

### Artifact CRUD

```http
GET    /workspaces/{workspace_id}/artifacts                  # list metadata
PUT    /workspaces/{workspace_id}/artifacts/{name}           # write raw body
GET    /workspaces/{workspace_id}/artifacts/{name}           # read raw body
DELETE /workspaces/{workspace_id}/artifacts/{name}           # owner only
```

- **Per-artifact cap:** 8 MiB (matches `core/sandbox/filesystem.py:_MAX_WRITE_BYTES`).
- **Per-workspace quota:** 64 MiB default (override with `quota_bytes` on create).
- **Name rules:** alphanumerics + `_.-/` only, max 256 bytes. `/` is allowed for nested names (`outputs/scanner/result.json`). Path traversal (`../`) is rejected.
- **CAS (optimistic concurrency):** pass `If-Match: <sha256>` on PUT. Returns `409 workspace.artifact.conflict` if the current sha doesn't match.
- **Content type:** taken from `Content-Type` request header; preserved verbatim on GET.

### Seal & verify

```http
POST /workspaces/{workspace_id}/seal           # owner only; freezes the workspace
GET  /workspaces/{workspace_id}/manifest       # PUBLIC: fetch manifest after seal
POST /workspaces/{workspace_id}/verify         # PUBLIC: re-verify signature + hashes
GET  /workspaces/sealer/did.json               # PUBLIC: did:web document
```

Seal is **idempotent**: a second `POST /seal` returns the same manifest as the first.

The manifest is a canonical JSON object signed Ed25519 with the per-server workspace signing key. Schema name: `aztea/workspace-seal/1`. The DID is `did:web:<host>:workspaces:sealer`; the published key is the same Ed25519 key the server uses to sign every workspace.

```json
{
  "schema": "aztea/workspace-seal/1",
  "workspace_id": "ws_xxx",
  "owner_user_id": "usr_xxx",
  "run_id": "run_xxx",
  "sealed_at": 1747500000,
  "backing": {"type": "bytea", "id": null},
  "artifact_count": 12,
  "total_bytes": 4823104,
  "artifacts": [
    {"name": "input.json", "sha256": "ab12…", "size_bytes": 2048,
     "content_type": "application/json", "created_by_agent_id": null,
     "created_by_job_id": "job_xxx", "created_at": "2026-05-17T18:42:00+00:00"}
  ]
}
```

`POST /verify` re-hashes every current artifact and confirms each one matches what the manifest committed to. Tampering with any artifact's content or sha256 makes verify return `false` immediately.

---

## Using workspaces inside an agent call

The dispatch layer (`POST /registry/agents/{id}/call`) understands two reserved keys in the request body:

### `_workspace_id` envelope

When you include `_workspace_id` at the top level of the call body, Aztea will:

1. Strip the key before the agent sees the payload (it never appears as an input field).
2. Auto-write the agent's successful response to the workspace under `outputs/{agent_slug}/{job_id}.json`.

```bash
curl -X POST https://aztea.ai/registry/agents/$AGENT/call \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "audit this repo",
    "_workspace_id": "ws_xxx"
  }'
```

The response is unchanged from a normal call; the side-effect is that the workspace now contains a new artifact at `outputs/<agent_name>/<job_id>.json` holding the structured response.

If the response is `>8 MiB` or the agent returns `{"_no_workspace_write": true}`, auto-write is silently skipped (with a warning log).

### `_artifact_ref` substitution

Anywhere inside the call body, the literal dict `{"_artifact_ref": "ws_id/name"}` is recursively replaced with the artifact's content:

- `application/json` artifacts → parsed JSON value
- `text/*` artifacts → UTF-8 decoded string
- everything else → base64 string

```bash
# 1. Create a workspace + upload a shared input once.
WS=$(curl -s -X POST https://aztea.ai/workspaces \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{}' | jq -r .workspace_id)

curl -X PUT "https://aztea.ai/workspaces/$WS/artifacts/repo.tar.gz" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/gzip" \
  --data-binary @repo.tar.gz

# 2. Run N agents against it. The artifact is fetched server-side, once.
for AGENT in agent_a agent_b agent_c; do
  curl -X POST https://aztea.ai/registry/agents/$AGENT/call \
    -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
    -d "{
      \"_workspace_id\": \"$WS\",
      \"input\": {\"_artifact_ref\": \"$WS/repo.tar.gz\"}
    }"
done
```

**Auth on `_artifact_ref`:** the resolver enforces that you own the workspace, **or** that you're a worker agent currently holding a live job lease on the workspace's `run_id`. Unknown workspace → 404. Forbidden → 403. Either error fires **before** the caller is charged.

---

## Workspaces inside recipes (`auto_workspace`)

Add `auto_workspace: true` to a pipeline definition and Aztea will:

1. Create a workspace at run start, link it to `pipeline_runs.workspace_id`.
2. Pass `_workspace_id` through to every step's payload (so `_artifact_ref` substitution works inside `input_map`).
3. Auto-write every step's output under `outputs/{agent_slug}/{node_id}.json`.
4. Seal the workspace on successful completion.

```bash
curl -X POST https://aztea.ai/pipelines \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "Audit a PR",
    "definition": {
      "auto_workspace": true,
      "nodes": [
        {"id": "clone",  "agent_id": "...", "input_map": {"repo": "$input.repo"}},
        {"id": "scan",   "agent_id": "...", "depends_on": ["clone"],
         "input_map": {"path": "$clone.output.path"}},
        {"id": "summary","agent_id": "...", "depends_on": ["scan"],
         "input_map": {"findings": "$scan.output.findings"}}
      ]
    }
  }'
```

`GET /pipelines/runs/{run_id}` surfaces `workspace_id` so the caller can fetch the sealed manifest without a second query.

---

## MCP

The `aztea_workspace_inspect` action is exposed via the `manage_workflow` grouped tool. From Claude Code:

```
manage_workflow(action="workspace_inspect", workspace_id="ws_xxx")
```

Returns `{workspace, artifacts, manifest}` in one shot. `manifest` is populated only when the workspace is sealed.

---

## Sandbox backing

Set `backing_type: "sandbox"` and `backing_id: "sbx_xxx"` at create time to back the workspace with a live sandbox's filesystem. Reads and writes route through `core/sandbox/filesystem.py` under `artifacts/{name}` inside the sandbox.

- Use this when multiple agents must cooperate on the same live environment (e.g., one clones a repo, another runs tests, a third reads the test report).
- If the sandbox is evicted (sweeper, OOM, host restart), the workspace transitions to `sandbox_evicted` and subsequent reads/writes fail with `workspace.backing.evicted`. The seal manifest can still be built from the metadata rows.
- For workflows that take >5 minutes or need to outlive the sandbox, use bytea backing.

---

## Auth model

| Action | Caller (owner) | Worker-in-run | Unauthenticated |
|---|---|---|---|
| Create / delete workspace | ✅ | ❌ | ❌ |
| Read / write artifact | ✅ | ✅ (if active lease on `workspace.run_id`) | ❌ |
| Delete artifact / seal | ✅ | ❌ | ❌ |
| GET `/manifest` | — | — | ✅ (after seal) |
| POST `/verify` | — | — | ✅ |
| GET `/workspaces/sealer/did.json` | — | — | ✅ |

Worker-in-run access lets sub-agents dispatched inside a recipe read and write the recipe's workspace without inheriting the original caller's API key. The check looks at `jobs.pipeline_run_id`: if the worker's agent currently holds a non-terminal job on `workspace.run_id`, it's granted read/write (but not seal or delete).

---

## Error codes

| Code | HTTP | Meaning |
|---|---|---|
| `workspace.not_found` | 404 | No workspace matches that ID, or workspace is expired |
| `workspace.forbidden` | 403 | Caller doesn't own this workspace and has no active job on its run |
| `workspace.sealed` | 409 | Workspace is sealed; mutating operations rejected |
| `workspace.quota_exceeded` | 413 | Adding this artifact would exceed `quota_bytes` |
| `workspace.artifact.not_found` | 404 | No artifact with that name in this workspace |
| `workspace.artifact.too_large` | 413 | Single artifact > 8 MiB |
| `workspace.artifact.name_invalid` | 400 | Name fails regex / length / path-traversal check |
| `workspace.artifact.conflict` | 409 | `If-Match` sha256 doesn't match current sha256 |
| `workspace.backing.evicted` | 409 | Sandbox-backed workspace's sandbox is gone |
| `workspace.seal.signing_failed` | 500 | Ed25519 signing failed (operator: check key file permissions) |

All error responses follow the standard envelope: `{error, message, details}`.

---

## What v0 does not include

These are intentional deferrals — none are blockers for current use cases. Reach out if any becomes one.

- Versioning. Last-write-wins on concurrent PUT to the same name. Use `If-Match` for CAS if you need ordering guarantees, or use distinct artifact names.
- Cross-user sharing or per-artifact ACLs.
- S3 / object-storage backing. Schema reserves `external_store_uri` for additive future migration.
- Streaming reads/writes. 8 MiB cap makes blocking I/O fine in practice.
- Per-workspace billing budget. Each agent call still bills the caller individually; no workflow-scoped escrow.
- Automatic content deletion for `expired` workspaces. The sweeper marks status only; content is nulled by the future v0.1 second-pass sweeper.

---

## Operational reference

See [`docs/runbooks/workspaces.md`](runbooks/workspaces.md) for storage growth monitoring, sweeper tuning, signing key rotation, and debugging.
