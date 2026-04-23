# server.application shard 2 — caller-context helpers: spend caps, admin
# IP allowlist, agent authorisation, job/agent response shaping, job-message
# protocol normalisation. Pure helpers; no routes registered here.


def _caller_key_spend_cap(caller: core_models.CallerContext) -> int | None:
    if caller.get("type") != "user":
        return None
    user = caller.get("user") or {}
    raw = user.get("max_spend_cents")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _caller_key_per_job_cap(caller: core_models.CallerContext) -> int | None:
    if caller.get("type") != "user":
        return None
    user = caller.get("user") or {}
    raw = user.get("per_job_cap_cents")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _pre_call_charge_or_402(
    *,
    caller: core_models.CallerContext,
    caller_wallet_id: str,
    charge_cents: int,
    agent_id: str,
) -> str:
    try:
        return payments.pre_call_charge(
            caller_wallet_id,
            charge_cents,
            agent_id,
            charged_by_key_id=str(caller.get("key_id") or "").strip() or None,
            max_spend_cents=_caller_key_spend_cap(caller),
        )
    except payments.InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.INSUFFICIENT_FUNDS,
                "Insufficient wallet balance.",
                {
                    "balance_cents": exc.balance_cents,
                    "required_cents": exc.required_cents,
                    "wallet_id": caller_wallet_id,
                },
            ),
        )
    except payments.KeySpendLimitExceededError as exc:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.SPEND_LIMIT_EXCEEDED,
                "API key spend cap exceeded.",
                {
                    "scope": "api_key",
                    "key_id": str(caller.get("key_id") or "").strip() or None,
                    "limit_cents": exc.limit_cents,
                    "spent_cents": exc.spent_cents,
                    "attempted_cents": exc.attempted_cents,
                },
            ),
        )
    except payments.WalletDailySpendLimitExceededError as exc:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.SPEND_LIMIT_EXCEEDED,
                "Wallet daily spend cap exceeded.",
                {
                    "scope": "wallet_daily",
                    "wallet_id": caller_wallet_id,
                    "limit_cents": exc.limit_cents,
                    "spent_last_24h_cents": exc.spent_last_24h_cents,
                    "attempted_cents": exc.attempted_cents,
                },
            ),
        )


def _agent_has_verified_contract(agent: dict) -> bool:
    if "verified_contract" in agent:
        try:
            return bool(int(agent.get("verified_contract") or 0))
        except (TypeError, ValueError):
            return bool(agent.get("verified_contract"))
    try:
        return bool(int(agent.get("verified") or 0))
    except (TypeError, ValueError):
        return bool(agent.get("verified"))


def _deposit_below_minimum_error(attempted_cents: int) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=error_codes.make_error(
            error_codes.DEPOSIT_BELOW_MINIMUM,
            f"Minimum deposit is {MINIMUM_DEPOSIT_CENTS} cents.",
            {
                "minimum_cents": MINIMUM_DEPOSIT_CENTS,
                "attempted_cents": int(attempted_cents),
            },
        ),
    )


