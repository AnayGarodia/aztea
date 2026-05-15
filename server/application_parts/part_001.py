# server.application shard 1 — migrations, FastAPI app + lifespan setup,
# exception handlers, CORS, /api/* compat middleware, security headers,
# request tracing, prometheus metrics, /metrics endpoint, auth helpers.


def _migrate_job_event_deliveries_status_schema(conn: _db.DbConnection) -> None:
    # sqlite_master is SQLite-only; Postgres uses information_schema. In Postgres
    # mode the migration SQL files handle schema evolution so we skip this helper.
    if _db.IS_POSTGRES:
        return
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_event_deliveries'"
    ).fetchone()
    if row is None:
        return
    table_sql = str(row["sql"] or "").lower()
    if (
        "dead_letter" not in table_sql
        and "retrying" not in table_sql
        and "'failed'" in table_sql
        and "'cancelled'" in table_sql
    ):
        return

    conn.execute(
        """
        CREATE TABLE job_event_deliveries_new (
            delivery_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id            INTEGER NOT NULL,
            hook_id             TEXT NOT NULL,
            owner_id            TEXT NOT NULL,
            target_url          TEXT NOT NULL,
            secret              TEXT,
            payload             TEXT NOT NULL,
            status              TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'failed', 'cancelled')),
            attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            next_attempt_at     TEXT NOT NULL,
            last_attempt_at     TEXT,
            last_success_at     TEXT,
            last_status_code    INTEGER,
            last_error          TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            UNIQUE(event_id, hook_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO job_event_deliveries_new (
            delivery_id, event_id, hook_id, owner_id, target_url, secret, payload, status,
            attempt_count, next_attempt_at, last_attempt_at, last_success_at, last_status_code,
            last_error, created_at, updated_at
        )
        SELECT
            delivery_id,
            event_id,
            hook_id,
            owner_id,
            target_url,
            secret,
            payload,
            CASE
                WHEN status = 'retrying' THEN 'pending'
                WHEN status = 'dead_letter' THEN 'failed'
                WHEN status IN ('pending', 'delivered', 'failed', 'cancelled') THEN status
                ELSE 'pending'
            END AS status,
            attempt_count,
            next_attempt_at,
            last_attempt_at,
            last_success_at,
            last_status_code,
            last_error,
            created_at,
            updated_at
        FROM job_event_deliveries
        """
    )
    conn.execute("DROP TABLE job_event_deliveries")
    conn.execute("ALTER TABLE job_event_deliveries_new RENAME TO job_event_deliveries")


def _init_ops_db() -> None:
    if _db.IS_POSTGRES:
        return
    with jobs._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id            TEXT NOT NULL,
                agent_id          TEXT NOT NULL,
                agent_owner_id    TEXT NOT NULL,
                caller_owner_id   TEXT NOT NULL,
                event_type        TEXT NOT NULL,
                actor_owner_id    TEXT,
                payload           TEXT NOT NULL DEFAULT '{}',
                created_at        TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_event_hooks (
                hook_id            TEXT PRIMARY KEY,
                owner_id           TEXT NOT NULL,
                target_url         TEXT NOT NULL,
                secret             TEXT,
                is_active          INTEGER NOT NULL DEFAULT 1,
                created_at         TEXT NOT NULL,
                last_attempt_at    TEXT,
                last_success_at    TEXT,
                last_status_code   INTEGER,
                last_error         TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_event_deliveries (
                delivery_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id            INTEGER NOT NULL,
                hook_id             TEXT NOT NULL,
                owner_id            TEXT NOT NULL,
                target_url          TEXT NOT NULL,
                secret              TEXT,
                payload             TEXT NOT NULL,
                status              TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'failed', 'cancelled')),
                attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                next_attempt_at     TEXT NOT NULL,
                last_attempt_at     TEXT,
                last_success_at     TEXT,
                last_status_code    INTEGER,
                last_error          TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                UNIQUE(event_id, hook_id)
            )
            """
        )
        _migrate_job_event_deliveries_status_schema(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_owner_created ON job_events(caller_owner_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_agent_owner_created ON job_events(agent_owner_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_job_created ON job_events(job_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_hooks_owner_active ON job_event_hooks(owner_id, is_active)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_event_deliveries_status_due
            ON job_event_deliveries(status, next_attempt_at, delivery_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_event_deliveries_owner_created
            ON job_event_deliveries(owner_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_requests (
                request_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id         TEXT NOT NULL,
                scope            TEXT NOT NULL,
                idempotency_key  TEXT NOT NULL,
                request_hash     TEXT NOT NULL,
                status           TEXT NOT NULL CHECK(status IN ('in_progress', 'completed')),
                response_status  INTEGER,
                response_body    TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                UNIQUE(owner_id, scope, idempotency_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_updated ON idempotency_requests(updated_at DESC)"
        )


def _init_stripe_db() -> None:
    """Create Stripe bookkeeping tables used for top-ups and webhook idempotency."""
    if _db.IS_POSTGRES:
        return
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_sessions (
                session_id    TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL,
                processed_at  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_webhook_events (
                session_id    TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL,
                status        TEXT NOT NULL CHECK(status IN ('processing', 'processed', 'failed')),
                attempts      INTEGER NOT NULL DEFAULT 0,
                last_error    TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stripe_webhook_events_status_updated "
            "ON stripe_webhook_events(status, updated_at DESC)"
        )


# ---------------------------------------------------------------------------
# Startup — register built-in agents
# ---------------------------------------------------------------------------


def _output_schema_object(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties)}
    if required:
        schema["required"] = list(required)
    return schema


def _quality_judge_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "input_payload": {"type": "object"},
            "output_payload": {"type": "object"},
            "agent_description": {"type": "string"},
        },
        "required": ["input_payload", "output_payload"],
    }


def _builtin_agent_specs() -> list[dict[str, Any]]:
    return _builtin_specs.builtin_agent_specs()


def _name_owned_by_other_user(name: str, system_owner_id: str) -> bool:
    """True iff an agent with this name exists owned by a non-system user.

    Used to gate builtin auto-registration: we want the builtin to land in
    the DB whenever no NON-system user has claimed the name. A stale
    system-owned row with the same name doesn't matter — we'd simply have
    re-registered it on a previous deploy. Without this scoping, a single
    duplicate row anywhere in the DB silently blocks every future deploy
    from registering newly curated builtins (the eval on 2026-05-03 found
    DNS, browser, multi-language, semantic-search, AI red teamer, and
    visual regression all missing from the prod catalog because of this).
    """
    with registry._conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM agents WHERE name = %s AND owner_id != %s LIMIT 1",
            (name, system_owner_id),
        ).fetchone()
    return row is not None


def _ensure_system_user() -> str:
    with _auth._conn() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE username = %s ORDER BY created_at ASC LIMIT 1",
            (_SYSTEM_USERNAME,),
        ).fetchone()
        if existing is not None:
            user_id = str(existing["user_id"])
            conn.execute(
                "UPDATE users SET status = 'suspended' WHERE user_id = %s", (user_id,)
            )
            return user_id

        user_id = str(uuid.uuid4())
        now = _utc_now_iso()
        email = _SYSTEM_USER_EMAIL
        if (
            conn.execute(
                "SELECT 1 FROM users WHERE email = %s LIMIT 1", (email,)
            ).fetchone()
            is not None
        ):
            email = f"system-{user_id[:8]}@aztea.internal"
        salt = "system-account-disabled"
        password_hash = hashlib.sha256(f"{user_id}:{salt}".encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO users (user_id, username, email, password_hash, salt, created_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'suspended')
            """,
            (user_id, _SYSTEM_USERNAME, email, password_hash, salt, now),
        )
        return user_id


