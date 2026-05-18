# server.application shard 13 — co-pilot mode HTTP surface.
#
# Loaded BEFORE shard 14 (the SPA catch-all `/{full_path:path}`) so that
# GET /jobs/{job_id}/receipt and POST /jobs/{job_id}/steer are matched
# before the catch-all 404s on any path under /jobs/. If you add more
# co-pilot routes, add them here rather than to shard 14.
#
# Endpoints:
#   POST /jobs/{job_id}/steer    Caller injects mid-flight guidance into a
#                                running job. Capped per-job and per-caller.
#   GET  /jobs/{job_id}/receipt  Fetch the signed transcript receipt for a
#                                terminal job. 425 until the settlement
#                                runner has built and stored the JWS.
#
# OWNS: the two routes above plus the in-memory caller-side rate limit for
#   steer. Everything heavier (predicate eval, settlement, JWS signing)
#   lives in core/ — this shard is a thin HTTP wrapper.
# NOT OWNS: the messaging tx (core/jobs/messaging.py), settlement runner
#   (core/settlement_runner.py), or receipt construction (core/receipts).
# INVARIANTS:
#   - STEER_MAX_PER_JOB caps the per-job steer count. Enforced by re-reading
#     jobs.steer_count from the DB before each accept; never trust the
#     in-memory bucket for the per-job cap.
#   - The per-caller token bucket is best-effort and process-local. Cross-
#     worker leakage is acceptable in v1; revisit if abuse appears.
#   - Both routes go through _caller_can_view_job for ownership; the steer
#     route additionally requires caller scope. Receipts are readable by
#     either side of the job (caller or agent worker).
# DECISIONS:
#   - JobAlreadyTerminal is translated to 409 job.terminal so clients can
#     distinguish "you raced the stop" from "input invalid".
#   - 425 Too Early is used for receipt-not-ready rather than 404 because
#     the resource will exist; clients should retry, not give up.

from collections import defaultdict, deque
from threading import Lock as _SteerRateLock
from time import monotonic as _steer_monotonic

from core.jobs import messaging as jobs_messaging

# Per-job and per-caller bounds for steer. The per-job cap is also encoded
# in core/copilot_predicates / messaging side-effects, but enforcing it at
# the HTTP boundary first means a hot-loop client gets a fast 429 instead
# of churning the messaging tx.
STEER_MAX_PER_JOB = 20
STEER_MAX_RATE_PER_CALLER = 30  # tokens per window
STEER_RATE_WINDOW_SECONDS = 60.0

# Token-bucket state. defaultdict(deque) of monotonic timestamps; we trim
# old entries on each touch. Bounded automatically by the trim — no leak
# from idle callers because empty deques are cheap to keep around in v1.
_steer_caller_buckets: dict[str, deque] = defaultdict(deque)
_steer_caller_buckets_lock = _SteerRateLock()


def _steer_rate_check_or_429(caller_owner_id: str) -> None:
    """Trim and check the per-caller steer bucket. Raise 429 if exhausted.

    Pure side effect: also records the new timestamp on success so the
    caller's window slides forward. Process-local; cross-worker drift is
    acceptable for v1 because the per-job cap is the real backstop.
    """
    now = _steer_monotonic()
    cutoff = now - STEER_RATE_WINDOW_SECONDS
    with _steer_caller_buckets_lock:
        bucket = _steer_caller_buckets[caller_owner_id]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= STEER_MAX_RATE_PER_CALLER:
            raise HTTPException(
                status_code=429,
                detail=error_codes.make_error(
                    "steer.rate_limit.per_caller",
                    (
                        f"Steer rate limit exceeded: max "
                        f"{STEER_MAX_RATE_PER_CALLER} per "
                        f"{int(STEER_RATE_WINDOW_SECONDS)}s per caller."
                    ),
                    {"window_seconds": int(STEER_RATE_WINDOW_SECONDS)},
                ),
            )
        bucket.append(now)


