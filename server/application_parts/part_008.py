# server.application shard 8 — registry search + admin review + registry
# sync call flow (pre-charge → dispatch → settle/refund, with built-in
# agent routing, verifier hooks, and dispute-window enforcement).
# Variable-pricing helpers (_estimate_variable_charge,
# _resolve_agent_pricing, _maybe_refund_pricing_diff) live in part_004.

# --- Idempotency cache for sync calls ---
# Key: (caller_owner_id, agent_id, idempotency_key), Value: (response_body, created_at)
_IDEMPOTENCY_CACHE: dict[tuple, tuple] = {}
_IDEMPOTENCY_TTL = 300  # 5 minutes


def _idempotency_lookup(owner_id: str, agent_id: str, key: str) -> dict | None:
    now = time.monotonic()
    cache_key = (owner_id, agent_id, key)
    entry = _IDEMPOTENCY_CACHE.get(cache_key)
    if entry is None:
        return None
    body, created_at = entry
    if now - created_at > _IDEMPOTENCY_TTL:
        _IDEMPOTENCY_CACHE.pop(cache_key, None)
        return None
    return body


def _idempotency_store(owner_id: str, agent_id: str, key: str, body: dict) -> None:
    # Evict stale entries occasionally to prevent unbounded growth
    if len(_IDEMPOTENCY_CACHE) > 10_000:
        cutoff = time.monotonic() - _IDEMPOTENCY_TTL
        stale = [k for k, (_, ts) in _IDEMPOTENCY_CACHE.items() if ts < cutoff]
        for k in stale:
            _IDEMPOTENCY_CACHE.pop(k, None)
    _IDEMPOTENCY_CACHE[(owner_id, agent_id, key)] = (body, time.monotonic())


def _coerce_payload_to_schema(payload: dict, schema: dict) -> dict:
    """
    Coerce string values in payload to the types declared in JSON Schema properties.
    HTML form inputs always arrive as strings; this lets integer/number/boolean
    fields pass jsonschema validation without a 422 error.
    Only touches top-level properties — nested objects are not recursed.
    """
    props = schema.get("properties")
    if not isinstance(props, dict) or not payload:
        return payload
    out = dict(payload)
    for key, defn in props.items():
        if key not in out:
            continue
        raw = out[key]
        if not isinstance(raw, str):
            continue
        declared = defn.get("type") if isinstance(defn, dict) else None
        if declared == "integer":
            try:
                out[key] = int(raw)
            except (ValueError, TypeError):
                pass
        elif declared == "number":
            try:
                out[key] = float(raw)
            except (ValueError, TypeError):
                pass
        elif declared == "boolean":
            out[key] = raw.lower() not in ("", "false", "0", "no")
        elif declared == "array":
            # Accept JSON-encoded arrays or comma-separated strings
            stripped = raw.strip()
            if stripped.startswith("["):
                try:
                    import json as _json

                    out[key] = _json.loads(stripped)
                except Exception:
                    pass
            elif stripped:
                out[key] = [s.strip() for s in stripped.split(",") if s.strip()]
            else:
                out[key] = []
    return out


def _allow_schema_string_coercion(request: Request) -> bool:
    content_type = str(request.headers.get("Content-Type") or "").lower()
    return content_type.startswith(
        "application/x-www-form-urlencoded"
    ) or content_type.startswith("multipart/form-data")


class _SchemaValidationError(ValueError):
    def __init__(self, message: str, path: list[Any] | None = None):
        super().__init__(message)
        self.message = message
        self.absolute_path = list(path or [])


def _raise_schema_error(message: str, path: list[Any] | None = None) -> None:
    raise _SchemaValidationError(message, path)