def ensure_builtin_agents_registered() -> None:
    system_user_id = _ensure_system_user()
    system_owner_id = f"user:{system_user_id}"
    specs = _builtin_agent_specs()
    managed_ids = {
        str(spec.get("agent_id") or "").strip()
        for spec in specs
        if str(spec.get("agent_id") or "").strip()
    }
    # Self-heal: any curated built-in stuck in `suspended` from a prior
    # run gets reactivated on startup. The auto-suspender now excludes
    # curated builtins (see part_005._auto_suspend_low_performing_agents)
    # but historic suspensions persist in the DB until cleared. Running
    # this on every boot means a deploy is always sufficient to recover
    # — no manual SQL UPDATE needed when an eval flips a builtin to
    # suspended via failure-rate accumulation.
    if managed_ids:
        try:
            with registry._conn() as conn:
                placeholders = ",".join(["%s"] * len(managed_ids))
                conn.execute(
                    f"""
                    UPDATE agents
                    SET status = 'active', suspension_reason = NULL
                    WHERE status = 'suspended'
                      AND agent_id IN ({placeholders})
                    """,
                    tuple(managed_ids),
                )
        except Exception:
            _LOG.exception(
                "Failed to auto-heal suspended built-in agents at startup; "
                "search may exclude them until manually cleared."
            )
    # Install the per-agent match/block keyword overlay used by the search
    # ranker so jargon queries (SBOM, IMDS, ReDoS, prototype pollution,
    # log4shell, ...) route to the right agent. Overlay lives in core/
    # but is sourced from the built-in specs the server owns.
    try:
        from core.registry.agents_ops import set_routing_overlay

        set_routing_overlay(
            match_keywords={
                str(spec.get("agent_id") or ""): list(spec.get("match_keywords") or [])
                for spec in specs
                if spec.get("match_keywords")
            },
            block_keywords={
                str(spec.get("agent_id") or ""): list(spec.get("block_keywords") or [])
                for spec in specs
                if spec.get("block_keywords")
            },
        )
    except Exception:
        _LOG.exception("Failed to install routing keyword overlay; search ranking degraded.")
    now = _utc_now_iso()

    for spec in specs:
        existing = registry.get_agent(spec["agent_id"])
        output_examples = spec.get("output_examples")
        output_examples_json = None
        if isinstance(output_examples, list):
            output_examples_json = (
                json.dumps([item for item in output_examples if isinstance(item, dict)])
                or None
            )
        if existing is None:
            # Historical guard: skip registration if another agent already
            # uses this name. We keep it scoped to non-system owners so a
            # stale user-registered agent doesn't block a curated builtin
            # from being added on a later deploy. Builtins use deterministic
            # UUID v5 IDs so the agent_id collision check above is the
            # authoritative one — name uniqueness is enforced per-owner via
            # DB constraints, not as a blanket gate here.
            if _name_owned_by_other_user(spec["name"], system_owner_id):
                continue
            registry.register_agent(
                agent_id=spec["agent_id"],
                name=spec["name"],
                description=spec["description"],
                endpoint_url=spec["endpoint_url"],
                price_per_call_usd=float(spec.get("price_per_call_usd", 0.01)),
                tags=spec["tags"],
                input_schema=spec["input_schema"],
                output_schema=spec["output_schema"],
                output_verifier_url=None,
                output_examples=output_examples,
                internal_only=bool(spec.get("internal_only", False)),
                status="active",
                owner_id=system_owner_id,
                embed_listing=False,
                model_provider="groq",
                model_id="llama-3.3-70b-versatile",
                kind="aztea_built",
                cacheable=spec.get("cacheable"),
            )
            continue

        with registry._conn() as conn:
            conn.execute(
                """
                UPDATE agents
                SET owner_id = %s,
                    name = %s,
                    description = %s,
                    endpoint_url = %s,
                    price_per_call_usd = %s,
                    tags = %s,
                    input_schema = %s,
                    output_schema = %s,
                    output_examples = %s,
                    internal_only = %s,
                    cacheable = %s,
                    status = 'active',
                    review_status = 'approved',
                    reviewed_by = %s,
                    reviewed_at = %s,
                    model_provider = %s,
                    model_id = %s,
                    kind = 'aztea_built'
                WHERE agent_id = %s
                """,
                (
                    system_owner_id,
                    spec["name"],
                    spec["description"],
                    spec["endpoint_url"],
                    float(spec.get("price_per_call_usd", 0.01)),
                    json.dumps(spec.get("tags") or []),
                    json.dumps(spec.get("input_schema") or {}, sort_keys=True),
                    json.dumps(spec.get("output_schema") or {}, sort_keys=True),
                    output_examples_json,
                    1 if bool(spec.get("internal_only", False)) else 0,
                    None
                    if spec.get("cacheable") is None
                    else (1 if bool(spec.get("cacheable")) else 0),
                    _SYSTEM_USERNAME,
                    now,
                    "groq",
                    "llama-3.3-70b-versatile",
                    spec["agent_id"],
                ),
            )

    registry.backfill_agent_signing_keys(list(managed_ids), now)

    # Defensive idempotent re-activation: every agent in the curated public
    # set must have status=active, review_status=approved, internal_only=0.
    # Without this, a single past deploy that suspended a curated builtin
    # leaves it stranded in 'suspended' state forever — the UPDATE path in
    # the main loop sets status='active' but only fires when the spec row is
    # newly inserted; if the row already existed and was simply marked
    # suspended later, the resurrection never happened. (Bug found in prod:
    # shell_executor was suspended despite being in CURATED_PUBLIC.)
    with registry._conn() as conn:
        # Seed every managed builtin (public + sunset). Sunset agents are
        # hidden from list_agents but must remain approved/active so direct
        # slug/agent_id calls (and historical receipts referencing them)
        # continue to resolve cleanly.
        for curated_id in _CURATED_BUILTIN_AGENT_IDS - {_QUALITY_JUDGE_AGENT_ID}:
            conn.execute(
                """
                UPDATE agents
                SET status = 'active',
                    review_status = 'approved',
                    internal_only = 0
                WHERE agent_id = %s
                  AND (status != 'active'
                       OR review_status != 'approved'
                       OR internal_only != 0)
                """,
                (curated_id,),
            )

    deprecated_ids = _BUILTIN_AGENT_IDS - managed_ids
    for agent_id in deprecated_ids:
        stale = registry.get_agent(agent_id, include_unapproved=True)
        if (
            stale is not None
            and str(stale.get("status") or "").strip().lower() != "suspended"
        ):
            registry.set_agent_status(agent_id, "suspended")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _ENVIRONMENT == "production" and not _ADMIN_IP_ALLOWLIST_NETWORKS:
        _LOG.warning(
            "ADMIN_IP_ALLOWLIST is not set. Admin routes are accessible from any IP. "
            "Set ADMIN_IP_ALLOWLIST=<cidr>,... to restrict access in production."
        )
    if _ENVIRONMENT == "production":
        if not os.environ.get("API_KEY", "").strip():
            raise RuntimeError("API_KEY must be set when ENVIRONMENT=production.")
        _sbu = (os.environ.get("SERVER_BASE_URL", "") or "").strip()
        if _sbu and not _sbu.lower().startswith("https://"):
            _LOG.warning(
                "SERVER_BASE_URL should use https in production (current: %s). "
                "Behind TLS-terminating reverse proxies, set this to the public https URL.",
                _sbu[:64],
            )
    apply_migrations(jobs.DB_PATH)
    registry.init_db()
    payments.init_payments_db()
    _auth.init_auth_db()
    jobs.init_jobs_db()
    disputes.init_disputes_db()
    reputation.init_reputation_db()
    compare.init_db()
    pipelines.init_db()
    _init_ops_db()
    _init_stripe_db()
    ensure_builtin_agents_registered()
    recipes.ensure_builtin_recipes()

    # 1.7.7 — clear stale claim-state on pending jobs FIRST. Pre-1.7.7
    # debugging found ~7 jobs stuck in pending with `claim_owner_id` and
    # `claim_token` still populated from a previous claim that didn't
    # clean up. lease_expires_at was None so the lease was technically
    # expired, but downstream code paths still saw the dirty claim
    # fields and quietly refused to re-claim. Reset them.
    try:
        with registry._conn() as conn:
            res = conn.execute(
                """
                UPDATE jobs
                SET claim_owner_id = NULL,
                    claim_token = NULL,
                    claimed_at = NULL,
                    lease_expires_at = NULL,
                    last_heartbeat_at = NULL,
                    updated_at = updated_at
                WHERE status = 'pending'
                  AND (claim_owner_id IS NOT NULL OR claim_token IS NOT NULL)
                """,
            )
            cleared = int(getattr(res, "rowcount", 0) or 0)
        if cleared:
            _LOG.warning(
                "startup_pending_claim_clear: reset claim_owner/token on "
                "%d pending jobs (legacy 1.7.4 wedge leftover)",
                cleared,
            )
    except Exception:
        _LOG.exception("startup_pending_claim_clear: top-level failure (ignored)")

    # 1.7.5.1 — startup cleanup: fail any pending job older than 1 hour.
    # The 1.7.4 ThreadPoolExecutor bug left a wedge of regex/SAST/diff
    # jobs cycling claim → timeout → re-pending forever, starving worker
    # capacity (1.7.5 prod symptom: queue stuck at 18 even after worker
    # restart). One-shot sweep on each deploy guarantees a clean slate
    # — pending-older-than-1h jobs are stale by any reasonable definition
    # (worker's tick interval is 1s; legitimate jobs claim within seconds).
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with registry._conn() as conn:
            stuck_rows = conn.execute(
                """
                SELECT job_id, agent_id, caller_charge_cents, caller_wallet_id,
                       charge_tx_id, agent_wallet_id, platform_wallet_id,
                       status, completed_at, settled_at
                FROM jobs
                WHERE status = 'pending'
                  AND claim_owner_id IS NULL
                  AND created_at < %s
                """,
                (cutoff,),
            ).fetchall()
        stuck = [dict(r) for r in stuck_rows]
        if stuck:
            _LOG.warning(
                "startup_pending_sweep: failing %d pending jobs older than 1h",
                len(stuck),
            )
            # _settle_failed_job lives in another shard (part_005). The shards
            # share namespace via server.application compilation, so by the
            # time lifespan() runs the name is in module globals. A direct
            # `from server.application_parts.part_005 import _settle_failed_job`
            # fails because part_005.py is not standalone-importable. We pick
            # the live binding via `globals()`.
            _settle = globals().get("_settle_failed_job")
            for j in stuck:
                try:
                    updated = jobs.update_job_status(
                        j["job_id"],
                        "failed",
                        error_message=(
                            "Failed by startup sweep: this job was pending "
                            "more than 1 hour without ever being claimed. "
                            "Likely orphaned from a previous deploy. Refunded."
                        ),
                        completed=True,
                    )
                    if updated is not None and _settle is not None:
                        _settle(
                            updated,
                            actor_owner_id="system:startup-sweep",
                            event_type="job.failed_stale_pending",
                        )
                except Exception:
                    _LOG.exception(
                        "startup_pending_sweep: failed to drain job %s",
                        j.get("job_id"),
                    )
        else:
            _LOG.info("startup_pending_sweep: no stale pending jobs to drain")
    except Exception:
        _LOG.exception("startup_pending_sweep: top-level failure (ignored)")

    # Optional warm-up of sentence-transformers MiniLM. With uvicorn's 3
    # worker processes each independently loading ~80MB of weights at
    # startup, a t-class EC2 instance OOM-kills the workers (silent
    # SIGKILL — no Python traceback, just "Child process died" in the
    # uvicorn supervisor). Default OFF so prod stays stable; opt in via
    # AZTEA_WARM_EMBEDDINGS=1 on hosts with enough RAM, or pre-load a
    # singleton in a forked-once pattern under gunicorn for the real
    # cache-miss-latency win. Lazy load on first request still works.
    if (
        _feature_flags.flag("AZTEA_WARM_EMBEDDINGS", default=False)
        and not _feature_flags.DISABLE_EMBEDDINGS
    ):
        try:
            embeddings._local_model()
        except Exception:
            _LOG.exception(
                "Embedding model warm-up failed; semantic search will degrade gracefully on first hit."
            )

    # 1.7.2 — N15 cache-state observability. If the result cache is off
    # in this process (env override of AZTEA_RESULT_CACHE_V2), surface
    # it loudly at startup so journalctl shows "cache off" before we
    # discover via "10× identical calls → 10× charges" in a wallet audit.
    if _feature_flags.RESULT_CACHE_V2:
        _LOG.info("result_cache.enabled  AZTEA_RESULT_CACHE_V2=on")
    else:
        _LOG.warning(
            "result_cache.disabled  AZTEA_RESULT_CACHE_V2=off — "
            "identical-input calls will NOT hit the cache; every call "
            "will settle a fresh charge. Check env config."
        )

    _set_server_shutting_down(False)
    stop_event: threading.Event | None = None
    sweeper_thread: threading.Thread | None = None
    hook_stop_event: threading.Event | None = None
    hook_thread: threading.Thread | None = None
    builtin_stop_event: threading.Event | None = None
    builtin_thread: threading.Thread | None = None
    dispute_judge_stop_event: threading.Event | None = None
    dispute_judge_thread: threading.Thread | None = None
    payments_reconciliation_stop_event: threading.Event | None = None
    payments_reconciliation_thread: threading.Thread | None = None
    is_background_worker_leader = _acquire_background_worker_lock()
    if not is_background_worker_leader:
        _LOG.info(
            "Background workers disabled in this process; another worker owns the lock."
        )

    if is_background_worker_leader and _SWEEPER_ENABLED:
        stop_event = threading.Event()
        sweeper_thread = threading.Thread(
            target=_jobs_sweeper_loop,
            args=(stop_event,),
            daemon=True,
            name="aztea-job-sweeper",
        )
        sweeper_thread.start()
    else:
        _set_sweeper_state(running=False)

    if is_background_worker_leader and _HOOK_DELIVERY_ENABLED:
        hook_stop_event = threading.Event()
        hook_thread = threading.Thread(
            target=_hook_delivery_loop,
            args=(hook_stop_event,),
            daemon=True,
            name="aztea-hook-delivery",
        )
        hook_thread.start()
    else:
        _set_hook_worker_state(running=False)

    if is_background_worker_leader and _BUILTIN_JOB_WORKER_ENABLED:
        builtin_stop_event = threading.Event()
        builtin_thread = threading.Thread(
            target=_builtin_worker_loop,
            args=(builtin_stop_event,),
            daemon=True,
            name="aztea-builtin-worker",
        )
        builtin_thread.start()
    else:
        _set_builtin_worker_state(running=False)

    if is_background_worker_leader and _DISPUTE_JUDGE_ENABLED:
        dispute_judge_stop_event = threading.Event()
        dispute_judge_thread = threading.Thread(
            target=_dispute_judge_loop,
            args=(dispute_judge_stop_event,),
            daemon=True,
            name="aztea-dispute-judge",
        )
        dispute_judge_thread.start()
    else:
        _set_dispute_judge_state(running=False)

    agent_health_stop_event: threading.Event | None = None
    agent_health_thread: threading.Thread | None = None
    if is_background_worker_leader and _AGENT_HEALTH_CHECK_ENABLED:
        agent_health_stop_event = threading.Event()
        agent_health_thread = threading.Thread(
            target=_agent_health_loop,
            args=(agent_health_stop_event,),
            daemon=True,
            name="aztea-agent-health",
        )
        agent_health_thread.start()

    watchers_stop_event: threading.Event | None = None
    watchers_thread: threading.Thread | None = None
    if is_background_worker_leader and _watchers_sweeper.WATCHERS_ENABLED:
        watchers_stop_event = threading.Event()
        watchers_thread = threading.Thread(
            target=_watchers_sweeper.watchers_sweeper_loop,
            args=(watchers_stop_event,),
            daemon=True,
            name="aztea-watchers-sweeper",
        )
        watchers_thread.start()

    if is_background_worker_leader and _PAYMENTS_RECONCILIATION_ENABLED:
        payments_reconciliation_stop_event = threading.Event()
        payments_reconciliation_thread = threading.Thread(
            target=_payments_reconciliation_loop,
            args=(payments_reconciliation_stop_event,),
            daemon=True,
            name="aztea-payments-reconciliation",
        )
        payments_reconciliation_thread.start()
    else:
        _set_payments_reconciliation_state(running=False)

    if os.environ.get("OPENAI_API_KEY"):
        try:
            embeddings.embed_text("warmup")
        except Exception as exc:
            _LOG.warning("Embedding warmup failed: %s", exc)

    try:
        yield
    finally:
        _set_server_shutting_down(True)
        drain_deadline = time.monotonic() + _SHUTDOWN_DRAIN_TIMEOUT_SECONDS
        while time.monotonic() < drain_deadline:
            if _inflight_requests_count() <= 0:
                break
            await asyncio.sleep(0.05)
        if stop_event is not None:
            stop_event.set()
        if sweeper_thread is not None:
            sweeper_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if hook_stop_event is not None:
            hook_stop_event.set()
        if hook_thread is not None:
            hook_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if builtin_stop_event is not None:
            builtin_stop_event.set()
        if builtin_thread is not None:
            builtin_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if dispute_judge_stop_event is not None:
            dispute_judge_stop_event.set()
        if dispute_judge_thread is not None:
            dispute_judge_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if payments_reconciliation_stop_event is not None:
            payments_reconciliation_stop_event.set()
        if payments_reconciliation_thread is not None:
            payments_reconciliation_thread.join(
                timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS
            )
        if agent_health_stop_event is not None:
            agent_health_stop_event.set()
        if agent_health_thread is not None:
            agent_health_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if watchers_stop_event is not None:
            watchers_stop_event.set()
        if watchers_thread is not None:
            watchers_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if is_background_worker_leader:
            _release_background_worker_lock()
        _close_all_db_connections()


