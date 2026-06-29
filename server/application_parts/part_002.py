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
    except payments.WalletSessionBudgetExceededError as exc:
        # 2026-05-19 (B3): server-side session budget. Distinct code
        # (wallet.session_budget_exceeded) so callers can branch on
        # "tighten my session" vs "extend my daily allowance".
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.WALLET_SESSION_BUDGET_EXCEEDED,
                "Wallet session budget exceeded.",
                {
                    "scope": "wallet_session",
                    "wallet_id": caller_wallet_id,
                    "limit_cents": exc.limit_cents,
                    "session_spent_cents": exc.session_spent_cents,
                    "attempted_cents": exc.attempted_cents,
                    "next_step": (
                        "Increase or reset the cap via "
                        "POST /wallets/{wallet_id}/set_session_budget "
                        "(reset_counter=true to restart the window)."
                    ),
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
            f"Minimum deposit is ${MINIMUM_DEPOSIT_CENTS / 100:.2f}.",
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
        forwarded_for = (
            (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
        )
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
        raise HTTPException(
            status_code=403, detail="Admin endpoint access denied from this network."
        )
    if any(client_ip in network for network in _ADMIN_IP_ALLOWLIST_NETWORKS):
        return
    raise HTTPException(
        status_code=403, detail="Admin endpoint access denied from this network."
    )


def _get_owner_email(owner_id: str) -> str | None:
    """Return email address for a user owner_id (user:<uuid>), or None."""
    if not isinstance(owner_id, str) or not owner_id.startswith("user:"):
        return None
    user_id = owner_id[len("user:") :]
    try:
        user = _auth.get_user_by_id(user_id)
        return user.get("email") if user else None
    except Exception:
        return None


def _admin_email_allowlist() -> set[str]:
    """Emails (lowercased) granted admin regardless of their key's stored scopes.

    Why: session keys are re-minted on login with DEFAULT_KEY_SCOPES, so admin
    granted on a key doesn't survive the next sign-in. Deriving admin from the
    user's email instead makes it durable — set ADMIN_EMAILS=a@x,b@y and restart.
    """
    raw = os.environ.get("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _caller_email(caller: core_models.CallerContext) -> str | None:
    user = caller.get("user") if isinstance(caller, dict) else None
    email = user.get("email") if isinstance(user, dict) else None
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


def _caller_is_admin_email(caller: core_models.CallerContext) -> bool:
    allow = _admin_email_allowlist()
    if not allow:
        return False
    email = _caller_email(caller)
    return email is not None and email in allow


def _caller_has_scope(caller: core_models.CallerContext, required_scope: str) -> bool:
    if caller["type"] == "master":
        return True
    # Email-based admin allowlist: durable across session-key re-minting.
    if _caller_is_admin_email(caller):
        return True
    if caller["type"] == "agent_key":
        return required_scope == "worker"
    scopes = {
        str(scope).strip().lower()
        for scope in (caller.get("scopes") or [])
        if str(scope).strip()
    }
    if "admin" in scopes:
        return True
    return required_scope in scopes


def _require_scope(
    caller: core_models.CallerContext, required_scope: str, detail: str | None = None
) -> None:
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


def _require_admin_caller(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.CallerContext:
    """Route-level dependency: hard 403 BEFORE body parse for admin routes.

    Why this exists separately from the in-body ``_require_scope`` call:
    FastAPI runs Pydantic body validation at the same time as parameter
    dependencies, so a malformed admin-route body returns 422 even when the
    caller has no admin scope. Mounting this as ``dependencies=[Depends(...)]``
    on the route decorator forces the scope check to run before body parsing,
    so callers see 403 (not 422) and can't probe the body schema unauthenticated.
    """
    _require_scope(
        caller, "admin", detail="This endpoint requires admin scope."
    )
    return caller


def _proxy_headers_for_agent(
    agent: dict,
    *,
    body: bytes | None = None,
    job_id: str | None = None,
    caller_owner_id: str | None = None,
) -> dict[str, str]:
    """Side-effect-free: assemble outbound headers, optionally signing the body.

    The signing path activates when the agent has an ``endpoint_signing_secret``
    column (Plan B Phase 1, migration 0074) AND the caller supplied the raw
    body bytes. Agents missing a secret (legacy, pre-migration) get unsigned
    headers — back-compat path.

    Signature scheme mirrors ``core/watchers/delivery.py``: HMAC-SHA256 over
    ``f"{timestamp}.{body}"`` so a captured signature can't be replayed against
    a different body or at a different time. The seller verifies with
    ``core.crypto.verify_endpoint_request`` (re-exported by the SDK).
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    secret = agent.get("endpoint_signing_secret") if isinstance(agent, dict) else None
    if secret and isinstance(body, (bytes, bytearray)):
        from core import crypto as _crypto
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers["X-Aztea-Signature"] = _crypto.sign_endpoint_request(
            bytes(body), secret, timestamp,
        )
        headers["X-Aztea-Timestamp"] = timestamp
        if job_id:
            headers["X-Aztea-Job-Id"] = str(job_id)
        if caller_owner_id:
            headers["X-Aztea-Caller"] = str(caller_owner_id)
    return headers


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


_bulk_stats_cache: dict | None = None
_bulk_stats_cache_at: float = 0.0
_BULK_STATS_TTL = 30.0  # seconds


def _compute_bulk_agent_stats(agent_ids: list[str]) -> dict:
    """
    Returns {agent_id: {jobs_last_30_days, job_completion_rate, median_latency_seconds}}
    for all supplied agent IDs in a single pass.
    Results are cached for 30 seconds to avoid re-scanning the jobs table on every page load.
    """
    global _bulk_stats_cache, _bulk_stats_cache_at
    import time as _time

    now = _time.monotonic()
    if _bulk_stats_cache is not None and (now - _bulk_stats_cache_at) < _BULK_STATS_TTL:
        cached = _bulk_stats_cache
        # Return only the requested IDs from the cache (may be a subset)
        return {aid: cached[aid] for aid in agent_ids if aid in cached}

    if not agent_ids:
        return {}
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    placeholders = ",".join(["%s"] * len(agent_ids))
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT agent_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status = 'failed'   THEN 1 ELSE 0 END) AS failed
            FROM jobs
            WHERE agent_id IN ({placeholders}) AND created_at >= %s
            GROUP BY agent_id
            """,
            (*agent_ids, since),
        ).fetchall()
        latency_expr = (
            "EXTRACT(EPOCH FROM (completed_at::timestamptz - claimed_at::timestamptz))"
            if _db.IS_POSTGRES
            else "(julianday(completed_at) - julianday(claimed_at)) * 86400"
        )
        latency_rows = conn.execute(
            f"""
            SELECT agent_id,
                   {latency_expr} AS latency_s
            FROM jobs
            WHERE agent_id IN ({placeholders})
              AND status = 'complete'
              AND claimed_at IS NOT NULL
              AND completed_at IS NOT NULL
              AND created_at >= %s
            ORDER BY agent_id, latency_s
            """,
            (*agent_ids, since),
        ).fetchall()
    stats: dict[str, dict] = {
        aid: {
            "jobs_last_30_days": 0,
            "job_completion_rate": None,
            "median_latency_seconds": None,
        }
        for aid in agent_ids
    }
    for r in rows:
        aid = r["agent_id"]
        total = int(r["total"] or 0)
        completed = int(r["completed"] or 0)
        failed = int(r["failed"] or 0)
        denom = completed + failed
        stats[aid]["jobs_last_30_days"] = total
        stats[aid]["job_completion_rate"] = (
            round(completed / denom, 4) if denom > 0 else None
        )
    by_agent: dict[str, list] = {}
    for lr in latency_rows:
        by_agent.setdefault(lr["agent_id"], []).append(float(lr["latency_s"]))
    for aid, lats in by_agent.items():
        if lats:
            mid = len(lats) // 2
            stats[aid]["median_latency_seconds"] = round(
                lats[mid] if len(lats) % 2 else (lats[mid - 1] + lats[mid]) / 2, 2
            )
    _bulk_stats_cache = stats
    _bulk_stats_cache_at = _time.monotonic()
    return stats


def _agent_response(
    agent: dict, caller: core_models.CallerContext | None, stats: dict | None = None
) -> dict:
    min_caller_trust = _extract_caller_trust_min(agent.get("input_schema"))
    price_cents = _usd_to_cents(agent.get("price_per_call_usd") or 0.0)
    caller_charge_cents = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=payments.PLATFORM_FEE_PCT,
        fee_bearer_policy="caller",
    )["caller_charge_cents"]
    is_internal = bool(agent.get("internal_only")) or str(
        agent.get("endpoint_url", "")
    ).startswith("internal://")
    caller_type = (caller or {}).get("type")
    out = dict(agent)
    if caller_type != "master":
        out.pop("owner_id", None)
    # Phase 5 (red-team 2026-05-19): the envelope contract test caught
    # ``signing_private_key`` (the agent's Ed25519 private PEM) leaking
    # via ``dict(agent)`` passthrough — every catalog read was emitting
    # the key in plaintext. Strip the private key + any related signing
    # secrets from every caller's view; the public_key + DID identify
    # the agent cryptographically and stay in the response.
    for _sensitive_agent_field in (
        "signing_private_key",
        "signing_private_key_pem",
        "signing_secret",
        "callback_secret",
        # 2026-05-27 (Plan B Phase 1): per-agent HMAC secret used to sign
        # outbound calls. The seller saw this exactly once at registration
        # (or after a rotate). Never re-surface it on any list/detail read.
        "endpoint_signing_secret",
        "endpoint_signing_secret_rotated_at",
    ):
        out.pop(_sensitive_agent_field, None)
    out["caller_trust_min"] = min_caller_trust
    out["caller_charge_cents"] = caller_charge_cents
    # U-H1 (audit 2026-05-20): list_agents was missing the governance
    # fields surfaced by do_specialist_task's candidate listing. Add
    # caller_total_usd, platform_fee_pct, and pricing transparency for
    # tiered/per_unit agents so discovery matches the actual charge.
    _fee_pct = int(payments.PLATFORM_FEE_PCT)
    out["platform_fee_pct"] = _fee_pct
    out["caller_total_usd"] = round(caller_charge_cents / 100.0, 4)
    try:
        from server.builtin_agents import pricing_overlay as _pricing_overlay
        _overlay = _pricing_overlay.get_pricing_overlay().get(
            str(agent.get("agent_id") or "")
        )
    except Exception:
        _overlay = None
    if _overlay:
        _pricing_model = str(_overlay.get("pricing_model") or "").lower()
        _pricing_config = _overlay.get("pricing_config") or {}
        if _pricing_model and _pricing_model not in ("fixed", "per_call"):
            out["pricing_model"] = _pricing_model
            _summary: dict[str, Any] = {}
            for _key in (
                "unit", "input_field", "rate_cents_per_unit",
                "min_cents", "max_cents", "tiers",
            ):
                if _key in _pricing_config:
                    _summary[_key] = _pricing_config[_key]
            if _summary:
                out["pricing_summary"] = _summary
            out["price_is_floor"] = True
    # U-H1 (cont): tag broken agents (consistent low success_rate) so
    # discovery + auto-hire can deprioritize them. Tier is derived only
    # when we have enough signal (>=10 calls). Operator-set
    # stability_tier on the spec wins.
    _explicit_tier = str(agent.get("stability_tier") or "").strip().lower()
    if _explicit_tier == "broken":
        out["stability_tier"] = "broken"
        out["broken_reason"] = str(
            agent.get("broken_reason") or "operator-marked broken"
        )
    else:
        _total_calls = int(agent.get("total_calls") or 0)
        _success_rate_raw = agent.get("success_rate")
        try:
            _success_rate = (
                float(_success_rate_raw) if _success_rate_raw is not None else None
            )
        except (TypeError, ValueError):
            _success_rate = None
        if (
            _total_calls >= 10
            and _success_rate is not None
            and _success_rate < 0.30
            and not _explicit_tier
        ):
            out["stability_tier"] = "broken"
            out["broken_reason"] = (
                f"success_rate {_success_rate:.0%} below 30% over "
                f"{_total_calls} calls — auto-hire deprioritized"
            )
    builtin_meta = _builtin_specs.builtin_catalog_metadata(
        str(agent.get("agent_id") or "")
    )
    if builtin_meta:
        out.update(
            {key: value for key, value in builtin_meta.items() if value is not None}
        )
    # Strip stored work examples for sensitive agents — privacy gate applies to reads, not just writes.
    _SENSITIVE_IDS = frozenset(
        {"1021c65c-d2bf-54ff-823a-897f9deb1029"}
    )  # secret_scanner
    if (
        str(out.get("agent_id") or "") in _SENSITIVE_IDS
        or bool(out.get("examples_sensitive"))
        or str(out.get("category") or "").strip().lower() == "security"
    ):
        out.pop("output_examples", None)
    if is_internal:
        out["last_health_status"] = "healthy"
        out["last_health_check_at"] = _utc_now_iso()
    if stats is not None:
        out["jobs_last_30_days"] = stats.get("jobs_last_30_days", 0)
        out["job_completion_rate"] = stats.get("job_completion_rate")
        out["median_latency_seconds"] = stats.get("median_latency_seconds")
    # Expose a `trust_breakdown` view of the reputation metrics so callers
    # can see WHY a trust score is what it is. Pre-2026-05-08 only the
    # rolled-up score was visible, which led the eval to grade reputation
    # "B" for compression — without the components, callers couldn't tell
    # whether a 51 reflected slow latency, low ratings, or low success.
    # Aliases an existing field; no DB or migration change.
    _rep = out.get("reputation")
    if isinstance(_rep, dict):
        out["trust_breakdown"] = {
            "trust_score": _rep.get("trust_score"),
            "quality_score": _rep.get("quality_score"),
            "success_score": _rep.get("success_score"),
            "latency_score": _rep.get("latency_score"),
            "confidence_score": _rep.get("confidence_score"),
            "rating_count": _rep.get("rating_count"),
            "total_calls": _rep.get("total_calls"),
            "successful_calls": _rep.get("successful_calls"),
            "avg_latency_ms": _rep.get("avg_latency_ms"),
        }
    return out


def _job_response(
    job: dict,
    caller: core_models.CallerContext,
    *,
    output_mode: str = "summary",
    disputable_signals: dict | None = None,
) -> dict:
    from core import feature_flags as _feature_flags
    from core import output_shaping as _output_shaping

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
        # F2 (red-team 2026-05-19): callback_secret was leaking back to the
        # caller in JobResponse despite the schema docstring promising
        # "Sent only on creation; never echoed back". Treat it the same
        # as the wallet ids — pre-existing on the row but stripped from
        # the response.
        "callback_secret",
    }
    for key in hidden:
        result.pop(key, None)

    if owner_id != job.get("caller_owner_id") and owner_id != job.get("claim_owner_id"):
        result.pop("caller_owner_id", None)
        result.pop("output_verification_decision_owner_id", None)
    if owner_id != job.get("claim_owner_id"):
        result.pop("claim_token", None)
    if _feature_flags.OUTPUT_TRUNCATION and "output_payload" in result:
        shaped_output, truncated = _output_shaping.shape_output(
            result.get("output_payload"),
            output_mode,
        )
        result["output_payload"] = shaped_output
        if truncated:
            result["output_truncated"] = True
            if job.get("job_id"):
                result["full_output_available"] = True
                result["full_output_path"] = f"/jobs/{job['job_id']}/full"
                result["full_output_hint"] = (
                    "Call aztea_job(action='full_output', job_id=..., offset=0, "
                    "limit=20000) to fetch chunks."
                )

    # Annotate dispute eligibility so the CLI/SDK can render a picker without
    # re-implementing the predicate. Lazy-fetches signals when caller didn't
    # pre-batch them; list endpoints should pre-batch (see _bulk_disputable_signals).
    _attach_disputable(result, job, owner_id, signals=disputable_signals)
    return result


def _attach_disputable(
    result: dict,
    job: dict,
    owner_id: str | None,
    *,
    signals: dict | None,
) -> None:
    from core import disputes as _disputes
    from core import reputation as _reputation
    from core.jobs import disputable as _disputable

    job_id = job.get("job_id")
    if not job_id:
        return
    if signals is not None:
        has_existing_dispute = bool(signals.get("has_dispute"))
        has_quality_rating = bool(signals.get("has_rating"))
    else:
        has_existing_dispute = _disputes.has_dispute_for_job(job_id)
        has_quality_rating = _reputation.get_job_quality_rating(job_id) is not None
    deadline = _dispute_window_deadline(job)
    reason = _disputable.is_disputable(
        job,
        deadline=deadline,
        has_existing_dispute=has_existing_dispute,
        has_quality_rating=has_quality_rating,
    )
    result["disputable"] = reason is None
    result["disputable_reason"] = reason.message if reason else None
    result["disputable_code"] = reason.code if reason else None


def _caller_can_view_job(caller: core_models.CallerContext, job: dict) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return (
            str(caller.get("agent_id") or "").strip()
            == str(job.get("agent_id") or "").strip()
        )
    owner_id = caller["owner_id"]
    return owner_id == job["caller_owner_id"] or jobs.is_worker_authorized(
        job, owner_id
    )


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
        raise HTTPException(
            status_code=404,
            detail=f"Parent job '{normalized_parent_job_id}' not found.",
        )

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
        return (
            str(caller.get("agent_id") or "").strip()
            == str(agent.get("agent_id") or "").strip()
        )
    return caller["owner_id"] == agent.get("owner_id")


def _caller_is_admin(caller: core_models.CallerContext) -> bool:
    if caller.get("type") == "master":
        return True
    scopes = caller.get("scopes") or []
    return "admin" in scopes


def _caller_can_access_agent(caller: core_models.CallerContext, agent: dict) -> bool:
    if _caller_is_admin(caller):
        return True
    # Sunset agents stay callable by direct slug/agent_id so existing receipts
    # and integrations don't break; they're filtered out of list_agents,
    # search, MCP manifest, and auto-hire elsewhere (see part_007).
    if bool(agent.get("internal_only")):
        return _caller_can_manage_agent(caller, agent)
    review_status = str(agent.get("review_status") or "approved").strip().lower()
    # 'probation' is intentionally accessible: probationary listings ARE
    # live and callable by direct slug/agent_id; the soft gate lives in
    # core/registry/auto_hire.py (rank penalty + $1.00 price cap on
    # unsolicited auto-invoke). Treating probation as inaccessible here
    # would amount to silently rejecting them, which defeats the purpose.
    #
    # 'sunset' is also accessible at this gate so the call hot path can
    # reach _assert_agent_callable and emit a clean HTTP 410 ``agent.sunset``
    # — matching the legacy frozenset behavior. Without this, sunset agents
    # would surface a 404 ``agent.not_found`` which is misleading (the row
    # exists; it has been retired).
    if review_status not in {"approved", "probation", "sunset"}:
        return False
    # F7 (red-team 2026-05-19): list_agents excludes BOTH 'banned' and
    # 'suspended' (see core/registry/agents_ops.py:746) but the call gate
    # used to only block 'banned'. Result: suspended agents were hidden
    # from discovery yet still accepted jobs and charged callers. The
    # call hot path must mirror the list filter; ops staff suspending an
    # agent expect every surface (catalog + call) to honor the gate.
    if str(agent.get("status") or "").strip().lower() in {"banned", "suspended"}:
        return False
    return True


def _assert_agent_callable(agent_id: str, agent: dict) -> None:
    endpoint = str(agent.get("endpoint_url") or "").strip()
    agent_id_str = str(agent_id).strip()
    is_internal_builtin = (
        agent_id_str in _BUILTIN_AGENT_IDS and endpoint.startswith("internal://")
    )
    # Sunset agents (json_schema_validator, regex_tester, etc.) live in the DB
    # for receipt resolution but have no internal endpoint. Without this check
    # we'd dispatch into a missing handler and surface a confusing 502
    # `agent.endpoint_misconfigured`. Return a clean 410 Gone instead.
    sunset_via_review_status = (
        str(agent.get("review_status") or "").strip().lower() == "sunset"
    )
    if agent_id_str in _SUNSET_DEPRECATED_AGENT_IDS or sunset_via_review_status:
        raise HTTPException(
            status_code=410,
            detail=error_codes.make_error(
                error_codes.AGENT_SUNSET,
                f"Agent '{agent_id}' was removed from the public catalog and is no longer callable.",
                {"agent_id": agent_id, "deprecated": True},
            ),
        )
    if agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if agent.get("status") == "suspended":
        if is_internal_builtin:
            registry.set_agent_status(agent_id, "active")
            agent["status"] = "active"
            return
        raise HTTPException(
            status_code=503,
            detail=error_codes.make_error(
                error_codes.AGENT_SUSPENDED,
                f"Agent '{agent_id}' is suspended.",
                {"agent_id": agent_id},
            ),
        )


def _caller_worker_authorized_for_job(
    caller: core_models.CallerContext, job: dict
) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return (
            str(caller.get("agent_id") or "").strip()
            == str(job.get("agent_id") or "").strip()
        )
    return jobs.is_worker_authorized(job, caller["owner_id"])


def _assert_worker_claim(
    job: dict,
    caller: core_models.CallerContext,
    worker_owner_id: str,
    claim_token: str | None,
) -> None:
    if not _caller_worker_authorized_for_job(caller, job):
        raise HTTPException(
            status_code=403, detail="Not authorized for this agent job."
        )
    if (job.get("claim_owner_id") or "").strip() != worker_owner_id:
        raise HTTPException(
            status_code=409, detail="Job is not currently claimed by this worker."
        )
    stored_token = (job.get("claim_token") or "").strip()
    if not stored_token:
        raise HTTPException(status_code=409, detail="Job claim token is missing.")
    # Constant-time comparison to avoid leaking the token via timing.
    if not claim_token or not hmac.compare_digest(claim_token, stored_token):
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                error_codes.JOB_INVALID_CLAIM_TOKEN,
                "claim_token is invalid or missing. Re-claim the job to mint a fresh token — POST /jobs/{job_id}/claim.",
                {"job_id": job["job_id"]},
            ),
        )


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
        raise HTTPException(
            status_code=403, detail="Not authorized for this agent job."
        )

    if (job.get("claim_owner_id") or "").strip() == actor_owner_id:
        _assert_worker_claim(job, caller, actor_owner_id, claim_token)
        return

    if not _job_supports_late_worker_grace(job):
        raise HTTPException(
            status_code=409, detail="Job is not currently claimed by this worker."
        )
    if not claim_token:
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error(
                error_codes.JOB_INVALID_CLAIM_TOKEN,
                "claim_token is invalid or missing. Re-claim the job to mint a fresh token — POST /jobs/{job_id}/claim.",
                {"job_id": job["job_id"]},
            ),
        )
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


