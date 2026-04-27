# server.application shard 7 — registry routes: register, list, search, get,
# update, delist, MCP manifest + invoke. All auth + SSRF validation lives
# here for agent-facing surfaces.


@app.post(
    "/registry/register",
    status_code=201,
    response_model=core_models.RegistryRegisterResponse,
    responses=_error_responses(400, 401, 403, 409, 429, 500),
)
@limiter.limit("20/minute")
def registry_register(
    request: Request,
    body: AgentRegisterRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistryRegisterResponse:
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(status_code=403, detail="Agent-scoped keys cannot register new agents.")
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
    try:
        safe_endpoint_url = _validate_agent_endpoint_url(request, body.endpoint_url)
        if not os.environ.get("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE"):
            _probe_register_endpoint_or_400(safe_endpoint_url)
        safe_healthcheck_url = None
        if body.healthcheck_url:
            safe_healthcheck_url = _validate_outbound_url(body.healthcheck_url, "healthcheck_url")
        safe_verifier_url = None
        if body.output_verifier_url:
            safe_verifier_url = _validate_outbound_url(body.output_verifier_url, "output_verifier_url")
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
            model_provider=body.model_provider,
            model_id=body.model_id,
            pricing_model=body.pricing_model,
            pricing_config=body.pricing_config,
            kind="self_hosted",
        )
        agent = registry.get_agent_with_reputation(agent_id, include_unapproved=True) or registry.get_agent(
            agent_id,
            include_unapproved=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Agent ID or name already exists.")
    message = "Agent registered successfully."
    if safe_verifier_url:
        if agent and agent.get("verified"):
            message = "Agent registered and verifier approved."
        else:
            message = f"Agent registered; verifier did not approve ({verifier_reason})."
    if (agent or {}).get("review_status") == "pending_review":
        message = "Your agent listing is pending review. You will be notified when it goes live."
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "message": message,
            "review_status": (agent or {}).get("review_status"),
            "agent": _agent_response(agent, caller) if agent else None,
        },
        status_code=201,
    )