# ---------------------------------------------------------------------------
# Rate limiter — keyed per caller identity
# ---------------------------------------------------------------------------


def _key_from_request(request: Request) -> str:
    caller = _resolve_caller(request)
    if caller:
        if caller["type"] == "master":
            return "master"
        key_id = str(caller.get("key_id") or "").strip()
        if key_id:
            return f"key:{key_id}"
        return caller["owner_id"]
    client_ip = _request_client_ip(request)
    return str(client_ip) if client_ip is not None else "unknown"


limiter = Limiter(key_func=_key_from_request, default_limits=[_DEFAULT_RATE_LIMIT])
app = FastAPI(
    title="aztea v1",
    lifespan=lifespan,
    # The SPA owns /docs (it's the user-facing documentation page).
    # Move FastAPI's swagger UI under /api/docs so it doesn't shadow the SPA.
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
app.state.limiter = limiter

register_exception_handlers(app, logger=_LOG)

# CORS — origins come from CORS_ALLOW_ORIGINS env var (comma-separated).
# Defaults include common local dev ports.  In production, set the env var
# to your deployed frontend origin(s), e.g.:
#   CORS_ALLOW_ORIGINS=https://aztea.dev,https://www.aztea.dev
_cors_env = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
elif _ENVIRONMENT == "production":
    _cors_origins = []
else:
    _cors_origins = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ]
