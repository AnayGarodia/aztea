
from core import db as _db
# server.application shard 12 — hosted skills API.
#
# Endpoints for OpenClaw skill builders:
#   POST   /skills/validate    Parse a SKILL.md preview without persisting.
#   POST   /skills             Upload a SKILL.md, register the agent, persist.
#   GET    /skills             List the caller's hosted skills.
#   GET    /skills/{skill_id}  Fetch one (owner-scoped).
#   DELETE /skills/{skill_id}  Delist + remove the hosted_skills row.
#
# Hosted skills auto-approve: the endpoint URL is ``skill://{skill_id}`` and
# Aztea owns execution, so the human-review gate that exists for unknown
# external HTTP endpoints is unnecessary here. The existing review_status
# column still exists; we just write ``approved`` on insert.


@app.post("/skills/validate", responses=_error_responses(400, 401, 403, 413))
@limiter.limit("30/minute")
def skills_validate(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    """Dry-run a SKILL.md against the safety scanner + parser.

    Public (worker scope), mirroring POST /skills. Both were reopened to
    non-master callers in the 2026-05-26 Wave 3 pivot: the publish wizard's
    preview step depends on this route, so gating it master-only locked
    non-master publishers out of the UI at step 1 even though they're allowed
    to publish. The preview is not an information-leak oracle — /skills is
    public and returns the same block findings, so validate exposes nothing
    the real publish path doesn't already surface.
    """
    _require_scope(caller, "worker")
    raw_md = str((body or {}).get("skill_md") or "")
    if not raw_md.strip():
        raise HTTPException(status_code=400, detail="skill_md is required.")
    if len(raw_md.encode("utf-8")) > 256 * 1024:
        raise HTTPException(status_code=413, detail="skill_md exceeds 256 KB.")

    # Run the same safety scanner /skills runs at upload time. Without this,
    # /skills/validate would happily preview blocked content as `valid=true`,
    # giving an attacker an oracle for whether the live route would refuse.
    safety_findings = _listing_safety.scan_skill_md(raw_md)
    if _listing_safety.has_block(safety_findings):
        first_block = next(
            f for f in safety_findings if f.level == _listing_safety.LEVEL_BLOCK
        )
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                "listing.safety_block",
                first_block.message,
                {"code": first_block.code, "detail": first_block.detail},
            ),
        )

    try:
        parsed = _skill_parser.parse_skill_md(raw_md, source="upload")
    except _skill_parser.SkillParseError as exc:
        raise HTTPException(status_code=400, detail=f"SKILL.md is invalid: {exc}")
    return {
        "valid": True,
        "name": parsed.name,
        "description": parsed.description,
        "warnings": parsed.warnings,
        "registration_preview": parsed.to_aztea_registration(),
        "safety_warnings": [
            {"code": f.code, "level": f.level, "message": f.message}
            for f in safety_findings
            if f.level != _listing_safety.LEVEL_BLOCK
        ],
    }


_SKILL_DEFAULT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Natural-language request for the skill.",
        }
    },
    "required": ["task"],
}

_SKILL_DEFAULT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {
            "type": "string",
            "description": "The skill's response.",
        }
    },
    "required": ["result"],
}