def _mcp_tool_slug(name: str, fallback: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    return base or f"agent_{fallback}"


def _mcp_active_agents() -> list[dict[str, Any]]:
    agents = registry.get_agents(include_internal=True, include_banned=True)
    return [
        agent
        for agent in agents
        if str(agent.get("status") or "").strip().lower() == "active"
        and not bool(agent.get("internal_only"))
    ]


def _mcp_tools_and_lookup() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    tools: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}
    used_names: set[str] = set()
    for agent in _mcp_active_agents():
        agent_id = str(agent.get("agent_id") or "").strip()
        if not agent_id:
            continue
        fallback = (agent_id.replace("-", "")[:8] or "agent").lower()
        slug = _mcp_tool_slug(str(agent.get("name") or ""), fallback)
        if slug in used_names:
            slug = f"{slug}_{fallback}"
        while slug in used_names:
            slug = f"{slug}_x"
        used_names.add(slug)

        raw_input_schema = agent.get("input_schema")
        if isinstance(raw_input_schema, dict) and raw_input_schema:
            input_schema = raw_input_schema
        else:
            input_schema = {"type": "object", "properties": {}}
        raw_output_schema = agent.get("output_schema")
        output_schema = raw_output_schema if isinstance(raw_output_schema, dict) else {}
        tool = {
            "name": slug,
            "description": str(agent.get("description") or ""),
            "input_schema": input_schema,
            "output_schema": output_schema,
        }
        tools.append(tool)
        lookup[slug] = agent
    return tools, lookup


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
        return json.loads(body_text)
    except json.JSONDecodeError:
        return body_text


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
        for key in ("summary", "message", "answer", "title", "one_line_summary", "signal_reasoning"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _mcp_media_content_from_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for artifact in artifacts[:6]:
        mime = str(artifact.get("mime") or "").strip().lower()
        source = str(artifact.get("url_or_base64") or "").strip()
        if not mime or not source:
            continue
        parsed_mime, base64_payload = _parse_data_uri(source)
        effective_mime = parsed_mime or mime
        if effective_mime.startswith("image/") and base64_payload:
            rendered.append({"type": "image", "mimeType": effective_mime, "data": base64_payload})
            continue
        if source.startswith("http://") or source.startswith("https://"):
            rendered.append({"type": "resource", "resource": {"uri": source, "mimeType": effective_mime}})
            continue
        if base64_payload:
            rendered.append(
                {
                    "type": "resource",
                    "resource": {"uri": f"data:{effective_mime};base64,{base64_payload}", "mimeType": effective_mime},
                }
            )
            continue
    return rendered


def _mcp_content_from_payload(payload: Any) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": _mcp_text_from_payload(payload)}]
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
    summary="Google A2A: per-agent card. Also served at /.well-known/agent.json?agent_id=...",
    responses=_error_responses(404),
)
def a2a_agent_card(agent_id: str, request: Request) -> JSONResponse:
    agent = registry.get_agent_with_reputation(agent_id)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if agent.get("internal_only"):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return JSONResponse(
        content=_a2a_agent_card(agent),
        headers={"Content-Type": "application/json"},
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
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    public_pem = agent.get("signing_public_key")
    did = agent.get("did")
    if not public_pem or not did:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_id}' has no published cryptographic identity yet.",
        )
    try:
        from core import crypto as _crypto
        jwk = _crypto.public_key_to_jwk(public_pem)
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Failed to render the agent's public key.",
        )
    key_id = f"{did}#key-1"
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
    }
    return JSONResponse(
        content=document,
        headers={"Content-Type": "application/did+json"},
    )


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
    if agent is None or not _caller_can_access_agent(caller, agent) or agent.get("status") in {"banned"}:
        raise HTTPException(status_code=404, detail=f"Skill (agent) '{body.skill_id}' not found.")
    if agent.get("status") == "suspended":
        raise HTTPException(status_code=503, detail=f"Skill (agent) '{body.skill_id}' is suspended.")
    if agent.get("internal_only"):
        raise HTTPException(status_code=404, detail=f"Skill (agent) '{body.skill_id}' not found.")

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
            charge_tx_id=charge_tx_id,
            input_payload=body.input or {},
            agent_owner_id=agent.get("owner_id"),
            max_attempts=3,
            dispute_window_hours=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
            judge_agent_id=_QUALITY_JUDGE_AGENT_ID,
            callback_url=body.callback_url or None,
        )
    except Exception:
        payments.post_call_refund(caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent["agent_id"])
        raise HTTPException(status_code=500, detail="Failed to create task.")

    _record_job_event(job, "job.created", actor_owner_id=caller["owner_id"])
    return JSONResponse(content={
        "id": job["job_id"],
        "skill_id": agent["agent_id"],
        "status": "submitted",
        "job_id": job["job_id"],
        "price_cents": price_cents,
        "caller_charge_cents": caller_charge_cents,
        "created_at": job["created_at"],
        "aztea_job": _job_response(job, caller),
    }, status_code=201)


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
    if job is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view this task.")
    a2a_status_map = {
        "pending": "submitted", "claimed": "working", "complete": "completed",
        "failed": "failed", "awaiting_clarification": "input-required",
    }
    return JSONResponse(content={
        "id": task_id,
        "skill_id": job["agent_id"],
        "status": a2a_status_map.get(job.get("status", ""), job.get("status", "")),
        "output": job.get("output_payload"),
        "error": job.get("error_message"),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "aztea_job": _job_response(job, caller),
    })


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
    if job is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to cancel this task.")
    if job.get("status") not in {"pending"}:
        raise HTTPException(status_code=409, detail=f"Cannot cancel task in status '{job.get('status')}'.")
    cancelled = jobs.update_job_status(task_id, "failed", error_message="Cancelled by caller.", completed=True)
    if cancelled:
        _settle_failed_job(cancelled, actor_owner_id=caller["owner_id"])
    return JSONResponse(content={"id": task_id, "status": "cancelled"})