def _validate_schema_subset(
    instance: Any, schema: dict[str, Any], path: list[Any] | None = None
) -> None:
    current_path = list(path or [])
    declared_type = schema.get("type")
    if isinstance(declared_type, list):
        if not any(
            _schema_type_matches(instance, candidate) for candidate in declared_type
        ):
            _raise_schema_error(
                f"Expected one of {', '.join(str(candidate) for candidate in declared_type)}.",
                current_path,
            )
    elif declared_type is not None and not _schema_type_matches(
        instance, declared_type
    ):
        _raise_schema_error(f"Expected {declared_type}.", current_path)

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and instance not in enum_values:
        _raise_schema_error(
            f"Value must be one of: {', '.join(repr(item) for item in enum_values)}.",
            current_path,
        )

    if isinstance(instance, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in instance:
                    _raise_schema_error(
                        f"Missing required field '{key}'.", current_path + [key]
                    )
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, prop_schema in properties.items():
                if key in instance and isinstance(prop_schema, dict):
                    _validate_schema_subset(
                        instance[key], prop_schema, current_path + [key]
                    )
        additional = schema.get("additionalProperties", True)
        if additional is False and isinstance(properties, dict):
            allowed = set(properties.keys())
            for key in instance:
                if key not in allowed:
                    _raise_schema_error(
                        f"Unexpected field '{key}'.", current_path + [key]
                    )
        return

    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(instance) < min_items:
            _raise_schema_error(
                f"Array must contain at least {min_items} item(s).", current_path
            )
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(instance) > max_items:
            _raise_schema_error(
                f"Array must contain at most {max_items} item(s).", current_path
            )
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _validate_schema_subset(item, item_schema, current_path + [index])
        return

    if isinstance(instance, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(instance) < min_length:
            _raise_schema_error(
                f"String must be at least {min_length} character(s).", current_path
            )
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(instance) > max_length:
            _raise_schema_error(
                f"String must be at most {max_length} character(s).", current_path
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, instance) is None:
                    _raise_schema_error(
                        "String does not match the required pattern.", current_path
                    )
            except re.error:
                pass
        return

    if isinstance(instance, bool):
        return

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            _raise_schema_error(f"Value must be >= {minimum}.", current_path)
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and instance > maximum:
            _raise_schema_error(f"Value must be <= {maximum}.", current_path)
        exclusive_minimum = schema.get("exclusiveMinimum")
        if (
            isinstance(exclusive_minimum, (int, float))
            and instance <= exclusive_minimum
        ):
            _raise_schema_error(f"Value must be > {exclusive_minimum}.", current_path)
        exclusive_maximum = schema.get("exclusiveMaximum")
        if (
            isinstance(exclusive_maximum, (int, float))
            and instance >= exclusive_maximum
        ):
            _raise_schema_error(f"Value must be < {exclusive_maximum}.", current_path)


def _schema_type_matches(value: Any, declared_type: Any) -> bool:
    if declared_type == "object":
        return isinstance(value, dict)
    if declared_type == "array":
        return isinstance(value, list)
    if declared_type == "string":
        return isinstance(value, str)
    if declared_type == "boolean":
        return isinstance(value, bool)
    if declared_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(
            value, bool
        )
    if declared_type == "null":
        return value is None
    return True


def _validate_payload_against_schema(
    *,
    payload: dict[str, Any],
    schema: dict[str, Any] | None,
    allow_string_coercion: bool,
) -> dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        return payload
    normalized_payload = (
        _coerce_payload_to_schema(payload, schema)
        if allow_string_coercion
        else dict(payload)
    )
    try:
        import jsonschema as _jsc

        _jsc.validate(instance=normalized_payload, schema=schema)
    except ImportError:
        _validate_schema_subset(normalized_payload, schema, [])
    return normalized_payload


@app.post(
    "/registry/search",
    response_model=core_models.RegistrySearchResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
)
@limiter.limit(_SEARCH_RATE_LIMIT)
def registry_search(
    request: Request,
    body: RegistrySearchRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistrySearchResponse:
    """
    Recommended discovery endpoint.
    Performs semantic natural-language matching with trust, pricing, and input-schema compatibility checks.
    The legacy GET /registry/agents?tag=... route remains supported for backward compatibility.
    """
    try:
        include_unapproved = _caller_is_admin(caller)
        # 1.7.2 — wrap the FULL search (trust lookup + ranker) in a single
        # 18s budget. Pre-1.7.2 (1.7.1 fix) only wrapped `search_agents`;
        # the `_caller_trust_score` wallet-DB lookup ran OUTSIDE the
        # executor, so any stall there ran past Caddy's ~30s gateway
        # timeout and produced HTTP 502 with empty body — exactly what
        # the 1.7.1 eval found. Budget lowered from 25 → 18s to leave
        # margin for Python overhead + gateway round-trip before Caddy
        # 502s on its end.
        import concurrent.futures as _cf
        _SEARCH_BUDGET_S = 18.0

        def _do_full_search() -> list[dict]:
            _caller_trust = None
            if body.respect_caller_trust_min and caller["type"] != "master":
                _caller_trust = _caller_trust_score(caller["owner_id"])
            return registry.search_agents(
                query=body.query,
                limit=body.limit,
                min_trust=body.min_trust,
                max_price_cents=body.max_price_cents,
                required_input_fields=body.required_input_fields,
                caller_trust=_caller_trust,
                include_unapproved=include_unapproved,
                model_provider=body.model_provider,
                kind=body.kind,
                pii_safe=body.pii_safe,
                outputs_not_stored=body.outputs_not_stored,
                audit_logged=body.audit_logged,
                region_locked=body.region_locked,
            )

        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(_do_full_search)
            try:
                ranked = _fut.result(timeout=_SEARCH_BUDGET_S)
            except _cf.TimeoutError:
                raise HTTPException(
                    status_code=503,
                    detail=error_codes.make_error(
                        "registry.search_unavailable",
                        (
                            f"Search exceeded the {_SEARCH_BUDGET_S:.0f}s budget. "
                            "The embedding model may be cold-loading or wallet "
                            "lookup may be stalled; retry in 30s, or use "
                            "GET /registry/agents?tag=... to browse."
                        ),
                        {"retry_after_seconds": 30},
                    ),
                )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Hide sunset/deprecated builtins from search results for non-admins.
    # They stay callable by direct slug, just not discoverable.
    if not include_unapproved:
        sunset = _builtin_constants.SUNSET_DEPRECATED_AGENT_IDS
        ranked = [
            item
            for item in ranked
            if item["agent"].get("agent_id") not in sunset
            and str(item["agent"].get("review_status") or "").strip().lower() != "sunset"
        ]

    search_stats = _compute_bulk_agent_stats(
        [item["agent"]["agent_id"] for item in ranked]
    )
    results = [
        {
            "agent": _agent_response(
                item["agent"], caller, search_stats.get(item["agent"]["agent_id"])
            ),
            "similarity": item["similarity"],
            "trust": item["trust"],
            "blended_score": item["blended_score"],
            "match_reasons": item["match_reasons"],
        }
        for item in ranked
    ]
    # Empty-result signal: when the ranker gates a query off-catalog (no
    # content-matching candidate, or all below the relevance floor),
    # callers need to distinguish "we ran the search and nothing matches"
    # from "search infrastructure failed." The 2026-05-09 eval noted that
    # gibberish queries returned three generic agents — that path is gone,
    # but the empty response now ships a structured `off_catalog: true`
    # signal so MCP/SDK layers can surface a friendly explanation instead
    # of a silent zero-results UX.
    payload: dict[str, Any] = {"results": results, "count": len(results)}
    if not results:
        payload["off_catalog"] = True
        payload["note"] = (
            "No agent in the current catalog matches this query. "
            "Try aztea_workflow(action='list_agents') to browse, or "
            "rephrase the query to describe the capability you need."
        )
    return JSONResponse(content=payload)


@app.get(
    "/registry/agents/{agent_id}",
    response_model=core_models.AgentResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def registry_get(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentResponse:
    _require_any_scope(caller, "caller", "worker")
    include_unapproved = _caller_is_admin(caller)
    agent = registry.get_agent_with_reputation(
        agent_id, include_unapproved=include_unapproved
    )
    if (
        agent is None
        or agent.get("status") == "banned"
        or not _caller_can_access_agent(caller, agent)
    ):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    agent_stats = _compute_bulk_agent_stats([agent_id]).get(agent_id)
    return JSONResponse(content=_agent_response(agent, caller, agent_stats))


@app.get(
    "/registry/agents/{agent_id}/work-history",
    response_model=core_models.DynamicListResponse,
    responses=_error_responses(401, 404, 429, 500),
)
@limiter.limit("60/minute")
def registry_agent_work_history(
    request: Request,
    agent_id: str,
    limit: int = 20,
    offset: int = 0,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicListResponse:
    """Return paginated public work examples for an agent."""
    capped_limit = max(1, min(int(limit), 50))
    capped_offset = max(0, int(offset))
    agent = registry.get_agent(agent_id)
    if (
        agent is None
        or agent.get("status") == "banned"
        or not _caller_can_access_agent(caller, agent)
    ):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    # Privacy gate: never return work examples for sensitive or security-category agents.
    if (
        bool(agent.get("examples_sensitive"))
        or str(agent.get("category") or "").strip().lower() == "security"
        or str(agent.get("agent_id") or "") in {"1021c65c-d2bf-54ff-823a-897f9deb1029"}
    ):
        return JSONResponse(
            content={
                "items": [],
                "total": 0,
                "limit": capped_limit,
                "offset": capped_offset,
                "note": "Work examples are not published for this agent.",
            }
        )
    examples: list = agent.get("output_examples") or []
    page = examples[capped_offset : capped_offset + capped_limit]
    return JSONResponse(
        content={
            "items": page,
            "total": len(examples),
            "limit": capped_limit,
            "offset": capped_offset,
        }
    )


# --- Agent removal: owner self-sunset + admin sunset/reactivate/hard-delete ---
#
# Sunset writes review_status='sunset' on the agent row. The call hot path
# (server/application_parts/part_002.py::_assert_agent_callable) returns HTTP
# 410 ``agent.sunset`` for sunset rows, and list/search filters hide them from
# non-admin callers. Reversible via /reactivate. Receipts and signed history
# remain intact regardless. The hardcoded SUNSET_DEPRECATED_AGENT_IDS frozenset
# remains as a fallback so existing built-in sunsets keep working without DB
# writes; ``core.registry.agents_ops.is_agent_sunset`` unifies both.

def _invalidate_agents_list_cache() -> None:
    """Drop the 15s ``GET /registry/agents`` cache after a registry mutation.

    Without this, a freshly-sunset agent stays visible in /registry/agents for
    up to 15 seconds — confusing UX (`aztea unpublish foo` says success, then
    `aztea agents list` still shows foo). The cache is module-level state in
    part_007's ``registry_list``; we just clear both halves.
    """
    global _agents_list_cache, _agents_list_cache_at  # noqa: PLW0603
    _agents_list_cache = None
    _agents_list_cache_at = 0.0


class AgentSunsetRequest(BaseModel):
    reason: str | None = None


@app.post(
    "/registry/agents/{agent_id}/sunset",
    status_code=200,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
    tags=["Registry"],
    summary="Soft-remove an agent you own from the catalog (reversible).",
)
@limiter.limit("30/minute")
def registry_agent_sunset(
    request: Request,
    agent_id: str,
    body: AgentSunsetRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Mark the agent as sunset. Owner-callable for own listings; admins can
    sunset any agent. Returns the updated agent row. Idempotent."""
    _require_scope(caller, "worker")
    if caller["type"] in {"agent_key", "agent_caller"}:
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped keys cannot manage listings.",
        )
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    actor_owner_id = (
        "master"
        if caller.get("type") == "master"
        else str(caller.get("owner_id") or "").strip() or "unknown"
    )
    updated = registry.sunset_agent(
        agent_id, actor_owner_id=actor_owner_id, reason=body.reason
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    _invalidate_agents_list_cache()
    return JSONResponse(
        content={
            "ok": True,
            "agent_id": agent_id,
            "review_status": updated.get("review_status"),
            "review_note": updated.get("review_note"),
            "reviewed_by": updated.get("reviewed_by"),
            "reviewed_at": updated.get("reviewed_at"),
            "message": (
                f"Agent '{agent_id}' is sunset. Callers receive HTTP 410. "
                "Reactivate via POST /registry/agents/{id}/reactivate."
            ),
        }
    )


@app.post(
    "/registry/agents/{agent_id}/reactivate",
    status_code=200,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
    tags=["Registry"],
    summary="Reverse a prior sunset, restoring the agent to the catalog.",
)
@limiter.limit("30/minute")
def registry_agent_reactivate(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Restore review_status='approved' for a sunset agent. Owner-callable for
    own listings; admins can reactivate any agent. Refuses if the row is not
    currently in sunset state."""
    _require_scope(caller, "worker")
    if caller["type"] in {"agent_key", "agent_caller"}:
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped keys cannot manage listings.",
        )
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    if str(agent.get("review_status") or "").strip().lower() != "sunset":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Agent '{agent_id}' is not sunset "
                f"(current review_status: {agent.get('review_status')!r})."
            ),
        )
    actor_owner_id = (
        "master"
        if caller.get("type") == "master"
        else str(caller.get("owner_id") or "").strip() or "unknown"
    )
    updated = registry.reactivate_agent(agent_id, actor_owner_id=actor_owner_id)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    _invalidate_agents_list_cache()
    return JSONResponse(
        content={
            "ok": True,
            "agent_id": agent_id,
            "review_status": updated.get("review_status"),
            "reviewed_by": updated.get("reviewed_by"),
            "reviewed_at": updated.get("reviewed_at"),
            "message": f"Agent '{agent_id}' is back in the catalog.",
        }
    )


@app.delete(
    "/admin/agents/{agent_id}",
    status_code=200,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
    tags=["Admin"],
    summary="Hard-delete an agent. Admin only. Receipts preserved by design.",
    dependencies=[Depends(_require_admin_caller)],
)
@limiter.limit("10/minute")
def admin_agent_delete(
    request: Request,
    agent_id: str,
    force: bool = False,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Remove the agent row entirely. Receipts (jobs.output_signature, did
    document) reference agent_id as a denormalized string with no FK cascade,
    so historical receipts continue to verify after deletion.

    Refuses if the agent has in-flight jobs (status in pending|running|
    awaiting_clarification). Pass ``?force=true`` to cancel and refund those
    jobs before deleting.
    """
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    inflight = jobs.list_jobs_for_agent_in_states(
        agent_id, states=("pending", "running", "awaiting_clarification")
    )
    cancelled_count = 0
    refunded_cents = 0
    if inflight:
        if not force:
            raise HTTPException(
                status_code=409,
                detail=error_codes.make_error(
                    "agent.has_inflight_jobs",
                    (
                        f"Agent '{agent_id}' has {len(inflight)} in-flight job(s). "
                        "Pass ?force=true to cancel and refund them before deleting."
                    ),
                    {"agent_id": agent_id, "inflight_count": len(inflight)},
                ),
            )
        actor = (
            "master"
            if caller.get("type") == "master"
            else str(caller.get("owner_id") or "admin").strip()
        )
        for job in inflight:
            job_id = str(job.get("job_id") or "").strip()
            if not job_id:
                continue
            cancelled = jobs.update_job_status(
                job_id,
                "failed",
                error_message=f"Agent deleted by admin ({actor}).",
                completed=True,
            )
            if cancelled is None:
                continue
            settled = _settle_failed_job(cancelled, actor_owner_id=actor)
            cancelled_count += 1
            refunded_cents += int(
                (settled or cancelled).get("caller_charge_cents") or 0
            )

    deleted = registry.delete_agent(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    _invalidate_agents_list_cache()
    return JSONResponse(
        content={
            "ok": True,
            "agent_id": agent_id,
            "deleted": True,
            "jobs_cancelled": cancelled_count,
            "refund_cents": refunded_cents,
            "message": (
                f"Agent '{agent_id}' deleted. {cancelled_count} in-flight "
                f"job(s) cancelled, {refunded_cents}¢ refunded. Existing "
                "receipts remain verifiable."
            ),
        }
    )


def _extract_sync_cache_controls(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool | None, int]:
    """Extract cache controls from a /call body.

    Returns (cleaned_payload, use_cache, cache_ttl_hours).

    `use_cache` is tri-state:
      - True  → caller explicitly opted in
      - False → caller explicitly opted out
      - None  → caller didn't say. The call site defaults to True for any
                agent flagged cacheable so deterministic builtins (CVE lookup,
                linter, type_checker, etc.) cache organically without every
                client having to remember to pass use_cache=True.
    """
    raw = dict(payload or {})
    raw.pop("output_format", None)  # extracted separately by _extract_output_format
    use_cache_raw = raw.pop("use_cache", None)
    cache_ttl_raw = raw.pop("cache_ttl_hours", None)
    try:
        use_cache: bool | None = (
            bool(_normalize_optional_bool(use_cache_raw, field_name="use_cache"))
            if use_cache_raw is not None
            else None
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(error_codes.INVALID_INPUT, str(exc)),
        )
    if cache_ttl_raw is None:
        cache_ttl_hours = 24
    else:
        try:
            cache_ttl_hours = int(cache_ttl_raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    "cache_ttl_hours must be an integer between 1 and 168.",
                ),
            )
        if cache_ttl_hours < 1 or cache_ttl_hours > 168:
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    "cache_ttl_hours must be an integer between 1 and 168.",
                    {"cache_ttl_hours": cache_ttl_hours},
                ),
            )
    return raw, use_cache, cache_ttl_hours


def _extract_output_format(payload_or_body: Any) -> str | None:
    """Pull `output_format` out of a request body without mutating it."""
    if not isinstance(payload_or_body, dict):
        return None
    from core import output_formats as _output_formats

    return _output_formats.normalize_format(payload_or_body.get("output_format"))


def _decorate_with_rendered_output(
    response_payload: dict[str, Any],
    *,
    output_format: str | None,
) -> dict[str, Any]:
    """Attach `rendered_output` (string or dict) to a response when the caller
    requested a non-JSON output format. The canonical `output` field is left
    untouched so existing clients keep working."""
    if not output_format or output_format == "json":
        return response_payload
    output = response_payload.get("output")
    if output is None:
        return response_payload
    from core import output_formats as _output_formats

    try:
        rendered = _output_formats.render(output, format=output_format)
    except Exception:  # pragma: no cover - renderer must never break a call
        return response_payload
    response_payload["rendered_output"] = rendered
    response_payload["rendered_output_format"] = output_format
    return response_payload


_POST_CALL_ACTION_DOCS = "https://github.com/AnayGarodia/aztea/blob/main/docs/api-reference.md"


def _attach_post_call_actions(
    response_payload: dict[str, Any],
    *,
    job: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach a `next_actions` block telling the caller how to rate, dispute,
    or verify the job. The block lists both the HTTP endpoint and the MCP tool
    name so SDK / MCP / raw-HTTP callers all get the same hint format.

    The dispute deadline is computed from completed_at + dispute_window_hours.
    Best-effort: if any field is missing the block still includes the available
    actions but omits the deadline.
    """
    if not isinstance(response_payload, dict):
        return response_payload
    job_id = response_payload.get("job_id")
    if not job_id:
        return response_payload

    deadline_iso: str | None = None
    if isinstance(job, dict):
        completed_at = job.get("completed_at") or job.get("settled_at")
        window_hours = job.get("dispute_window_hours")
        if completed_at and window_hours:
            try:
                from datetime import datetime, timedelta, timezone

                base = str(completed_at)
                # SQLite/Postgres ISO strings sometimes lack tz; treat as UTC.
                dt = datetime.fromisoformat(base.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                deadline_iso = (dt + timedelta(hours=int(window_hours))).isoformat()
            except (TypeError, ValueError):
                deadline_iso = None

    actions: dict[str, Any] = {
        "rate": {
            "tool": "aztea_rate_job",
            "endpoint": f"POST /jobs/{job_id}/rating",
            "args": {"job_id": job_id},
            "purpose": "Rate the agent's output 1-5 to feed trust/quality signals.",
        },
        "dispute": {
            "tool": "aztea_dispute_job",
            "endpoint": f"POST /jobs/{job_id}/dispute",
            "args": {"job_id": job_id},
            "purpose": "Open a dispute if the output is wrong; clawback escrowed payout.",
        },
        "verify": {
            "tool": "aztea_verify_job",
            "endpoint": f"GET /jobs/{job_id}/signature",
            "args": {"job_id": job_id},
            "purpose": "Fetch the Ed25519-signed receipt to verify provenance.",
        },
    }
    if deadline_iso:
        actions["dispute"]["deadline_iso"] = deadline_iso
    response_payload["next_actions"] = actions
    return response_payload


def _cache_hit_response_payload(cached_output: Any) -> dict[str, Any]:
    # Return the same envelope shape as a live call so clients need not
    # branch on whether the response came from cache.
    inner = (
        dict(cached_output)
        if isinstance(cached_output, dict)
        else {"result": cached_output}
    )
    original_job_id = inner.pop("_cached_job_id", None)
    return {
        "job_id": original_job_id,
        "original_job_id": original_job_id,
        "status": "complete",
        "output": inner,
        "latency_ms": 0,
        "cached": True,
    }


# In-process singleflight for cache-eligible identical-input calls. Without
# this, a fan-out of 30 simultaneous CVE lookups all miss the cache (none has
# finished writing yet) and bill the caller 30 times. Now: the first request
# does the work, the others block on its Event up to a short ceiling and then
# re-check the cache. Process-local — multi-dyno fan-outs still race, but a
# single Uvicorn worker handles ~95% of the audit's "stampede" problem.
_CACHE_SINGLEFLIGHT_LOCK = threading.Lock()
_CACHE_SINGLEFLIGHT_INFLIGHT: dict[str, threading.Event] = {}
_CACHE_SINGLEFLIGHT_WAIT_SECONDS = 8.0


def _cache_singleflight_acquire(cache_key: str) -> threading.Event | None:
    """Returns None if this caller is the leader (must do the work + signal).
    Returns the existing Event if another caller is already in flight."""
    if not cache_key:
        return None
    with _CACHE_SINGLEFLIGHT_LOCK:
        existing = _CACHE_SINGLEFLIGHT_INFLIGHT.get(cache_key)
        if existing is not None:
            return existing
        _CACHE_SINGLEFLIGHT_INFLIGHT[cache_key] = threading.Event()
        return None


def _cache_singleflight_release(cache_key: str) -> None:
    if not cache_key:
        return
    with _CACHE_SINGLEFLIGHT_LOCK:
        ev = _CACHE_SINGLEFLIGHT_INFLIGHT.pop(cache_key, None)
    if ev is not None:
        ev.set()


def _build_inline_receipt(
    *,
    job: dict | None,
    agent: dict | None,
    output_payload: Any,
) -> dict[str, Any] | None:
    """Build a verifiable receipt block to inline in the sync call response.

    Mirrors the body of GET /jobs/{id}/signature so callers verifying
    "every call produces a signed receipt" don't need a follow-up GET.
    Returns None if the receipt cannot be assembled (e.g. job not yet
    complete, agent has no signing key after lazy provision attempt).
    The DID document at /agents/{agent_id}/did.json remains the canonical
    source of truth — fields here are a convenience copy.
    """
    if job is None or agent is None or output_payload is None:
        return None
    try:
        from core import crypto as _crypto
    except Exception:
        return None

    job_id = str(job.get("job_id") or "")
    agent_id = str(agent.get("agent_id") or "")
    sig_b64 = job.get("output_signature")
    sig_alg = job.get("output_signature_alg")
    sig_did = job.get("output_signed_by_did")
    sig_at = job.get("output_signed_at")

    # Lazy-sign when the job completed without a signature (HTTP-mode agents
    # don't sign at completion time today). This keeps the inline receipt
    # invariant honest for every successful call.
    if not sig_b64 and str(job.get("status") or "").lower() == "complete" and agent_id:
        try:
            priv, _pub, did_v = registry.ensure_agent_signing_keys(agent_id)
            if priv and did_v:
                sig_b64 = _crypto.sign_payload(priv, output_payload)
                sig_alg = str(agent.get("signing_alg") or "ed25519")
                sig_did = did_v
                sig_at = datetime.now(timezone.utc).isoformat()
                if job_id:
                    try:
                        jobs.update_job_signature(
                            job_id,
                            output_signature=sig_b64,
                            output_signature_alg=sig_alg,
                            output_signed_by_did=sig_did,
                            output_signed_at=sig_at,
                        )
                    except Exception:
                        _LOG.exception(
                            "Failed to persist lazy signature for job %s", job_id
                        )
        except Exception:
            _LOG.exception("Lazy-sign for inline receipt failed: %s", job_id)
            return None
    if not sig_b64:
        return None

    public_key_jwk: dict | None = None
    try:
        public_pem = agent.get("signing_public_key")
        if public_pem:
            public_key_jwk = _crypto.public_key_to_jwk(public_pem)
    except Exception:
        public_key_jwk = None

    signed_payload_b64: str | None = None
    output_hash: str | None = None
    try:
        signed_bytes = _crypto.canonical_json(output_payload)
        output_hash = hashlib.sha256(signed_bytes).hexdigest()
        import base64 as _b64

        signed_payload_b64 = _b64.b64encode(signed_bytes).decode("ascii")
    except Exception:
        _LOG.exception("Failed to canonicalize output for inline receipt %s", job_id)

    base_url = (os.environ.get("SERVER_BASE_URL") or "").rstrip("/")
    verify_url = (
        f"{base_url}/agents/{agent_id}/did.json" if base_url and agent_id else None
    )
    return {
        "job_id": job_id or None,
        "agent_id": agent_id or None,
        "did": sig_did,
        "alg": sig_alg or "ed25519",
        "signature": sig_b64,
        "signed_at": sig_at,
        "output_hash": output_hash,
        "public_key_jwk": public_key_jwk,
        "verify_url": verify_url,
        "signed_payload_b64": signed_payload_b64,
        "signed_payload_encoding": "base64-canonical-json",
    }


def _sync_success_response_payload(
    *,
    job_id: str | None,
    output: Any,
    latency_ms: float,
    cached: bool = False,
    pricing_units: dict[str, Any] | None = None,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": "complete",
        "output": output,
        "latency_ms": round(float(latency_ms), 1),
        "cached": bool(cached),
    }
    if pricing_units:
        payload["pricing_units"] = pricing_units
    if receipt:
        payload["receipt"] = receipt
    return payload


def _build_pricing_units_block(
    *,
    pricing_estimate: dict[str, Any] | None,
    output: dict[str, Any] | None,
    caller_charge_cents: int,
    success_distribution: dict[str, Any] | None = None,
    platform_fee_pct: int | None = None,
    fee_bearer_policy: str | None = None,
) -> dict[str, Any] | None:
    """Surface variable-pricing units in a structured response field so callers
    no longer have to parse the agent description for billing semantics."""
    if not pricing_estimate:
        return None
    pricing_model = str(pricing_estimate.get("pricing_model") or "fixed")
    units_estimated_raw = pricing_estimate.get("units")
    unit_label = pricing_estimate.get("unit")
    units_actual: int | None = None
    if isinstance(output, dict):
        try:
            actual_raw = output.get("billing_units_actual")
            if actual_raw is not None:
                units_actual = int(actual_raw)
        except (TypeError, ValueError):
            units_actual = None
    block: dict[str, Any] = {
        "pricing_model": pricing_model,
        "unit": unit_label,
        "caller_charge_cents": int(caller_charge_cents),
        "caller_charge_usd": round(int(caller_charge_cents) / 100.0, 4),
    }
    if units_estimated_raw is not None:
        try:
            block["units_estimated"] = int(units_estimated_raw)
        except (TypeError, ValueError):
            block["units_estimated"] = units_estimated_raw
    if units_actual is not None:
        block["units_actual"] = units_actual
    if pricing_estimate.get("detail") is not None:
        block["detail"] = pricing_estimate.get("detail")
    if success_distribution is not None:
        block["agent_payout_cents"] = int(
            success_distribution.get("agent_payout_cents") or 0
        )
        block["platform_fee_cents"] = int(
            success_distribution.get("platform_fee_cents") or 0
        )
    if platform_fee_pct is not None:
        block["platform_fee_pct"] = int(platform_fee_pct)
    if fee_bearer_policy is not None:
        block["fee_bearer_policy"] = str(fee_bearer_policy)
    if pricing_model == "fixed":
        # Fixed-price agents still benefit from the structured form so callers
        # don't need separate code paths for fixed vs variable.
        block["unit"] = block.get("unit") or "call"
        block.setdefault("units_estimated", 1)
    return block


def _response_output_mode(request: Request) -> str:
    mode = (
        request.query_params.get("mode")
        or request.headers.get("X-Aztea-Output-Mode")
        or "summary"
    )
    normalized = str(mode).strip().lower()
    return normalized if normalized in {"summary", "full"} else "summary"


def _shape_sync_output_for_response(
    request: Request,
    *,
    job_id: str | None,
    payload: Any,
) -> tuple[Any, dict[str, Any]]:
    from core import feature_flags as _feature_flags
    from core import output_shaping as _output_shaping

    if not _feature_flags.OUTPUT_TRUNCATION:
        return payload, {}
    shaped, truncated = _output_shaping.shape_output(
        payload, _response_output_mode(request)
    )
    extra: dict[str, Any] = {}
    if truncated and job_id:
        extra["output_truncated"] = True
        extra["full_output_available"] = True
        extra["full_output_path"] = f"/jobs/{job_id}/full"
        extra["full_output_hint"] = (
            "Call aztea_job(action='full_output', job_id=..., offset=0, "
            "limit=20000) to fetch chunks."
        )
    return shaped, extra


@app.post(
    "/agents/{agent_id}/estimate",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 404, 422, 429, 500),
)
@limiter.limit("30/minute")
def agent_cost_estimate(
    request: Request,
    agent_id: str,
    body: core_models.RegistryCallRequest | None = Body(default=None),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    agent = registry.get_agent_with_reputation(agent_id, include_unapproved=True)
    if (
        agent is None
        or agent.get("status") == "banned"
        or not _caller_can_access_agent(caller, agent)
    ):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    _assert_agent_callable(agent_id, agent)
    payload, _, _ = _extract_sync_cache_controls(
        dict(body.root) if body is not None else {}
    )
    try:
        payload, _ = _normalize_input_protocol_from_payload(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT, str(exc), {"agent_id": agent_id}
            ),
        )
    pricing_estimate = _estimate_variable_charge(
        agent=agent,
        payload=payload,
        per_job_cap_cents=_caller_key_per_job_cap(caller),
    )
    price_cents = int(pricing_estimate["price_cents"])
    success_distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
        fee_bearer_policy="caller",
    )
    ring = registry.get_agent_call_ring(agent_id)
    latency = registry.compute_latency_estimate(
        ring,
        fallback_latency_ms=float(agent.get("avg_latency_ms") or 0.0),
    )
    note = "Estimated from current pricing and recent call history."
    if pricing_estimate.get("pricing_model") != "fixed":
        note = "Estimated from variable pricing inputs and recent call history."
    if pricing_estimate.get("cap_violated"):
        note += " This estimate exceeds your API key's per-job cap."
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "estimated_cost_cents": int(success_distribution["caller_charge_cents"]),
            "p50_latency_ms": int(latency["p50_latency_ms"]),
            "p95_latency_ms": int(latency["p95_latency_ms"]),
            "confidence": str(latency["confidence"]),
            "based_on_calls": len(ring),
            "note": note,
        }
    )


@app.get(
    "/llm/providers",
    responses=_error_responses(401, 429, 500),
)
@limiter.limit("60/minute")
def llm_providers_list(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    """List all registered LLM providers and their availability."""
    from core.llm import registry as llm_registry

    providers = llm_registry.list_providers()
    return JSONResponse(content={"providers": providers})


@app.get(
    "/registry/agents/{agent_id}/keys",
    response_model=core_models.AgentKeyListResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def registry_agent_key_list(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentKeyListResponse:
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(
            status_code=403, detail="Agent-scoped keys cannot list keys."
        )
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    keys = _auth.list_agent_api_keys(agent_id)
    return JSONResponse(content={"keys": keys})


@app.post(
    "/registry/agents/{agent_id}/keys",
    status_code=201,
    response_model=core_models.AgentKeyCreateResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def registry_agent_key_create(
    request: Request,
    agent_id: str,
    body: AgentKeyCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentKeyCreateResponse:
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(
            status_code=403, detail="Agent-scoped keys cannot mint new keys."
        )
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    key = _auth.create_agent_api_key(agent_id, name=body.name)
    return JSONResponse(
        content={
            "key_id": key["key_id"],
            "agent_id": key["agent_id"],
            "raw_key": key["raw_key"],
            "key_prefix": key["key_prefix"],
            "created_at": key["created_at"],
        },
        status_code=201,
    )


@app.post(
    "/registry/agents/{agent_id}/caller-keys",
    status_code=201,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
    tags=["Registry"],
    summary="Mint an agent-as-caller key (azac_) so this agent can hire other agents.",
)
@limiter.limit("20/minute")
def registry_agent_caller_key_create(
    request: Request,
    agent_id: str,
    body: AgentKeyCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "worker")
    if caller["type"] in {"agent_key", "agent_caller"}:
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped keys cannot mint new keys.",
        )
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    key = _auth.create_agent_caller_api_key(agent_id, name=body.name)
    return JSONResponse(
        content={
            "key_id": key["key_id"],
            "agent_id": key["agent_id"],
            "raw_key": key["raw_key"],
            "key_prefix": key["key_prefix"],
            "key_type": key["key_type"],
            "created_at": key["created_at"],
        },
        status_code=201,
    )


@app.post(
    "/ops/identity/backfill",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
    tags=["Ops"],
    summary=(
        "Provision Ed25519 signing keys for any built-in agent that is "
        "missing one so completed jobs produce verifiable receipts."
    ),
)
@limiter.limit("10/minute")
def ops_identity_backfill(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    # Only act on agents that actually exist in this DB. Sunset/deprecated
    # builtin IDs may be in _BUILTIN_AGENT_IDS but not registered, and would
    # otherwise count as spurious "failures".
    candidate_ids = sorted(_BUILTIN_AGENT_IDS)
    builtin_ids: list[str] = []
    for aid in candidate_ids:
        if registry.get_agent(aid, include_unapproved=True) is not None:
            builtin_ids.append(aid)
    provisioned: list[str] = []
    failed: list[str] = []
    for agent_id in builtin_ids:
        private_pem, public_pem, did_value = registry.ensure_agent_signing_keys(
            agent_id
        )
        if not private_pem or not public_pem or not did_value:
            failed.append(agent_id)
            continue
        provisioned.append(agent_id)
    return JSONResponse(
        content={
            "provisioned_count": len(provisioned),
            "failed_count": len(failed),
            "total_builtin_agents": len(builtin_ids),
            "skipped_unregistered": len(candidate_ids) - len(builtin_ids),
            "failed": failed,
            "note": (
                "Idempotent: agents that already had keys were left unchanged. "
                "Run this once after a deploy if /agents/{id}/did.json reports "
                "missing identity."
            ),
        }
    )


@app.post(
    "/admin/agents/{agent_id}/suspend",
    response_model=core_models.AgentResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def admin_agent_suspend(
    request: Request,
    agent_id: str,
    body: AgentSuspendRequest = AgentSuspendRequest(),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    agent = registry.set_agent_status(agent_id, "suspended", reason=body.reason)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return JSONResponse(content=_agent_response(agent, caller))


@app.post(
    "/admin/agents/{agent_id}/ban",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def admin_agent_ban(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    agent = registry.set_agent_status(agent_id, "banned")
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    summary = _fail_open_jobs_for_agent(
        agent_id,
        actor_owner_id=caller["owner_id"],
        reason="Agent was banned by an administrator.",
    )
    return JSONResponse(
        content={"agent": _agent_response(agent, caller), "ban_summary": summary}
    )


@app.get(
    "/admin/agents/review-queue",
    response_model=core_models.RegistryAgentsResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def admin_agents_review_queue(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistryAgentsResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    agents = registry.list_pending_review_agents()
    return JSONResponse(
        content={
            "agents": [_agent_response(agent, caller) for agent in agents],
            "count": len(agents),
        }
    )


@app.post(
    "/admin/agents/{agent_id}/review",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("30/minute")
def admin_review_agent(
    request: Request,
    agent_id: str,
    body: AgentReviewDecisionRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    reviewed = registry.set_agent_review_decision(
        agent_id,
        decision=body.decision,
        note=body.note,
        reviewed_by=caller["owner_id"],
        reviewed_at=_utc_now_iso(),
    )
    if reviewed is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    health_probe: dict[str, Any] | None = None
    probe_url = str(reviewed.get("healthcheck_url") or "").strip()
    if body.decision == "approve" and probe_url:
        try:
            ok, error_text = _probe_agent_endpoint_health(
                probe_url,
                timeout_seconds=_ENDPOINT_MONITOR_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            ok = False
            error_text = str(exc)
        endpoint_status = "healthy" if ok else "degraded"
        endpoint_failures = (
            0
            if ok
            else max(1, int(reviewed.get("endpoint_consecutive_failures") or 0) + 1)
        )
        reviewed = (
            registry.set_agent_endpoint_health(
                agent_id,
                endpoint_health_status=endpoint_status,
                endpoint_consecutive_failures=endpoint_failures,
                endpoint_last_checked_at=_utc_now_iso(),
                endpoint_last_error=None if ok else error_text,
            )
            or reviewed
        )
        health_probe = {"ok": bool(ok), "error": error_text}

    return JSONResponse(
        content={
            "agent": _agent_response(reviewed, caller),
            "health_probe": health_probe,
        }
    )


@app.post(
    "/registry/agents/{agent_id}/call",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 429, 500, 502, 503),
)
@limiter.limit("10/minute")
def registry_call(
    request: Request,
    agent_id: str,
    body: core_models.RegistryCallRequest | None = Body(default=None),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> Response:
    """
    Invoke a registered agent with full payment lifecycle:
      1. Deduct price (402 if broke).
      2. Dispatch call (internal handler for internal:// endpoints, HTTP otherwise).
      3a. Success → payout 90% agent / 10% platform.
      3b. Failure → full refund to caller.
    """
    _require_scope(caller, "caller")
    idempotency_key = request.headers.get("X-Idempotency-Key", "").strip()
    caller_owner_id_early = _caller_owner_id(request)
    if idempotency_key:
        cached = _idempotency_lookup(caller_owner_id_early, agent_id, idempotency_key)
        if cached is not None:
            return JSONResponse(
                content=cached, headers={"X-Idempotency-Replayed": "true"}
            )
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_access_agent(caller, agent):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    _assert_agent_callable(agent_id, agent)
    builtin_agent_id = _resolve_builtin_agent_id(agent)
    hosted_skill_row: dict | None = None
    if builtin_agent_id is None and _hosted_skills.is_skill_endpoint(
        agent.get("endpoint_url")
    ):
        hosted_skill_row = _hosted_skills.get_hosted_skill_by_agent_id(
            str(agent["agent_id"])
        )
        if hosted_skill_row is None:
            raise HTTPException(
                status_code=502,
                detail=error_codes.make_error(
                    error_codes.AGENT_INTERNAL_ERROR,
                    "Hosted skill record is missing. Contact the agent owner.",
                    {"agent_id": agent_id},
                ),
            )
    safe_endpoint_url = ""
    if builtin_agent_id is None and hosted_skill_row is None:
        try:
            safe_endpoint_url = _validate_agent_endpoint_url(
                request, str(agent.get("endpoint_url") or "")
            )
        except ValueError as exc:
            _LOG.warning(
                "Blocked misconfigured endpoint for agent %s: %s", agent_id, exc
            )
            raise HTTPException(
                status_code=502, detail="Agent endpoint is misconfigured."
            )

    caller_owner_id = _caller_owner_id(request)
    client_id = _request_client_id(request)
    fee_bearer_policy = "caller"
    platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
    raw_body = dict(body.root) if body is not None else {}
    requested_output_format = _extract_output_format(raw_body)
    payload, use_cache, cache_ttl_hours = _extract_sync_cache_controls(raw_body)
    requested_output_formats: list[str] = []
    try:
        payload, requested_output_formats = _normalize_input_protocol_from_payload(
            payload
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                str(exc),
                {"agent_id": agent_id},
            ),
        )
    if builtin_agent_id is not None:
        try:
            _validate_builtin_agent_payload(builtin_agent_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    str(exc),
                    {"agent_id": agent_id},
                ),
            )
    agent_input_schema = agent.get("input_schema")
    if isinstance(agent_input_schema, dict) and agent_input_schema:
        try:
            payload = _validate_payload_against_schema(
                payload=payload,
                schema=agent_input_schema,
                allow_string_coercion=_allow_schema_string_coercion(request),
            )
        except Exception as _schema_exc:
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.INPUT_SCHEMA_VIOLATION,
                    f"Input validation failed: {_schema_exc.message if hasattr(_schema_exc, 'message') else str(_schema_exc)}",
                    {
                        "path": list(getattr(_schema_exc, "absolute_path", [])),
                        "agent_id": agent_id,
                    },
                ),
            )

    pricing_estimate = _estimate_variable_charge(
        agent=agent,
        payload=payload,
        per_job_cap_cents=_caller_key_per_job_cap(caller),
    )
    if pricing_estimate.get("cap_violated"):
        violation = pricing_estimate["cap_violated"]
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.SPEND_LIMIT_EXCEEDED,
                "Variable-price estimate exceeds your API key's per-job cap.",
                {
                    "scope": "api_key_per_job",
                    "limit_cents": violation["limit_cents"],
                    "attempted_cents": violation["price_cents"],
                    "pricing_model": pricing_estimate["pricing_model"],
                    "detail": pricing_estimate.get("detail"),
                },
            ),
        )
    price_cents = int(pricing_estimate["price_cents"])
    private_task = _is_private_task_payload(payload)
    from core import feature_flags as _feature_flags

    cache_version_token = _cache.cache_identity(agent, agent_id)
    cache_enabled = (
        _feature_flags.RESULT_CACHE_V2
        and _cache.agent_cacheable(agent)
        and not private_task
    )
    # Default: cache reads/writes are on whenever the agent is cacheable. Caller
    # can still opt out explicitly with use_cache=False. The previous opt-in
    # default left the cache dormant — even repeated CVE lookups never hit.
    if use_cache is None:
        use_cache = cache_enabled
    singleflight_key: str | None = None
    if use_cache and cache_enabled:
        cached_output = _cache.get_cached(
            agent_id, payload, version_token=cache_version_token
        )
        if cached_output is not None:
            cache_response = _cache_hit_response_payload(cached_output)
            shaped_output, extra = _shape_sync_output_for_response(
                request,
                job_id=cache_response.get("job_id"),
                payload=cache_response.get("output"),
            )
            cache_response["output"] = shaped_output
            cache_response.update(extra)
            cache_response = _decorate_with_rendered_output(
                cache_response, output_format=requested_output_format
            )
            return JSONResponse(content=cache_response)
        # Singleflight: collapse concurrent identical-input calls so a fan-out
        # of N doesn't all miss + all charge. Followers wait for the leader's
        # cache write and re-check.
        try:
            singleflight_key = _cache.cache_key(
                agent_id, payload, version_token=cache_version_token
            )
        except Exception:
            singleflight_key = None
        if singleflight_key:
            existing_event = _cache_singleflight_acquire(singleflight_key)
            if existing_event is not None:
                # Another caller is already executing this exact input. Wait
                # briefly, then re-check the cache. If the leader produced a
                # result, return it; otherwise fall through to fresh execution.
                existing_event.wait(_CACHE_SINGLEFLIGHT_WAIT_SECONDS)
                cached_output = _cache.get_cached(
                    agent_id, payload, version_token=cache_version_token
                )
                if cached_output is not None:
                    cache_response = _cache_hit_response_payload(cached_output)
                    shaped_output, extra = _shape_sync_output_for_response(
                        request,
                        job_id=cache_response.get("job_id"),
                        payload=cache_response.get("output"),
                    )
                    cache_response["output"] = shaped_output
                    cache_response.update(extra)
                    cache_response = _decorate_with_rendered_output(
                        cache_response, output_format=requested_output_format
                    )
                    return JSONResponse(content=cache_response)
                # Leader timed out / failed without writing — claim leadership
                # ourselves so subsequent followers wait on the right event.
                _cache_singleflight_acquire(singleflight_key)
    success_distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )
    caller_charge_cents = int(success_distribution["caller_charge_cents"])
    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    # Payouts settle to the canonical agent wallet keyed by agent_id.
    _agent_payout_owner = f"agent:{agent['agent_id']}"
    agent_wallet = payments.get_or_create_wallet(_agent_payout_owner)
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    charge_tx_id = _pre_call_charge_or_402(
        caller=caller,
        caller_wallet_id=caller_wallet["wallet_id"],
        charge_cents=caller_charge_cents,
        agent_id=agent_id,
    )
    if builtin_agent_id is not None or hosted_skill_row is not None:
        try:
            job = jobs.create_job(
                agent_id=agent["agent_id"],
                caller_owner_id=caller_owner_id,
                caller_wallet_id=caller_wallet["wallet_id"],
                agent_wallet_id=agent_wallet["wallet_id"],
                platform_wallet_id=platform_wallet["wallet_id"],
                price_cents=price_cents,
                caller_charge_cents=caller_charge_cents,
                platform_fee_pct_at_create=platform_fee_pct_at_create,
                fee_bearer_policy=fee_bearer_policy,
                client_id=client_id,
                charge_tx_id=charge_tx_id,
                input_payload=payload,
                agent_owner_id=agent.get("owner_id"),
                max_attempts=1,
                dispute_window_hours=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
                judge_agent_id=_extract_judge_agent_id(agent.get("input_schema"))
                or _QUALITY_JUDGE_AGENT_ID,
            )
        except Exception:
            payments.post_call_refund(
                caller_wallet["wallet_id"],
                charge_tx_id,
                caller_charge_cents,
                agent["agent_id"],
            )
            _LOG.exception(
                "Failed to create sync job for built-in agent %s.", agent["agent_id"]
            )
            raise HTTPException(status_code=500, detail="Failed to create job.")
        _record_job_event(
            job,
            "job.created",
            actor_owner_id=caller["owner_id"],
            payload={"source": "registry_call_sync", "max_attempts": 1},
        )
        try:
            if hosted_skill_row is not None:
                output = _skill_executor.execute_hosted_skill(hosted_skill_row, payload)
            else:
                try:
                    output = _execute_builtin_agent(builtin_agent_id, payload)
                except _AgentSlotUnavailable as slot_exc:
                    # Per-agent concurrency cap saturated. Refund the caller,
                    # mark the job failed, and surface 429 — was previously a
                    # 502 cascade from downstream-pool exhaustion.
                    failed = jobs.update_job_status(
                        job["job_id"],
                        "failed",
                        error_message=(
                            f"Agent '{slot_exc.agent_id}' is at capacity "
                            f"({slot_exc.limit} in flight). Refunded; retry shortly."
                        ),
                        completed=True,
                    )
                    if failed is not None:
                        _settle_failed_job(
                            failed,
                            actor_owner_id=caller["owner_id"],
                            event_type="job.failed_rate_limited",
                        )
                    raise HTTPException(
                        status_code=429,
                        headers={"Retry-After": "1"},
                        detail=error_codes.make_error(
                            error_codes.AGENT_UPSTREAM_TIMEOUT,
                            (
                                f"Agent is at capacity ({slot_exc.limit} concurrent "
                                "in flight). You were not charged. Retry in ~1s."
                            ),
                            {
                                "agent_id": slot_exc.agent_id,
                                "concurrency_limit": slot_exc.limit,
                                "refunded_cents": int(
                                    job.get("caller_charge_cents") or 0
                                ),
                            },
                        ),
                    )
                except _AgentWallClockTimeout as to_exc:
                    # 1.7.3 — agent exceeded its wall-clock budget. Refund
                    # the caller, mark failed, surface a structured 504.
                    # Pre-1.7.3 this scenario produced a Caddy 502 with
                    # empty body and no refund (B-3, B-4 in the 1.7.1 eval).
                    failed = jobs.update_job_status(
                        job["job_id"],
                        "failed",
                        error_message=(
                            f"Agent '{to_exc.agent_id}' exceeded its "
                            f"{to_exc.budget_seconds:.1f}s wall-clock budget. "
                            "Refunded."
                        ),
                        completed=True,
                    )
                    if failed is not None:
                        _settle_failed_job(
                            failed,
                            actor_owner_id=caller["owner_id"],
                            event_type="job.failed_timeout",
                        )
                    raise HTTPException(
                        status_code=504,
                        detail=error_codes.make_error(
                            error_codes.AGENT_CALL_TIMEOUT,
                            (
                                f"Agent took longer than the {to_exc.budget_seconds:.1f}s "
                                "wall-clock budget. You were refunded. Common causes: "
                                "regex catastrophic backtracking, unbounded loops, "
                                "pathological input shapes."
                            ),
                            {
                                "agent_id": to_exc.agent_id,
                                "budget_seconds": to_exc.budget_seconds,
                                "refunded_cents": int(
                                    job.get("caller_charge_cents") or 0
                                ),
                            },
                        ),
                    )
            output = _normalize_output_protocol_for_response(
                output,
                requested_output_formats=requested_output_formats,
            )
            if _is_unchargeable_degraded_output(str(builtin_agent_id), output):
                output = _degraded_unchargeable_error(str(builtin_agent_id))
            # If the agent reported a structured *.tool_unavailable / .not_configured
            # error, treat the job as a failure: refund the caller, mark the job
            # failed, and surface a 502 so the SDK can react. This closes the
            # charge-on-broken-tool gap reported in the 2026-04-28 audit (Browser,
            # Visual Regression, Linter, Type Checker, Image Generator all billed
            # users despite producing no usable output).
            agent_failed, failure_code, failure_message = _is_agent_failure_envelope(
                output
            )
            if agent_failed:
                failed = jobs.update_job_status(
                    job["job_id"],
                    "failed",
                    output_payload=output,
                    error_message=(
                        failure_message or f"Agent reported {failure_code}; no charge."
                    ),
                    completed=True,
                )
                if failed is not None:
                    _settle_failed_job(
                        failed,
                        actor_owner_id=caller["owner_id"],
                        event_type="job.failed_dependency",
                    )
                # 502 for genuine infra failures; 422 for caller-input validation
                # failures so the SDK can act on the distinction without parsing
                # error_code strings. Refund happened either way.
                input_failure_markers = (
                    ".invalid_input",
                    ".invalid_payload",
                    ".missing_",
                    ".invalid_",
                    ".unsupported_",
                    ".query_too_long",
                    ".code_too_long",
                    ".stdin_too_long",
                    ".url_too_long",
                    ".too_many_",
                    ".url_blocked",
                    # Dimension mismatch is a caller-input error (caller provided
                    # two images with different sizes) — should be 422, not 502.
                    ".dimension_mismatch",
                    # 1.7.1 — eight agents previously surfaced as agent.internal_error
                    # for what is plainly user input. JWT/openapi/ssl decode paths
                    # that fail are 4xx-class; same for terraform's "this isn't a
                    # plan file" detection and image/cert too-large rejections.
                    ".malformed",
                    ".parse_error",
                    ".parse_failed",
                    ".decode_failed",
                    ".ambiguous_input",
                    ".plan_too_large",
                    ".invalid_json",
                    ".private_domain",
                    ".redirect_blocked",
                    "not_a_",  # terraform_plan_analyzer.not_a_terraform_plan
                )
                # Target-unreachable: caller gave a domain/URL that doesn't
                # resolve, doesn't answer, or returns no useful data. Distinct
                # from "agent crashed" — the agent ran fine; the target is the
                # problem. Surface as 422 with a dedicated envelope code so
                # SDKs can branch on retry vs fix-input vs page-oncall.
                target_unreachable_markers = (
                    ".fetch_failed",
                    ".no_results",
                )
                lowered_code = (failure_code or "").lower()
                http_status = 502
                envelope_code = error_codes.AGENT_INTERNAL_ERROR
                if any(m in lowered_code for m in input_failure_markers):
                    http_status = 422
                    envelope_code = error_codes.AGENT_INVALID_INPUT
                elif any(m in lowered_code for m in target_unreachable_markers):
                    http_status = 422
                    envelope_code = error_codes.AGENT_TARGET_UNREACHABLE
                raise HTTPException(
                    status_code=http_status,
                    detail=error_codes.make_error(
                        envelope_code,
                        failure_message
                        or f"Agent unavailable ({failure_code}). You were not charged.",
                        {
                            "agent_id": agent_id,
                            "error_code": failure_code,
                            "refunded_cents": int(job.get("caller_charge_cents") or 0),
                        },
                    ),
                )
            sig_b64: str | None = None
            sig_alg: str | None = None
            sig_did: str | None = None
            sig_at: str | None = None
            try:
                from core import crypto as _crypto

                private_pem = agent.get("signing_private_key")
                agent_did_value = agent.get("did")
                # Lazy-provision keys when the lifespan backfill missed this
                # agent (e.g. older deploys, or new builtins added between
                # restarts). Without this, a missing key permanently breaks
                # receipts for the affected agent.
                if not private_pem or not agent_did_value:
                    private_pem, _public_pem, agent_did_value = (
                        registry.ensure_agent_signing_keys(agent["agent_id"])
                    )
                if private_pem and agent_did_value:
                    sig_b64 = _crypto.sign_payload(private_pem, output)
                    sig_alg = str(agent.get("signing_alg") or "ed25519")
                    sig_did = agent_did_value
                    sig_at = datetime.now(timezone.utc).isoformat()
            except Exception:
                _LOG.exception("Failed to sign sync output for job %s", job["job_id"])
                sig_b64 = sig_alg = sig_did = sig_at = None

            completed = jobs.update_job_status(
                job["job_id"],
                "complete",
                output_payload=output,
                completed=True,
                output_signature=sig_b64,
                output_signature_alg=sig_alg,
                output_signed_by_did=sig_did,
                output_signed_at=sig_at,
            )
            if completed is None:
                raise RuntimeError("Failed to mark built-in sync job complete.")
            _record_job_event(
                completed,
                "job.completed",
                actor_owner_id=caller["owner_id"],
                payload={"status": completed["status"], "source": "registry_call_sync"},
            )
            _settle_successful_job(completed, actor_owner_id=caller["owner_id"])
            # 1.7.2 — receipt build is now unconditional at completion
            # (decoupled from settlement). Sync /call already worked in
            # 1.7.1 because settlement fires inline; we still pull the
            # call out so the contract matches the async path. Re-read
            # `completed` after the build so the sync response carries
            # the populated `receipt_jws` field too.
            _build_job_receipt_best_effort(job["job_id"])
            completed = jobs.get_job(job["job_id"]) or completed
            _maybe_refund_pricing_diff(
                agent=agent,
                payload=payload,
                output=output,
                caller_wallet_id=caller_wallet["wallet_id"],
                agent_wallet_id=agent_wallet["wallet_id"],
                platform_wallet_id=platform_wallet["wallet_id"],
                charge_tx_id=charge_tx_id,
                estimate=pricing_estimate,
                caller_charge_cents=caller_charge_cents,
                success_distribution=success_distribution,
                platform_fee_pct=platform_fee_pct_at_create,
                fee_bearer_policy=fee_bearer_policy,
            )
            _record_public_work_example(
                agent,
                payload,
                output,
                job_id=job["job_id"],
                latency_ms=_job_latency_ms(completed),
            )
            response_payload = _sync_success_response_payload(
                job_id=job["job_id"],
                output=output,
                latency_ms=_job_latency_ms(completed),
                cached=False,
                pricing_units=_build_pricing_units_block(
                    pricing_estimate=pricing_estimate,
                    output=output if isinstance(output, dict) else None,
                    caller_charge_cents=caller_charge_cents,
                    success_distribution=success_distribution,
                    platform_fee_pct=platform_fee_pct_at_create,
                    fee_bearer_policy=fee_bearer_policy,
                ),
                receipt=_build_inline_receipt(
                    job=completed, agent=agent, output_payload=output
                ),
            )
            shaped_output, extra = _shape_sync_output_for_response(
                request,
                job_id=job["job_id"],
                payload=output,
            )
            response_payload["output"] = shaped_output
            response_payload.update(extra)
            if idempotency_key:
                _idempotency_store(
                    caller_owner_id_early, agent_id, idempotency_key, response_payload
                )
            if cache_enabled:
                _cache.set_cached(
                    agent["agent_id"],
                    payload,
                    output,
                    job["job_id"],
                    ttl_hours=cache_ttl_hours,
                    version_token=cache_version_token,
                )
                _cache_singleflight_release(singleflight_key or "")
            response_payload = _decorate_with_rendered_output(
                response_payload, output_format=requested_output_format
            )
            response_payload = _attach_post_call_actions(
                response_payload, job=completed
            )
            # Always wrap in a consistent envelope so callers can reliably
            # read job_id, status, and output without sniffing the shape.
            return JSONResponse(content=response_payload)
        except ValidationError as exc:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message="Request validation failed.",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_validation",
                )

            def _sanitize_errors(errors):
                clean = []
                for e in errors:
                    entry = {k: v for k, v in e.items() if k != "ctx"}
                    ctx = e.get("ctx")
                    if ctx:
                        entry["ctx"] = {k: str(v) for k, v in ctx.items()}
                    clean.append(entry)
                return clean

            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    "Request validation failed.",
                    {"errors": _sanitize_errors(exc.errors())},
                ),
            )
        except ValueError as exc:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message=str(exc),
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_input",
                )
            message = str(exc)
            status = 422 if message.startswith("Invalid ticker symbol:") else 400
            raise HTTPException(status_code=status, detail=message)
        except _groq.RateLimitError as exc:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message=f"All LLM models rate-limited. ({exc})",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_rate_limit",
                )
            raise HTTPException(
                status_code=503, detail=f"All LLM models rate-limited. ({exc})"
            )
        except HTTPException:
            # Already a structured HTTP error (e.g. our tool_unavailable 502
            # with a refund). Pass it through untouched — the broad Exception
            # handler below would otherwise downgrade it to a 500 with no
            # structured detail and re-bill the caller via _settle_failed_job
            # even though we already refunded.
            raise
        except Exception:
            _LOG.exception("Built-in agent execution failed for %s.", builtin_agent_id)
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message="Agent execution failed.",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_builtin",
                )
            raise HTTPException(status_code=500, detail="Agent execution failed.")

    # --- Input payload size cap (256 KB) ---
    try:
        payload_bytes = len(json.dumps(payload).encode("utf-8"))
    except Exception:
        payload_bytes = 0
    if payload_bytes > 256 * 1024:
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
        )
        raise HTTPException(
            status_code=413,
            detail=error_codes.make_error(
                error_codes.PAYLOAD_TOO_LARGE,
                "Input payload exceeds the 256 KB limit. Agents cannot process payloads this large. "
                "try summarizing or splitting into multiple calls.",
                {"size_bytes": payload_bytes, "limit_bytes": 256 * 1024},
            ),
        )

    try:
        job = jobs.create_job(
            agent_id=agent["agent_id"],
            caller_owner_id=caller_owner_id,
            caller_wallet_id=caller_wallet["wallet_id"],
            agent_wallet_id=agent_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            price_cents=price_cents,
            caller_charge_cents=caller_charge_cents,
            platform_fee_pct_at_create=platform_fee_pct_at_create,
            fee_bearer_policy=fee_bearer_policy,
            client_id=client_id,
            charge_tx_id=charge_tx_id,
            input_payload=payload,
            agent_owner_id=agent.get("owner_id"),
            max_attempts=1,
            dispute_window_hours=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
            judge_agent_id=_extract_judge_agent_id(agent.get("input_schema"))
            or _QUALITY_JUDGE_AGENT_ID,
        )
    except Exception:
        payments.post_call_refund(
            caller_wallet["wallet_id"],
            charge_tx_id,
            caller_charge_cents,
            agent["agent_id"],
        )
        _LOG.exception(
            "Failed to create sync job for remote agent %s.", agent["agent_id"]
        )
        raise HTTPException(status_code=500, detail="Failed to create job.")
    _record_job_event(
        job,
        "job.created",
        actor_owner_id=caller["owner_id"],
        payload={"source": "registry_call_sync_http", "max_attempts": 1},
    )

    try:
        proxy_agent = dict(agent)
        proxy_agent["endpoint_url"] = safe_endpoint_url
        resp = http.post(
            safe_endpoint_url,
            json=payload,
            headers=_proxy_headers_for_agent(proxy_agent),
            timeout=120,
            allow_redirects=False,
        )
    except http.exceptions.Timeout:
        failed = jobs.update_job_status(
            job["job_id"],
            "failed",
            error_message="Agent timed out.",
            completed=True,
        )
        if failed is not None:
            _settle_failed_job(
                failed,
                actor_owner_id=caller["owner_id"],
                event_type="job.failed_timeout",
            )
        _LOG.warning("Agent call timed out for %s", agent_id)
        raise HTTPException(
            status_code=504,
            detail=error_codes.make_error(
                error_codes.AGENT_CALL_TIMEOUT,
                "Agent didn't respond within 120 seconds. You were not charged.",
                {"agent_id": agent_id},
            ),
        )
    except http.RequestException as e:
        failed = jobs.update_job_status(
            job["job_id"],
            "failed",
            error_message=f"Agent endpoint unreachable ({type(e).__name__}).",
            completed=True,
        )
        if failed is not None:
            _settle_failed_job(
                failed,
                actor_owner_id=caller["owner_id"],
                event_type="job.failed_endpoint_offline",
            )
        _LOG.warning(
            "Upstream agent unreachable for %s: %s", agent_id, type(e).__name__
        )
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                error_codes.AGENT_ENDPOINT_OFFLINE,
                "This agent's endpoint is offline or unreachable. You were not charged.",
                {"agent_id": agent_id},
            ),
        )

    status_code = int(resp.status_code)
    success = 200 <= status_code < 300

    if not success:
        failed = jobs.update_job_status(
            job["job_id"],
            "failed",
            error_message=f"Agent returned HTTP {status_code}.",
            completed=True,
        )
        if failed is not None:
            _settle_failed_job(
                failed,
                actor_owner_id=caller["owner_id"],
                event_type="job.failed_rejected_request"
                if 400 <= status_code < 500
                else "job.failed_internal_error",
            )
        if 400 <= status_code < 500:
            # Surface agent's own error message (truncated) but never expose internals
            try:
                agent_err = resp.json()
                agent_msg = str(
                    agent_err.get("error")
                    or agent_err.get("message")
                    or agent_err.get("detail")
                    or ""
                )[:500]
            except Exception:
                agent_msg = ""
            msg = "Agent rejected the request. You were not charged."
            if agent_msg:
                msg = f"Agent rejected the request: {agent_msg}. You were not charged."
            raise HTTPException(
                status_code=status_code,
                detail=error_codes.make_error(
                    error_codes.AGENT_REJECTED_REQUEST,
                    msg,
                    {"agent_id": agent_id, "agent_status": status_code},
                ),
            )
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                error_codes.AGENT_INTERNAL_ERROR,
                "Agent is experiencing errors. You were not charged.",
                {"agent_id": agent_id, "agent_status": status_code},
            ),
        )

    # --- Output size cap (1 MB) ---
    raw_content = resp.content
    if len(raw_content) > 1_048_576:
        failed = jobs.update_job_status(
            job["job_id"],
            "failed",
            error_message="Agent returned a response larger than 1 MB.",
            completed=True,
        )
        if failed is not None:
            _settle_failed_job(
                failed,
                actor_owner_id=caller["owner_id"],
                event_type="job.failed_response_too_large",
            )
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                error_codes.AGENT_RESPONSE_TOO_LARGE,
                "Agent returned a response larger than 1 MB. You were not charged. Contact the agent owner.",
                {"agent_id": agent_id, "size_bytes": len(raw_content)},
            ),
        )

    # --- Non-JSON response handling ---
    content_type = resp.headers.get("content-type", "").lower()
    if "application/json" not in content_type and "text/json" not in content_type:
        try:
            json.loads(raw_content)
        except Exception:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message="Agent returned malformed JSON.",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_invalid_response",
                )
            raise HTTPException(
                status_code=502,
                detail=error_codes.make_error(
                    error_codes.AGENT_INVALID_RESPONSE,
                    "Agent returned a malformed response (not valid JSON). You were not charged.",
                    {"agent_id": agent_id},
                ),
            )

    result_payload = json.loads(raw_content)
    result_payload = _normalize_output_protocol_for_response(
        result_payload,
        requested_output_formats=requested_output_formats,
    )
    completed = jobs.update_job_status(
        job["job_id"],
        "complete",
        output_payload=result_payload,
        completed=True,
    )
    if completed is None:
        raise HTTPException(status_code=500, detail="Failed to mark sync job complete.")
    _record_job_event(
        completed,
        "job.completed",
        actor_owner_id=caller["owner_id"],
        payload={"status": completed["status"], "source": "registry_call_sync_http"},
    )
    settled = _settle_successful_job(completed, actor_owner_id=caller["owner_id"])
    _build_job_receipt_best_effort(completed["job_id"])  # 1.7.2 B-7
    settled = jobs.get_job(completed["job_id"]) or settled  # re-read receipt_jws
    _maybe_refund_pricing_diff(
        agent=agent,
        payload=payload,
        output=result_payload,
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        charge_tx_id=charge_tx_id,
        estimate=pricing_estimate,
        caller_charge_cents=caller_charge_cents,
        success_distribution=success_distribution,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )
    _record_public_work_example(
        agent,
        payload,
        result_payload,
        job_id=job["job_id"],
        latency_ms=_job_latency_ms(settled),
    )
    if cache_enabled:
        _cache.set_cached(
            agent["agent_id"],
            payload,
            result_payload,
            job["job_id"],
            ttl_hours=cache_ttl_hours,
            version_token=cache_version_token,
        )
        _cache_singleflight_release(singleflight_key or "")
    response_payload = _sync_success_response_payload(
        job_id=job["job_id"],
        output=result_payload,
        latency_ms=_job_latency_ms(settled),
        cached=False,
        pricing_units=_build_pricing_units_block(
            pricing_estimate=pricing_estimate,
            output=result_payload if isinstance(result_payload, dict) else None,
            caller_charge_cents=caller_charge_cents,
            success_distribution=success_distribution,
            platform_fee_pct=platform_fee_pct_at_create,
            fee_bearer_policy=fee_bearer_policy,
        ),
        receipt=_build_inline_receipt(
            job=settled, agent=agent, output_payload=result_payload
        ),
    )
    shaped_output, extra = _shape_sync_output_for_response(
        request,
        job_id=job["job_id"],
        payload=result_payload,
    )
    response_payload["output"] = shaped_output
    response_payload.update(extra)
    response_payload = _decorate_with_rendered_output(
        response_payload, output_format=requested_output_format
    )
    response_payload = _attach_post_call_actions(response_payload, job=settled)
    if idempotency_key:
        _idempotency_store(
            caller_owner_id_early, agent_id, idempotency_key, response_payload
        )
    return JSONResponse(content=response_payload, status_code=200)