def _request_client_ip(request: Request) -> Any | None:
    host = (request.client.host if request.client else "") or ""
    try:
        direct_ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if any(direct_ip in network for network in _TRUSTED_PROXY_NETWORKS):
        forwarded_for = (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
        if forwarded_for:
            try:
                return ipaddress.ip_address(forwarded_for)
            except ValueError:
                pass
        real_ip = (request.headers.get("x-real-ip", "") or "").strip()
        if real_ip:
            try:
                return ipaddress.ip_address(real_ip)
            except ValueError:
                pass
    return direct_ip


def _require_admin_ip_allowlist(request: Request) -> None:
    if not _ADMIN_IP_ALLOWLIST_NETWORKS:
        return
    client_ip = _request_client_ip(request)
    if client_ip is None:
        raise HTTPException(status_code=403, detail="Admin endpoint access denied from this network.")
    if any(client_ip in network for network in _ADMIN_IP_ALLOWLIST_NETWORKS):
        return
    raise HTTPException(status_code=403, detail="Admin endpoint access denied from this network.")


def _get_owner_email(owner_id: str) -> str | None:
    """Return email address for a user owner_id (user:<uuid>), or None."""
    if not isinstance(owner_id, str) or not owner_id.startswith("user:"):
        return None
    user_id = owner_id[len("user:"):]
    try:
        user = _auth.get_user_by_id(user_id)
        return user.get("email") if user else None
    except Exception:
        return None


def _caller_has_scope(caller: core_models.CallerContext, required_scope: str) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return required_scope == "worker"
    scopes = {str(scope).strip().lower() for scope in (caller.get("scopes") or []) if str(scope).strip()}
    if "admin" in scopes:
        return True
    return required_scope in scopes


def _require_scope(caller: core_models.CallerContext, required_scope: str, detail: str | None = None) -> None:
    if _caller_has_scope(caller, required_scope):
        return
    scope_name = required_scope.strip().lower()
    raise HTTPException(
        status_code=403,
        detail=error_codes.make_error(
            error_codes.INSUFFICIENT_SCOPE,
            detail or f"This endpoint requires an API key with '{scope_name}' scope.",
        ),
    )


def _require_any_scope(caller: core_models.CallerContext, *scopes: str) -> None:
    """Raise 403 if the caller has none of the given scopes."""
    if any(_caller_has_scope(caller, s) for s in scopes):
        return
    joined = " or ".join(f"'{s}'" for s in scopes)
    raise HTTPException(
        status_code=403,
        detail=error_codes.make_error(
            error_codes.INSUFFICIENT_SCOPE,
            f"This endpoint requires {joined} scope.",
        ),
    )


def _proxy_headers_for_agent(agent: dict) -> dict[str, str]:
    return {"Content-Type": "application/json"}


def _proxy_response(resp: http.Response) -> Response:
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type.lower():
        try:
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except ValueError:
            pass

    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    return Response(content=resp.content, status_code=resp.status_code, headers=headers)


def _extract_caller_trust_min(input_schema: dict | None) -> float | None:
    if not isinstance(input_schema, dict):
        return None
    candidate = input_schema.get("min_caller_trust")
    if candidate is None and isinstance(input_schema.get("metadata"), dict):
        candidate = input_schema["metadata"].get("min_caller_trust")
    if candidate is None:
        return None
    try:
        value = float(candidate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value > 1.0 and value <= 100.0:
        value = value / 100.0
    if value < 0.0 or value > 1.0:
        return None
    return value


def _extract_judge_agent_id(input_schema: dict | None) -> str | None:
    if not isinstance(input_schema, dict):
        return None
    candidate = input_schema.get("judge_agent_id")
    if candidate is None and isinstance(input_schema.get("metadata"), dict):
        candidate = input_schema["metadata"].get("judge_agent_id")
    text = str(candidate or "").strip()
    return text or None


def _caller_trust_score(owner_id: str) -> float:
    try:
        return payments.get_caller_trust(owner_id)
    except Exception:
        return 0.5


def _compute_bulk_agent_stats(agent_ids: list[str]) -> dict:
    """
    Returns {agent_id: {jobs_last_30_days, job_completion_rate, median_latency_seconds}}
    for all supplied agent IDs in a single pass.
    """
    if not agent_ids:
        return {}
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    placeholders = ",".join("?" * len(agent_ids))
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT agent_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status = 'failed'   THEN 1 ELSE 0 END) AS failed
            FROM jobs
            WHERE agent_id IN ({placeholders}) AND created_at >= ?
            GROUP BY agent_id
            """,
            (*agent_ids, since),
        ).fetchall()
        latency_rows = conn.execute(
            f"""
            SELECT agent_id,
                   (julianday(completed_at) - julianday(claimed_at)) * 86400 AS latency_s
            FROM jobs
            WHERE agent_id IN ({placeholders})
              AND status = 'complete'
              AND claimed_at IS NOT NULL
              AND completed_at IS NOT NULL
              AND created_at >= ?
            ORDER BY agent_id, latency_s
            """,
            (*agent_ids, since),
        ).fetchall()
    stats: dict[str, dict] = {aid: {"jobs_last_30_days": 0, "job_completion_rate": None, "median_latency_seconds": None} for aid in agent_ids}
    for r in rows:
        aid = r["agent_id"]
        total = int(r["total"] or 0)
        completed = int(r["completed"] or 0)
        failed = int(r["failed"] or 0)
        denom = completed + failed
        stats[aid]["jobs_last_30_days"] = total
        stats[aid]["job_completion_rate"] = round(completed / denom, 4) if denom > 0 else None
    by_agent: dict[str, list] = {}
    for lr in latency_rows:
        by_agent.setdefault(lr["agent_id"], []).append(float(lr["latency_s"]))
    for aid, lats in by_agent.items():
        if lats:
            mid = len(lats) // 2
            stats[aid]["median_latency_seconds"] = round(
                lats[mid] if len(lats) % 2 else (lats[mid - 1] + lats[mid]) / 2, 2
            )
    return stats


def _agent_response(agent: dict, caller: core_models.CallerContext, stats: dict | None = None) -> dict:
    min_caller_trust = _extract_caller_trust_min(agent.get("input_schema"))
    price_cents = _usd_to_cents(agent.get("price_per_call_usd") or 0.0)
    caller_charge_cents = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=payments.PLATFORM_FEE_PCT,
        fee_bearer_policy="caller",
    )["caller_charge_cents"]
    is_internal = bool(agent.get("internal_only")) or str(agent.get("endpoint_url", "")).startswith("internal://")
    out = dict(agent) if caller.get("type") == "master" else dict(agent)
    if caller.get("type") != "master":
        out.pop("owner_id", None)
    out["caller_trust_min"] = min_caller_trust
    out["caller_charge_cents"] = caller_charge_cents
    if is_internal:
        out["last_health_status"] = "healthy"
        out["last_health_check_at"] = _utc_now_iso()
    if stats is not None:
        out["jobs_last_30_days"] = stats.get("jobs_last_30_days", 0)
        out["job_completion_rate"] = stats.get("job_completion_rate")
        out["median_latency_seconds"] = stats.get("median_latency_seconds")
    return out


def _job_response(job: dict, caller: core_models.CallerContext) -> dict:
    if caller.get("type") == "master":
        out = dict(job)
        if out.get("caller_charge_cents") is None:
            out["caller_charge_cents"] = int(out.get("price_cents") or 0)
        return out

    owner_id = caller.get("owner_id")
    result = dict(job)
    if result.get("caller_charge_cents") is None:
        result["caller_charge_cents"] = int(result.get("price_cents") or 0)
    hidden = {
        "caller_wallet_id",
        "agent_wallet_id",
        "platform_wallet_id",
        "charge_tx_id",
        "agent_owner_id",
    }
    for key in hidden:
        result.pop(key, None)

    if owner_id != job.get("caller_owner_id") and owner_id != job.get("claim_owner_id"):
        result.pop("caller_owner_id", None)
        result.pop("output_verification_decision_owner_id", None)
    if owner_id != job.get("claim_owner_id"):
        result.pop("claim_token", None)
    return result


def _caller_can_view_job(caller: core_models.CallerContext, job: dict) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return str(caller.get("agent_id") or "").strip() == str(job.get("agent_id") or "").strip()
    owner_id = caller["owner_id"]
    return owner_id == job["caller_owner_id"] or jobs.is_worker_authorized(job, owner_id)


def _resolve_parent_job_for_creation(
    caller: core_models.CallerContext,
    parent_job_id: str | None,
    *,
    parent_cascade_policy: str,
) -> dict | None:
    normalized_parent_job_id = str(parent_job_id or "").strip()
    normalized_policy = str(parent_cascade_policy or "").strip().lower() or "detach"
    if not normalized_parent_job_id:
        if normalized_policy != "detach":
            raise HTTPException(
                status_code=422,
                detail="parent_cascade_policy requires parent_job_id.",
            )
        return None

    parent = jobs.get_job(normalized_parent_job_id)
    if parent is None:
        raise HTTPException(status_code=404, detail=f"Parent job '{normalized_parent_job_id}' not found.")

    if caller["type"] == "master":
        return parent

    owner_id = caller["owner_id"]
    if owner_id not in {parent.get("caller_owner_id"), parent.get("agent_owner_id")}:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to link jobs to this parent_job_id.",
        )
    return parent


def _caller_can_manage_agent(caller: core_models.CallerContext, agent: dict) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return str(caller.get("agent_id") or "").strip() == str(agent.get("agent_id") or "").strip()
    return caller["owner_id"] == agent.get("owner_id")


def _caller_is_admin(caller: core_models.CallerContext) -> bool:
    if caller.get("type") == "master":
        return True
    scopes = caller.get("scopes") or []
    return "admin" in scopes


def _caller_can_access_agent(caller: core_models.CallerContext, agent: dict) -> bool:
    if _caller_is_admin(caller):
        return True
    if bool(agent.get("internal_only")):
        return _caller_can_manage_agent(caller, agent)
    review_status = str(agent.get("review_status") or "approved").strip().lower()
    if review_status != "approved":
        return False
    if str(agent.get("status") or "").strip().lower() == "banned":
        return False
    return True


def _assert_agent_callable(agent_id: str, agent: dict) -> None:
    if agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if agent.get("status") == "suspended":
        raise HTTPException(
            status_code=503,
            detail=error_codes.make_error(
                error_codes.AGENT_SUSPENDED,
                f"Agent '{agent_id}' is suspended.",
                {"agent_id": agent_id},
            ),
        )


def _caller_worker_authorized_for_job(caller: core_models.CallerContext, job: dict) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return str(caller.get("agent_id") or "").strip() == str(job.get("agent_id") or "").strip()
    return jobs.is_worker_authorized(job, caller["owner_id"])


def _assert_worker_claim(
    job: dict,
    caller: core_models.CallerContext,
    worker_owner_id: str,
    claim_token: str | None,
) -> None:
    if not _caller_worker_authorized_for_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized for this agent job.")
    if (job.get("claim_owner_id") or "").strip() != worker_owner_id:
        raise HTTPException(status_code=409, detail="Job is not currently claimed by this worker.")
    stored_token = (job.get("claim_token") or "").strip()
    if not stored_token:
        raise HTTPException(status_code=409, detail="Job claim token is missing.")
    if not claim_token or claim_token != stored_token:
        raise HTTPException(status_code=403, detail="Invalid or missing claim_token.")


def _to_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _job_attempts_remaining(job: dict) -> bool:
    attempt_count = _to_non_negative_int(job.get("attempt_count"), default=0)
    max_attempts = max(1, _to_non_negative_int(job.get("max_attempts"), default=1))
    return attempt_count < max_attempts


def _job_has_stale_active_lease(job: dict) -> bool:
    if job.get("status") not in {"running", "awaiting_clarification"}:
        return False
    if not (job.get("claim_owner_id") or "").strip():
        return False
    lease_expires_at = _parse_iso_datetime(job.get("lease_expires_at"))
    if lease_expires_at is None:
        return False
    return lease_expires_at <= datetime.now(timezone.utc)


def _job_supports_late_worker_grace(job: dict) -> bool:
    if (job.get("claim_owner_id") or "").strip():
        return False
    if job.get("status") not in {"pending", "failed"}:
        return False
    if _to_non_negative_int(job.get("timeout_count"), default=0) <= 0:
        return False
    return _job_attempts_remaining(job)


def _audit_master_claim_bypass(job: dict, action: str, claim_token: str | None) -> None:
    jobs.add_claim_event(
        job["job_id"],
        event_type="master_claim_bypass",
        claim_owner_id=job.get("claim_owner_id"),
        claim_token=claim_token,
        lease_expires_at=job.get("lease_expires_at"),
        actor_id="master",
        metadata={"action": action, "status": job.get("status")},
    )


def _assert_settlement_claim_or_grace(
    job: dict,
    caller: core_models.CallerContext,
    claim_token: str | None,
    action: str,
) -> None:
    actor_owner_id = caller["owner_id"]
    if caller["type"] == "master":
        _audit_master_claim_bypass(job, action=action, claim_token=claim_token)
        return

    if not _caller_worker_authorized_for_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized for this agent job.")

    if (job.get("claim_owner_id") or "").strip() == actor_owner_id:
        _assert_worker_claim(job, caller, actor_owner_id, claim_token)
        return

    if not _job_supports_late_worker_grace(job):
        raise HTTPException(status_code=409, detail="Job is not currently claimed by this worker.")
    if not claim_token:
        raise HTTPException(status_code=403, detail="Invalid or missing claim_token.")
    if not jobs.claim_token_was_recently_active(
        job["job_id"],
        claim_owner_id=actor_owner_id,
        claim_token=claim_token,
        within_seconds=_DEFAULT_LEASE_SECONDS,
    ):
        raise HTTPException(status_code=403, detail="Invalid or stale claim_token.")

    jobs.add_claim_event(
        job["job_id"],
        event_type="late_worker_grace",
        claim_owner_id=actor_owner_id,
        claim_token=claim_token,
        lease_expires_at=job.get("lease_expires_at"),
        actor_id=actor_owner_id,
        metadata={"action": action, "status": job.get("status")},
    )


def _timeout_stale_lease_at_touchpoint(job: dict, actor_owner_id: str, touchpoint: str) -> dict | None:
    if not _job_has_stale_active_lease(job):
        return None

    updated = jobs.mark_job_timeout(
        job["job_id"],
        retry_delay_seconds=_SWEEPER_RETRY_DELAY_SECONDS,
        allow_retry=True,
    )
    if updated is None:
        return None

    metadata: dict[str, Any] = {
        "touchpoint": touchpoint,
        "status_after": updated.get("status"),
    }
    if updated.get("status") == "pending":
        metadata["next_retry_at"] = updated.get("next_retry_at")

    jobs.add_claim_event(
        job["job_id"],
        event_type="touchpoint_timeout",
        claim_owner_id=job.get("claim_owner_id"),
        claim_token=job.get("claim_token"),
        lease_expires_at=job.get("lease_expires_at"),
        actor_id=actor_owner_id,
        metadata=metadata,
    )

    if updated.get("status") == "pending":
        _record_job_event(
            updated,
            "job.timeout_retry_scheduled",
            actor_owner_id=actor_owner_id,
            payload={
                "touchpoint": touchpoint,
                "retry_count": updated.get("retry_count"),
                "next_retry_at": updated.get("next_retry_at"),
            },
        )
        return updated

    return _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.timeout_terminal")


def _job_latency_ms(job: dict) -> float:
    try:
        created = datetime.fromisoformat(job["created_at"])
        completed = datetime.fromisoformat(job["completed_at"])
        return max(0.0, (completed - created).total_seconds() * 1000)
    except Exception:
        return 0.0


def _validate_json_schema_subset(payload: Any, schema: dict, path: str = "$") -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return []

    errors: list[str] = []
    schema_type = str(schema.get("type") or "").strip().lower()
    if not schema_type and isinstance(schema.get("properties"), dict):
        schema_type = "object"

    def _is_type(value: Any, expected: str) -> bool:
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "null":
            return value is None
        return True

    if schema_type:
        if not _is_type(payload, schema_type):
            errors.append(f"{path}: expected type '{schema_type}'")
            return errors

    if schema_type == "object":
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for field in required:
            key = str(field)
            if key not in payload:
                errors.append(f"{path}.{key}: required field missing")
        if isinstance(properties, dict):
            for key, field_schema in properties.items():
                if key in payload and isinstance(field_schema, dict):
                    errors.extend(
                        _validate_json_schema_subset(payload[key], field_schema, path=f"{path}.{key}")
                    )
        additional_properties = schema.get("additionalProperties")
        if additional_properties is False and isinstance(properties, dict):
            allowed = set(properties.keys())
            for key in payload.keys():
                if key not in allowed:
                    errors.append(f"{path}.{key}: additional property not allowed")
    elif schema_type == "array" and isinstance(payload, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, value in enumerate(payload):
                errors.extend(_validate_json_schema_subset(value, item_schema, path=f"{path}[{idx}]"))

    return errors


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    idx = max(0, min(len(sorted_values) - 1, math.ceil(len(sorted_values) * 0.95) - 1))
    return sorted_values[idx]


def _encode_jobs_cursor(created_at: str, job_id: str) -> str:
    raw = f"{created_at}|{job_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_jobs_cursor(cursor: str | None) -> tuple[str, str] | tuple[None, None]:
    if cursor is None:
        return None, None
    token = cursor.strip()
    if not token:
        raise HTTPException(status_code=422, detail="cursor must not be empty.")
    try:
        padded = token + ("=" * (-len(token) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        created_at, job_id = decoded.split("|", 1)
        datetime.fromisoformat(created_at)
        if not job_id.strip():
            raise ValueError("job_id missing")
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor.") from exc
    return created_at, job_id


def _normalize_protocol_artifact_list(
    raw_value: Any,
    *,
    field_name: str,
    strict: bool = True,
) -> list[dict[str, Any]]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        if strict:
            raise ValueError(f"{field_name} must be an array of artifact objects.")
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_value):
        if not isinstance(item, dict):
            if strict:
                raise ValueError(f"{field_name}[{index}] must be an object.")
            continue
        artifact = dict(item)
        name = str(artifact.get("name") or "").strip()
        mime = str(artifact.get("mime") or "").strip().lower()
        locator = str(artifact.get("url_or_base64") or "").strip()
        size_raw = artifact.get("size_bytes")
        try:
            size_bytes = int(size_raw)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f"{field_name}[{index}].size_bytes must be a non-negative integer.")
            continue
        if strict and (not name or not mime or not locator or size_bytes < 0):
            raise ValueError(
                f"{field_name}[{index}] must include non-empty name/mime/url_or_base64 and non-negative size_bytes."
            )
        if not name or not mime or not locator or size_bytes < 0:
            continue
        artifact["name"] = name
        artifact["mime"] = mime
        artifact["url_or_base64"] = locator
        artifact["size_bytes"] = size_bytes
        normalized.append(artifact)
    return normalized


def _normalize_format_preferences(raw_value: Any, *, field_name: str) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise ValueError(f"{field_name} must be an array of MIME-like format strings.")
    normalized: list[str] = []
    for item in raw_value:
        text = str(item).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normalize_protocol_channel(raw_value: Any, *, field_name: str) -> str | None:
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    if len(text) > 128:
        raise ValueError(f"{field_name} must be <= 128 characters.")
    return text


def _normalize_protocol_metadata(raw_value: Any, *, field_name: str) -> dict[str, Any]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise ValueError(f"{field_name} must be an object.")
    return dict(raw_value)


def _normalize_optional_bool(raw_value: Any, *, field_name: str) -> bool | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        lowered = raw_value.strip().lower()
        if not lowered:
            return None
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    if isinstance(raw_value, (int, float)):
        if int(raw_value) == 1:
            return True
        if int(raw_value) == 0:
            return False
    raise ValueError(f"{field_name} must be a boolean.")


def _merge_protocol_input_envelope(
    payload: dict[str, Any],
    *,
    input_artifacts: list[dict[str, Any]] | None = None,
    preferred_input_formats: list[str] | None = None,
    preferred_output_formats: list[str] | None = None,
    communication_channel: str | None = None,
    protocol_metadata: dict[str, Any] | None = None,
    private_task: bool | None = None,
) -> dict[str, Any]:
    updated = dict(payload or {})
    current_protocol = updated.get("protocol")
    protocol = dict(current_protocol) if isinstance(current_protocol, dict) else {}
    if input_artifacts:
        protocol["input_artifacts"] = list(input_artifacts)
    if preferred_input_formats:
        protocol["preferred_input_formats"] = list(preferred_input_formats)
    if preferred_output_formats:
        protocol["preferred_output_formats"] = list(preferred_output_formats)
    if communication_channel:
        protocol["communication_channel"] = communication_channel
    if private_task is not None:
        protocol["private_task"] = bool(private_task)
    if protocol_metadata:
        existing_metadata = protocol.get("metadata")
        merged_metadata = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
        merged_metadata.update(protocol_metadata)
        protocol["metadata"] = merged_metadata
    if protocol:
        updated["protocol"] = protocol
    return updated


def _merge_protocol_output_envelope(
    payload: dict[str, Any],
    *,
    output_artifacts: list[dict[str, Any]] | None = None,
    output_format: str | None = None,
    protocol_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updated = dict(payload or {})
    current_protocol = updated.get("protocol")
    protocol = dict(current_protocol) if isinstance(current_protocol, dict) else {}
    if output_artifacts:
        protocol["output_artifacts"] = list(output_artifacts)
    if output_format:
        protocol["output_format"] = output_format
    if protocol_metadata:
        existing_metadata = protocol.get("metadata")
        merged_metadata = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
        merged_metadata.update(protocol_metadata)
        protocol["metadata"] = merged_metadata
    if protocol:
        updated["protocol"] = protocol
    return updated


