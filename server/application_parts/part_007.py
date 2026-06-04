from core import db as _db
# server.application shard 7 — registry routes: register, list, search, get,
# update, delist, MCP manifest + invoke. All auth + SSRF validation lives
# here for agent-facing surfaces.


# 2026-05-19 (B9): agent-registration discoverability stubs. Pre-fix, callers
# trying intuitive paths (POST /agents, /agents/register, /registry/agents/
# register) got bare 405 Method Not Allowed responses with no hint. Now
# each returns a structured 404 that names the canonical path and the CLI
# helper, so the first-integrator experience surfaces the right URL.
def _registration_moved_response() -> JSONResponse:
    # Phase 5 (red-team 2026-05-19): the envelope contract test requires
    # every error body to carry a dot-namespaced ``error`` code. Pre-fix
    # the body used ``"error": "moved"`` which lacked a namespace and
    # failed the contract test.
    return JSONResponse(
        status_code=404,
        content=error_codes.make_error(
            "registry.endpoint_moved",
            (
                "Agent registration moved. POST /registry/register is the "
                "canonical self-serve endpoint."
            ),
            {
                "correct_path": "/registry/register",
                "cli_hint": "aztea publish <path-to-agent.md|*.py>",
                "docs": "/api/docs#/Registry/post__registry_register",
            },
        ),
    )


@app.post("/agents", include_in_schema=False)
@app.post("/agents/register", include_in_schema=False)
@app.post("/registry/agents/register", include_in_schema=False)
def _agent_registration_discoverability(request: Request) -> JSONResponse:
    del request  # accept request only so FastAPI routes consistently
    return _registration_moved_response()


# Price-jump caps (2026-05-22). A probation listing cannot raise price
# more than 2× per PATCH; an approved listing cannot more than 5×. These
# are intentionally generous — legitimate publishers occasionally adjust
# pricing — but cap the 100×-after-graduation scam pattern described in
# tests/security/GAP_REPORT.md D4. Override per-deploy via
# AZTEA_PRICE_JUMP_MAX_RATIO_PROBATION / _APPROVED.
_PROBATION_PRICE_JUMP_MAX_RATIO_DEFAULT = 2.0
_APPROVED_PRICE_JUMP_MAX_RATIO_DEFAULT = 5.0


def _enforce_price_jump_cap(*, existing: dict, new_price: float) -> None:
    """Refuse a PATCH that raises price beyond the per-listing-state cap.

    Pure-effect: raises HTTPException(400) with a structured error or
    returns without raising. The check fires only when the *new* price
    strictly exceeds the cap — lowering the price is always allowed.
    """
    try:
        old_price = float(existing.get("price_per_call_usd") or 0.0)
    except (TypeError, ValueError):
        return
    if new_price <= old_price:
        return
    if old_price <= 0:
        return
    review_status = str(existing.get("review_status") or "").lower()
    if review_status == "probation":
        cap = float(os.environ.get(
            "AZTEA_PRICE_JUMP_MAX_RATIO_PROBATION",
            _PROBATION_PRICE_JUMP_MAX_RATIO_DEFAULT,
        ))
    else:
        cap = float(os.environ.get(
            "AZTEA_PRICE_JUMP_MAX_RATIO_APPROVED",
            _APPROVED_PRICE_JUMP_MAX_RATIO_DEFAULT,
        ))
    ratio = new_price / old_price
    if ratio > cap:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                "listing.price_jump_capped",
                (
                    f"Price increase from ${old_price:.4f} to ${new_price:.4f} "
                    f"exceeds the {cap:.1f}x cap for listings in state "
                    f"'{review_status or 'approved'}'. Raise price in "
                    f"smaller steps."
                ),
                {
                    "old_price": old_price,
                    "new_price": new_price,
                    "max_ratio": cap,
                    "review_status": review_status or "approved",
                },
            ),
        )


# Owner-level reputation gate (2026-05-22). A scammer whose agent is
# sunset or admin-rejected can re-register fresh under a new name because
# probation lives on the agent row. Cap the count of such rows per owner.
_OWNER_REJECTED_AGENT_CAP_DEFAULT = 3


# Name-normalisation entry points (2026-05-22). Strips zero-width and
# bidi control characters that would otherwise let a scammer pin to the
# top of sort-by-name listings, and NFKC-folds so visually-identical
# inputs collapse to one canonical form (so two registrations of the
# same visual name actually trigger the uniqueness constraint).
_NAME_INVISIBLE_RE = re.compile(
    r"[​-‏‪-‮⁠-⁯﻿]"
)


def _normalize_agent_name(name: str) -> str:
    if not name:
        return name
    import unicodedata as _ud  # local — see part_000 line budget note
    n = _ud.normalize("NFKC", name)
    n = _NAME_INVISIBLE_RE.sub("", n)
    return n.strip()