# Production safety: refuse wildcard CORS in production deployments.
if _ENVIRONMENT == "production" and "*" in _cors_origins:
    raise RuntimeError(
        "CORS_ALLOW_ORIGINS must not contain '*' when ENVIRONMENT=production."
    )
# Always include the configured frontend base URL so Stripe redirects work.
if _FRONTEND_BASE_URL and _FRONTEND_BASE_URL not in _cors_origins:
    _cors_origins.append(_FRONTEND_BASE_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    max_age=600,
)

def _health_check_db() -> str:
    """Run a tiny SELECT 1 against the configured DB. Returns 'ok' or 'error'."""
    try:
        with _db.get_db_connection() as conn:
            row = conn.execute("SELECT 1").fetchone()
        if row is None:
            return "error"
        return "ok"
    except Exception as exc:
        _LOG.warning("health: DB probe failed: %s", exc)
        return "error"


def _health_available_llm_providers() -> list[str]:
    """Return the names of registered LLM providers that are currently usable.

    Pure read of the in-memory registry — no network calls.
    """
    try:
        from core.llm import registry as _llm_registry

        return [
            entry["name"]
            for entry in _llm_registry.list_providers()
            if entry.get("available")
        ]
    except Exception as exc:
        _LOG.warning("health: llm registry probe failed: %s", exc)
        return []