class JobSteerRequest(BaseModel):
    """Body for POST /jobs/{id}/steer.

    ``message`` is the caller-visible nudge that gets threaded into the
    next agent turn. ``metadata`` is opaque structured context the agent
    runtime may key off — keep it small; the messaging tx persists it
    verbatim.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "Skip TypeScript files; focus on Python.",
                "metadata": {"focus": "py"},
            }
        }
    )

    # 4000 char limit matches the documented MCP tool description and
    # the message-payload normaliser in core.models.messages_ops; pre-1.6.9
    # this Pydantic constraint accidentally capped at 2000 and rejected
    # legitimately-long steers with a confusing 422.
    message: str = Field(min_length=1, max_length=4000)
    metadata: dict | None = None


@app.post(
    "/jobs/{job_id}/steer",
    tags=["Jobs"],
    summary="Inject mid-flight guidance into a running job (co-pilot mode).",
    responses=_error_responses(400, 401, 403, 404, 409, 422, 429, 500),
)
@limiter.limit("60/minute")
def jobs_steer(
    request: Request,
    job_id: str,
    body: JobSteerRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Append a caller→agent steer message to the job's inbox.

    Caller-only: the agent worker uses its own claim_token + complete /
    fail / message routes for its half of the conversation. Returns 409
    job.terminal if the job has already settled, 429 with the appropriate
    code on either rate limit, and the new {message_id, steer_count} on
    success.
    """
    _require_scope(caller, "caller")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Job not found or not authorized.")
    caller_owner_id = _caller_owner_id(request)
    if caller["type"] != "master" and caller_owner_id != job.get("caller_owner_id"):
        # Agent-side workers can view a job (heartbeat / complete) but must
        # not be able to inject caller-side steers. Master keys retain the
        # privilege for ops-level intervention.
        raise HTTPException(
            status_code=403,
            detail="Only the job's caller may steer it.",
        )

    # Per-job hard cap: re-read steer_count from the live row so a racing
    # second steer can't sneak past. This is cheap; the row is already in
    # the page cache from get_job above on most backends.
    current_steer_count = int(job.get("steer_count") or 0)
    if current_steer_count >= STEER_MAX_PER_JOB:
        raise HTTPException(
            status_code=429,
            detail=error_codes.make_error(
                "steer.rate_limit.per_job",
                (
                    f"This job has reached the max of {STEER_MAX_PER_JOB} "
                    "steer messages."
                ),
                {"steer_count": current_steer_count, "limit": STEER_MAX_PER_JOB},
            ),
        )

    _steer_rate_check_or_429(caller_owner_id)

    payload = {
        "message": body.message,
        "metadata": body.metadata if isinstance(body.metadata, dict) else None,
    }
    try:
        msg = jobs.add_message(
            job_id,
            from_id=caller_owner_id,
            msg_type="steer",
            payload=payload,
        )
    except jobs_messaging.JobAlreadyTerminal as exc:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                "job.invalid_state",
                str(exc) or "Job is already terminal; steer was rejected.",
            ),
        )
    except ValueError as exc:
        # 1.7.1 — bare ValueErrors (payload validation, correlation problems)
        # were surfacing as a generic FastAPI 500. Translate to 422 so
        # SDKs can branch on caller-input vs server-side fault.
        _LOG.warning(
            "steer.value_error", extra={"job_id": job_id, "error": str(exc)},
        )
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                "steer.invalid_payload", str(exc) or "Steer payload was rejected.",
            ),
        )
    except Exception as exc:
        # 1.7.1 — eval B-2 found steer 500'ing deterministically on running
        # async jobs without a discoverable cause. Log the full exception so
        # the next eval round can pinpoint the path; surface a structured
        # 500 envelope rather than FastAPI's bare HTML error.
        _LOG.exception(
            "steer.unexpected_error",
            extra={"job_id": job_id, "caller_owner_id": caller_owner_id},
        )
        raise HTTPException(
            status_code=500,
            detail=error_codes.make_error(
                "steer.internal_error",
                f"Steer failed: {type(exc).__name__}: {exc!s}",
                {"exception_type": type(exc).__name__},
            ),
        )
    if msg is None:
        # add_message returned None — the messaging tx silently rolled back,
        # likely because the inner SELECT saw the job vanish between
        # guard-check and INSERT. Surface as 409 (state-changed-under-us)
        # rather than letting `msg["message_id"]` KeyError into a 500.
        _LOG.warning(
            "steer.message_missing_after_insert", extra={"job_id": job_id},
        )
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                "job.invalid_state",
                "Steer could not be persisted; job state may have changed.",
            ),
        )

    # Re-read so we report the post-insert counter rather than the stale
    # value from before the messaging tx.
    refreshed = jobs.get_job(job_id) or job
    new_count = int(refreshed.get("steer_count") or current_steer_count + 1)
    return JSONResponse(
        content={
            "message_id": msg["message_id"],
            "steer_count": new_count,
        },
        status_code=201,
    )