def _refuse_if_owner_has_too_many_rejections(owner_id: str) -> None:
    """Refuse registration when the owner has too many rejected/sunset agents."""
    cap = int(os.environ.get(
        "AZTEA_OWNER_REJECTED_AGENT_CAP",
        _OWNER_REJECTED_AGENT_CAP_DEFAULT,
    ))
    if cap <= 0:
        return
    try:
        # Use registry's connection (which honours the monkeypatched
        # DB_PATH in integration tests) rather than ``_db._conn()`` which
        # falls back to the process-wide default path.
        from core.registry.core_schema import _conn as _registry_conn
        with _registry_conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM agents
                WHERE owner_id = %s
                  AND review_status IN ('rejected', 'sunset')
                """,
                (owner_id,),
            ).fetchone()
    except Exception:  # noqa: BLE001 — owner-history check is best-effort
        return
    rejected = int((row or {}).get("n") or 0)
    if rejected >= cap:
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                "registry.owner_history_capped",
                (
                    f"You have {rejected} previously rejected or sunset listings. "
                    "New registrations are blocked while owner reputation is "
                    "poor. Contact support to appeal."
                ),
                {"rejected_count": rejected, "cap": cap},
            ),
        )


@app.post(
    "/registry/register",
    status_code=201,
    response_model=core_models.RegistryRegisterResponse,
    responses=_error_responses(400, 401, 403, 409, 429, 500),
)
@limiter.limit("20/minute")
def registry_register(
    request: Request,
    background_tasks: BackgroundTasks,
    body: AgentRegisterRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistryRegisterResponse:
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                error_codes.REGISTRY_AGENT_KEY_CANNOT_REGISTER,
                "Agent-scoped keys cannot register new agents.",
            ),
        )
    _MAX_AGENTS_PER_OWNER = 20
    if caller["type"] != "master":
        current_count = registry.count_owner_agents(caller["owner_id"])
        if current_count >= _MAX_AGENTS_PER_OWNER:
            raise HTTPException(
                status_code=403,
                detail=error_codes.make_error(
                    error_codes.REGISTRY_AGENT_LIMIT,
                    f"You've reached the {_MAX_AGENTS_PER_OWNER}-agent limit. "
                    "Delete or archive an existing agent to register a new one.",
                    {"current": current_count, "max": _MAX_AGENTS_PER_OWNER},
                ),
            )
        # Owner-level reputation gate: too many prior rejections → refuse.
        _refuse_if_owner_has_too_many_rejections(caller["owner_id"])
    # Normalise the agent name BEFORE the safety scan so leading zero-
    # width characters and homoglyphs cannot pin the row to the top of
    # sort-by-name listings (tests/security/GAP_REPORT.md H5).
    body.name = _normalize_agent_name(body.name)
    try:
        safe_endpoint_url = _validate_agent_endpoint_url(request, body.endpoint_url)
        # Polling/async workers (SDK AgentServer) have no inbound endpoint —
        # they pull jobs from the async /jobs queue. The endpoint scan, liveness
        # probe, and adversarial listing-safety probe all assume a dialable URL,
        # so skip them; there is nothing to reach. The async path still gates
        # behaviour at call time (claim/heartbeat/complete + settlement).
        if not _url_security.is_polling_worker_endpoint(safe_endpoint_url):
            # Defence-in-depth: refuse anyone trying to register against an
            # Aztea-owned host as their endpoint. SSRF check above already blocks
            # private IPs; this catches the "list a clone of a built-in" footgun.
            endpoint_findings = _listing_safety.scan_agent_md_endpoint(safe_endpoint_url)
            if _listing_safety.has_block(endpoint_findings):
                block = next(
                    f for f in endpoint_findings
                    if f.level == _listing_safety.LEVEL_BLOCK
                )
                raise HTTPException(
                    status_code=400,
                    detail=error_codes.make_error(
                        "listing.safety_block", block.message,
                        {"code": block.code, "detail": block.detail},
                    ),
                )
            if not os.environ.get("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE"):
                _probe_register_endpoint_or_400(safe_endpoint_url)
            _run_listing_safety_probe(
                safe_endpoint_url,
                input_schema=body.input_schema,
                output_schema=body.output_schema,
                output_examples=body.output_examples,
            )
        safe_healthcheck_url = None
        if body.healthcheck_url:
            safe_healthcheck_url = _validate_outbound_url(
                body.healthcheck_url, "healthcheck_url"
            )
        safe_verifier_url = None
        if body.output_verifier_url:
            safe_verifier_url = _validate_outbound_url(
                body.output_verifier_url, "output_verifier_url"
            )
            # 2026-05-22: also run the aztea-suffix / homoglyph check the
            # endpoint URL is subject to. Without this the verifier slot
            # becomes a back door — an attacker can register a malicious
            # listing whose verifier URL points at aztea.ai (causing the
            # verifier call to 405 but the listing to still land on
            # probation marked unverified). See tests/security/GAP_REPORT
            # I2.
            verifier_findings = _listing_safety.scan_agent_md_endpoint(safe_verifier_url)
            if _listing_safety.has_block(verifier_findings):
                block = next(
                    f for f in verifier_findings
                    if f.level == _listing_safety.LEVEL_BLOCK
                )
                raise HTTPException(
                    status_code=400,
                    detail=error_codes.make_error(
                        "listing.safety_block",
                        f"output_verifier_url: {block.message}",
                        {"code": block.code, "detail": block.detail},
                    ),
                )
        registration_payload = {
            "name": body.name,
            "description": body.description,
            "endpoint_url": safe_endpoint_url,
            "healthcheck_url": safe_healthcheck_url,
            "price_per_call_usd": body.price_per_call_usd,
            "tags": body.tags,
            "input_schema": body.input_schema,
            "output_schema": body.output_schema,
        }
        verified = False
        verifier_reason = "no verifier configured"
        if safe_verifier_url:
            verified, verifier_reason = _run_registration_verifier(
                safe_verifier_url,
                registration_payload=registration_payload,
            )
        # Non-master registrations enter probation: live + callable, but
        # ranked last in discovery and rate/price-capped until the listing
        # accumulates a track record. See core/registry/auto_hire for the
        # gating logic; the column itself is plain TEXT.
        initial_review_status = (
            "probation" if caller["type"] != "master" else None
        )
        agent_id = registry.register_agent(
            name=body.name,
            description=body.description,
            endpoint_url=safe_endpoint_url,
            healthcheck_url=safe_healthcheck_url,
            price_per_call_usd=body.price_per_call_usd,
            tags=body.tags,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            output_verifier_url=safe_verifier_url,
            output_examples=body.output_examples or None,
            verified=verified,
            owner_id=caller["owner_id"],
            review_status=initial_review_status,
            model_provider=body.model_provider,
            model_id=body.model_id,
            pricing_model=body.pricing_model,
            pricing_config=body.pricing_config,
            kind="self_hosted",
            pii_safe=body.pii_safe,
            outputs_not_stored=body.outputs_not_stored,
            audit_logged=body.audit_logged,
            region_locked=body.region_locked,
            payout_curve=body.payout_curve,
            cacheable=body.cacheable,
        )
        agent = registry.get_agent_with_reputation(
            agent_id, include_unapproved=True
        ) or registry.get_agent(
            agent_id,
            include_unapproved=True,
        )
        # Advisory verification (cosine near-dup + council) runs after the
        # response. The repeat-probe only runs when the endpoint was actually
        # reachable this request — otherwise we'd POST to an unprobed/polling URL.
        if _feature_flags.listing_verify_async_enabled():
            _endpoint_probed = _endpoint_is_probeable(safe_endpoint_url)
            background_tasks.add_task(
                _listing_verification.run_and_annotate,
                agent_id,
                _listing_verification.KIND_EXTERNAL,
                name=body.name,
                description=body.description,
                tags=list(body.tags or []),
                input_schema=body.input_schema,
                output_schema=body.output_schema,
                endpoint_url=safe_endpoint_url,
                http_post=http.post if _endpoint_probed else None,
            )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.VALIDATION_ERROR,
                str(e) or "Invalid registry payload.",
            ),
        )
    except _db.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.REGISTRY_AGENT_DUPLICATE,
                "Agent ID or name already exists.",
            ),
        )
    message = "Agent registered successfully."
    if safe_verifier_url:
        if agent and agent.get("verified"):
            message = "Agent registered and verifier approved."
        else:
            message = f"Agent registered; verifier did not approve ({verifier_reason})."
    if (agent or {}).get("review_status") == "pending_review":
        message = "Your agent listing is pending review. You will be notified when it goes live."
    # Plan B Phase 1 (2026-05-27): surface the HMAC signing secret EXACTLY ONCE
    # at registration. _agent_response scrubs it from every other read, so this
    # is the only opportunity for the owner to copy it. Mirrors Stripe's API
    # key handling.
    response_body: dict[str, Any] = {
        "agent_id": agent_id,
        "message": message,
        "review_status": (agent or {}).get("review_status"),
        "agent": _agent_response(agent, caller) if agent else None,
    }
    endpoint_signing_secret = (agent or {}).get("endpoint_signing_secret")
    if endpoint_signing_secret:
        response_body["endpoint_signing_secret"] = endpoint_signing_secret
        response_body["endpoint_signing_secret_note"] = (
            "Save this secret now — it's shown only once. Your endpoint MUST "
            "verify the X-Aztea-Signature header on every inbound call so a "
            "leaked URL can't be called without paying. Rotate via "
            "POST /registry/agents/{agent_id}/rotate-secret."
        )
    return JSONResponse(content=response_body, status_code=201)


# Cached snapshot of the active-agent list. Keyed by the catalog_broadcast
# version: any mutation in core/registry/agents_ops.py calls
# catalog_broadcast.bump() which increments the version, the next read here
# sees the mismatch and rebuilds. TTL is a safety net for the case where
# bump() didn't reach this worker (network blip on the LISTEN connection).
# See /autoplan 2026-05-28 Eng F1/F2 + D5 user resolution.
_MCP_ACTIVE_AGENTS_CACHE_TTL_S = 30.0
_mcp_active_agents_cache: tuple[int, float, list[dict[str, Any]]] | None = None
_mcp_active_agents_cache_lock = threading.Lock()


def _build_mcp_active_agents() -> list[dict[str, Any]]:
    """Compute the active-agent list from the DB. No caching here."""
    agents = [
        reputation.enrich_agent_record(agent)
        for agent in registry.get_agents(include_internal=True, include_banned=True)
    ]
    sunset_ids = set(_builtin_constants.SUNSET_DEPRECATED_AGENT_IDS)
    return [
        agent
        for agent in agents
        if (
            str(agent.get("agent_id") or "") not in sunset_ids
            and str(agent.get("review_status") or "").strip().lower() != "sunset"
            and (
                str(agent.get("status") or "").strip().lower() == "active"
                or str(agent.get("agent_id") or "")
                in _builtin_constants.CURATED_PUBLIC_BUILTIN_AGENT_IDS
            )
        )
        and not bool(agent.get("internal_only"))
    ]


def _mcp_active_agents() -> list[dict[str, Any]]:
    """Cached active-agent list with broadcast + TTL invalidation."""
    global _mcp_active_agents_cache
    now = time.monotonic()
    version = catalog_broadcast.current_version()
    cached = _mcp_active_agents_cache
    if cached is not None:
        cached_version, cached_at, cached_list = cached
        if cached_version == version and (now - cached_at) < _MCP_ACTIVE_AGENTS_CACHE_TTL_S:
            return cached_list
    with _mcp_active_agents_cache_lock:
        # Re-check under the lock so concurrent readers share one rebuild.
        cached = _mcp_active_agents_cache
        now = time.monotonic()
        version = catalog_broadcast.current_version()
        if cached is not None:
            cached_version, cached_at, cached_list = cached
            if cached_version == version and (now - cached_at) < _MCP_ACTIVE_AGENTS_CACHE_TTL_S:
                return cached_list
        fresh = _build_mcp_active_agents()
        _mcp_active_agents_cache = (version, time.monotonic(), fresh)
        return fresh


def _invalidate_mcp_active_agents_cache(_new_version: int | None = None) -> None:
    """Drop the cache. Called by catalog_broadcast on incoming NOTIFY."""
    global _mcp_active_agents_cache
    _mcp_active_agents_cache = None


catalog_broadcast.register_invalidate(_invalidate_mcp_active_agents_cache)


def _mcp_tools_and_lookup() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    agents = _mcp_active_agents()
    entries = mcp_manifest.build_mcp_tool_entries(agents)
    return [entry["tool"] for entry in entries], {
        str(entry["tool_name"]): agent for entry, agent in zip(entries, agents)
    }


def _merge_workspace_context_into_payload(
    payload: dict[str, Any],
    body: MCPInvokeRequest,
) -> bool:
    """Merge MCP-attached workspace context into the agent payload.

    Two ingest modes: (a) full bundle dict on `body.workspace_context` — cache
    it under its fingerprint and merge into payload; (b) fingerprint-only on
    `body.workspace_context_fingerprint` — look up cached bundle, merge if
    found, silently skip otherwise. Returns True when context was attached.

    Privacy: the bundle content lives only in the in-memory cache; it is never
    persisted to the database, never written to the audit log, and never
    forwarded to `_record_public_work_example` (see registry_call's strip).
    """
    from core import workspace_bundle_cache as _wb_cache

    bundle = body.workspace_context
    if isinstance(bundle, dict) and bundle:
        fingerprint = str(bundle.get("fingerprint") or "").strip()
        if fingerprint:
            _wb_cache.cache_workspace_bundle(fingerprint, bundle)
        payload["workspace_context"] = bundle
        _LOG.info(
            "mcp.workspace_context attached tool=%s mode=full fingerprint=%s",
            body.tool_name,
            fingerprint or "-",
        )
        return True
    fingerprint = str(body.workspace_context_fingerprint or "").strip()
    if not fingerprint:
        return False
    cached = _wb_cache.get_workspace_bundle(fingerprint)
    if cached is None:
        _LOG.info(
            "mcp.workspace_context fingerprint miss tool=%s fingerprint=%s",
            body.tool_name,
            fingerprint,
        )
        return False
    payload["workspace_context"] = cached
    _LOG.info(
        "mcp.workspace_context attached tool=%s mode=fingerprint hit=true",
        body.tool_name,
    )
    return True


def _mcp_invoke_lookup() -> dict[str, dict[str, Any]]:
    """Tool-name → agent map for /mcp/invoke. Includes sunset agents so existing
    integrations that call them by exact slug keep working, even though the
    manifest endpoints (/mcp/tools, /openai_*, /gemini_*) hide them."""
    agents = [
        reputation.enrich_agent_record(agent)
        # include_sunset=True because the dispatch table intentionally
        # accepts owner-retracted slugs so live integrations get an HTTP
        # 410 (sunset) response from the call site rather than 404 here.
        for agent in registry.get_agents(
            include_internal=True, include_banned=True, include_sunset=True,
        )
        if (
            str(agent.get("status") or "").strip().lower() == "active"
            or str(agent.get("agent_id") or "")
            in _builtin_constants.CURATED_BUILTIN_AGENT_IDS
        )
        and not bool(agent.get("internal_only"))
    ]
    entries = mcp_manifest.build_mcp_tool_entries(agents)
    return {str(entry["tool_name"]): agent for entry, agent in zip(entries, agents)}


def _caller_from_raw_api_key(raw_api_key: str) -> core_models.CallerContext | None:
    raw = str(raw_api_key or "").strip()
    if not raw:
        return None
    if hmac.compare_digest(raw, _MASTER_KEY):
        return {
            "type": "master",
            "owner_id": "master",
            "scopes": ["caller", "worker", "admin"],
        }
    user = _auth.verify_api_key(raw)
    if user:
        return {
            "type": "user",
            "owner_id": f"user:{user['user_id']}",
            "user": user,
            "scopes": list(user.get("scopes") or []),
        }
    agent_key = _auth.verify_agent_api_key(raw)
    if agent_key:
        return {
            "type": "agent_key",
            "owner_id": str(agent_key["owner_id"]),
            "agent_id": str(agent_key["agent_id"]),
            "key_id": str(agent_key["key_id"]),
            "scopes": ["worker"],
        }
    return None


def _mcp_text_from_response(response: Response) -> str:
    body_bytes = bytes(getattr(response, "body", b"") or b"")
    if not body_bytes:
        return "null"
    body_text = body_bytes.decode("utf-8", errors="replace")
    try:
        return json.dumps(json.loads(body_text), ensure_ascii=False)
    except json.JSONDecodeError:
        return json.dumps(body_text, ensure_ascii=False)


def _mcp_payload_from_response(response: Response) -> Any:
    body_bytes = bytes(getattr(response, "body", b"") or b"")
    if not body_bytes:
        return None
    body_text = body_bytes.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body_text)
    except json.JSONDecodeError:
        return body_text
    # Unwrap the standard sync call envelope so MCP structuredContent
    # contains the agent's output dict, not the envelope itself.
    if (
        isinstance(parsed, dict)
        and "output" in parsed
        and parsed.get("status") == "complete"
    ):
        return parsed["output"]
    return parsed


def _parse_data_uri(value: str) -> tuple[str | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    match = re.match(r"^data:([^;,]+);base64,([A-Za-z0-9+/=]+)$", text, re.IGNORECASE)
    if not match:
        return None, None
    return match.group(1).strip().lower(), match.group(2).strip()


def _mcp_text_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in (
            "summary",
            "message",
            "answer",
            "title",
            "one_line_summary",
            "signal_reasoning",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _mcp_media_content_from_artifacts(
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for artifact in artifacts[:6]:
        mime = str(artifact.get("mime") or "").strip().lower()
        source = str(artifact.get("url_or_base64") or "").strip()
        if not mime or not source:
            continue
        parsed_mime, base64_payload = _parse_data_uri(source)
        effective_mime = parsed_mime or mime
        if effective_mime.startswith("image/") and base64_payload:
            rendered.append(
                {"type": "image", "mimeType": effective_mime, "data": base64_payload}
            )
            continue
        if source.startswith("http://") or source.startswith("https://"):
            rendered.append(
                {
                    "type": "resource",
                    "resource": {"uri": source, "mimeType": effective_mime},
                }
            )
            continue
        if base64_payload:
            rendered.append(
                {
                    "type": "resource",
                    "resource": {
                        "uri": f"data:{effective_mime};base64,{base64_payload}",
                        "mimeType": effective_mime,
                    },
                }
            )
            continue
    return rendered


def _mcp_content_from_payload(payload: Any) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": _mcp_text_from_payload(payload)}
    ]
    if isinstance(payload, dict):
        raw_artifacts = payload.get("artifacts")
        if isinstance(raw_artifacts, list):
            artifact_rows = [item for item in raw_artifacts if isinstance(item, dict)]
            content.extend(_mcp_media_content_from_artifacts(artifact_rows))
    return content


def _a2a_agent_card(agent: dict) -> dict:
    """Build a Google A2A Agent Card for a single registered agent."""
    price_usd = float(agent.get("price_per_call_usd") or 0.0)
    return {
        "name": str(agent.get("name") or ""),
        "description": str(agent.get("description") or ""),
        "url": f"{_SERVER_BASE_URL}/registry/agents/{agent['agent_id']}/call",
        "version": "1.0.0",
        "provider": {"organization": "Aztea", "url": _SERVER_BASE_URL},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": True,
        },
        "skills": [
            {
                "id": agent["agent_id"],
                "name": str(agent.get("name") or ""),
                "description": str(agent.get("description") or ""),
                "tags": list(agent.get("tags") or []),
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
                "inputSchema": agent.get("input_schema") or {},
                "outputSchema": agent.get("output_schema") or {},
            }
        ],
        "authentication": {"schemes": ["ApiKey"]},
        "aztea": {
            "agent_id": agent["agent_id"],
            "price_per_call_usd": price_usd,
            "trust_score": agent.get("trust_score"),
            "total_calls": agent.get("total_calls"),
            "avg_latency_ms": agent.get("avg_latency_ms"),
            "success_rate": agent.get("success_rate"),
            "hire_endpoint": f"{_SERVER_BASE_URL}/jobs",
            "status_endpoint": f"{_SERVER_BASE_URL}/jobs/{{job_id}}",
        },
    }


@app.get(
    "/.well-known/agent.json",
    include_in_schema=True,
    tags=["A2A"],
    summary="Google A2A: platform-level agent card listing all registered agents as skills.",
)
def a2a_platform_agent_card(request: Request) -> JSONResponse:
    agents = registry.get_agents_with_reputation()
    visible = [a for a in agents if not a.get("internal_only")]
    skills = []
    for agent in visible:
        skills.append(
            {
                "id": agent["agent_id"],
                "name": str(agent.get("name") or ""),
                "description": str(agent.get("description") or ""),
                "tags": list(agent.get("tags") or []),
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
                "aztea": {
                    "agent_id": agent["agent_id"],
                    "price_per_call_usd": float(agent.get("price_per_call_usd") or 0.0),
                    "trust_score": agent.get("trust_score"),
                    "total_calls": agent.get("total_calls"),
                    "success_rate": agent.get("success_rate"),
                    "avg_latency_ms": agent.get("avg_latency_ms"),
                },
            }
        )
    card = {
        "name": "Aztea",
        "description": "AI agent labor marketplace. Discover, hire, and orchestrate specialist agents. Pay per invocation.",
        "url": _SERVER_BASE_URL,
        "version": "1.0.0",
        "provider": {"organization": "Aztea", "url": _SERVER_BASE_URL},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": True,
        },
        "skills": skills,
        "authentication": {"schemes": ["ApiKey"]},
        "aztea": {
            "hire_endpoint": f"{_SERVER_BASE_URL}/jobs",
            "search_endpoint": f"{_SERVER_BASE_URL}/registry/search",
            "list_endpoint": f"{_SERVER_BASE_URL}/registry/agents",
            "mcp_tools_endpoint": f"{_SERVER_BASE_URL}/mcp/tools",
        },
    }
    return JSONResponse(content=card, headers={"Content-Type": "application/json"})


@app.get(
    "/registry/agents/{agent_id}/agent.json",
    include_in_schema=True,
    tags=["A2A"],
    summary="Google A2A: per-agent card. Also served at /agents/{agent_id}/agent.json and /.well-known/agent.json?agent_id=...",
    responses=_error_responses(404),
)
def a2a_agent_card(agent_id: str, request: Request) -> JSONResponse:
    agent = registry.get_agent_with_reputation(agent_id)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Agent not found.",
                details={"agent_id": agent_id},
            ),
        )
    if agent.get("internal_only"):
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Agent not found.",
                details={"agent_id": agent_id},
            ),
        )
    return JSONResponse(
        content=_a2a_agent_card(agent),
        headers={"Content-Type": "application/json"},
    )


# Standard A2A discovery placement (sibling of /.well-known/agent.json).
# External tools looking up agent.json at the canonical /agents/{id}/agent.json
# path used to 404; this alias keeps the existing /registry/... route intact
# for back-compat while answering the standard location.
@app.get(
    "/agents/{agent_id}/agent.json",
    include_in_schema=False,
    tags=["A2A"],
    responses=_error_responses(404),
)
def a2a_agent_card_alias(agent_id: str, request: Request) -> JSONResponse:
    return a2a_agent_card(agent_id, request)


# Polite 410 Gone for the well-known slugs of agents that used to be public
# but have since been removed from the catalog. The CLI/SDK now flags these
# at hire-time (see SUNSET_AGENT_SLUGS in sdks/python-sdk/aztea/cli/common.py),
# but raw curl callers hitting POST /agents/<slug>/call previously got a bare
# 405 from FastAPI. Emit the structured `agent.sunset` envelope instead so
# downstream agent-orchestration tools can branch correctly.
_SUNSET_PUBLIC_SLUGS: frozenset[str] = frozenset({
    "arxiv-research-agent",
    "multi-file-executor",
    "linter",
    "shell-executor",
    "type-checker",
    "semantic-codebase-search",
    "image-generator",
    "financial-agent",
    "live-endpoint-tester",
    "sql-explainer",
    "web-researcher",
    "ai-red-teamer",
    "codereview",
    "code-review",
    "json-schema-validator",
    "git-diff-analyzer",
    "wikipedia-research-agent",
})


def _sunset_410_response(slug: str) -> HTTPException:
    return HTTPException(
        status_code=410,
        detail=error_codes.make_error(
            error_codes.AGENT_SUNSET,
            f"Agent '{slug}' was removed from the public catalog and is no longer callable.",
            {"slug": slug, "deprecated": True},
        ),
    )


@app.post(
    "/agents/{slug}/call",
    include_in_schema=False,  # internal courtesy route — the canonical hire path is /jobs
    responses={410: {"description": "Agent sunset"}, 404: {"description": "Unknown slug"}},
)
def agent_call_by_slug_sunset(slug: str) -> JSONResponse:
    """Catch sunset slugs hit via POST /agents/<slug>/call and return 410.

    Why: pre-existing routing has /jobs and /registry/agents/{agent_id}/call but
    no slug-keyed call route, so old README examples (`curl POST /agents/web-researcher/call`)
    used to fall through to a bare HTTP 405. This handler gives those callers
    the structured `agent.sunset` envelope they can match on.
    """
    if slug.lower() in _SUNSET_PUBLIC_SLUGS:
        raise _sunset_410_response(slug)
    raise HTTPException(
        status_code=404,
        detail=error_codes.make_error(
            "agent.unknown_slug",
            f"No agent with slug '{slug}'. Use POST /jobs (with agent_id) to hire.",
            {"slug": slug},
        ),
    )


@app.get(
    "/agents/{agent_id}/did.json",
    include_in_schema=True,
    tags=["Identity"],
    summary="W3C did:web — DID document for the agent's cryptographic identity.",
    responses=_error_responses(404),
)
def agent_did_document(agent_id: str, request: Request) -> JSONResponse:
    """Return a W3C-compliant DID document so external parties can resolve
    the agent's public key and verify signed outputs."""
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Agent not found.",
                details={"agent_id": agent_id},
            ),
        )
    public_pem = agent.get("signing_public_key")
    did = agent.get("did")
    if not public_pem or not did:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_IDENTITY_NOT_PUBLISHED,
                "Agent has no published cryptographic identity yet.",
                details={"agent_id": agent_id},
            ),
        )
    # Probation agents (vibe-generated, community-published) only publish
    # their DID document once they have a successful call on record. This
    # avoids polluting the DID namespace with spam-generated identities.
    # successful_calls_count comes from the agents row; treat NULL/missing
    # as zero so brand-new probation agents are deferred. Approved agents
    # always publish.
    if str(agent.get("review_status") or "").strip().lower() == "probation":
        successful = int(agent.get("successful_calls") or 0)
        if successful <= 0:
            raise HTTPException(
                status_code=404,
                detail=error_codes.make_error(
                    error_codes.AGENT_IDENTITY_NOT_PUBLISHED,
                    (
                        "Agent is on probation; its DID document is published "
                        "after the first successful paid call."
                    ),
                    details={"agent_id": agent_id, "review_status": "probation"},
                ),
            )
    try:
        from core import crypto as _crypto

        jwk = _crypto.public_key_to_jwk(public_pem)
    except Exception:
        raise HTTPException(
            status_code=500,
            detail=error_codes.make_error(
                error_codes.AGENT_IDENTITY_RENDER_FAILED,
                "Failed to render the agent's public key.",
                details={"agent_id": agent_id},
            ),
        )
    key_id = f"{did}#key-1"
    # `service[]` carries an Aztea-specific pointer to the human-readable
    # signature-scheme documentation so an offline verifier can resolve
    # the difference between `ed25519` (v1) and `Ed25519+aztea-output-sig/2`
    # (v2) without having to read the source. The W3C DID spec explicitly
    # allows arbitrary service endpoints — this is the standard way to
    # publish protocol-specific metadata alongside the verification keys.
    server_base = str(request.base_url).rstrip("/")
    document = {
        "@context": [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/security/suites/jws-2020/v1",
        ],
        "id": did,
        "verificationMethod": [
            {
                "id": key_id,
                "type": "JsonWebKey2020",
                "controller": did,
                "publicKeyJwk": jwk,
            }
        ],
        "authentication": [key_id],
        "assertionMethod": [key_id],
        "service": [
            {
                "id": f"{did}#output-signature-scheme",
                "type": "AzteaOutputSignatureScheme",
                "serviceEndpoint": f"{server_base}/docs/api-reference#output-signature-schemes",
                "supportedSchemes": [
                    "ed25519",
                    "Ed25519+aztea-output-sig/2",
                ],
            }
        ],
    }
    return JSONResponse(
        content=document,
        headers={"Content-Type": "application/did+json"},
    )