# ---------------------------------------------------------------------------
# Jobs routes
# ---------------------------------------------------------------------------


@app.post(
    "/jobs",
    status_code=201,
    response_model=core_models.JobResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 422, 429, 500, 503),
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def jobs_create(
    request: Request,
    body: JobCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "caller")
    parent_job = _resolve_parent_job_for_creation(
        caller,
        body.parent_job_id,
        parent_cascade_policy=body.parent_cascade_policy,
    )
    parent_tree_depth = _to_non_negative_int(
        (parent_job or {}).get("tree_depth"), default=0
    )
    tree_depth = parent_tree_depth + 1 if parent_job is not None else 0
    if tree_depth >= 10:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.ORCHESTRATION_DEPTH_EXCEEDED,
                "Maximum orchestration depth is 10 levels.",
                {"max_depth": 10, "attempted_depth": tree_depth},
            ),
        )
    agent = registry.get_agent(body.agent_id, include_unapproved=True)
    if agent is None or not _caller_can_access_agent(caller, agent):
        raise HTTPException(
            status_code=404, detail=f"Agent '{body.agent_id}' not found."
        )
    _assert_agent_callable(body.agent_id, agent)

    # Validate callback_url at creation time (not just at delivery)
    if body.callback_url:
        try:
            _validate_hook_url(str(body.callback_url))
        except (ValueError, HTTPException) as exc:
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    f"callback_url is invalid: {exc}",
                    {"field": "callback_url"},
                ),
            )

    # Co-pilot mode: validate stop_when bounds + JMESPath complexity before
    # the caller is charged so a malformed predicate never opens an escrow.
    # Storage shape (persisted later as JSON text on jobs.stop_when_json):
    #   {"predicates": [{"label","expr"}, ...]}
    # max_units lives next to predicates for forward-compat but is not in v1
    # request body; the column stays a single JSON envelope so future runtime
    # readers don't have to special-case missing keys.
    validated_stop_when: list[dict] = []
    if body.stop_when is not None:
        from core import copilot_predicates as _copilot_predicates

        raw_predicates = [
            {"label": item.label, "expr": item.expr} for item in body.stop_when
        ]
        try:
            validated_stop_when = _copilot_predicates.validate_stop_when(
                raw_predicates
            )
        except _copilot_predicates.StopWhenInvalid as exc:
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    "stop_when.invalid",
                    str(exc),
                    {"field": "stop_when"},
                ),
            )

    caller_owner_id = _caller_owner_id(request)
    client_id = _request_client_id(request, body.client_id)
    min_caller_trust = _extract_caller_trust_min(agent.get("input_schema"))
    if min_caller_trust is not None and caller["type"] != "master":
        caller_trust = _caller_trust_score(caller_owner_id)
        if caller_trust < min_caller_trust:
            raise HTTPException(
                status_code=403,
                detail=error_codes.make_error(
                    error_codes.UNAUTHORIZED,
                    "Caller trust is below this agent's required minimum.",
                    {
                        "caller_trust": round(caller_trust, 6),
                        "required_min_caller_trust": round(min_caller_trust, 6),
                        "agent_id": agent["agent_id"],
                    },
                ),
            )
    agent_input_schema = agent.get("input_schema")
    if isinstance(agent_input_schema, dict) and agent_input_schema:
        try:
            body.input_payload = _validate_payload_against_schema(
                payload=dict(body.input_payload or {}),
                schema=agent_input_schema,
                allow_string_coercion=_allow_schema_string_coercion(request),
            )
        except Exception as _schema_exc:
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.INPUT_SCHEMA_VIOLATION,
                    f"Input validation failed: {_schema_exc.message if hasattr(_schema_exc, 'message') else str(_schema_exc)}",
                    {
                        "path": list(getattr(_schema_exc, "absolute_path", [])),
                        "agent_id": agent["agent_id"],
                    },
                ),
            )

    effective_budget_cents = body.budget_cents
    if body.max_price_cents is not None:
        effective_budget_cents = (
            body.max_price_cents
            if effective_budget_cents is None
            else min(effective_budget_cents, body.max_price_cents)
        )

    pricing_estimate = _estimate_variable_charge(
        agent=agent,
        payload=body.input_payload,
        budget_cents=effective_budget_cents,
        per_job_cap_cents=_caller_key_per_job_cap(caller),
    )
    if pricing_estimate.get("cap_violated"):
        violation = pricing_estimate["cap_violated"]
        if violation["scope"] == "budget":
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.BUDGET_EXCEEDED,
                    (
                        f"Variable-price estimate "
                        f"({violation['price_cents']}¢) exceeds your budget "
                        f"({violation['limit_cents']}¢)."
                    ),
                    {
                        "price_cents": violation["price_cents"],
                        "budget_cents": violation["limit_cents"],
                        "max_price_cents": body.max_price_cents,
                        "pricing_model": pricing_estimate["pricing_model"],
                        "detail": pricing_estimate.get("detail"),
                        "agent_id": agent["agent_id"],
                    },
                ),
            )
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.SPEND_LIMIT_EXCEEDED,
                "Variable-price estimate exceeds your API key's per-job cap.",
                {
                    "scope": "api_key_per_job",
                    "limit_cents": violation["limit_cents"],
                    "attempted_cents": violation["price_cents"],
                    "pricing_model": pricing_estimate["pricing_model"],
                    "detail": pricing_estimate.get("detail"),
                },
            ),
        )
    price_cents = int(pricing_estimate["price_cents"])
    fee_bearer_policy = payments.normalize_fee_bearer_policy(body.fee_bearer_policy)
    platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
    success_distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )
    caller_charge_cents = int(success_distribution["caller_charge_cents"])
    if caller_charge_cents <= 0 and price_cents > 0:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_CHARGE_AMOUNT,
                "Computed caller charge is non-positive.",
                {
                    "caller_charge_cents": caller_charge_cents,
                    "price_cents": price_cents,
                },
            ),
        )
    if caller_charge_cents > price_cents * 2:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.CHARGE_EXCEEDS_LISTED_PRICE,
                "Caller charge must not exceed twice the listed price.",
                {
                    "caller_charge_cents": caller_charge_cents,
                    "price_cents": price_cents,
                },
            ),
        )
    if effective_budget_cents is not None and price_cents > effective_budget_cents:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.BUDGET_EXCEEDED,
                f"Agent price ({price_cents}¢) exceeds your budget ({effective_budget_cents}¢).",
                {
                    "price_cents": price_cents,
                    "budget_cents": effective_budget_cents,
                    "max_price_cents": body.max_price_cents,
                    "agent_id": agent["agent_id"],
                },
            ),
        )
    if price_cents > 2000 and not _agent_has_verified_contract(agent):
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.VERIFIED_CONTRACT_REQUIRED,
                "Jobs above $20 require a worker with a verified input/output contract.",
                {"agent_id": agent["agent_id"], "price_cents": price_cents},
            ),
        )
    key_per_job_cap_cents = _caller_key_per_job_cap(caller)
    if key_per_job_cap_cents is not None and price_cents > key_per_job_cap_cents:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.SPEND_LIMIT_EXCEEDED,
                "API key per-job cap exceeded.",
                {
                    "scope": "api_key_per_job",
                    "key_id": str(caller.get("key_id") or "").strip() or None,
                    "limit_cents": key_per_job_cap_cents,
                    "attempted_cents": price_cents,
                },
            ),
        )
    output_verification_window_seconds = (
        86400
        if body.output_verification_window_seconds is None
        else body.output_verification_window_seconds
    )
    try:
        input_payload = _merge_protocol_input_envelope(
            body.input_payload,
            input_artifacts=_normalize_protocol_artifact_list(
                body.input_artifacts,
                field_name="input_artifacts",
            ),
            preferred_input_formats=_normalize_format_preferences(
                body.preferred_input_formats,
                field_name="preferred_input_formats",
            ),
            preferred_output_formats=_normalize_format_preferences(
                body.preferred_output_formats,
                field_name="preferred_output_formats",
            ),
            communication_channel=_normalize_protocol_channel(
                body.communication_channel,
                field_name="communication_channel",
            ),
            protocol_metadata=_normalize_protocol_metadata(
                body.protocol_metadata,
                field_name="protocol_metadata",
            ),
            private_task=bool(body.private_task),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    _agent_payout_owner2 = f"agent:{agent['agent_id']}"
    agent_wallet = payments.get_or_create_wallet(_agent_payout_owner2)
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    charge_tx_id = _pre_call_charge_or_402(
        caller=caller,
        caller_wallet_id=caller_wallet["wallet_id"],
        charge_cents=caller_charge_cents,
        agent_id=agent["agent_id"],
    )

    try:
        job = jobs.create_job(
            agent_id=agent["agent_id"],
            caller_owner_id=caller_owner_id,
            caller_wallet_id=caller_wallet["wallet_id"],
            agent_wallet_id=agent_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            price_cents=price_cents,
            caller_charge_cents=caller_charge_cents,
            platform_fee_pct_at_create=platform_fee_pct_at_create,
            fee_bearer_policy=fee_bearer_policy,
            client_id=client_id,
            charge_tx_id=charge_tx_id,
            input_payload=input_payload,
            agent_owner_id=agent.get("owner_id"),
            max_attempts=body.max_attempts,
            parent_job_id=(parent_job or {}).get("job_id"),
            tree_depth=tree_depth,
            parent_cascade_policy=body.parent_cascade_policy,
            clarification_timeout_seconds=body.clarification_timeout_seconds,
            clarification_timeout_policy=body.clarification_timeout_policy,
            dispute_window_hours=body.dispute_window_hours
            or _DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
            judge_agent_id=_extract_judge_agent_id(agent.get("input_schema"))
            or _QUALITY_JUDGE_AGENT_ID,
            callback_url=body.callback_url or None,
            callback_secret=body.callback_secret or None,
            output_verification_window_seconds=output_verification_window_seconds,
        )
    except Exception:
        payments.post_call_refund(
            caller_wallet["wallet_id"],
            charge_tx_id,
            caller_charge_cents,
            agent["agent_id"],
        )
        _LOG.exception("Failed to create job for agent %s.", agent["agent_id"])
        raise HTTPException(status_code=500, detail="Failed to create job.")

    # Co-pilot mode: persist stop_when + billing_unit on the freshly created
    # job row. Done as a separate UPDATE rather than threading new kwargs
    # through jobs.create_job so the surface stays minimal until more callers
    # adopt the feature. Both fields are optional — skip the write entirely
    # when nothing is set.
    if validated_stop_when or body.billing_unit is not None:
        import json as _json

        _stop_when_json = (
            _json.dumps({"predicates": validated_stop_when})
            if validated_stop_when
            else None
        )
        # IMPORTANT: get_db_connection() yields the thread-local connection
        # but does NOT commit on context exit (per core/db.py:368, "Transaction
        # management (commit/rollback) is handled by `with conn:` blocks").
        # Pre-1.6.9 we relied on the bare context exit to commit — on Postgres
        # this UPDATE was rolled back when the connection returned to the pool,
        # so stop_when_json + billing_unit were silently dropped end-to-end
        # (the entire 1.6.0 co-pilot mode was non-functional in prod).
        # Use the connection AS a context manager — that triggers commit on
        # success / rollback on exception, matching the pattern in core/jobs/.
        with get_db_connection() as _conn:
            with _conn:
                _conn.execute(
                    "UPDATE jobs SET stop_when_json = %s, billing_unit = %s "
                    "WHERE job_id = %s",
                    (_stop_when_json, body.billing_unit, job["job_id"]),
                )
        # Re-fetch so the response surfaces the persisted stop_when /
        # billing_unit. Without this the caller saw stop_when_json: null on
        # both the create response and any subsequent GET racing the writer
        # cache, which made the predicate flow look broken end-to-end.
        refreshed = jobs.get_job(job["job_id"])
        if refreshed is not None:
            job = refreshed

    _record_job_event(
        job,
        "job.created",
        actor_owner_id=caller["owner_id"],
        payload={
            "max_attempts": body.max_attempts,
            "parent_job_id": (parent_job or {}).get("job_id"),
            "parent_cascade_policy": body.parent_cascade_policy,
            "tree_depth": tree_depth,
        },
    )
    # Wake the builtin worker so async hires start immediately rather than
    # waiting on the next polling cycle.
    try:
        _wake_builtin_worker()
    except Exception:
        pass
    return JSONResponse(content=_job_response(job, caller), status_code=201)


@app.get(
    "/registry/agents/{agent_id}/global-trust",
    responses=_error_responses(401, 404, 429, 500, 501),
    tags=["Registry"],
    summary="Federated trust score for an agent. Hosted-only.",
)
@limiter.limit("60/minute")
def registry_agent_global_trust(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Return the cross-instance trust score from aztea.ai's federated cache.

    OSS-mode returns 501. The local trust score (from caller_ratings on
    this instance) is always available via the regular agent endpoint.
    """
    _require_any_scope(caller, "caller", "worker")
    agent = registry.get_agent(agent_id)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    from core.hosted_client import get_hosted_client

    client = get_hosted_client()
    if not client.is_enabled():
        raise HTTPException(
            status_code=501,
            detail={
                "error": "registry.global_trust_disabled",
                "message": (
                    "Federated trust scores require hosted aztea.ai. "
                    "Local trust is available on the regular agent endpoint."
                ),
            },
        )
    did = str(agent.get("did") or "").strip()
    if not did:
        raise HTTPException(status_code=404, detail="Agent has no DID.")
    response = client.fetch_trust(did)
    if not response:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "registry.global_trust_fetch_failed",
                "message": "Hosted aztea.ai did not return a trust score.",
            },
        )
    return JSONResponse(content=response)


@app.post(
    "/registry/agents/{agent_id}/publish",
    status_code=200,
    responses=_error_responses(401, 403, 404, 429, 500, 501),
    tags=["Registry"],
    summary="Syndicate an agent to aztea.ai's public registry. Hosted-only.",
)
@limiter.limit("10/minute")
def registry_agent_publish_public(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Push this agent's spec to aztea.ai's public marketplace.

    Returns 501 in OSS-mode (no AZTEA_HOSTED_API_URL configured). When
    hosted-mode is enabled, sends the agent spec to the hosted publish
    endpoint and records the listing on the local agent row. The hosted
    side enforces the listing fee or commission on traffic through the
    public listing.
    """
    _require_scope(caller, "worker")
    if caller["type"] in {"agent_key", "agent_caller"}:
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped keys cannot publish listings.",
        )
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")

    from core.hosted_client import get_hosted_client

    client = get_hosted_client()
    if not client.is_enabled():
        raise HTTPException(
            status_code=501,
            detail={
                "error": "registry.public_publish_disabled",
                "message": (
                    "Public registry syndication requires hosted aztea.ai. "
                    "Set AZTEA_HOSTED_API_URL and AZTEA_HOSTED_API_KEY to opt in."
                ),
                "data": {
                    "hosted_url": "https://aztea.ai",
                    "docs": "https://github.com/aztea-ai/aztea/blob/main/docs/oss-vs-hosted.md",
                },
            },
        )

    # Build a minimal spec — the hosted side validates and stores the rest.
    spec = {
        "agent_id": agent.get("agent_id"),
        "name": agent.get("name"),
        "description": agent.get("description"),
        "category": agent.get("category"),
        "tags": agent.get("tags") or [],
        "price_per_call_usd": agent.get("price_per_call_usd"),
        "input_schema": agent.get("input_schema") or {},
        "output_schema": agent.get("output_schema") or {},
        "endpoint_url": agent.get("endpoint_url"),
        "did": agent.get("did"),
        "signing_public_key": agent.get("signing_public_key"),
        "signing_alg": agent.get("signing_alg"),
    }
    response = client.publish_listing(spec)
    if not response:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "registry.public_publish_failed",
                "message": "Hosted aztea.ai did not accept the listing. Try again shortly.",
            },
        )

    listing_id = str(response.get("listing_id") or "").strip() or None
    published_at = str(response.get("published_at") or "").strip() or _utc_now_iso()
    # Pass the agent row's own owner_id (already loaded) so the data layer
    # can defence-in-depth verify ownership even though the route already
    # called _caller_can_manage_agent above.
    updated = registry.mark_agent_published_public(
        agent_id,
        listing_id,
        published_at,
        owner_id=str(agent.get("owner_id") or ""),
    )
    if not updated:
        # Defensive: should not happen given prior _caller_can_manage_agent
        # check + non-empty agent['owner_id'], but if the row vanished or the
        # owner changed mid-request we surface a 409 rather than silently 200.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "registry.public_publish_lost_ownership",
                "message": "Agent ownership changed during publish. Retry.",
            },
        )

    return JSONResponse(
        content={
            "agent_id": agent_id,
            "listing_id": listing_id,
            "published_at": published_at,
            "public_url": response.get("public_url"),
        }
    )