@app.get(
    "/jobs/{job_id}/receipt",
    tags=["Jobs"],
    summary="Fetch the signed transcript receipt for a terminal job.",
    responses=_error_responses(401, 403, 404, 425, 429, 500),
)
@limiter.limit("60/minute")
def jobs_get_receipt(
    request: Request,
    job_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Return ``{jws, transcript, public_jwk, kid}`` for a terminal job.

    Both the caller and the agent owner can read the receipt — the JWS is
    signed by the agent's per-call Ed25519 key and is verifiable offline,
    so there is no leak in either direction. Returns 425 when the job is
    terminal but the settlement runner has not yet built the JWS, and
    also when core.receipts is not yet wired (Phase 3).
    """
    _require_any_scope(caller, "caller", "worker")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Job not found or not authorized.")

    receipt_jws_raw = job.get("receipt_jws")
    if not (isinstance(receipt_jws_raw, str) and receipt_jws_raw.strip()):
        raise HTTPException(
            status_code=425,
            detail=error_codes.make_error(
                "receipt.not_ready",
                "Receipt has not been built yet. Retry shortly.",
            ),
        )

    try:
        from core import receipts as _receipts
    except ImportError:
        raise HTTPException(
            status_code=425,
            detail=error_codes.make_error(
                "receipt.not_ready",
                "Receipt subsystem is not yet available. Retry shortly.",
            ),
        )

    read_fn = getattr(_receipts, "read_receipt", None)
    if read_fn is None:
        raise HTTPException(
            status_code=425,
            detail=error_codes.make_error(
                "receipt.not_ready",
                "Receipt subsystem is not yet available. Retry shortly.",
            ),
        )

    receipt_obj = read_fn(job_id)
    if not isinstance(receipt_obj, dict):
        raise HTTPException(
            status_code=425,
            detail=error_codes.make_error(
                "receipt.not_ready",
                "Receipt is not ready. Retry shortly.",
            ),
        )
    return JSONResponse(content=receipt_obj)


# --- Well-known static endpoints --------------------------------------------
#
# Registered in shard 13 so they resolve BEFORE the SPA catch-all in shard 14.
# robots.txt previously returned the SPA HTML (audit 2026-05-16 #16);
# /.well-known/security.txt 404'd (audit #17). Both are tiny, immutable
# strings — inlining keeps the surface auditable in one place rather than
# loading from disk at request time.

_SECURITY_TXT_CONTACT = os.environ.get(
    "SECURITY_TXT_CONTACT", "mailto:security@example.invalid"
)
_SECURITY_TXT_TTL_DAYS = 365


def _robots_txt_body() -> str:
    base = _SERVER_BASE_URL.rstrip("/")
    return (
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /admin/\n"
        "Disallow: /jobs/\n"
        "Disallow: /wallets/\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )


def _security_txt_body() -> str:
    # RFC 9116 requires an Expires field; recompute on each request rather
    # than at import time so a long-lived process doesn't drift past its
    # own expiry.
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=_SECURITY_TXT_TTL_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    canonical = f"{_SERVER_BASE_URL.rstrip('/')}/.well-known/security.txt"
    return (
        f"Contact: {_SECURITY_TXT_CONTACT}\n"
        f"Expires: {expires_at}\n"
        f"Preferred-Languages: en\n"
        f"Canonical: {canonical}\n"
    )


@app.get("/robots.txt", include_in_schema=False)
def robots_txt() -> Response:
    return Response(content=_robots_txt_body(), media_type="text/plain")


@app.get("/.well-known/security.txt", include_in_schema=False)
def security_txt() -> Response:
    return Response(content=_security_txt_body(), media_type="text/plain")




# ---------------------------------------------------------------------------
# Workspace routes (added 2026-05-17 for workspaces v0 PR 2/4).
#
# OWNS: HTTP surface for /workspaces and /workspaces/{id}/artifacts.
# Auth: caller-scope (owner of the workspace) for v0. Worker-in-run is
# added in PR 3 alongside dispatch integration.
# ---------------------------------------------------------------------------

from core import workspaces as _workspaces
from core import workspaces_errors as _wse


def _workspace_not_found_response(workspace_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=error_codes.make_error(
            error_codes.WORKSPACE_NOT_FOUND,
            "Workspace not found.",
            {"workspace_id": workspace_id},
        ),
    )


def _require_workspace_owner(workspace_id: str, caller) -> dict:
    """Return the workspace row if caller owns it; else raise 403/404.

    PR 3 will swap this for `_caller_can_access_workspace` which also
    accepts workers holding an active lease on the workspace's run_id.
    """
    try:
        ws = _workspaces.get_workspace(workspace_id)
    except _wse.WorkspaceNotFound:
        raise _workspace_not_found_response(workspace_id)
    if ws["owner_user_id"] != caller["owner_id"]:
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_FORBIDDEN,
                "Caller does not own this workspace.",
                {"workspace_id": workspace_id},
            ),
        )
    return ws


@app.post(
    "/workspaces",
    tags=["workspaces"],
    summary="Create a workspace.",
    responses=_error_responses(400, 401, 403, 422, 429),
)
@limiter.limit("60/minute")
def workspaces_create(
    request: Request,
    body: dict = Body(default={}),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    try:
        ttl = int(body.get("ttl_seconds") or 86400)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                "ttl_seconds must be an integer.",
                {"ttl_seconds": body.get("ttl_seconds")},
            ),
        )
    backing_type = str(body.get("backing_type") or "bytea")
    backing_id = body.get("backing_id")
    run_id = body.get("run_id")
    try:
        ws_id = _workspaces.create_workspace(
            owner_user_id=caller["owner_id"],
            backing_type=backing_type,
            backing_id=backing_id,
            ttl_seconds=ttl,
            run_id=run_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT, str(exc), {},
            ),
        )
    ws = _workspaces.get_workspace(ws_id)
    return {"workspace_id": ws_id, "expires_at": ws["expires_at"]}


@app.get(
    "/workspaces/{workspace_id}",
    tags=["workspaces"],
    summary="Read workspace metadata.",
    responses=_error_responses(401, 403, 404),
)
def workspaces_get(
    workspace_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    ws = _require_workspace_owner(workspace_id, caller)
    return {
        "workspace_id": ws["workspace_id"],
        "status": ws["status"],
        "backing_type": ws["backing_type"],
        "total_bytes": ws["total_bytes"],
        "artifact_count": ws["artifact_count"],
        "quota_bytes": ws["quota_bytes"],
        "created_at": ws["created_at"],
        "expires_at": ws["expires_at"],
        "sealed_at": ws["sealed_at"],
        "run_id": ws["run_id"],
    }


@app.delete(
    "/workspaces/{workspace_id}",
    tags=["workspaces"],
    summary="Delete a workspace and all its artifacts.",
    responses=_error_responses(401, 403, 404),
)
def workspaces_delete(
    workspace_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    _workspaces.cleanup_workspace(workspace_id)
    return Response(status_code=204)


@app.get(
    "/workspaces/{workspace_id}/artifacts",
    tags=["workspaces"],
    summary="List artifacts in a workspace.",
    responses=_error_responses(401, 403, 404),
)
def workspaces_list_artifacts(
    workspace_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    return {"artifacts": _workspaces.list_artifacts(workspace_id)}


@app.put(
    "/workspaces/{workspace_id}/artifacts/{name:path}",
    tags=["workspaces"],
    summary="Write or overwrite an artifact (raw body).",
    responses=_error_responses(400, 401, 403, 404, 409, 413, 429),
)
@limiter.limit("300/minute")
async def workspaces_put_artifact(
    workspace_id: str,
    name: str,
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    body = await request.body()
    # Fast-path size guard before we hit the module's own check, so we
    # don't even buffer the bytes through write_artifact when oversized.
    if len(body) > 8 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_TOO_LARGE,
                "Artifact exceeds 8 MiB cap.",
                {"size_bytes": len(body)},
            ),
        )
    content_type = request.headers.get("content-type", "application/octet-stream")
    if_match = request.headers.get("if-match")
    try:
        meta = _workspaces.write_artifact(
            workspace_id, name, body, content_type,
            if_match_sha256=if_match,
        )
    except _wse.ArtifactNameInvalid as exc:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_NAME_INVALID, str(exc), {"name": name},
            ),
        )
    except _wse.ArtifactTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_TOO_LARGE, str(exc), {},
            ),
        )
    except _wse.WorkspaceQuotaExceeded as exc:
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_QUOTA_EXCEEDED, str(exc), {},
            ),
        )
    except _wse.ArtifactConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_CONFLICT, str(exc), {},
            ),
        )
    except _wse.WorkspaceSealed:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_SEALED,
                "Workspace is sealed; writes are not permitted.",
                {"workspace_id": workspace_id},
            ),
        )
    return meta


@app.get(
    "/workspaces/{workspace_id}/artifacts/{name:path}",
    tags=["workspaces"],
    summary="Read an artifact as raw bytes.",
    responses=_error_responses(400, 401, 403, 404),
)
def workspaces_get_artifact(
    workspace_id: str,
    name: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    try:
        content, content_type = _workspaces.read_artifact(workspace_id, name)
    except _wse.ArtifactNameInvalid as exc:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_NAME_INVALID, str(exc),
                {"name": name},
            ),
        )
    except _wse.ArtifactNotFound:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_NOT_FOUND,
                "Artifact not found.",
                {"workspace_id": workspace_id, "name": name},
            ),
        )
    return Response(content=content, media_type=content_type)


@app.delete(
    "/workspaces/{workspace_id}/artifacts/{name:path}",
    tags=["workspaces"],
    summary="Delete an artifact.",
    responses=_error_responses(400, 401, 403, 404, 409),
)
def workspaces_delete_artifact(
    workspace_id: str,
    name: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    try:
        _workspaces.delete_artifact(workspace_id, name)
    except _wse.ArtifactNameInvalid as exc:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_NAME_INVALID, str(exc),
                {"name": name},
            ),
        )
    except _wse.ArtifactNotFound:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_ARTIFACT_NOT_FOUND,
                "Artifact not found.",
                {"workspace_id": workspace_id, "name": name},
            ),
        )
    except _wse.WorkspaceSealed:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_SEALED,
                "Workspace is sealed.",
                {"workspace_id": workspace_id},
            ),
        )
    return Response(status_code=204)


@app.post(
    "/workspaces/{workspace_id}/seal",
    tags=["workspaces"],
    summary="Seal a workspace (build signed Ed25519 manifest).",
    responses=_error_responses(401, 403, 404, 409, 500),
)
def workspaces_seal(
    workspace_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "caller")
    _require_workspace_owner(workspace_id, caller)
    try:
        return _workspaces.seal_workspace(workspace_id)
    except _wse.SealSigningFailed as exc:
        raise HTTPException(
            status_code=500,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_SEAL_SIGNING_FAILED,
                "Failed to sign workspace seal manifest.",
                {"reason": str(exc)},
            ),
        )


@app.get(
    "/workspaces/{workspace_id}/manifest",
    tags=["workspaces"],
    summary="Public: fetch the signed manifest for a sealed workspace.",
    responses=_error_responses(404),
)
def workspaces_manifest(workspace_id: str) -> dict:
    # Public — sealed manifests are designed to be shareable evidence.
    # 404 if the workspace exists but is not yet sealed (manifest does
    # not exist), so external verifiers can poll without auth.
    try:
        ws = _workspaces.get_workspace(workspace_id)
    except _wse.WorkspaceNotFound:
        raise _workspace_not_found_response(workspace_id)
    if ws["status"] != "sealed":
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.WORKSPACE_NOT_FOUND,
                "Workspace has no manifest (not sealed).",
                {"workspace_id": workspace_id, "status": ws["status"]},
            ),
        )
    import json as _json
    return {
        "manifest": _json.loads(ws["seal_manifest"]),
        "signature": ws["seal_signature"],
        "public_key_did": ws["seal_public_key_did"],
    }


@app.post(
    "/workspaces/{workspace_id}/verify",
    tags=["workspaces"],
    summary="Public: verify the seal signature over current artifact hashes.",
    responses=_error_responses(404),
)
def workspaces_verify(workspace_id: str) -> dict:
    try:
        ws = _workspaces.get_workspace(workspace_id)
    except _wse.WorkspaceNotFound:
        raise _workspace_not_found_response(workspace_id)
    valid = _workspaces.verify_seal(workspace_id)
    return {
        "valid": valid,
        "signer_did": ws["seal_public_key_did"],
        "sealed_at": ws["sealed_at"],
    }


@app.get(
    "/workspaces/sealer/did.json",
    tags=["workspaces"],
    summary="Public: did:web document for the workspace seal signing key.",
    responses=_error_responses(500),
)
def workspaces_sealer_did_document() -> dict:
    _private_pem, public_pem = _workspaces._load_or_create_signing_keypair()
    from core import crypto as _crypto_local
    did = _workspaces.workspace_signer_did()
    jwk = _crypto_local.public_key_to_jwk(public_pem)
    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": did,
        "verificationMethod": [
            {
                "id": f"{did}#key-1",
                "type": "JsonWebKey2020",
                "controller": did,
                "publicKeyJwk": jwk,
            }
        ],
        "assertionMethod": [f"{did}#key-1"],
    }