@app.get(
    "/.well-known/did.json",
    include_in_schema=False,
    tags=["Identity"],
    responses=_error_responses(400, 404),
)
def well_known_did_document(
    request: Request, agent_id: str | None = None,
) -> JSONResponse:
    """Well-known alias for ``/agents/{agent_id}/did.json``.

    Why: spec consumers (and our own docs) sometimes look for the W3C
    well-known path. Without this alias they 404 even though the canonical
    DID document is reachable elsewhere. Honouring the well-known path
    matches the agent.json pattern above.
    """
    if not agent_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "agent_id query parameter is required. Use "
                "/agents/{id}/did.json for the canonical path."
            ),
        )
    return agent_did_document(agent_id, request)


@app.post(
    "/a2a/tasks/send",
    status_code=201,
    tags=["A2A"],
    summary="Google A2A: submit a task to an Aztea skill (agent). Returns a task/job object.",
    responses=_error_responses(400, 401, 402, 403, 404, 429, 500),
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def a2a_tasks_send(
    request: Request,
    body: core_models.A2ATaskSendRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    agent = registry.get_agent(body.skill_id, include_unapproved=True)
    if (
        agent is None
        or not _caller_can_access_agent(caller, agent)
        or agent.get("status") in {"banned"}
    ):
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Skill (agent) not found.",
                details={"skill_id": body.skill_id},
            ),
        )
    if agent.get("status") == "suspended":
        raise HTTPException(
            status_code=503,
            detail=error_codes.make_error(
                error_codes.AGENT_SUSPENDED,
                "Skill (agent) is suspended.",
                details={"skill_id": body.skill_id},
            ),
        )
    if agent.get("internal_only"):
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Skill (agent) not found.",
                details={"skill_id": body.skill_id},
            ),
        )

    price_cents = _usd_to_cents(agent["price_per_call_usd"])
    fee_bearer_policy = "caller"
    platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
    success_distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )
    caller_charge_cents = int(success_distribution["caller_charge_cents"])
    caller_owner_id = _caller_owner_id(request)
    client_id = _request_client_id(request, body.client_id)
    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
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
            input_payload=body.input or {},
            agent_owner_id=agent.get("owner_id"),
            max_attempts=3,
            dispute_window_hours=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
            judge_agent_id=_QUALITY_JUDGE_AGENT_ID,
            callback_url=body.callback_url or None,
            origin="direct",
        )
    except Exception as exc:
        payments.post_call_refund(
            caller_wallet["wallet_id"],
            charge_tx_id,
            caller_charge_cents,
            agent["agent_id"],
        )
        _LOG.exception(
            "A2A task creation failed for agent %s (caller=%s); charge refunded.",
            agent["agent_id"], caller["owner_id"],
        )
        # 2026-05-18 (E15): the refund path previously emitted a bare-string
        # 500 detail, which downstream serialisers dropped into `error_code:
        # null` envelopes. Use the same JOB_CREATE_FAILED code that the
        # sync /registry/agents/{id}/call refund path uses so clients can
        # branch consistently on a refunded-create failure.
        raise HTTPException(
            status_code=500,
            detail=error_codes.make_error(
                error_codes.JOB_CREATE_FAILED,
                "Task could not be created. Your charge was refunded. Retry shortly.",
                {
                    "agent_id": agent["agent_id"],
                    "refunded_cents": int(caller_charge_cents),
                    "source": "a2a_tasks_create",
                    "underlying": type(exc).__name__,
                },
            ),
        )

    _record_job_event(job, "job.created", actor_owner_id=caller["owner_id"])
    return JSONResponse(
        content={
            "id": job["job_id"],
            "skill_id": agent["agent_id"],
            "status": "submitted",
            "job_id": job["job_id"],
            "price_cents": price_cents,
            "caller_charge_cents": caller_charge_cents,
            "created_at": job["created_at"],
            "aztea_job": _job_response(job, caller),
        },
        status_code=201,
    )