@app.get("/health", include_in_schema=False)
def health_endpoint():
    """Liveness + dependency-status probe. Always returns HTTP 200.

    The body's ``status`` field flips to ``degraded`` if any sub-check failed,
    so external monitors can alert on the JSON without parsing HTTP codes.
    Registered before the legacy system-router /health so this lighter,
    SDK-friendly contract takes precedence on FastAPI's first-match resolution.
    """
    db_status = _health_check_db()
    providers = _health_available_llm_providers()
    overall = "ok" if db_status == "ok" else "degraded"
    return JSONResponse(
        {
            "status": overall,
            "db": db_status,
            "llm_providers": providers,
            "version": SERVER_VERSION,
        }
    )


app.include_router(_system_routes.router)

# Trust X-Forwarded-For only from explicitly trusted proxy networks.
# TRUSTED_PROXY_IPS defaults to loopback for local reverse-proxy setups.
_TRUSTED_PROXY_NETWORKS = _parse_ip_allowlist(
    "TRUSTED_PROXY_IPS",
    os.environ.get("TRUSTED_PROXY_IPS", "127.0.0.1"),
)


# ---------------------------------------------------------------------------
# Middleware — security headers + request size cap
# ---------------------------------------------------------------------------

# Paths the React Router owns whose first segment ALSO matches a real API
# route (`/jobs/{id}`, `/agents/{id}`, `/wallets/me`, etc.). On a browser
# hard-reload the request lands on the API route first and returns a raw
# JSON 401 to the user. This middleware short-circuits those navigations
# (Accept: text/html, GET, no Authorization header) by serving index.html
# so the SPA can render the page and fetch its own data with the saved key.
_SPA_OWNED_PATHS: tuple[str, ...] = (
    "/agents",
    "/jobs",
    "/wallet",
    "/wallets",
    "/worker",
    "/overview",
    "/settings",
    "/keys",
    "/my-agents",
    "/register-agent",
    "/list-skill",
    "/platform",
    "/integrations",
    "/admin/disputes",
    "/admin/earnings",
    "/docs",
    "/demos",
    "/welcome",
    "/terms",
    "/privacy",
    "/legal/accept",
)


def _is_browser_navigation(request: Request) -> bool:
    """Heuristic: GET request from a browser navigation, not an XHR/fetch.

    Browsers send Accept: text/html on top-level navigations. XHR/fetch
    callers either omit Accept or send application/json. The Authorization
    header is also a strong negative signal — SDK callers always include it
    and would never want HTML back. Sec-Fetch-Mode=navigate is the modern
    canonical signal but isn't sent by every browser, so we OR it with the
    Accept check.
    """
    if request.method != "GET":
        return False
    if request.headers.get("Authorization"):
        return False
    if request.headers.get("X-Requested-With"):  # legacy XHR signal
        return False
    sec_fetch_mode = (request.headers.get("Sec-Fetch-Mode") or "").lower()
    if sec_fetch_mode == "navigate":
        return True
    accept = (request.headers.get("Accept") or "").lower()
    return "text/html" in accept


def _path_is_spa_owned(path: str) -> bool:
    if not path or path == "/":
        return False
    for prefix in _SPA_OWNED_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


@app.middleware("http")
async def spa_navigation_passthrough(request: Request, call_next):
    """Serve index.html for SPA-owned paths on hard reload.

    Without this, hard-reloading e.g. ``/jobs/abc-123`` lands on the real
    ``GET /jobs/{job_id}`` API endpoint, which 401s and renders raw JSON in
    the browser tab.
    """
    path = request.scope.get("path") or ""
    if _is_browser_navigation(request) and _path_is_spa_owned(path):
        index_file = _FRONTEND_DIST_DIR / "index.html"
        if index_file.is_file():
            return _SpaFileResponse(
                index_file,
                status_code=200,
                media_type="text/html",
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
            )
    return await call_next(request)