@app.post(
    "/skills", status_code=201, responses=_error_responses(400, 401, 403, 409, 413, 429)
)
@limiter.limit("10/minute")
def skills_create(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    """Upload a SKILL.md, register an agent for it, and persist the skill row.

    History note: this route was restricted to master callers on 2026-05-17
    (prompt-only "tools" failed the original value test — callers could
    replicate them with their own LLM). The 2026-05-26 Wave-3 platform pivot
    reverses that policy. Hosted SKILL.md publishing is the cheapest path
    from "I have an idea" to "I'm earning per call" for non-infra builders.
    The security concern is now addressed by two new layers that did not
    exist in 2026-05-17:

      * ``core/listing_safety.scan_skill_md`` — static prompt-injection
        / API-key / base64 / internal-path scans (already in place).
      * ``core/listing_safety_judge.judge_skill_md`` — LLM-driven
        semantic intent review on every new publish AND every
        edit-republish (Wave 3 [1]).

    Non-master callers still land in probation (rank-penalised + price-capped
    until track record graduates them) so the auto-invoke surface stays
    conservative.
    """
    _require_scope(caller, "worker")
    payload = body or {}
    raw_md = str(payload.get("skill_md") or "")
    if not raw_md.strip():
        raise HTTPException(status_code=400, detail="skill_md is required.")
    if len(raw_md.encode("utf-8")) > 256 * 1024:
        raise HTTPException(status_code=413, detail="skill_md exceeds 256 KB.")
    try:
        price_per_call_usd = float(payload.get("price_per_call_usd"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="price_per_call_usd is required and must be a number.",
        )
    if not (price_per_call_usd >= 0.0 and price_per_call_usd <= 25.0):
        raise HTTPException(
            status_code=400, detail="price_per_call_usd must be between 0 and 25."
        )

    # Server-side safety scan. CLI runs the same scanner pre-flight, but the
    # /skills route is reachable directly so we re-enforce here. Any block
    # finding refuses the upload before we even parse the SKILL.md body.
    #
    # Two layers, in order:
    #   1. Static scan (scan_skill_md) — pattern match for prompt-injection,
    #      embedded API keys, base64 blobs, internal paths.
    #   2. LLM judge (judge_skill_md) — semantic-intent review for things
    #      the static scanner can't see (e.g. obvious instructions to leak
    #      the caller's API key that don't match any pre-canned phrase).
    #
    # The judge runs ONLY when the static layer didn't already produce a
    # BLOCK. This bounds LLM spend: payloads we've already refused don't
    # pay for an LLM call. /api/playground/test deliberately runs ONLY the
    # static scanner — the publish path is where the judge earns its keep.
    safety_findings = _listing_safety.scan_skill_md(raw_md)
    if not _listing_safety.has_block(safety_findings):
        try:
            from core.listing_safety_judge import judge_skill_md as _judge_skill_md
            safety_findings.extend(_judge_skill_md(raw_md))
        except Exception:  # noqa: BLE001 — judge failure must never block publish
            _LOG.warning("listing_safety_judge: judge_skill_md raised", exc_info=True)
    if _listing_safety.has_block(safety_findings):
        first_block = next(
            f for f in safety_findings if f.level == _listing_safety.LEVEL_BLOCK
        )
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                "listing.safety_block",
                first_block.message,
                {"code": first_block.code, "detail": first_block.detail},
            ),
        )

    try:
        parsed = _skill_parser.parse_skill_md(raw_md, source="upload")
    except _skill_parser.SkillParseError as exc:
        raise HTTPException(status_code=400, detail=f"SKILL.md is invalid: {exc}")

    if caller["type"] != "master":
        current_count = registry.count_owner_agents(caller["owner_id"])
        if current_count >= 20:
            raise HTTPException(
                status_code=403,
                detail=error_codes.make_error(
                    error_codes.REGISTRY_AGENT_LIMIT,
                    "You've reached the 20-agent limit. Delete or archive an existing listing.",
                    {"current": current_count, "max": 20},
                ),
            )

    base = parsed.to_aztea_registration()
    display_name = str(base.get("name") or parsed.name).strip()
    description = str(base.get("description") or parsed.description).strip()
    if not display_name:
        raise HTTPException(
            status_code=400, detail="Skill name is empty after parsing."
        )
    if not description:
        raise HTTPException(
            status_code=400, detail="Skill description is empty after parsing."
        )

    # Optional override fields the builder may set in the request body.
    requested_temperature = payload.get("temperature")
    requested_max_tokens = payload.get("max_output_tokens")
    requested_chain = payload.get("model_chain")
    if requested_chain is not None and not isinstance(requested_chain, list):
        raise HTTPException(
            status_code=400, detail="model_chain must be a list of strings."
        )

    # Resolve a unique listing name. ``agents.name`` has a UNIQUE constraint,
    # so we retry with a numeric suffix when a collision occurs.
    candidate_name = display_name
    agent_id: str | None = None
    last_error: Exception | None = None
    # Master keys publish trusted hosted skills (internal automation, ops);
    # everyone else lands in probation per CLAUDE.md so auto-invoke caps the
    # price + rank-penalises until graduate_probation_listings() promotes the
    # skill on track record. The earlier behaviour silently rubber-stamped
    # every external publish, which let a community skill show up next to
    # master-curated agents on day one — a real trust regression.
    is_master = caller.get("type") == "master"
    initial_review_status = "approved" if is_master else "probation"
    initial_review_note = (
        "Auto-approved hosted skill." if is_master
        else "Auto-published hosted skill — probation pending track record."
    )
    initial_reviewed_by = (
        "system:auto-approve-hosted-skill" if is_master
        else "system:auto-probation-hosted-skill"
    )
    for attempt in range(1, 8):
        try:
            agent_id = registry.register_agent(
                name=candidate_name,
                description=description,
                endpoint_url="skill://placeholder",  # rewritten below to skill://{skill_id}
                price_per_call_usd=price_per_call_usd,
                tags=list(base.get("tags") or []),
                input_schema=_SKILL_DEFAULT_INPUT_SCHEMA,
                output_schema=_SKILL_DEFAULT_OUTPUT_SCHEMA,
                owner_id=caller["owner_id"],
                review_status=initial_review_status,
                review_note=initial_review_note,
                reviewed_at=_utc_now_iso(),
                reviewed_by=initial_reviewed_by,
                kind="community_skill",
            )
            break
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=_envelope_from_value_error(exc, "skill"),
            )
        except _db.IntegrityError as exc:
            last_error = exc
            candidate_name = f"{display_name} #{attempt + 1}"
    if agent_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"Could not allocate a unique name (last: {last_error}).",
        )

    try:
        skill_row = _hosted_skills.create_hosted_skill(
            agent_id=agent_id,
            owner_id=caller["owner_id"],
            slug=parsed.name,
            raw_md=raw_md,
            system_prompt=parsed.body,
            parsed_metadata={
                "emoji": parsed.emoji,
                "homepage": parsed.homepage,
                "primary_env": parsed.primary_env,
                "skill_key": parsed.skill_key,
                "user_invocable": parsed.user_invocable,
                "allowed_tools": parsed.allowed_tools,
                "os_constraints": parsed.os_constraints,
                "warnings": parsed.warnings,
                "requires": {
                    "bins": parsed.requires.bins,
                    "any_bins": parsed.requires.any_bins,
                    "env": parsed.requires.env,
                    "config": parsed.requires.config,
                },
            },
            model_chain=requested_chain,
            temperature=float(requested_temperature)
            if requested_temperature is not None
            else 0.2,
            max_output_tokens=int(requested_max_tokens)
            if requested_max_tokens is not None
            else 1500,
        )
    except Exception:
        # Roll back the agent registration so we never leave a half-persisted skill.
        try:
            registry.delist_agent(agent_id, caller["owner_id"])
        except Exception:
            _LOG.exception(
                "Failed to roll back agent %s after hosted_skills insert failure.",
                agent_id,
            )
        raise

    # Now rewrite the agent's endpoint_url to point at the just-created skill_id.
    # ``update_agent`` doesn't expose ``endpoint_url`` because external agents
    # are forbidden from changing it after registration. For hosted skills we
    # write it once at creation time.
    final_endpoint = _hosted_skills.make_skill_endpoint_url(skill_row["skill_id"])
    # 1.6.9 fix: get_db_connection() yields the thread-local connection but
    # does NOT commit on context exit (per core/db.py). Pre-1.6.9 every
    # hosted-skill registration's endpoint_url UPDATE was silently rolled
    # back when the connection returned to the pool, so the agent kept the
    # placeholder skill://placeholder URL forever. Use the connection AS a
    # context manager so the UPDATE actually commits.
    with get_db_connection() as _conn:
        with _conn:
            _conn.execute(
                "UPDATE agents SET endpoint_url = %s WHERE agent_id = %s AND owner_id = %s",
                (final_endpoint, agent_id, caller["owner_id"]),
            )

    try:
        _owner_email = _get_owner_email(caller["owner_id"])
        if _owner_email:
            _user_obj = _auth.get_user_by_id(caller["owner_id"].replace("user:", ""))
            _owner_username = (_user_obj or {}).get("username", "there")
            _email.send_skill_live(
                _owner_email,
                _owner_username,
                candidate_name,
                price_per_call_usd,
                final_endpoint,
            )
    except Exception:
        _LOG.warning(
            "Failed to send skill-live email for skill %s", skill_row.get("skill_id")
        )

    return JSONResponse(
        content={
            "skill_id": skill_row["skill_id"],
            "agent_id": agent_id,
            "endpoint_url": final_endpoint,
            "name": candidate_name,
            "price_per_call_usd": price_per_call_usd,
            "review_status": initial_review_status,
            "warnings": parsed.warnings,
            "message": (
                "Skill is live and approved. Callers can hire it now."
                if initial_review_status == "approved"
                else "Skill is live on probation. Successful calls + a few "
                     "ratings ≥ 3.5 graduate it to approved."
            ),
        },
        status_code=201,
    )