@app.get(
    "/a2a/tasks/{task_id}",
    tags=["A2A"],
    summary="Google A2A: get task status by task/job ID.",
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("120/minute")
def a2a_tasks_get(
    request: Request,
    task_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    job = jobs.get_job(task_id)
    # Return 403 in both "not found" and "not authorized" cases to prevent job-ID enumeration.
    if job is None or not _caller_can_view_job(caller, job):
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                error_codes.JOB_FORBIDDEN,
                "Task not found or not authorized.",
                details={"task_id": task_id},
            ),
        )
    a2a_status_map = {
        "pending": "submitted",
        "claimed": "working",
        "complete": "completed",
        "failed": "failed",
        "awaiting_clarification": "input-required",
    }
    return JSONResponse(
        content={
            "id": task_id,
            "skill_id": job["agent_id"],
            "status": a2a_status_map.get(job.get("status", ""), job.get("status", "")),
            "output": job.get("output_payload"),
            "error": job.get("error_message"),
            "created_at": job.get("created_at"),
            "completed_at": job.get("completed_at"),
            "aztea_job": _job_response(job, caller),
        }
    )


@app.post(
    "/a2a/tasks/{task_id}/cancel",
    tags=["A2A"],
    summary="Google A2A: cancel a pending task.",
    responses=_error_responses(401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def a2a_tasks_cancel(
    request: Request,
    task_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    job = jobs.get_job(task_id)
    # Return 403 in both "not found" and "not authorized" cases to prevent job-ID enumeration.
    if job is None or not _caller_can_view_job(caller, job):
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                error_codes.JOB_FORBIDDEN,
                "Task not found or not authorized.",
                details={"task_id": task_id},
            ),
        )
    if job.get("status") not in {"pending"}:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.JOB_CANCEL_INVALID_STATE,
                "Cannot cancel task in this status.",
                details={"task_id": task_id, "status": job.get("status")},
            ),
        )
    cancelled = jobs.update_job_status(
        task_id, "failed", error_message="Cancelled by caller.", completed=True
    )
    if cancelled:
        _settle_failed_job(cancelled, actor_owner_id=caller["owner_id"])
    return JSONResponse(content={"id": task_id, "status": "cancelled"})