@app.middleware("http")
async def api_prefix_compat(request: Request, call_next):
    """Transparently strip a leading ``/api`` from incoming requests.

    The canonical nginx layout proxies ``/api/*`` to uvicorn and rewrites the
    prefix so FastAPI receives ``/auth/...``, ``/jobs/...``, etc. Some setups
    (dev servers, alternative reverse proxies, single-host Docker compose)
    forward the prefix verbatim. Without this shim every such request would
    404 because no FastAPI route is registered under ``/api``.

    The middleware rewrites ``request.scope`` before routing runs, so the
    Pydantic body/dependency machinery and the real handler all see the
    canonical path. The public API surface is therefore ``/api/<path>`` and
    ``/<path>`` — fully interchangeable.
    """
    path = request.scope.get("path") or ""
    # FastAPI registers swagger UI under `/api/docs` / `/api/redoc` /
    # `/api/openapi.json` (intentional — docs URL must not shadow the SPA's
    # /docs route). Stripping the /api prefix on those routes would 404
    # them because the actual route is registered with the prefix in the
    # path. Skip the rewrite for these specific swagger paths so they
    # resolve to the registered handler. (Pre-1.6.9 buyers got
    # `Not Found: /openapi.json` when calling /api/openapi.json.)
    _SWAGGER_PATHS = ("/api/openapi.json", "/api/docs", "/api/redoc")
    if path in _SWAGGER_PATHS or any(path.startswith(p + "/") for p in _SWAGGER_PATHS):
        return await call_next(request)
    if path == "/api" or path.startswith("/api/"):
        rewritten = path[4:] or "/"
        request.scope["path"] = rewritten
        request.scope["raw_path"] = rewritten.encode("utf-8")
    return await call_next(request)


@app.middleware("http")
async def shutdown_draining(request: Request, call_next):
    _inc_inflight_requests()
    try:
        return await call_next(request)
    finally:
        _dec_inflight_requests()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    has_primary = (request.headers.get(_PROTOCOL_VERSION_HEADER, "") or "").strip()
    has_legacy = (
        request.headers.get(_LEGACY_PROTOCOL_VERSION_HEADER, "") or ""
    ).strip()
    if not (has_primary or has_legacy):
        logging_utils.log_event(
            _LOG,
            logging.WARNING,
            "request.missing_protocol_header",
            {
                "header": _PROTOCOL_VERSION_HEADER,
                "method": request.method,
                "path": request.url.path,
            },
        )
    response = await call_next(request)
    response.headers[_PROTOCOL_VERSION_HEADER] = _PROTOCOL_VERSION
    response.headers[_LEGACY_PROTOCOL_VERSION_HEADER] = _PROTOCOL_VERSION
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # P3 fix: HSTS. The DNS Inspector flagged aztea.ai as missing the
    # Strict-Transport-Security header. Add it here so it's enforced
    # consistently regardless of whether traffic comes through Caddy or hits
    # uvicorn directly. 1 year, all subdomains, eligible for browser preload.
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains; preload"
    )
    return response


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl:
        try:
            content_length = int(cl)
        except ValueError:
            return JSONResponse(
                content=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    "Invalid Content-Length header.",
                ),
                status_code=400,
            )
        if content_length > _MAX_BODY_BYTES:
            return JSONResponse(
                content=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    f"Request body too large (max {_MAX_BODY_BYTES // 1024} KB).",
                ),
                status_code=413,
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        Counter,
        Histogram,
        generate_latest,
    )

    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False