def _timeout_stale_lease_at_touchpoint(
    job: dict, actor_owner_id: str, touchpoint: str
) -> dict | None:
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

    return _settle_failed_job(
        updated, actor_owner_id=actor_owner_id, event_type="job.timeout_terminal"
    )


def _job_latency_ms(job: dict) -> float:
    try:
        created = datetime.fromisoformat(job["created_at"])
        completed = datetime.fromisoformat(job["completed_at"])
        return max(0.0, (completed - created).total_seconds() * 1000)
    except Exception:
        return 0.0


def _validate_json_schema_subset(
    payload: Any, schema: dict, path: str = "$"
) -> list[str]:
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
            return (
                isinstance(value, int) and not isinstance(value, bool)
            ) or isinstance(value, float)
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
        properties = (
            schema.get("properties")
            if isinstance(schema.get("properties"), dict)
            else {}
        )
        required = (
            schema.get("required") if isinstance(schema.get("required"), list) else []
        )
        for field in required:
            key = str(field)
            if key not in payload:
                errors.append(f"{path}.{key}: required field missing")
        if isinstance(properties, dict):
            for key, field_schema in properties.items():
                if key in payload and isinstance(field_schema, dict):
                    errors.extend(
                        _validate_json_schema_subset(
                            payload[key], field_schema, path=f"{path}.{key}"
                        )
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
                errors.extend(
                    _validate_json_schema_subset(
                        value, item_schema, path=f"{path}[{idx}]"
                    )
                )

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
                raise ValueError(
                    f"{field_name}[{index}].size_bytes must be a non-negative integer."
                )
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
        merged_metadata = (
            dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
        )
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
        merged_metadata = (
            dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
        )
        merged_metadata.update(protocol_metadata)
        protocol["metadata"] = merged_metadata
    if protocol:
        updated["protocol"] = protocol
    return updated