# Buyer-facing cancel — broader status acceptance than the A2A surface so
# Claude Code / SDK callers can abort a long-running compare or arxiv research
# session. Refunds the pre-call charge via _settle_failed_job. See the
# 2026-05-01 production-eval audit for context (no async cancel was the #3 P0).
_CANCELLABLE_JOB_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "claimed",
        "running",
        "awaiting_clarification",
    }
)


@app.post(
    "/jobs/{job_id}/cancel",
    response_model=core_models.JobResponse,
    tags=["Jobs"],
    summary="Cancel an in-flight async job and refund any unsettled charge.",
    responses=_error_responses(401, 403, 404, 409, 422, 429, 500),
)
@limiter.limit("30/minute")
def jobs_cancel(
    request: Request,
    job_id: str,
    body: JobCancelRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    job = jobs.get_job(job_id)
    # 1.7.3 — collapse "not found" and "not yours" into a single 403 so
    # this route matches GET /jobs/{job_id} and the eval's noted
    # inconsistency disappears. Probing for valid UUIDs by status code
    # is harder when both branches return identical responses.
    if job is None or not _caller_can_view_job(caller, job):
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                error_codes.JOB_FORBIDDEN,
                "Job not found or not authorized.",
                details={"job_id": job_id},
            ),
        )
    current_status = str(job.get("status") or "").strip().lower()
    if current_status not in _CANCELLABLE_JOB_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.JOB_INVALID_STATE,
                (
                    f"Cannot cancel a job in status '{current_status}'. "
                    "Already-complete jobs return their original output; "
                    "already-failed jobs were refunded automatically."
                ),
            ),
        )
    # L-6 (audit 2026-05-19): without prefix-stripping, callers who passed
    # the default reason ("Cancelled by caller.") landed on
    # "Cancelled by caller: Cancelled by caller." in the error_message
    # field. Strip a leading prefix if present so the field reads cleanly
    # whether the caller supplied a reason, the default phrase, or nothing.
    _prefix = "Cancelled by caller"
    _cleaned_reason = (body.reason or "").strip()
    if _cleaned_reason.lower().startswith(_prefix.lower()):
        _cleaned_reason = _cleaned_reason[len(_prefix):].lstrip(":. ").strip()
    if _cleaned_reason:
        error_message = f"{_prefix}: {_cleaned_reason[:160]}"
    else:
        error_message = f"{_prefix}."
    # 1.7.3 — status="cancelled" (was "failed") so callers can distinguish
    # caller-initiated cancellation from agent-side failure.
    # 1.7.8 — detect the race where the worker completes the job between
    # our get_job() above and the UPDATE below. update_job_status's WHERE
    # clause has `(%s = 0 OR completed_at IS NULL)` and we pass completed=1,
    # so if completed_at was just set by the worker the UPDATE does
    # NOTHING but the function still returns get_job() = the completed
    # row. Pre-1.7.8 the cancel route trusted that return and ran
    # _settle_failed_job (refunding a completed job), then later the
    # caller would dispute that "cancelled" job and the platform would
    # see status=complete and accept the dispute — silently taking the
    # 5¢ deposit. Compare the returned row's status against "cancelled"
    # and 409 if the UPDATE didn't actually apply.
    cancelled = jobs.update_job_status(
        job_id,
        "cancelled",
        error_message=error_message,
        completed=True,
    )
    actual_status = (
        str((cancelled or {}).get("status") or "").strip().lower()
    )
    if cancelled is None or actual_status != "cancelled":
        # Race: worker completed (or failed) the job between get_job
        # above and the UPDATE. UPDATE didn't apply because completed_at
        # is already set. Honest 409 so the caller knows the cancel was
        # a no-op and the job's current terminal state is what stands.
        # 1.7.9 — structured log so prod journalctl can confirm the race
        # path is firing when the eval reproduces (the 1.7.7 → 1.7.8 →
        # 1.7.9 cycle has been blocked by ambiguity about whether the
        # race actually fires in prod or the eval is misreading a 409).
        latest = cancelled or jobs.get_job(job_id) or job
        _LOG.warning(
            "job_cancel_race_lost",
            extra={
                "job_id": job_id,
                "expected_status": "cancelled",
                "actual_status": latest.get("status"),
                "completed_at": latest.get("completed_at"),
                "caller_owner_id": caller.get("owner_id"),
            },
        )
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                error_codes.JOB_INVALID_STATE,
                (
                    f"Cancel was a no-op: the job already reached "
                    f"'{latest.get('status') or 'unknown'}' before the "
                    "cancel arrived. If the job completed successfully, "
                    "the agent has already been paid. To contest, file "
                    "a dispute (the 5¢ filing deposit is held until the "
                    "panel rules)."
                ),
                {
                    "current_status": latest.get("status"),
                    "completed_at": latest.get("completed_at"),
                    "cancel_race_lost": True,
                },
            ),
        )
    settled = _settle_failed_job(cancelled, actor_owner_id=caller["owner_id"])
    final_job = settled or cancelled
    # 1.7.9 — the response JSON returned 200 with `status: null` in the
    # 1.7.7/1.7.8 eval traces. The exact shape coming out of _job_response
    # for a freshly-cancelled job should always have status="cancelled" by
    # construction (the UPDATE we just ran set it), but defense-in-depth:
    # if the response dict somehow lost the field, fix it from the
    # update_job_status return rather than emitting null on the wire.
    response_body = _job_response(final_job, caller)
    if not response_body.get("status"):
        response_body["status"] = "cancelled"
    return JSONResponse(content=response_body)


