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


@app.post("/skills/validate", responses=_error_responses(400, 401, 413))
@limiter.limit("30/minute")
def skills_validate(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    _require_scope(caller, "worker")
    raw_md = str((body or {}).get("skill_md") or "")
    if not raw_md.strip():
        raise HTTPException(status_code=400, detail="skill_md is required.")
    if len(raw_md.encode("utf-8")) > 256 * 1024:
        raise HTTPException(status_code=413, detail="skill_md exceeds 256 KB.")
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


@app.post("/skills", status_code=201, responses=_error_responses(400, 401, 403, 409, 413, 429))
@limiter.limit("10/minute")
def skills_create(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> dict:
    """Upload a SKILL.md, register an agent for it, and persist the skill row."""
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(status_code=403, detail="Agent-scoped keys cannot register hosted skills.")
    payload = body or {}
    raw_md = str(payload.get("skill_md") or "")
    if not raw_md.strip():
        raise HTTPException(status_code=400, detail="skill_md is required.")
    if len(raw_md.encode("utf-8")) > 256 * 1024:
        raise HTTPException(status_code=413, detail="skill_md exceeds 256 KB.")
    try:
        price_per_call_usd = float(payload.get("price_per_call_usd"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="price_per_call_usd is required and must be a number.")
    if not (price_per_call_usd >= 0.0 and price_per_call_usd <= 25.0):
        raise HTTPException(status_code=400, detail="price_per_call_usd must be between 0 and 25.")

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
        raise HTTPException(status_code=400, detail="Skill name is empty after parsing.")
    if not description:
        raise HTTPException(status_code=400, detail="Skill description is empty after parsing.")

    # Optional override fields the builder may set in the request body.
    requested_temperature = payload.get("temperature")
    requested_max_tokens = payload.get("max_output_tokens")
    requested_chain = payload.get("model_chain")
    if requested_chain is not None and not isinstance(requested_chain, list):
        raise HTTPException(status_code=400, detail="model_chain must be a list of strings.")

    # Resolve a unique listing name. ``agents.name`` has a UNIQUE constraint,
    # so we retry with a numeric suffix when a collision occurs.
    candidate_name = display_name
    agent_id: str | None = None
    last_error: Exception | None = None
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
                review_status="approved",
                review_note="Auto-approved hosted skill.",
                reviewed_at=_utc_now_iso(),
                reviewed_by="system:auto-approve-hosted-skill",
            )
            break
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except sqlite3.IntegrityError as exc:
            last_error = exc
            candidate_name = f"{display_name} #{attempt + 1}"
    if agent_id is None:
        raise HTTPException(status_code=409, detail=f"Could not allocate a unique name (last: {last_error}).")

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
            temperature=float(requested_temperature) if requested_temperature is not None else 0.2,
            max_output_tokens=int(requested_max_tokens) if requested_max_tokens is not None else 1500,
        )
    except Exception:
        # Roll back the agent registration so we never leave a half-persisted skill.
        try:
            registry.delist_agent(agent_id, caller["owner_id"])
        except Exception:
            _LOG.exception("Failed to roll back agent %s after hosted_skills insert failure.", agent_id)
        raise

    # Now rewrite the agent's endpoint_url to point at the just-created skill_id.
    # ``update_agent`` doesn't expose ``endpoint_url`` because external agents
    # are forbidden from changing it after registration. For hosted skills we
    # write it once at creation time.
    final_endpoint = _hosted_skills.make_skill_endpoint_url(skill_row["skill_id"])
    with get_db_connection() as _conn:
        _conn.execute(
            "UPDATE agents SET endpoint_url = ? WHERE agent_id = ? AND owner_id = ?",
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
        _LOG.warning("Failed to send skill-live email for skill %s", skill_row.get("skill_id"))

    return JSONResponse(
        content={
            "skill_id": skill_row["skill_id"],
            "agent_id": agent_id,
            "endpoint_url": final_endpoint,
            "name": candidate_name,
            "price_per_call_usd": price_per_call_usd,
            "review_status": "approved",
            "warnings": parsed.warnings,
            "message": "Skill is live. Callers can hire it now.",
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
        raise HTTPException(status_code=403, detail="Skill belongs to a different owner.")
    return _skill_response(row, include_raw_md=True)


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
        raise HTTPException(status_code=403, detail="Skill belongs to a different owner.")
    # Delist the agent first so callers stop seeing it; then remove the skill row.
    try:
        registry.delist_agent(row["agent_id"], row["owner_id"])
    except Exception:
        _LOG.exception("Failed to delist agent %s during skill delete.", row["agent_id"])
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