@app.get("/skills", responses=_error_responses(401))
def skills_list(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "worker")
    rows = _hosted_skills.list_hosted_skills_for_owner(caller["owner_id"], limit=200)
    return {"skills": [_skill_response(row) for row in rows]}


@app.get("/skills/{skill_id}", responses=_error_responses(401, 403, 404))
def skills_get(
    request: Request,
    skill_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "worker")
    row = _hosted_skills.get_hosted_skill(skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Skill not found.")
    if caller["type"] != "master" and row.get("owner_id") != caller["owner_id"]:
        raise HTTPException(
            status_code=403, detail="Skill belongs to a different owner."
        )
    return _skill_response(row, include_raw_md=True)


@app.post(
    "/skills/{skill_id}/run",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 429, 500, 502, 503),
)
@limiter.limit("10/minute")
def skills_run(
    request: Request,
    skill_id: str,
    body: core_models.RegistryCallRequest | None = Body(default=None),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> Response:
    """Invoke a hosted skill by its skill_id.

    Thin alias over ``POST /registry/agents/{agent_id}/call``: looks up the
    skill's underlying agent and forwards the call so SDK callers don't need
    to know that hosted skills are implemented as auto-registered agents.
    """
    row = _hosted_skills.get_hosted_skill(skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found.")
    agent_id = str(row.get("agent_id") or "").strip()
    if not agent_id:
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                error_codes.AGENT_INTERNAL_ERROR,
                "Hosted skill is missing its agent_id; contact support.",
                {"skill_id": skill_id},
            ),
        )
    # registry_call lives in part_008 but shares the same compiled module
    # namespace, so we can hand off directly without an HTTP redirect.
    return registry_call(request=request, agent_id=agent_id, body=body, caller=caller)


