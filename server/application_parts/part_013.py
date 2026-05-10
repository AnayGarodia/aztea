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

    message: str = Field(min_length=1, max_length=2000)
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
                "job.terminal",
                str(exc) or "Job is already terminal; steer was rejected.",
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