@app.get(
    "/openai/tools",
    tags=["Integrations"],
    summary="Legacy OpenAI chat-completions/assistants tool manifest for Aztea tools.",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def openai_tools(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    payload = tool_adapters.build_openai_chat_manifest(_mcp_active_agents())
    payload["hire_endpoint"] = f"{_SERVER_BASE_URL}/jobs"
    payload["status_endpoints"] = {
        "jobs": f"{_SERVER_BASE_URL}/jobs/{{job_id}}",
        "compare": f"{_SERVER_BASE_URL}/jobs/compare/{{compare_id}}",
        "pipeline_runs": f"{_SERVER_BASE_URL}/pipelines/{{pipeline_id}}/runs/{{run_id}}",
        "recipes": f"{_SERVER_BASE_URL}/recipes",
    }
    return JSONResponse(content=payload)


@app.get(
    "/openai/responses-tools",
    tags=["Integrations"],
    summary="OpenAI Responses API / Codex-compatible Aztea tool manifest.",
    responses=_error_responses(401, 403, 429, 500),
)
@app.get(
    "/codex/tools",
    tags=["Integrations"],
    summary="Codex/OpenAI Responses-compatible Aztea tool manifest.",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def openai_responses_tools(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    payload = tool_adapters.build_openai_responses_manifest(_mcp_active_agents())
    payload["job_create_endpoint"] = f"{_SERVER_BASE_URL}/jobs"
    payload["compare_create_endpoint"] = f"{_SERVER_BASE_URL}/jobs/compare"
    payload["pipeline_list_endpoint"] = f"{_SERVER_BASE_URL}/pipelines"
    payload["recipes_list_endpoint"] = f"{_SERVER_BASE_URL}/recipes"
    return JSONResponse(content=payload)


@app.get(
    "/gemini/tools",
    tags=["Integrations"],
    summary="Gemini function-declarations manifest for Aztea tools.",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def gemini_tools(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    payload = tool_adapters.build_gemini_manifest(_mcp_active_agents())
    payload["job_create_endpoint"] = f"{_SERVER_BASE_URL}/jobs"
    payload["compare_create_endpoint"] = f"{_SERVER_BASE_URL}/jobs/compare"
    payload["pipeline_list_endpoint"] = f"{_SERVER_BASE_URL}/pipelines"
    payload["recipes_list_endpoint"] = f"{_SERVER_BASE_URL}/recipes"
    return JSONResponse(content=payload)


@app.get(
    "/mcp/tools",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def mcp_tools_manifest(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    tools, _ = _mcp_tools_and_lookup()
    return JSONResponse(content={"tools": tools, "count": len(tools)})


@app.get(
    "/mcp/manifest",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def mcp_manifest_payload(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    tools, _ = _mcp_tools_and_lookup()
    return JSONResponse(
        content={
            "schema_version": "v1",
            "name": "aztea",
            "description": "AI agent marketplace: specialized agents as callable tools",
            "tools": tools,
        }
    )


_MCP_COMPUTE_HEAVY_AGENT_IDS = frozenset(
    {
        _PYTHON_EXECUTOR_AGENT_ID,
    }
)


@app.post(
    "/mcp/invoke",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 402, 404, 429, 500),
)
def mcp_invoke(
    request: Request,
    body: MCPInvokeRequest,
) -> core_models.DynamicObjectResponse:
    # 1. Auth: accept agent keys (azk_), regular user caller keys (az_), or master key.
    raw_key = str(body.api_key or "").strip()
    if not raw_key:
        raise HTTPException(
            status_code=401,
            detail=error_codes.make_error("auth.invalid_key", "API key is required."),
        )
    agent_key = _auth.verify_agent_api_key(raw_key)
    user_key = None
    if agent_key is None:
        user_key = _auth.verify_api_key(raw_key)
        if user_key is not None and "caller" not in (user_key.get("scopes") or []):
            user_key = None
    is_master = hmac.compare_digest(raw_key, _MASTER_KEY)
    if agent_key is None and user_key is None and not is_master:
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                "auth.invalid_key", "Invalid or inactive API key."
            ),
        )
    caller_key_id = str(
        (agent_key or {}).get("key_id") or (user_key or {}).get("key_id") or "master"
    )

    # 2. Per-key sliding-window rate limit: 60 req/min.
    if not _mcp_check_rate_limit(caller_key_id):
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": "60"},
            detail=error_codes.make_error(
                error_codes.RATE_LIMITED,
                "MCP rate limit exceeded. Maximum 60 requests per minute per key.",
            ),
        )

    # 3. Tool lookup. Sunset agents stay resolvable here so existing slug-based
    # invocations don't break, even though they're hidden from the public
    # manifest endpoints (see _mcp_active_agents).
    lookup = _mcp_invoke_lookup()
    agent = lookup.get(body.tool_name)
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Tool not found.",
                details={"tool_name": body.tool_name},
            ),
        )

    # Build caller context from the agent key.
    if agent_key is not None:
        caller: core_models.CallerContext = {
            "type": "agent_key",
            "owner_id": f"agent_key:{agent_key['agent_id']}",
            "agent_id": str(agent_key["agent_id"]),
            "key_id": caller_key_id,
            "scopes": ["worker"],
        }
    elif user_key is not None:
        caller = {
            "type": "user",
            "owner_id": f"user:{user_key['user_id']}",
            "key_id": caller_key_id,
            "scopes": user_key.get("scopes") or ["caller"],
        }
    else:
        caller = {
            "type": "master",
            "owner_id": "master",
            "scopes": ["caller", "worker", "admin"],
        }

    # 4. Dispatch. registry_call owns pre-call charge, payout, and refund-on-failure.
    agent_id = str(agent["agent_id"])
    request.state._caller = caller
    # Workspace context (optional): MCP-attached summary of caller's local cwd.
    # Two ingest modes — full bundle (first call from a directory) or just a
    # fingerprint (subsequent calls). Fingerprint mode looks up the cached
    # bundle and rehydrates it; cache miss silently degrades to no-context.
    merged_input = dict(body.input or {})
    _merge_workspace_context_into_payload(merged_input, body)
    t0 = time.monotonic()
    success = False
    error_code: str | None = None
    delegated = None
    raised: BaseException | None = None
    try:
        delegated = registry_call(
            request=request,
            agent_id=agent_id,
            body=core_models.RegistryCallRequest(root=merged_input),
            caller=caller,
        )
        success = True
    except BaseException as exc:
        raised = exc
        error_code = _extract_mcp_error_code(exc)
    duration_ms = int((time.monotonic() - t0) * 1000)

    # 6. Audit log (non-blocking; failure does not abort the response). The
    # log MUST happen on the failure path too — otherwise mcp_invocation_log
    # silently misses every failure and the /admin/usage failures view
    # returns empty.
    input_json = json.dumps(body.input, default=str) if body.input is not None else "{}"
    _mcp_log_invocation(
        agent_id, caller_key_id, body.tool_name, input_json,
        duration_ms, success, error_code=error_code,
    )
    if raised is not None:
        raise raised

    payload = _mcp_payload_from_response(delegated)
    response_body: dict[str, Any] = {
        "content": _mcp_content_from_payload(payload),
    }
    if isinstance(payload, dict):
        response_body["structuredContent"] = payload
    elif payload is not None:
        response_body["structuredContent"] = {"result": payload}
    return JSONResponse(content=response_body)


def _normalize_model_provider_filter(raw_value: str | None) -> str | None:
    text = str(raw_value or "").strip().lower()
    if not text:
        return None
    normalized = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-")
    return normalized or None


def _build_model_catalog(
    agents: list[dict[str, Any]],
    *,
    model_provider: str | None = None,
    model_id: str | None = None,
    include_examples: bool = True,
    example_limit: int = 5,
) -> list[dict[str, Any]]:
    normalized_provider = _normalize_model_provider_filter(model_provider)
    normalized_model_id = str(model_id or "").strip() or None
    capped_examples = min(max(0, int(example_limit)), 20)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for agent in agents:
        provider = _normalize_model_provider_filter(agent.get("model_provider"))
        model = str(agent.get("model_id") or "").strip()
        if not provider or not model:
            continue
        if normalized_provider and provider != normalized_provider:
            continue
        if normalized_model_id and model != normalized_model_id:
            continue
        key = (provider, model)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "model_provider": provider,
                "model_id": model,
                "agent_count": 0,
                "total_calls": 0,
                "avg_success_rate": 0.0,
                "agents": [],
                "work_examples": [],
            }
            grouped[key] = bucket
        bucket["agent_count"] += 1
        bucket["total_calls"] += int(agent.get("total_calls") or 0)
        bucket["agents"].append(
            {
                "agent_id": str(agent.get("agent_id") or ""),
                "name": str(agent.get("name") or ""),
                "price_per_call_usd": float(agent.get("price_per_call_usd") or 0.0),
                "success_rate": float(agent.get("success_rate") or 0.0),
            }
        )
        if (
            include_examples
            and capped_examples > 0
            and len(bucket["work_examples"]) < capped_examples
        ):
            examples = agent.get("output_examples")
            if isinstance(examples, list):
                for example in examples:
                    if not isinstance(example, dict):
                        continue
                    bucket["work_examples"].append(
                        {
                            "agent_id": str(agent.get("agent_id") or ""),
                            "agent_name": str(agent.get("name") or ""),
                            "example": example,
                        }
                    )
                    if len(bucket["work_examples"]) >= capped_examples:
                        break

    models: list[dict[str, Any]] = []
    for bucket in grouped.values():
        model_agents = bucket.pop("agents")
        if model_agents:
            bucket["avg_success_rate"] = round(
                sum(float(item.get("success_rate") or 0.0) for item in model_agents)
                / len(model_agents),
                6,
            )
        else:
            bucket["avg_success_rate"] = 0.0
        bucket["agents"] = sorted(
            model_agents,
            key=lambda item: (
                item.get("success_rate") or 0.0,
                -(item.get("price_per_call_usd") or 0.0),
            ),
            reverse=True,
        )
        models.append(bucket)

    return sorted(
        models,
        key=lambda item: (item.get("agent_count") or 0, item.get("total_calls") or 0),
        reverse=True,
    )


@app.get(
    "/registry/models",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def registry_models_list(
    request: Request,
    model_provider: str | None = None,
    model_id: str | None = None,
    include_examples: bool = True,
    example_limit: int = 5,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    include_unapproved = _caller_is_admin(caller)
    agents = registry.get_agents_with_reputation(
        include_unapproved=include_unapproved,
    )
    models = _build_model_catalog(
        agents,
        model_provider=model_provider,
        model_id=model_id,
        include_examples=include_examples,
        example_limit=example_limit,
    )
    return JSONResponse(content={"models": models, "count": len(models)})


_agents_list_cache: dict | None = None
_agents_list_cache_at: float = 0.0
_AGENTS_LIST_TTL = 15.0  # seconds — agents don't change by the second


@app.get(
    "/registry/agents",
    response_model=core_models.RegistryAgentsResponse,
    responses=_error_responses(422, 429, 500),
)
@limiter.limit("60/minute")
def registry_list(
    request: Request,
    tag: str | None = None,
    rank_by: str | None = None,
    include_reputation: bool = True,
    model_provider: str | None = None,
    owner_id: str | None = None,
    caller: core_models.CallerContext | None = Depends(_optional_api_key),
) -> core_models.RegistryAgentsResponse:
    global _agents_list_cache, _agents_list_cache_at
    import time as _time

    include_unapproved = caller is not None and _caller_is_admin(caller)
    # Use cached agent+reputation rows for non-admin, no-filter requests.
    # The cache key is the same for every "default browse" page hit, so any
    # filter-bearing query (tag, model_provider, owner_id) bypasses it.
    use_cache = (
        not include_unapproved
        and tag is None
        and model_provider is None
        and owner_id is None
        and include_reputation
    )
    now = _time.monotonic()
    if (
        use_cache
        and _agents_list_cache is not None
        and (now - _agents_list_cache_at) < _AGENTS_LIST_TTL
    ):
        agents = _agents_list_cache
    else:
        try:
            agents = (
                registry.get_agents_with_reputation(
                    tag=tag,
                    include_unapproved=include_unapproved,
                    model_provider=model_provider,
                )
                if include_reputation
                else registry.get_agents(
                    tag=tag,
                    include_unapproved=include_unapproved,
                    model_provider=model_provider,
                )
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=_envelope_from_value_error(exc, "registry"),
            )
        if use_cache:
            _agents_list_cache = agents
            _agents_list_cache_at = now
    agents = _sorted_agents(agents, rank_by=rank_by)
    # Hide sunset/deprecated builtins from the public catalog. They remain
    # callable by direct slug or agent_id (so historical job_ids and signed
    # receipts still resolve), but they no longer surface to discovery,
    # search, or auto-hire. 1.7.5 — admins were previously seeing sunset
    # rows here, which leaked into `aztea agents list` for any operator
    # using a master-derived key (eval N10 reproduced this). The default
    # is now to filter sunset for ALL callers; admins who need to see
    # sunset agents (ops work) pass `?include_sunset=1` explicitly.
    include_sunset = (
        include_unapproved
        and str(request.query_params.get("include_sunset") or "").lower() in ("1", "true", "yes")
    )
    if not include_sunset:
        sunset = _builtin_constants.SUNSET_DEPRECATED_AGENT_IDS
        agents = [
            a
            for a in agents
            if a.get("agent_id") not in sunset
            and str(a.get("review_status") or "").strip().lower() != "sunset"
        ]

    # Wave 2 (2026-05-26): owner_id filter for the builder-profile page.
    # Applied here (post-fetch, post-sunset-filter) rather than as a SQL
    # WHERE clause because it's a presentation concern, not a query concern,
    # and owner_id-bearing requests bypass the agents-list cache so the
    # filter never hits any cached rows. Empty owner_id is treated as "no
    # filter" — same convention as the other query params.
    owner_filter = (owner_id or "").strip()
    if owner_filter:
        agents = [
            a for a in agents
            if str(a.get("owner_id") or "").strip() == owner_filter
        ]

    # Curated-public-set parity: any agent in CURATED_PUBLIC_BUILTIN_AGENT_IDS
    # must surface here, even if the registry seed hasn't run on a fresh
    # deploy or the agents-list cache is stale. The 2026-05-08 power-user
    # eval saw list_agents return 7 while search and the spend ledger
    # revealed 9 reachable agents (Browser Agent + Visual Regression
    # missing) — symptom of a half-seeded registry. Augmenting from the
    # spec ensures the public surface is always self-consistent. We DON'T
    # write to the DB here (that belongs in the lifespan startup); we
    # just synthesize a registry-shaped row from the spec for any missing
    # curated id, so the response is correct without any cache to evict.
    #
    # Only augment when the caller is browsing the unfiltered public catalog.
    # `tag` / `model_provider` filters were applied to the SQL query, so
    # synthesizing builtin rows here would bypass them and silently inflate
    # the response (regression caught by test_quality_rating_and_trust_ranking).
    # Same logic for owner_id — a builder's profile shows ONLY agents they
    # actually own; built-in agents are owned by the platform user.
    if (
        not include_unapproved
        and tag is None
        and model_provider is None
        and not owner_filter
    ):
        present_ids = {str(a.get("agent_id") or "") for a in agents}
        # 1.7.2 — exclude sunset agents from spec-synthesis. Pre-1.7.2 the
        # curated set included sunset IDs (CURATED_BUILTIN_AGENT_IDS is
        # `set(SUNSET_DEPRECATED_AGENT_IDS) | {...}`), so missing sunset
        # rows got synthesized as `review_status:"approved", status:"active"`.
        # The call gate at part_002 correctly returned 410 sunset, so users
        # saw "approved/active" agents fail every call. Filter at synthesis
        # time so the listing matches the call gate.
        sunset_ids = set(_builtin_constants.SUNSET_DEPRECATED_AGENT_IDS)
        missing_curated = (
            set(_builtin_constants.CURATED_PUBLIC_BUILTIN_AGENT_IDS)
            - present_ids
            - sunset_ids
        )
        if missing_curated:
            spec_by_id = _builtin_specs.builtin_spec_by_id()
            for missing_id in missing_curated:
                spec = spec_by_id.get(missing_id)
                if spec is None:
                    continue
                agents.append(
                    {
                        "agent_id": missing_id,
                        "name": spec.get("name"),
                        "description": spec.get("description", ""),
                        "endpoint_url": spec.get("endpoint_url"),
                        "price_per_call_usd": float(
                            spec.get("price_per_call_usd", 0.01)
                        ),
                        "tags": list(spec.get("tags") or []),
                        "input_schema": spec.get("input_schema"),
                        "output_schema": spec.get("output_schema"),
                        "category": spec.get("category"),
                        "status": "active",
                        "review_status": "approved",
                        "internal_only": 0,
                        "trust_score": None,
                        "success_rate": None,
                    }
                )
    bulk_stats = _compute_bulk_agent_stats([a["agent_id"] for a in agents])
    # Weak ETag derived from (count, sorted agent_id|updated_at pairs). Stable
    # across re-orderings of the agents list and cheap to compute. Used by the
    # MCP server's tight (~5 s) registry poll: a 304 returns no body, so the
    # bandwidth/CPU cost of polling every 5 s ≈ polling every 60 s before.
    etag_seed = "|".join(
        f"{a.get('agent_id') or ''}:{a.get('updated_at') or ''}:"
        f"{a.get('review_status') or ''}:{a.get('status') or ''}"
        for a in sorted(agents, key=lambda a: str(a.get("agent_id") or ""))
    )
    etag_seed += f"|count={len(agents)}|inc_unapp={int(include_unapproved)}"
    etag_value = 'W/"' + hashlib.sha1(etag_seed.encode("utf-8")).hexdigest()[:24] + '"'
    if_none_match = request.headers.get("if-none-match", "").strip()
    if if_none_match and if_none_match == etag_value:
        return Response(status_code=304, headers={"ETag": etag_value})
    return JSONResponse(
        content={
            "agents": [
                _agent_response(a, caller, bulk_stats.get(a["agent_id"]))
                for a in agents
            ],
            "count": len(agents),
        },
        headers={"ETag": etag_value},
    )


# ─── Builder profile (Wave 2, 2026-05-26) ──────────────────────────────────
#
# Public endpoint backing /builders/<username> on the frontend. PUBLIC by
# design — builder profiles are part of the marketplace trust signal, the
# same way GitHub user pages are. Auth context is optional; the response
# shape is identical whether the caller is signed in or not.
#
# Earnings are gated by users.profile_visible_earnings (migration 0073).
# The aggregator (core.builder_profiles) omits the field when the flag
# is 0 so the frontend hides the section entirely rather than showing $0.

@app.get(
    "/registry/builders/{username}",
    response_model=core_models.BuilderProfileResponse,
    response_model_exclude_none=True,
    responses=_error_responses(404, 429, 500),
    tags=["Registry"],
    summary="Public builder profile — aggregate stats for one publisher.",
)
# Tighter than `/registry/agents` (60/min) because this endpoint is keyed
# only on `username` and otherwise public — easy to scrape for username
# enumeration. /review caught this 2026-05-27 along with the 404-detail
# leak (we used to echo the requested username in the error context).
@limiter.limit("10/minute")
def registry_builder_profile(
    request: Request,
    username: str,
) -> core_models.BuilderProfileResponse:
    from core import builder_profiles

    try:
        profile = builder_profiles.build_profile(username)
    except builder_profiles.BuilderNotFound:
        # Generic message + no `username` echo in the context — denies an
        # enumeration attacker the ability to confirm "this username exists"
        # vs "this username is taken but has no public profile". Both look
        # the same from the outside now.
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                "builder.unknown_username",
                "No public builder profile found.",
                {},
            ),
        )
    return core_models.BuilderProfileResponse(**profile.to_jsonable())


@app.get(
    "/registry/agents/mine",
    responses=_error_responses(401, 403, 429, 500),
    tags=["Registry"],
    summary="List agents owned by the authenticated caller.",
)
@limiter.limit("60/minute")
def registry_list_mine(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    agents = registry.get_agents_by_owner(caller["owner_id"])
    bulk_stats = _compute_bulk_agent_stats([a["agent_id"] for a in agents])
    return JSONResponse(
        content={
            "agents": [
                _agent_response(a, caller, bulk_stats.get(a["agent_id"]))
                for a in agents
            ],
            "count": len(agents),
        }
    )


class AgentUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    price_per_call_usd: float | None = None
    pii_safe: bool | None = None
    outputs_not_stored: bool | None = None
    audit_logged: bool | None = None
    region_locked: str | None = None
    payout_curve: dict | None = None
    clear_payout_curve: bool = False
    cacheable: bool | None = None


@app.patch(
    "/registry/agents/{agent_id}",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
    tags=["Registry"],
    summary="Update mutable fields on your own agent.",
)
@limiter.limit("30/minute")
def registry_update_agent(
    request: Request,
    agent_id: str,
    body: AgentUpdateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "worker")
    # Re-run the listing safety scanner on any text the owner is mutating.
    # Without this, an author could register a clean listing and then PATCH
    # the description to inject prompt-injection content or leak an API key
    # after the listing has earned trust.
    #
    # 2026-05-22: extended to also re-scan tags (each tag) so a PATCH
    # cannot smuggle prompt-injection into the marketplace surface via the
    # tag slot. ``output_examples`` is not in AgentUpdateRequest so cannot
    # be PATCHed; if that changes, scan it here too.
    mutable_text_parts: list[str] = [
        part for part in (body.name, body.description) if part
    ]
    if body.tags:
        mutable_text_parts.extend(str(tag) for tag in body.tags if tag)
    if mutable_text_parts:
        combined = "\n".join(mutable_text_parts)
        update_findings = _listing_safety.scan_skill_md(combined)
        if _listing_safety.has_block(update_findings):
            block = next(
                f for f in update_findings
                if f.level == _listing_safety.LEVEL_BLOCK
            )
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    "listing.safety_block", block.message,
                    {"code": block.code, "detail": block.detail},
                ),
            )
    # Price-change cooldown: a probation listing must not raise its price
    # more than _PROBATION_PRICE_JUMP_MAX_RATIO×, and an approved listing
    # must not jump more than _APPROVED_PRICE_JUMP_MAX_RATIO× per call.
    # Without this, a scammer who graduates probation can immediately 100×
    # the price and harvest one expensive call before reviewers notice.
    if body.price_per_call_usd is not None and caller["type"] != "master":
        existing = registry.get_agent(agent_id, include_unapproved=True)
        if existing is not None:
            _enforce_price_jump_cap(
                existing=existing, new_price=float(body.price_per_call_usd),
            )
    try:
        updated = registry.update_agent(
            agent_id,
            caller["owner_id"],
            name=body.name,
            description=body.description,
            tags=body.tags,
            price_per_call_usd=body.price_per_call_usd,
            pii_safe=body.pii_safe,
            outputs_not_stored=body.outputs_not_stored,
            audit_logged=body.audit_logged,
            region_locked=body.region_locked,
            payout_curve=body.payout_curve,
            clear_payout_curve=body.clear_payout_curve,
            cacheable=body.cacheable,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=_envelope_from_value_error(exc, "registry"),
        )
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Agent not found or you don't own it.",
                details={"agent_id": agent_id},
            ),
        )
    bulk_stats = _compute_bulk_agent_stats([agent_id])
    return JSONResponse(
        content=_agent_response(updated, caller, bulk_stats.get(agent_id))
    )