@app.delete("/skills/{skill_id}", responses=_error_responses(401, 403, 404))
def skills_delete(
    request: Request,
    skill_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "worker")
    row = _hosted_skills.get_hosted_skill(skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Skill not found.")
    if caller["type"] != "master" and row.get("owner_id") != caller["owner_id"]:
        raise HTTPException(
            status_code=403, detail="Skill belongs to a different owner."
        )
    # Delist the agent first so callers stop seeing it; then remove the skill row.
    try:
        registry.delist_agent(row["agent_id"], row["owner_id"])
    except Exception:
        _LOG.exception(
            "Failed to delist agent %s during skill delete.", row["agent_id"]
        )
    # App-level cleanup of this skill's learnings (no DB cascade on skill_id —
    # see migration 0077). Best-effort: a learnings cleanup failure must not
    # block the skill delete the owner asked for.
    try:
        _skill_learnings.archive_learnings_for_skill(skill_id)
    except Exception:
        _LOG.exception(
            "Failed to archive learnings during delete of skill %s.", skill_id
        )
    _hosted_skills.delete_hosted_skill(skill_id)
    return {"deleted": True, "skill_id": skill_id}


def _skill_response(row: dict, include_raw_md: bool = False) -> dict:
    out = {
        "skill_id": row["skill_id"],
        "agent_id": row["agent_id"],
        "owner_id": row["owner_id"],
        "slug": row["slug"],
        "endpoint_url": _hosted_skills.make_skill_endpoint_url(row["skill_id"]),
        "temperature": row.get("temperature"),
        "max_output_tokens": row.get("max_output_tokens"),
        "model_chain": row.get("model_chain"),
        "parsed_metadata": row.get("parsed_metadata") or {},
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if include_raw_md:
        out["raw_md"] = row.get("raw_md")
        out["system_prompt"] = row.get("system_prompt")
    return out


# ── Auto-hire (aztea_do) ──────────────────────────────────────────────────
# Resolves a natural-language intent to a single agent and invokes it in one
# round-trip — when (and only when) every gate in core.registry.auto_hire
# passes. Otherwise returns a structured "candidates + reason" payload with
# no charge. The MCP frontend (aztea.mcp.server) proxies its
# `do_specialist_task` / `aztea_do` tool here.


class _AutoHireRequestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: str = Field(min_length=1, max_length=2000)
    input: dict[str, Any] | None = None
    max_cost_usd: float = Field(default=0.10, ge=0.0, le=100.0)
    dry_run: bool = False
    # When True, lower the confidence floor to 0.20 so unambiguous intents
    # like "Run this Python: ..." don't bounce to recommendation mode.
    # Caller still owns max_cost_usd; price + trust gates remain in force.
    aggressive: bool = False
    # Optional rendering hint applied AFTER invocation. The agent's JSON
    # output stays canonical; if set, `rendered_output` (string or dict
    # for slack_blocks) is attached alongside.
    output_format: str | None = None
    # MCP-attached workspace summary (file tree + manifests + README) for
    # the caller's local cwd. Forwarded into the agent payload; never
    # persisted. See core/workspace_bundle.py for the shape.
    workspace_context: dict[str, Any] | None = None
    # 2026-05-28 (Phase 3): caller may bypass the decision cache. Pass
    # ``"bypass"`` to force a fresh ranking; any other value (or omission)
    # uses the cache. Mirrors the noun-first reserved-envelope-key pattern
    # of ``_workspace_id``/``_artifact_ref`` (see DX C1).
    cache: str | None = Field(default=None, alias="_cache")


@lru_cache(maxsize=1)
def _builtin_routing_overlay() -> dict[str, dict[str, list[str]]]:
    """Map agent_id → {match_keywords, block_keywords} from builtin specs.

    Cached because builtin specs are static at process startup. Recompute by
    bouncing the process; do not mutate at runtime.
    """
    overlay: dict[str, dict[str, list[str]]] = {}
    for spec in _builtin_specs.builtin_agent_specs():
        agent_id = str(spec.get("agent_id") or "").strip()
        if not agent_id:
            continue
        match_kw = spec.get("match_keywords") or []
        block_kw = spec.get("block_keywords") or []
        if match_kw or block_kw:
            overlay[agent_id] = {
                "match_keywords": list(match_kw),
                "block_keywords": list(block_kw),
            }
    return overlay


def _merge_routing_overlay(
    record: dict[str, Any], overlay: dict[str, dict[str, list[str]]]
) -> dict[str, Any]:
    agent_id = str(record.get("agent_id") or "").strip()
    extra = overlay.get(agent_id)
    if not extra:
        return record
    merged = dict(record)
    if "match_keywords" not in merged or not merged.get("match_keywords"):
        merged["match_keywords"] = list(extra.get("match_keywords") or [])
    if "block_keywords" not in merged or not merged.get("block_keywords"):
        merged["block_keywords"] = list(extra.get("block_keywords") or [])
    return merged


@app.post(
    "/registry/agents/auto-hire",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 429, 500, 502),
    tags=["Registry"],
    summary="One-shot pick-best-agent-and-hire-it (with hard gates).",
)
@limiter.limit("20/minute")
def registry_auto_hire(
    request: Request,
    body: _AutoHireRequestBody,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Resolve `intent` to a single agent and invoke it, gated by:

    - feature flag         (AZTEA_AUTO_INVOKE_ENABLED)
    - confidence floor     (AZTEA_AUTO_INVOKE_CONFIDENCE, default 0.30;
                            0.20 when the caller passes aggressive=True)
    - stability tier       (no auto-invoke for beta agents)
    - trust score          (AZTEA_AUTO_INVOKE_TRUST_FLOOR, default 30)
    - success rate         (AZTEA_AUTO_INVOKE_SUCCESS_FLOOR, default 0.80)
    - per-call price       (min(caller.max_cost_usd, AZTEA_AUTO_INVOKE_SERVER_CAP_USD))
    - required-input fields present in caller-supplied `input` or extractable

    On any gate failure, returns auto_invoked=False with a `reason` and
    enough context for the caller (typically an LLM) to decide what to do
    next. No wallet activity occurs in the gated path.

    On success, delegates to the existing `registry_call` so settlement,
    receipts, and refund-on-failure stay identical to the manual path.
    """
    _require_scope(caller, "caller")
    # Auto-hire latency is the SLI for the do_specialist_task reflex. The
    # model only calls speculatively when the wall-clock cost feels like
    # grep, not like a search. Capture wall time at every return path.
    _route_started_at = time.perf_counter()

    # 1. Build candidate set from the live, public agent registry.
    raw_agents = _mcp_active_agents()
    # Overlay routing vocabulary from builtin specs onto DB records. The agents
    # table does not persist match_keywords/block_keywords; the spec is the
    # source of truth for those routing hints.
    routing_overlay = _builtin_routing_overlay()
    candidates = [
        _auto_hire.CandidateAgent.from_agent_record(
            _merge_routing_overlay(record, routing_overlay)
        )
        for record in raw_agents
        if _caller_can_access_agent(caller, record)
    ]

    # 2. Pure decision (cached by intent_hash + catalog_version + caller).
    # Phase 1 (C3): caller_owner_id is threaded into decide() so per-caller
    # affinity scoring has the data it needs, AND it's part of the cache
    # key so per-caller bias remains correct under caching — two callers
    # with the same intent can have different chosen agents and must not
    # serve each other's cached winner.
    bypass_cache = str(body.cache or "").strip().lower() == "bypass"
    with _observability.time_segment("embed_search"):
        decision, decision_meta = _auto_hire.decide_cached(
            intent=body.intent,
            explicit_input=body.input,
            max_cost_usd=float(body.max_cost_usd),
            candidates=candidates,
            aggressive=bool(body.aggressive),
            bypass_cache=bypass_cache,
            caller_owner_id=caller.get("owner_id"),
        )

    # 3. Gated path: short-circuit, no charge.
    if not decision.auto_invoked:
        top_candidate = decision.candidates[0] if decision.candidates else None
        estimated_cost_usd = (
            top_candidate.get("price_per_call_usd")
            if isinstance(top_candidate, dict)
            else None
        )
        estimated_cost_cents = (
            _usd_to_cents(estimated_cost_usd) if estimated_cost_usd is not None else None
        )
        _observability.record_route_decision(
            "gated", str(decision.reason or "unknown"),
            time.perf_counter() - _route_started_at,
        )
        # Belt-and-suspenders /cso H2 layer 4 (2026-05-29):
        # `compound_intent` rows are unactionable telemetry — they
        # have NULL chosen_agent_id so they don't pump the catchall
        # numerator, but they do pump the denominator. Suppressing
        # them shrinks the surface area an attacker can use to
        # adjust the catchall rate via spam-shaped refused intents.
        if str(decision.reason or "") != "compound_intent":
            _decision_audit.record_decision(
                intent_text=body.intent,
                auto_invoked=False,
                dry_run=bool(body.dry_run),
                reason=decision.reason,
                chosen_agent_id=None,
                confidence=decision.confidence,
                candidates=decision.candidates,
                caller_owner_id=caller.get("owner_id"),
                caller_key_id=caller.get("key_id"),
                resulting_job_id=None,
            )
        return JSONResponse(
            content={
                "auto_invoked": False,
                "mode": "recommendation",
                "charge_status": "not_charged",
                "delegation": {
                    "status": "not_hired",
                    "reason": decision.reason,
                    "intent": body.intent,
                },
                "reason": decision.reason,
                "confidence": decision.confidence,
                "candidates": decision.candidates,
                "missing_fields": decision.missing_fields,
                "dry_run_cost_usd": estimated_cost_usd,
                "estimated_cost_cents": estimated_cost_cents,
                "next_step": decision.next_step,
                "decision_meta": decision_meta,
            }
        )

    chosen = decision.chosen
    payload = decision.payload or {}
    assert chosen is not None  # for type checkers

    # Forward output_format into the call payload so the underlying
    # registry_call route applies the renderer once. We don't render here
    # to keep a single source of truth for output decoration.
    if body.output_format:
        payload = dict(payload)
        payload["output_format"] = body.output_format

    # Forward MCP-attached workspace context into the agent payload. Stays
    # in the in-memory call envelope only; stripped before public examples.
    if body.workspace_context:
        payload = dict(payload)
        payload["workspace_context"] = body.workspace_context

    # 4. Dry-run: report what *would* happen, no invocation.
    #
    # Single-call is now the canonical shape (the router refuses for free
    # when nothing matches, so a separate preview round-trip is rarely
    # useful). dry_run stays available for callers that want an explicit
    # preview, but we surface a deprecation hint on the response so the
    # client owners can migrate away.
    if body.dry_run:
        # Soft deprecation: structured hint on the response, not a runtime
        # warning that callers won't see. Keeps the parameter working
        # (backward-compat is non-negotiable for an MCP API) while telling
        # human readers of the JSON to migrate.
        _observability.record_route_decision(
            "dry_run", "dry_run",
            time.perf_counter() - _route_started_at,
        )
        _decision_audit.record_decision(
            intent_text=body.intent,
            auto_invoked=True,
            dry_run=True,
            reason="dry_run",
            chosen_agent_id=chosen.agent_id,
            confidence=decision.confidence,
            candidates=decision.candidates,
            caller_owner_id=caller.get("owner_id"),
            caller_key_id=caller.get("key_id"),
            resulting_job_id=None,
        )
        return JSONResponse(
            content={
                "auto_invoked": False,
                "reason": "dry_run",
                "would_invoke": True,
                "agent": {"slug": chosen.slug, "name": chosen.name},
                "confidence": decision.confidence,
                "estimated_cost_usd": chosen.price_per_call_usd,
                "estimated_cost_cents": _usd_to_cents(chosen.price_per_call_usd),
                "next_step": "Re-call without dry_run to execute (or set dry_run=false explicitly).",
                "deprecation_hint": (
                    "dry_run is being de-emphasised in favour of the single-call "
                    "shape; the router refuses for free when no agent matches, "
                    "so the two-step preview is rarely worth the round-trip."
                ),
                "decision_meta": decision_meta,
            }
        )

    # 5. Real invocation. Delegate in-process to the existing call route so
    #    the money path stays canonical (single source of truth for
    #    pre_call_charge → dispatch → settle/refund). The ``use_origin``
    #    wrapper tells the downstream ``jobs.create_job`` site to stamp the
    #    job row with ``origin='auto_hire'`` instead of the default 'direct'.
    try:
        with _origin_context.use_origin("auto_hire"):
            delegated = registry_call(
                request=request,
                agent_id=chosen.agent_id,
                body=core_models.RegistryCallRequest(root=payload),
                caller=caller,
            )
    except HTTPException as exc:
        # registry_call already refunds on failure paths it owns. Surface a
        # structured response so the LLM can recover gracefully without
        # confusing "auto_invoked=true but no output" semantics.
        detail = exc.detail if hasattr(exc, "detail") else str(exc)
        _observability.record_route_decision(
            "delegation_failed", "delegation_exception",
            time.perf_counter() - _route_started_at,
        )
        _decision_audit.record_decision(
            intent_text=body.intent,
            auto_invoked=True,
            dry_run=False,
            reason="invocation_failed",
            chosen_agent_id=chosen.agent_id,
            confidence=decision.confidence,
            candidates=decision.candidates,
            caller_owner_id=caller.get("owner_id"),
            caller_key_id=caller.get("key_id"),
            resulting_job_id=None,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "auto_invoked": True,
                "mode": "hired_specialist",
                "charge_status": "refunded_or_not_charged",
                "agent": chosen.public_dict(),
                "delegation": {
                    "status": "failed",
                    "intent": body.intent,
                    # `specialist` removed; agent identity is already at the
                    # top-level `agent` key. Was duplicated which made
                    # responses ~30% larger and confused buyers about which
                    # field to consume.
                    "spend_cap_usd": float(body.max_cost_usd),
                },
                "settlement": {
                    "status": "refunded_if_charged",
                    "refund_on_failure": True,
                },
                "confidence": decision.confidence,
                "error": detail,
                "next_step": (
                    "The agent failed. Charges were refunded automatically "
                    "if the platform initiated them."
                ),
            },
        )

    # 6. Unwrap the delegated response and decorate with auto-hire metadata.
    inner: dict[str, Any]
    if isinstance(delegated, JSONResponse):
        inner = json.loads(delegated.body.decode("utf-8")) if delegated.body else {}
    elif isinstance(delegated, dict):
        inner = dict(delegated)
    else:
        # Belt-and-suspenders: registry_call always returns JSONResponse today.
        inner = {}

    job_id = inner.get("job_id")
    cost_cents = inner.get("cost_cents")
    _decision_audit.record_decision(
        intent_text=body.intent,
        auto_invoked=True,
        dry_run=False,
        reason=None,
        chosen_agent_id=chosen.agent_id,
        confidence=decision.confidence,
        candidates=decision.candidates,
        caller_owner_id=caller.get("owner_id"),
        caller_key_id=caller.get("key_id"),
        resulting_job_id=str(job_id) if job_id else None,
    )
    response_body: dict[str, Any] = {
        "auto_invoked": True,
        "mode": "hired_specialist",
        "agent": chosen.public_dict(),
        "delegation": {
            "status": "hired",
            "intent": body.intent,
            "specialist": chosen.public_dict(),
            "spend_cap_usd": float(body.max_cost_usd),
        },
        "confidence": decision.confidence,
        "cost_usd": (
            int(cost_cents or 0) / 100
            if cost_cents is not None
            else chosen.price_per_call_usd
        ),
        "job_id": job_id,
        "charge_status": (
            "settled" if job_id and not inner.get("cached") else "cached_or_settled"
        ),
        "settlement": {
            "status": (
                "settled"
                if job_id and not inner.get("cached")
                else "cached_or_settled"
            ),
            "refund_on_failure": True,
            "ledger": "pre_call_charge -> post_call_payout/refund",
        },
        "receipt": {
            "status": "available" if job_id else "unavailable",
            "job_id": job_id,
            "signature_endpoint": f"/jobs/{job_id}/signature" if job_id else None,
            "verify_with": "aztea_verify_job",
        },
        "output": inner.get("output"),
        "latency_ms": inner.get("latency_ms"),
        "cached": bool(inner.get("cached", False)),
        "next_step": (
            f"Verify the signed receipt with aztea_verify_job(job_id='{job_id}')."
            if job_id
            else "No new receipt was created for this response."
        ),
    }
    if "rendered_output" in inner:
        response_body["rendered_output"] = inner["rendered_output"]
        response_body["rendered_output_format"] = inner.get(
            "rendered_output_format", body.output_format
        )
    _observability.record_route_decision(
        "auto_invoked", "ok",
        time.perf_counter() - _route_started_at,
    )
    return JSONResponse(content=response_body)


# ---------------------------------------------------------------------------
# Diff-watchers: /watch/* — register a target + agent + budget; sweeper fires
# the agent only when the target's fingerprint changes.
# ---------------------------------------------------------------------------


def _watcher_or_404(watcher_id: str, caller: core_models.CallerContext) -> dict:
    row = _watchers.get_watcher(watcher_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Watcher '{watcher_id}' not found.")
    if caller["type"] != "master" and row["owner_user_id"] != caller["owner_id"]:
        raise HTTPException(status_code=403, detail="Not authorized for this watcher.")
    return row


def _validate_watcher_target_url(target_kind: str, target_url: str) -> str:
    try:
        return _url_security.validate_outbound_url(target_url, "target_url")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                f"target_url is invalid: {exc}",
                {"field": "target_url", "target_kind": target_kind},
            ),
        )


def _validate_watcher_webhook_url(webhook_url: str | None) -> None:
    if not webhook_url:
        return
    try:
        _validate_hook_url(webhook_url)
    except (ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                f"delivery_webhook_url is invalid: {exc}",
                {"field": "delivery_webhook_url"},
            ),
        )


@app.post(
    "/watch",
    status_code=201,
    responses=_error_responses(400, 401, 402, 403, 404, 422, 429, 500),
    tags=["Watchers"],
    summary="Register a diff-watcher: fire an agent only when a target changes.",
)
@limiter.limit("20/minute")
def watcher_create(
    request: Request,
    body: _watchers.WatcherCreate,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    agent = registry.get_agent(body.agent_id, include_unapproved=True)
    if agent is None or not _caller_can_access_agent(caller, agent):
        raise HTTPException(
            status_code=404, detail=f"Agent '{body.agent_id}' not found."
        )
    _assert_agent_callable(body.agent_id, agent)
    safe_target = _validate_watcher_target_url(body.target_kind, body.target_url)
    _validate_watcher_webhook_url(body.delivery_webhook_url)

    # Reject watchers whose budget can't even fund a single fire — it would
    # accept the row, then immediately trip 'budget_exhausted' on the first
    # tick. Better to fail loudly at create time.
    raw_price = (
        agent.get("price_per_call_cents")
        if agent.get("price_per_call_cents") is not None
        else int(round(float(agent.get("price_per_call_usd") or 0) * 100))
    )
    try:
        per_call_cents = int(raw_price or 0)
    except (TypeError, ValueError):
        per_call_cents = 0
    if per_call_cents > 0 and body.budget_per_day_cents < per_call_cents:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                (
                    f"budget_per_day_cents ({body.budget_per_day_cents}c) is "
                    f"below this agent's per-call price ({per_call_cents}c)."
                ),
                {
                    "budget_per_day_cents": body.budget_per_day_cents,
                    "price_per_call_cents": per_call_cents,
                },
            ),
        )

    caller_owner_id = _caller_owner_id(request)
    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    row = _watchers.create_watcher(
        owner_user_id=caller["owner_id"],
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_id=body.agent_id,
        target_kind=body.target_kind,
        target_url=safe_target,
        target_meta=body.target_meta,
        on_change_policy=body.on_change_policy,
        tick_interval_seconds=body.tick_interval_seconds,
        budget_per_day_cents=body.budget_per_day_cents,
        delivery_webhook_url=body.delivery_webhook_url,
        delivery_email=body.delivery_email,
        payload=body.payload,
    )
    return JSONResponse(
        content=_watchers.watcher_to_view(row), status_code=201
    )


@app.get(
    "/watch",
    responses=_error_responses(401, 429, 500),
    tags=["Watchers"],
    summary="List the caller's watchers.",
)
@limiter.limit("60/minute")
def watcher_list(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    rows = _watchers.list_watchers_for_owner(caller["owner_id"], limit=limit)
    return JSONResponse(
        content={"watchers": [_watchers.watcher_to_view(r) for r in rows]}
    )


@app.get(
    "/watch/{watcher_id}",
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Watchers"],
    summary="Get one watcher.",
)
@limiter.limit("60/minute")
def watcher_get(
    request: Request,
    watcher_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    row = _watcher_or_404(watcher_id, caller)
    return JSONResponse(content=_watchers.watcher_to_view(row))


@app.patch(
    "/watch/{watcher_id}",
    responses=_error_responses(400, 401, 403, 404, 422, 429, 500),
    tags=["Watchers"],
    summary="Update a watcher (status / budget / interval / delivery).",
)
@limiter.limit("30/minute")
def watcher_update(
    request: Request,
    watcher_id: str,
    body: _watchers.WatcherUpdate,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    _watcher_or_404(watcher_id, caller)
    if body.delivery_webhook_url is not None:
        _validate_watcher_webhook_url(body.delivery_webhook_url)
    updated = _watchers.update_watcher(
        watcher_id,
        status=body.status,
        tick_interval_seconds=body.tick_interval_seconds,
        budget_per_day_cents=body.budget_per_day_cents,
        delivery_webhook_url=body.delivery_webhook_url,
        delivery_email=body.delivery_email,
        on_change_policy=body.on_change_policy,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Watcher '{watcher_id}' not found.")
    return JSONResponse(content=_watchers.watcher_to_view(updated))


@app.delete(
    "/watch/{watcher_id}",
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Watchers"],
    summary="Delete a watcher and its run history.",
)
@limiter.limit("30/minute")
def watcher_delete(
    request: Request,
    watcher_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    _watcher_or_404(watcher_id, caller)
    _watchers.delete_watcher(watcher_id)
    return JSONResponse(content={"deleted": True, "watcher_id": watcher_id})


@app.get(
    "/watch/{watcher_id}/runs",
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Watchers"],
    summary="List recent runs for a watcher (most recent first).",
)
@limiter.limit("60/minute")
def watcher_runs(
    request: Request,
    watcher_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    _watcher_or_404(watcher_id, caller)
    runs = _watchers.list_watcher_runs(watcher_id, limit=limit)
    return JSONResponse(content={"runs": runs})


@app.post(
    "/watch/{watcher_id}/test",
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Watchers"],
    summary="Compute the watcher's fingerprint right now without firing or billing.",
)
@limiter.limit("20/minute")
def watcher_test(
    request: Request,
    watcher_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    row = _watcher_or_404(watcher_id, caller)
    target_meta = json.loads(row.get("target_meta_json") or "{}")
    fp, err = _watchers.fingerprint_target(
        row["target_kind"], row["target_url"], target_meta
    )
    would_fire = (
        err is None
        and (
            row.get("on_change_policy") == "always"
            or fp != (row.get("last_fingerprint") or None)
        )
    )
    return JSONResponse(
        content={
            "watcher_id": watcher_id,
            "fingerprint": fp,
            "previous_fingerprint": row.get("last_fingerprint"),
            "would_fire": bool(would_fire),
            "error": err,
        }
    )
# ── Vibe-an-agent (self-serve agent generation) ───────────────────────────
# Users describe an agent in natural language with example I/O; the platform
# writes a SKILL.md, validates → safety-scans → self-tests → mints a
# probation-listed agent. Gated behind AZTEA_AGENT_GENERATION_ENABLED so OSS
# self-hosters opt in explicitly.
#
# Money flow: pre-charge max_total_cost_cents on submit; on terminal failure
# refund the full pre-charge; on success refund the unused remainder via a
# compensating ledger entry. Idempotency: (owner_id, idempotency_key) is
# UNIQUE so safe retries don't double-charge.


def _vibe_disabled_error() -> HTTPException:
    return HTTPException(
        status_code=501,
        detail=error_codes.make_error(
            "agent_generation.disabled",
            "Agent generation is disabled on this instance. Set "
            "AZTEA_AGENT_GENERATION_ENABLED=1 to enable.",
            {"docs": "docs/oss-vs-hosted.md"},
        ),
    )


def _vibe_count_today(owner_id: str) -> int:
    """Count this owner's generation attempts in the trailing 24h.

    Cheap query because of idx_gen_jobs_owner_created. We use a moving 24h
    window rather than calendar-day so a burst at 23:55 UTC doesn't reset
    five minutes later.
    """
    from core.agent_generator import persistence as _gen_persist
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = _gen_persist.list_recent_for_owner(owner_id, since_iso=cutoff)
    return len(rows)


def _vibe_result_from_row(row: dict) -> dict:
    """Render the persisted job row as the public response payload."""
    from core.agent_generator import persistence as _gen_persist
    result = _gen_persist.deserialize_result(row) or {}
    error = result.get("error") if isinstance(result, dict) else None
    return {
        "generation_job_id": row.get("generation_job_id"),
        "status": row.get("status"),
        "agent_id": row.get("agent_id") or (result.get("agent_id") if isinstance(result, dict) else None),
        "handle": result.get("handle") if isinstance(result, dict) else None,
        "skill_md": result.get("skill_md") if isinstance(result, dict) else None,
        "iterations": int(row.get("iterations") or 0),
        "qa_score": result.get("qa_score") if isinstance(result, dict) else None,
        "cost_cents_charged": int(row.get("cost_cents") or 0),
        "error": error,
    }


@app.post(
    "/agents/generate",
    status_code=202,
    responses=_error_responses(400, 401, 402, 403, 429, 501),
    tags=["Registry"],
    summary="Self-serve agent generation from a natural-language description.",
)
@limiter.limit("5/minute")
def agents_generate(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Submit a generation request. Returns 202 with the generation_job_id.

    The pipeline runs synchronously inside this request — generation is
    bounded to a few LLM calls plus a couple of skill self-test runs, well
    within reasonable HTTP timeouts.  If we ever need true async kickoff,
    the persistence + status fields are already shaped for it.
    """
    if not _feature_flags.agent_generation_enabled():
        raise _vibe_disabled_error()
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped keys cannot generate new agents.",
        )
    from core.models.agent_generation import GenerateAgentRequest
    try:
        req = GenerateAgentRequest.model_validate(body or {})
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors())
    owner_id = caller["owner_id"]
    daily_cap = _feature_flags.agent_generation_max_per_day()
    if _vibe_count_today(owner_id) >= daily_cap:
        raise HTTPException(
            status_code=429,
            detail=error_codes.make_error(
                "agent_generation.rate_limited",
                f"Per-owner limit of {daily_cap} generations / 24h reached.",
                {"limit": daily_cap},
            ),
        )

    payload = req.model_dump()
    job_row, created = _agent_generator.create_or_get_generation_job(
        owner_id=owner_id,
        idempotency_key=req.idempotency_key,
        request_payload=payload,
    )
    if not created:
        # Idempotent retry — return the existing row's status without charging.
        return JSONResponse(
            status_code=200, content=_vibe_result_from_row(job_row)
        )

    wallet = payments.get_or_create_wallet(owner_id)
    charged_by_key_id = caller.get("api_key_id") if isinstance(caller, dict) else None
    from core.agent_generator import ledger as _vibe_ledger
    try:
        charge_tx_id = _vibe_ledger.precharge_for_generation(
            caller_wallet_id=wallet["wallet_id"],
            max_cents=req.max_total_cost_cents,
            charged_by_key_id=charged_by_key_id,
        )
    except payments.InsufficientBalanceError as exc:
        from core.agent_generator import persistence as _gen_persist
        _gen_persist.update_status(
            job_row["generation_job_id"],
            status="failed",
            error_code="insufficient_funds",
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                "wallet.insufficient_funds",
                "Caller wallet cannot cover the generation budget.",
                {"required_cents": req.max_total_cost_cents},
            ),
        )

    from core.agent_generator import persistence as _gen_persist
    _gen_persist.update_status(
        job_row["generation_job_id"],
        status="running",
        charge_tx_id=charge_tx_id,
    )
    try:
        _agent_generator.generate_agent(
            generation_job_id=job_row["generation_job_id"],
            request=payload,
            owner_id=owner_id,
            caller_wallet_id=wallet["wallet_id"],
            charge_tx_id=charge_tx_id,
            max_total_cost_cents=req.max_total_cost_cents,
            charged_by_key_id=charged_by_key_id,
        )
    except Exception:
        _LOG.exception(
            "vibe.route.unexpected job=%s", job_row["generation_job_id"]
        )
    final_row = _gen_persist.get_generation_job(job_row["generation_job_id"])
    return JSONResponse(
        status_code=202, content=_vibe_result_from_row(final_row or job_row)
    )


@app.get(
    "/agents/generate/{generation_job_id}",
    responses=_error_responses(401, 403, 404, 501),
    tags=["Registry"],
    summary="Poll the status of a vibe-an-agent generation job.",
)
def agents_generate_status(
    request: Request,
    generation_job_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    if not _feature_flags.agent_generation_enabled():
        raise _vibe_disabled_error()
    _require_scope(caller, "worker")
    from core.agent_generator import persistence as _gen_persist
    row = _gen_persist.get_generation_job(generation_job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Generation job not found.")
    if caller["type"] != "master" and row.get("owner_id") != caller["owner_id"]:
        raise HTTPException(
            status_code=403, detail="Generation job belongs to a different owner."
        )
    return _vibe_result_from_row(row)
