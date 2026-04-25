# server.application shard 1 — migrations, FastAPI app + lifespan setup,
# exception handlers, CORS, /api/* compat middleware, security headers,
# request tracing, prometheus metrics, /metrics endpoint, auth helpers.


def _migrate_job_event_deliveries_status_schema(conn: sqlite3.Connection) -> None:
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

def _output_schema_object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
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



def _ensure_system_user() -> str:
    with _auth._conn() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE username = ? ORDER BY created_at ASC LIMIT 1",
            (_SYSTEM_USERNAME,),
        ).fetchone()
        if existing is not None:
            user_id = str(existing["user_id"])
            conn.execute("UPDATE users SET status = 'suspended' WHERE user_id = ?", (user_id,))
            return user_id

        user_id = str(uuid.uuid4())
        now = _utc_now_iso()
        email = _SYSTEM_USER_EMAIL
        if conn.execute("SELECT 1 FROM users WHERE email = ? LIMIT 1", (email,)).fetchone() is not None:
            email = f"system-{user_id[:8]}@aztea.internal"
        salt = "system-account-disabled"
        password_hash = hashlib.sha256(f"{user_id}:{salt}".encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO users (user_id, username, email, password_hash, salt, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'suspended')
            """,
            (user_id, _SYSTEM_USERNAME, email, password_hash, salt, now),
        )
        return user_id


def ensure_builtin_agents_registered() -> None:
    system_user_id = _ensure_system_user()
    system_owner_id = f"user:{system_user_id}"
    specs = _builtin_agent_specs()
    managed_ids = {str(spec.get("agent_id") or "").strip() for spec in specs if str(spec.get("agent_id") or "").strip()}
    now = _utc_now_iso()

    for spec in specs:
        existing = registry.get_agent(spec["agent_id"])
        output_examples = spec.get("output_examples")
        output_examples_json = None
        if isinstance(output_examples, list):
            output_examples_json = json.dumps([item for item in output_examples if isinstance(item, dict)]) or None
        if existing is None:
            if registry.agent_exists_by_name(spec["name"]):
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
            )
            continue

        with registry._conn() as conn:
            conn.execute(
                """
                UPDATE agents
                SET owner_id = ?,
                    name = ?,
                    description = ?,
                    endpoint_url = ?,
                    price_per_call_usd = ?,
                    tags = ?,
                    input_schema = ?,
                    output_schema = ?,
                    output_examples = ?,
                    internal_only = ?,
                    status = 'active',
                    review_status = 'approved',
                    reviewed_by = ?,
                    reviewed_at = ?,
                    model_provider = ?,
                    model_id = ?,
                    kind = 'aztea_built'
                WHERE agent_id = ?
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
                    _SYSTEM_USERNAME,
                    now,
                    "groq",
                    "llama-3.3-70b-versatile",
                    spec["agent_id"],
                ),
            )

    deprecated_ids = _BUILTIN_AGENT_IDS - managed_ids
    for agent_id in deprecated_ids:
        stale = registry.get_agent(agent_id, include_unapproved=True)
        if stale is not None and str(stale.get("status") or "").strip().lower() != "suspended":
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
    _init_ops_db()
    _init_stripe_db()
    ensure_builtin_agents_registered()
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
        _LOG.info("Background workers disabled in this process; another worker owns the lock.")

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
            payments_reconciliation_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if agent_health_stop_event is not None:
            agent_health_stop_event.set()
        if agent_health_thread is not None:
            agent_health_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
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
app = FastAPI(title="aztea v1", lifespan=lifespan)
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
    raise RuntimeError("CORS_ALLOW_ORIGINS must not contain '*' when ENVIRONMENT=production.")
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
    has_legacy = (request.headers.get(_LEGACY_PROTOCOL_VERSION_HEADER, "") or "").strip()
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
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, REGISTRY
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
        return JSONResponse({"error": "prometheus_client not installed"}, status_code=503)
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
        request.state._caller = None
        return None

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
    or "https://aztea.dev"
).rstrip("/")
_SIGNUP_URL = f"{_PUBLIC_FRONTEND_URL}/signup"
_DOCS_URL = f"{_PUBLIC_FRONTEND_URL}/docs"

_PUBLIC_DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
_PUBLIC_DOCS_EXCLUDED = {"future-features.md"}
_PUBLIC_DOCS_PRIORITY = {
    "quickstart.md": 0,
    "mcp-integration.md": 1,
    "skill-md-reference.md": 2,
    "agent-builder.md": 3,
    "auth-onboarding.md": 4,
    "api-reference.md": 5,
    "errors.md": 6,
    "orchestrator-guide.md": 7,
    "verification-contracts.md": 8,
    "reputation.md": 9,
    "cli.md": 20,
    "aztea-tui.md": 21,
    "stripe-setup.md": 22,
    "terms-of-service.md": 90,
    "privacy-policy.md": 91,
}


def _public_docs_entries() -> list[dict[str, str]]:
    if not os.path.isdir(_PUBLIC_DOCS_DIR):
        return []

    filenames = [
        name for name in os.listdir(_PUBLIC_DOCS_DIR)
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
        full_path = os.path.join(_PUBLIC_DOCS_DIR, filename)
        try:
            with open(full_path, encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        heading = stripped[2:].strip()
                        if heading:
                            title = heading
                        break
                    if stripped:
                        break
        except OSError:
            continue
        entries.append({
            "slug": slug,
            "title": title,
            "filename": filename,
            "full_path": full_path,
        })

    return entries


def _find_public_doc(doc_slug: str) -> dict[str, str] | None:
    normalized_slug = str(doc_slug or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", normalized_slug):
        return None
    for entry in _public_docs_entries():
        if entry["slug"] == normalized_slug:
            return entry
    return None


def _require_api_key(request: Request) -> core_models.CallerContext:
    caller = _resolve_caller(request)
    if caller is None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "AUTHENTICATION_REQUIRED",
                    "message": "No API key provided. Sign up to get one; it includes $1 free credit.",
                    "signup_url": _SIGNUP_URL,
                    "docs_url": _DOCS_URL,
                },
            )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "INVALID_API_KEY",
                "message": "API key is invalid or expired.",
                "signup_url": _SIGNUP_URL,
                "docs_url": _DOCS_URL,
            },
        )
    return caller


def _caller_owner_id(request: Request) -> str:
    caller = _resolve_caller(request)
    if caller is None:
        raise HTTPException(status_code=403, detail="Invalid API key.")
    return caller["owner_id"]