@app.get(
    "/openai/tools",
    tags=["Integrations"],
    summary="OpenAI Agents SDK: tool definitions for all registered agents in function-calling format.",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def openai_tools(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    agents = registry.get_agents_with_reputation()
    visible = [a for a in agents if not a.get("internal_only")]
    tools = []
    for agent in visible:
        input_schema = agent.get("input_schema") or {}
        props = input_schema.get("properties", {})
        required = input_schema.get("required", [])
        tools.append({
            "type": "function",
            "function": {
                "name": f"hire_{agent['agent_id'].replace('-', '_')}",
                "description": (
                    f"{agent.get('description', '')} "
                    f"[Aztea: {agent['agent_id']} | "
                    f"${float(agent.get('price_per_call_usd', 0)):.4f}/call]"
                ).strip(),
                "parameters": {
                    "type": "object",
                    "properties": props if props else {"input": {"type": "string", "description": "Task input"}},
                    "required": required if required else [],
                },
                "metadata": {
                    "aztea_agent_id": agent["agent_id"],
                    "price_per_call_usd": float(agent.get("price_per_call_usd", 0)),
                    "trust_score": agent.get("trust_score"),
                    "success_rate": agent.get("success_rate"),
                    "hire_endpoint": f"{_SERVER_BASE_URL}/jobs",
                },
            },
        })
    return JSONResponse(content={"tools": tools, "count": len(tools), "hire_endpoint": f"{_SERVER_BASE_URL}/jobs"})


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


_MCP_COMPUTE_HEAVY_AGENT_IDS = frozenset({
    _PYTHON_EXECUTOR_AGENT_ID,
    _IMAGE_GENERATOR_AGENT_ID,
})


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
            detail=error_codes.make_error("auth.invalid_key", "Invalid or inactive API key."),
        )
    caller_key_id = str(
        (agent_key or {}).get("key_id")
        or (user_key or {}).get("key_id")
        or "master"
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

    # 3. Tool lookup.
    _, lookup = _mcp_tools_and_lookup()
    agent = lookup.get(body.tool_name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Tool '{body.tool_name}' not found.")

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
        caller = {"type": "master", "owner_id": "master", "scopes": ["caller", "worker", "admin"]}

    # 4. Dispatch. registry_call owns pre-call charge, payout, and refund-on-failure.
    agent_id = str(agent["agent_id"])
    request.state._caller = caller
    t0 = time.monotonic()
    success = False
    try:
        delegated = registry_call(
            request=request,
            agent_id=agent_id,
            body=core_models.RegistryCallRequest(root=body.input),
            caller=caller,
        )
        success = True
    except Exception:
        raise
    duration_ms = int((time.monotonic() - t0) * 1000)

    # 6. Audit log (non-blocking; failure does not abort the response).
    input_json = json.dumps(body.input, default=str) if body.input is not None else "{}"
    _mcp_log_invocation(agent_id, caller_key_id, body.tool_name, input_json, duration_ms, success)

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
        if include_examples and capped_examples > 0 and len(bucket["work_examples"]) < capped_examples:
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
                sum(float(item.get("success_rate") or 0.0) for item in model_agents) / len(model_agents),
                6,
            )
        else:
            bucket["avg_success_rate"] = 0.0
        bucket["agents"] = sorted(
            model_agents,
            key=lambda item: (item.get("success_rate") or 0.0, -(item.get("price_per_call_usd") or 0.0)),
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
    caller: core_models.CallerContext | None = Depends(_optional_api_key),
) -> core_models.RegistryAgentsResponse:
    global _agents_list_cache, _agents_list_cache_at
    import time as _time
    include_unapproved = caller is not None and _caller_is_admin(caller)
    # Use cached agent+reputation rows for non-admin, no-filter requests
    use_cache = not include_unapproved and tag is None and model_provider is None and include_reputation
    now = _time.monotonic()
    if use_cache and _agents_list_cache is not None and (now - _agents_list_cache_at) < _AGENTS_LIST_TTL:
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
            raise HTTPException(status_code=400, detail=str(exc))
        if use_cache:
            _agents_list_cache = agents
            _agents_list_cache_at = now
    agents = _sorted_agents(agents, rank_by=rank_by)
    bulk_stats = _compute_bulk_agent_stats([a["agent_id"] for a in agents])
    return JSONResponse(content={"agents": [_agent_response(a, caller, bulk_stats.get(a["agent_id"])) for a in agents], "count": len(agents)})


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
    return JSONResponse(content={"agents": [_agent_response(a, caller, bulk_stats.get(a["agent_id"])) for a in agents], "count": len(agents)})


class AgentUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    price_per_call_usd: float | None = None


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
    try:
        updated = registry.update_agent(
            agent_id,
            caller["owner_id"],
            name=body.name,
            description=body.description,
            tags=body.tags,
            price_per_call_usd=body.price_per_call_usd,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if updated is None:
        raise HTTPException(status_code=404, detail="Agent not found or you don't own it.")
    bulk_stats = _compute_bulk_agent_stats([agent_id])
    return JSONResponse(content=_agent_response(updated, caller, bulk_stats.get(agent_id)))


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
        raise HTTPException(status_code=404, detail="Agent not found or you don't own it.")
    return JSONResponse(content={"delisted": True, "agent_id": agent_id})