@app.delete(
    "/registry/agents/{agent_id}",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Registry"],
    summary="Delist (soft-delete) your own agent.",
)
@limiter.limit("10/minute")
def registry_delist_agent(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "worker")
    ok = registry.delist_agent(agent_id, caller["owner_id"])
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Agent not found or you don't own it.",
                details={"agent_id": agent_id},
            ),
        )
    return JSONResponse(content={"delisted": True, "agent_id": agent_id})


@app.post(
    "/registry/agents/{agent_id}/verify-domain",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 404, 409, 429, 500),
    tags=["Registry"],
    summary="Verify ownership of the domain hosting your agent's endpoint.",
)
@limiter.limit("5/minute")
def registry_verify_domain(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Plan B Phase 3c (2026-05-27): optional domain-ownership badge.

    Tries ``.well-known/aztea-agent.json`` then DNS TXT
    (``_aztea-agent.<host>``). Either method, once verified, sets
    ``domain_verified=true`` on the agent and lifts ranking in auto-hire.

    Aztea-hosted agents (``internal://`` / ``skill://``) return 409 —
    domain verification is for self-hosted endpoints only.
    """
    _require_scope(caller, "worker")
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if not agent or agent.get("owner_id") != caller["owner_id"]:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Agent not found or you don't own it.",
                details={"agent_id": agent_id},
            ),
        )
    endpoint_url = str(agent.get("endpoint_url") or "")
    if endpoint_url.startswith(("internal://", "skill://")):
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                "registry.domain_verification_not_applicable",
                "This agent is Aztea-hosted. Domain verification only applies "
                "to self-hosted http(s):// endpoints.",
                details={"agent_id": agent_id, "endpoint_url": endpoint_url},
            ),
        )
    from core import domain_proof
    ok, detail = domain_proof.verify_domain_ownership(
        endpoint_url=endpoint_url,
        agent_id=agent_id,
        owner_id=caller["owner_id"],
    )
    if not ok:
        return JSONResponse(
            content={"verified": False, "detail": detail},
            status_code=200,  # 200 — the request succeeded, verification didn't
        )
    registry.mark_agent_domain_verified(agent_id, method=detail.get("method") or "unknown")
    return JSONResponse(content={
        "verified": True,
        "method": detail.get("method"),
        "detail": detail,
        "note": (
            "Domain verified. A 'Domain verified' badge will appear on the "
            "agent detail page and auto-hire ranking will receive a small bonus."
        ),
    })


@app.post(
    "/registry/agents/{agent_id}/rotate-secret",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 404, 409, 429, 500),
    tags=["Registry"],
    summary="Rotate the agent's HMAC endpoint signing secret.",
)
@limiter.limit("10/minute")
def registry_rotate_endpoint_signing_secret(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Plan B Phase 1 (2026-05-27): rotate the per-agent shared secret.

    The new secret is returned ONCE in this response — the seller must copy
    it into their endpoint's verification config before the next inbound
    call signs with it. Owner-only; the secret is never re-displayable.

    Aztea-hosted agents (``internal://`` / ``skill://`` endpoints) return
    409 — those never receive outbound HTTP, so an HMAC secret is moot.
    """
    _require_scope(caller, "worker")
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if not agent or agent.get("owner_id") != caller["owner_id"]:
        raise HTTPException(
            status_code=404,
            detail=error_codes.make_error(
                error_codes.AGENT_NOT_FOUND,
                "Agent not found or you don't own it.",
                details={"agent_id": agent_id},
            ),
        )
    new_secret = registry.rotate_endpoint_signing_secret(agent_id)
    if new_secret is None:
        raise HTTPException(
            status_code=409,
            detail=error_codes.make_error(
                "registry.endpoint_signing_secret_not_applicable",
                "This agent is Aztea-hosted (internal:// or skill:// endpoint). "
                "No outbound HMAC signing is performed, so there's no secret to rotate.",
                details={"agent_id": agent_id, "endpoint_url": agent.get("endpoint_url")},
            ),
        )
    return JSONResponse(content={
        "agent_id": agent_id,
        "endpoint_signing_secret": new_secret,
        "endpoint_signing_secret_note": (
            "Save this secret now — it's shown only once. Your endpoint MUST "
            "verify the X-Aztea-Signature header on every inbound call. "
            "The previous secret stops working immediately."
        ),
    })