if _PROM_AVAILABLE:
    _prom_requests_total = Counter(
        "aztea_http_requests_total",
        "Total HTTP requests",
        ["method", "path", "status"],
    )
    _prom_request_latency = Histogram(
        "aztea_http_request_duration_seconds",
        "HTTP request latency",
        ["method", "path"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )

    def _metrics_path_label(request: Request) -> str:
        route = request.scope.get("route")
        route_path = getattr(route, "path", None)
        if isinstance(route_path, str) and route_path.strip():
            return route_path
        return request.url.path

    @app.middleware("http")
    async def prometheus_middleware(request: Request, call_next):
        raw_path = request.url.path
        # Don't instrument /metrics itself to avoid recursion noise
        if raw_path == "/metrics":
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        latency = time.perf_counter() - start
        path = _metrics_path_label(request)
        _prom_requests_total.labels(
            method=request.method, path=path, status=response.status_code
        ).inc()
        _prom_request_latency.labels(method=request.method, path=path).observe(latency)
        return response


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint(request: Request):
    """Prometheus-compatible metrics. Restricted to internal/admin callers."""
    allow_cidr = os.environ.get("METRICS_ALLOW_CIDR", "127.0.0.1/32")
    client_ip = _request_client_ip(request)
    try:
        network = ipaddress.ip_network(allow_cidr, strict=False)
        if client_ip is None or client_ip not in network:
            raise HTTPException(status_code=403, detail="Forbidden")
    except ValueError:
        pass
    if not _PROM_AVAILABLE:
        return JSONResponse(
            {"error": "prometheus_client not installed"}, status_code=503
        )
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def _parse_incoming_request_id(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s or len(s) > 128:
        return None
    if not re.match(r"^[A-Za-z0-9._:@-]+$", s):
        return None
    return s


# ---------------------------------------------------------------------------
# Transport-layer rate limiting (per-key sliding window + 1s burst gate).
# Runs INSIDE request_tracing so 429 responses still get an X-Request-ID,
# and BEFORE any auth-dependent route handler so an exhausted-quota client
# never triggers a DB lookup it can't afford anyway.
# ---------------------------------------------------------------------------


def _rate_limit_key(request: Request, bearer_key: str | None) -> str:
    """Pure-ish: return the accounting key — the bearer if present, else IP.

    Anonymous traffic keys by client IP so a single bad actor cannot hide
    behind dropping the Authorization header. Falls back to the literal
    ``ip:unknown`` token only when the client address cannot be parsed,
    which the middleware logs at warning level.
    """
    if bearer_key:
        return f"key:{bearer_key}"
    client_ip = _request_client_ip(request)
    return f"ip:{client_ip}" if client_ip is not None else "ip:unknown"


def _rate_limit_response(decision: _rate_limit.Decision) -> JSONResponse:
    """Pure: build the documented 429 JSON body + Retry-After header."""
    retry = max(1, int(decision.retry_after_seconds))
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "message": f"Too many requests. Retry after {retry} seconds.",
            "details": {
                "limit_per_minute": decision.limit_per_minute,
                "burst_limit_per_second": decision.burst_limit_per_second,
                "retry_after_seconds": retry,
            },
        },
        headers={"Retry-After": str(retry)},
    )


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Fail-open per-key sliding-window rate limiter.

    Why: any exception in this path must never block legitimate traffic — a
    broken rate limiter is much less harmful than a rate limiter that 500s
    every request. The outer try/except logs a structured warning and
    passes the request through unchanged.
    """
    try:
        path = request.url.path or ""
        if _rate_limit.is_path_exempt(path):
            return await call_next(request)
        bearer_key = _rate_limit.extract_bearer_key(
            request.headers.get("Authorization")
        )
        scope = _rate_limit.classify(bearer_key, _MASTER_KEY)
        if scope == _rate_limit.SCOPE_ADMIN:
            return await call_next(request)
        if request.headers.get("Authorization") and bearer_key is None:
            logging_utils.log_event(
                _LOG,
                logging.WARNING,
                "ratelimit.bearer_malformed",
                {"path": path, "method": request.method},
            )
        key = _rate_limit_key(request, bearer_key)
        decision = _rate_limit.check_and_record(key, scope)
        if not decision.allowed:
            return _rate_limit_response(decision)
    except Exception as exc:  # noqa: BLE001 — middleware must fail open
        logging_utils.log_event(
            _LOG,
            logging.WARNING,
            "ratelimit.fail_open",
            {"error": str(exc), "path": request.url.path},
        )
    return await call_next(request)


@app.middleware("http")
async def request_tracing(request: Request, call_next):
    incoming = _parse_incoming_request_id(request.headers.get("X-Request-ID"))
    request_id = incoming or str(uuid.uuid4())
    request.state.request_id = request_id
    token: Token = logging_utils.set_request_id(request_id)
    start = time.monotonic()
    response: Response | None = None
    response = await call_next(request)
    try:
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration_ms = round((time.monotonic() - start) * 1000, 3)
        logging_utils.log_event(
            _LOG,
            logging.INFO,
            "http.request.completed",
            {
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
                "status_code": response.status_code if response is not None else 500,
                "client_ip": str(_request_client_ip(request) or ""),
            },
        )
        logging_utils.reset_request_id(token)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _resolve_caller(request: Request) -> core_models.CallerContext | None:
    cached = getattr(request.state, "_caller", _CALLER_CACHE_MISSING)
    if cached is not _CALLER_CACHE_MISSING:
        return cached

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        # EventSource (SSE) cannot set custom headers; accept ?key= as a fallback.
        # This is intentional — see frontend JobDetailPage SSE connection.
        # Keys in query params appear in access logs so this is only a fallback.
        key_param = (request.query_params.get("key") or "").strip()
        if not key_param:
            request.state._caller = None
            return None
        auth = f"Bearer {key_param}"

    raw = auth[7:]
    if hmac.compare_digest(raw, _MASTER_KEY):
        caller = {
            "type": "master",
            "owner_id": "master",
            "scopes": ["caller", "worker", "admin"],
        }
        request.state._caller = caller
        return caller

    user = _auth.verify_api_key(raw)
    if user:
        scopes = list(user.get("scopes") or [])
        caller = {
            "type": "user",
            "owner_id": f"user:{user['user_id']}",
            "user": user,
            "scopes": scopes,
            "key_id": str(user.get("key_id") or ""),
        }
        request.state._caller = caller
        return caller

    agent_key = _auth.verify_agent_api_key(raw)
    if agent_key:
        key_type = str(agent_key.get("key_type") or "worker").lower()
        if key_type == "caller":
            # Caller-scoped agent key: authenticate AS the agent so the agent's
            # sub-wallet is the funding source. ``owner_id`` matches the wallet
            # naming convention (``agent:<id>``) so existing wallet/billing logic
            # naturally targets the sub-wallet without special cases.
            caller = {
                "type": "agent_caller",
                "owner_id": f"agent:{agent_key['agent_id']}",
                "scopes": ["caller"],
                "agent_id": str(agent_key["agent_id"]),
                "agent_owner_user_id": str(agent_key["owner_id"]),
                "key_id": str(agent_key["key_id"]),
            }
        else:
            caller = {
                "type": "agent_key",
                "owner_id": f"agent_key:{agent_key['agent_id']}",
                "scopes": ["worker"],
                "agent_id": str(agent_key["agent_id"]),
                "key_id": str(agent_key["key_id"]),
            }
        request.state._caller = caller
        return caller

    request.state._caller = None
    return None


_PUBLIC_FRONTEND_URL = (
    os.environ.get("AZTEA_FRONTEND_URL")
    or os.environ.get("AGENTMARKET_FRONTEND_URL")
    or os.environ.get("FRONTEND_BASE_URL")
    or os.environ.get("SERVER_BASE_URL")
    or "http://localhost:8000"
).rstrip("/")
_SIGNUP_URL = f"{_PUBLIC_FRONTEND_URL}/signup"
_DOCS_URL = f"{_PUBLIC_FRONTEND_URL}/docs"

_PUBLIC_DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
_PUBLIC_DOCS_EXCLUDED = {"future-features.md"}
_PUBLIC_DOCS_PRIORITY = {
    "quickstart.md": 0,
    "mcp-integration.md": 1,  # promoted — primary distribution channel
    "skill-md-reference.md": 2,
    "agent-builder.md": 3,
    "auth-onboarding.md": 4,
    "api-reference.md": 5,
    "errors.md": 6,
    "orchestrator-guide.md": 7,
    "verification-contracts.md": 8,
    "reputation.md": 9,
    "cli.md": 20,  # demoted — developer add-on
    "aztea-tui.md": 21,  # demoted — developer add-on
    "stripe-setup.md": 22,
    "terms-of-service.md": 90,
    "privacy-policy.md": 91,
}

_PUBLIC_DOCS_CATEGORY = {
    "quickstart.md": "Get Started",
    "mcp-integration.md": "Get Started",
    "cli.md": "Interfaces",
    "aztea-tui.md": "Interfaces",
    "api-reference.md": "Reference",
    "errors.md": "Reference",
    "skill-md-reference.md": "Reference",
    "verification-contracts.md": "Reference",
    "agent-builder.md": "Builders",
    "auth-onboarding.md": "Builders",
    "orchestrator-guide.md": "Builders",
    "reputation.md": "Marketplace",
    "stripe-setup.md": "Marketplace",
    "terms-of-service.md": "Legal",
    "privacy-policy.md": "Legal",
}


def _public_docs_entries() -> list[dict[str, str]]:
    if not os.path.isdir(_PUBLIC_DOCS_DIR):
        return []

    filenames = [
        name
        for name in os.listdir(_PUBLIC_DOCS_DIR)
        if name.endswith(".md")
        and os.path.isfile(os.path.join(_PUBLIC_DOCS_DIR, name))
        and name not in _PUBLIC_DOCS_EXCLUDED
    ]
    filenames.sort(key=lambda name: (_PUBLIC_DOCS_PRIORITY.get(name, 100), name))

    entries: list[dict[str, str]] = []
    for filename in filenames:
        slug = filename[:-3].strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            continue
        title = slug.replace("-", " ").title()
        summary = ""
        full_path = os.path.join(_PUBLIC_DOCS_DIR, filename)
        try:
            with open(full_path, encoding="utf-8") as handle:
                body_lines: list[str] = []
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        heading = stripped[2:].strip()
                        if heading:
                            title = heading
                        continue
                    if (
                        not stripped
                        or stripped.startswith("---")
                        or stripped.startswith("```")
                    ):
                        continue
                    if stripped.startswith("## "):
                        if body_lines:
                            break
                        continue
                    body_lines.append(stripped)
                    if len(" ".join(body_lines)) >= 220:
                        break
                summary = " ".join(body_lines).strip()
                if len(summary) > 220:
                    summary = summary[:217].rstrip() + "..."
        except OSError:
            continue
        entries.append(
            {
                "slug": slug,
                "title": title,
                "summary": summary,
                "category": _PUBLIC_DOCS_CATEGORY.get(filename, "Reference"),
                "filename": filename,
                "full_path": full_path,
            }
        )

    return entries


def _find_public_doc(doc_slug: str) -> dict[str, str] | None:
    normalized_slug = str(doc_slug or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", normalized_slug):
        return None
    for entry in _public_docs_entries():
        if entry["slug"] == normalized_slug:
            return entry
    return None


# Mutating methods that require legal acceptance before they may be invoked.
# GET / HEAD / OPTIONS remain accessible so users can read their own data,
# inspect their wallet, list agents, and reach the legal-acceptance endpoint
# itself before they're cleared to spend money.
_LEGAL_GATED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Routes that must remain reachable for end users to *complete* onboarding even
# while their account is still pre-acceptance. Anchored prefixes — anything
# starting with one of these strings is allowed regardless of HTTP method.
_LEGAL_GATE_EXEMPT_PREFIXES = (
    "/auth/legal/accept",
    "/auth/login",
    "/auth/logout",
    "/auth/register",
    "/auth/google",
    "/auth/role",
    "/auth/forgot",
    "/auth/reset",
    "/health",
    "/openapi.json",
    "/docs",
    "/redoc",
)


def _legal_acceptance_required(caller: core_models.CallerContext) -> bool:
    """True when an authenticated end-user has not accepted the current legal docs."""
    # Allow the test/CI pipeline (and self-hosters who explicitly opt out) to
    # disable the gate. The default is False — production deployments enforce.
    if str(os.environ.get("AZTEA_BYPASS_LEGAL_GATE", "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return False
    if caller.get("type") != "user":
        return False
    user = caller.get("user") or {}
    if not isinstance(user, dict):
        return False
    # Mirror ``core.auth.schema._legal_state_from_row`` — without re-reading the
    # row, since the caller is already attached. Treat any missing/older
    # acceptance as "not accepted".
    try:
        from core.auth.schema import LEGAL_PRIVACY_VERSION, LEGAL_TERMS_VERSION
    except Exception:
        return False
    accepted_terms = str(user.get("terms_version_accepted") or "").strip()
    accepted_privacy = str(user.get("privacy_version_accepted") or "").strip()
    accepted_at = str(user.get("legal_accepted_at") or "").strip()
    return not (
        accepted_terms == LEGAL_TERMS_VERSION
        and accepted_privacy == LEGAL_PRIVACY_VERSION
        and accepted_at
    )


def _require_api_key(request: Request) -> core_models.CallerContext:
    caller = _resolve_caller(request)
    if caller is None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "AUTHENTICATION_REQUIRED",
                    "message": "No API key provided. Sign up to get starter credit; no card required.",
                    "details": {
                        "signup_url": _SIGNUP_URL,
                        "docs_url": _DOCS_URL,
                    },
                },
            )
        raw = auth[7:]
        if _auth.api_key_is_revoked(raw):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "API_KEY_REVOKED",
                    "message": "API key has been revoked. Use the replacement key or create a new one.",
                },
            )
        raise HTTPException(
            status_code=401,
            detail={
                "error": "INVALID_API_KEY",
                "message": "API key is invalid or expired.",
                "details": {
                    "signup_url": _SIGNUP_URL,
                    "docs_url": _DOCS_URL,
                },
            },
        )
    # Soft legal-acceptance gate: only block mutating requests, and only when
    # the route is not on the onboarding-friendly exempt list. This prevents
    # spending without accepted ToS while letting users complete onboarding.
    method = (request.method or "").upper()
    if method in _LEGAL_GATED_METHODS and _legal_acceptance_required(caller):
        path = request.url.path or ""
        if not any(path.startswith(prefix) for prefix in _LEGAL_GATE_EXEMPT_PREFIXES):
            raise HTTPException(
                status_code=451,
                detail={
                    "error": "LEGAL_ACCEPTANCE_REQUIRED",
                    "message": (
                        "Accept the Terms of Service and Privacy Policy before performing "
                        "this action. POST /auth/legal/accept to record acceptance."
                    ),
                    "accept_url": "/auth/legal/accept",
                },
            )
    return caller


def _optional_api_key(request: Request) -> core_models.CallerContext | None:
    """Like _require_api_key but returns None instead of raising 401/403."""
    return _resolve_caller(request)


def _fold_in_master_owner_ids(caller: core_models.CallerContext) -> list[str]:
    """Extra caller_owner_ids whose jobs should appear under this caller.

    The master/ops key writes jobs with caller_owner_id="master". Operators
    who run MCP / CLI with the master key still want to see those jobs in
    their website dashboard. When the authenticated email matches
    AZTEA_MASTER_OWNER_EMAIL we fold master-owned jobs into their view.
    """
    # Default to the deployment-owner email so the demo "just works" without
    # an extra env-var on the server. Override with AZTEA_MASTER_OWNER_EMAIL.
    target = (
        os.environ.get("AZTEA_MASTER_OWNER_EMAIL")
        or "founders@aztea.ai"
    ).strip().lower()
    if not target:
        return []
    if caller.get("type") != "user":
        return []
    user = caller.get("user") or {}
    if not isinstance(user, dict):
        return []
    email = str(user.get("email") or "").strip().lower()
    return ["master"] if email == target else []


def _caller_owner_id(request: Request) -> str:
    caller = _resolve_caller(request)
    if caller is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "INVALID_API_KEY",
                "message": "API key is invalid or expired.",
                "details": {
                    "signup_url": _SIGNUP_URL,
                    "docs_url": _DOCS_URL,
                },
            },
        )
    return caller["owner_id"]


def _normalize_client_id(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if len(text) > 64:
        text = text[:64]
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", text):
        return None
    return text


def _request_client_id(request: Request, explicit: str | None = None) -> str | None:
    for candidate in (
        explicit,
        request.headers.get(_CLIENT_ID_HEADER),
        request.query_params.get("client_id"),
    ):
        normalized = _normalize_client_id(candidate)
        if normalized:
            return normalized
    return None
