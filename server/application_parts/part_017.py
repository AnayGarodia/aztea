# ── Otto Composio proxy ───────────────────────────────────────────────────────
# Authenticated, ALLOWLISTED passthrough to Composio's v3 API for the Otto desktop app,
# so the Composio project key never ships in the app. Mirrors /otto/chat's shared-bearer
# auth (part_015) but for the many small Composio endpoints the app uses.
#
# Security model (the bearer IS baked into the app → extractable, same as /otto/chat):
#   • ALLOWLIST: only the exact (method, path) pairs Composio.swift calls are forwarded.
#     Everything else — project-admin endpoints, path traversal — is 403. This stops a
#     bearer-holder from reaching the whole Composio project API with the server key.
#   • user_id is validated to Otto's format (otto-<uuid>) wherever it appears (query + body).
#     It is the per-install scoping secret (122-bit random UUID, stored locally, never shared),
#     so it isolates each user's connected accounts. We deliberately do NOT collapse it to one
#     server-side id — that would make every user share one Gmail. A real per-user identity
#     (login) is deferred; until then the unguessable UUID is the scope. Residual risk: a
#     bearer-holder who ALSO knows a victim's UUID could act as them — but the UUID never
#     leaves the victim's machine, and the 122-bit space defeats enumeration.
#   • rate-limit (per client) + a shared daily call cap. Composio is not $-per-token billed,
#     so there's no dollar pool like /otto/chat — the daily cap is the abuse ceiling.
#
# Server env:
#   OTTO_APP_TOKEN            shared bearer (the same one the app bakes + /otto/chat uses)
#   COMPOSIO_API_KEY          the Composio project key (server-side ONLY)
#   OTTO_COMPOSIO_DAILY_CAP   max forwarded calls/day (default 5000)
#   OTTO_BUDGET_DB            sqlite path (shared with the other Otto proxies)
import asyncio
import hmac
import logging
import os
import re
import sqlite3
import time
from urllib.parse import parse_qsl, urlencode

_otto_composio_log = logging.getLogger("otto.composio")
_COMPOSIO_BASE = "https://backend.composio.dev/api/v3"
_OTTO_USERID_RE = re.compile(r"^otto-[0-9a-fA-F-]{36}$")

# Async + concurrency cap (mirrors part_018). The handler is async + uses the shared httpx
# client (_otto_http, part_018) so a slow Composio upstream never blocks a worker threadpool
# slot; the semaphore bounds in-flight upstream calls. The sqlite daily-cap runs via
# asyncio.to_thread so it doesn't block the event loop.
_OTTO_COMPOSIO_UPSTREAM_TIMEOUT = float(os.environ.get("OTTO_COMPOSIO_TIMEOUT_S") or 30)
try:
    _OTTO_COMPOSIO_MAX_CONCURRENCY = int(os.environ.get("OTTO_COMPOSIO_MAX_CONCURRENCY") or 24)
except (TypeError, ValueError):
    _OTTO_COMPOSIO_MAX_CONCURRENCY = 24
_OTTO_COMPOSIO_SEM = asyncio.Semaphore(_OTTO_COMPOSIO_MAX_CONCURRENCY)

# Exactly the (method, path-under-/api/v3) pairs Composio.swift + the cherry-picked
# listToolkits/listActions call. Anything else → 403.
_OTTO_COMPOSIO_ALLOW = [
    ("GET", re.compile(r"^/toolkits/?$")),                       # listToolkits
    ("GET", re.compile(r"^/tools/?$")),                          # listActions
    ("GET", re.compile(r"^/auth_configs/?$")),                   # ensureAuthConfig (list)
    ("POST", re.compile(r"^/auth_configs/?$")),                  # ensureAuthConfig (create)
    ("GET", re.compile(r"^/connected_accounts/?$")),             # activeConnection (list)
    ("GET", re.compile(r"^/connected_accounts/[A-Za-z0-9_-]+/?$")),  # status by id
    ("POST", re.compile(r"^/connected_accounts/link/?$")),       # connect / startLink
    ("POST", re.compile(r"^/tools/execute/[A-Za-z0-9_.-]+/?$")),  # execute / app_call
]


def _otto_composio_daily_cap() -> int:
    try:
        return int(os.environ.get("OTTO_COMPOSIO_DAILY_CAP") or 5000)
    except (TypeError, ValueError):
        return 5000


def _otto_composio_db() -> sqlite3.Connection:
    path = os.environ.get("OTTO_BUDGET_DB") or os.path.expanduser("~/.otto-proxy-budget.sqlite3")
    conn = sqlite3.connect(path, timeout=10)
    # WAL = writers don't block readers and commits are fast; busy_timeout caps a lock wait at
    # 2s so a sync (threadpooled) handler can never hold a worker thread for the full 10s on
    # contention — that pile-up is what starved the pool and hung every /otto/* route.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS otto_composio_calls ("
        "  day TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    return conn


def _otto_composio_try_count() -> bool:
    """Atomically bump today's (UTC) counter iff under the cap. False when exhausted.

    FAIL OPEN on a transient SQLite lock: under concurrent writers (24-deep semaphore × 3
    workers all hammering one local file) `database is locked` can outlast busy_timeout. The
    daily cap is a soft abuse ceiling, not a correctness gate — a contended counter must never
    turn a legitimate call into a 500. Worst case a few calls slip past the cap.
    """
    cap = _otto_composio_daily_cap()
    day = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        conn = _otto_composio_db()
    except sqlite3.OperationalError:
        return True
    try:
        conn.execute("INSERT OR IGNORE INTO otto_composio_calls (day, n) VALUES (?, 0)", (day,))
        cur = conn.execute(
            "UPDATE otto_composio_calls SET n = n + 1 WHERE day = ? AND n < ?", (day, cap)
        )
        conn.commit()
        return cur.rowcount == 1
    except sqlite3.OperationalError:
        return True
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _otto_composio_valid_userid(v) -> bool:
    return isinstance(v, str) and bool(_OTTO_USERID_RE.match(v))


@app.api_route("/otto/composio/{path:path}", methods=["GET", "POST"])  # noqa: F821
@limiter.limit("120/minute")  # noqa: F821
async def otto_composio(request: Request, path: str, body: dict | None = Body(default=None)) -> Response:  # noqa: F821
    """Allowlisted, authenticated relay to Composio v3 for the Otto app (async + sem-capped)."""
    # 1. Auth — shared bearer (constant-time).
    expected = os.environ.get("OTTO_APP_TOKEN", "").strip()
    if not expected:
        raise HTTPException(  # noqa: F821
            status_code=503,
            detail=error_codes.make_error("server.unavailable", "Otto service is not configured (no app token)."),  # noqa: F821
        )
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(  # noqa: F821
            status_code=401,
            detail=error_codes.make_error("auth.invalid_or_expired_token", "Invalid Otto app token."),  # noqa: F821
        )

    composio_key = os.environ.get("COMPOSIO_API_KEY", "").strip()
    if not composio_key:
        raise HTTPException(  # noqa: F821
            status_code=503,
            detail=error_codes.make_error("server.unavailable", "App integrations are not configured."),  # noqa: F821
        )

    # 2. Allowlist (method + path). Reject traversal and anything not used by the app.
    norm = "/" + path
    if ".." in norm or "//" in norm:
        raise HTTPException(status_code=403, detail=error_codes.make_error("forbidden", "Path not allowed."))  # noqa: F821
    method = request.method.upper()
    if not any(m == method and rx.match(norm) for m, rx in _OTTO_COMPOSIO_ALLOW):
        raise HTTPException(  # noqa: F821
            status_code=403,
            detail=error_codes.make_error("forbidden", f"{method} {norm} is not an allowed Composio endpoint."),  # noqa: F821
        )

    # 3. Validate user_id (query + body) — the per-install scope secret. Reject malformed.
    q = dict(parse_qsl(request.url.query, keep_blank_values=True))
    if "user_id" in q and not _otto_composio_valid_userid(q["user_id"]):
        raise HTTPException(status_code=400, detail=error_codes.make_error(error_codes.INVALID_INPUT, "Bad user_id."))  # noqa: F821
    if isinstance(body, dict) and "user_id" in body and not _otto_composio_valid_userid(body["user_id"]):
        raise HTTPException(status_code=400, detail=error_codes.make_error(error_codes.INVALID_INPUT, "Bad user_id."))  # noqa: F821

    # 4-5. Daily cap + upstream forward, concurrency-bounded. The sqlite cap runs in a thread
    #      (it's blocking) and the upstream uses the shared async client, so neither blocks the
    #      event loop and a slow Composio can't exhaust the worker.
    async with _OTTO_COMPOSIO_SEM:
        # 4. Daily cap (abuse ceiling; Composio isn't per-token billed).
        if not await asyncio.to_thread(_otto_composio_try_count):
            raise HTTPException(  # noqa: F821
                status_code=429,
                detail=error_codes.make_error("payment.spend_limit_exceeded", "Otto app integrations are at today's capacity. Please try again later."),  # noqa: F821
            )

        # 5. Forward to Composio with the server-side key (shared httpx client).
        url = _COMPOSIO_BASE + norm
        if q:
            url += "?" + urlencode(q)
        headers = {"x-api-key": composio_key, "content-type": "application/json"}
        t0 = time.time()
        try:
            if method == "GET":
                upstream = await _otto_http().get(url, headers=headers, timeout=_OTTO_COMPOSIO_UPSTREAM_TIMEOUT)  # noqa: F821
            else:
                upstream = await _otto_http().post(url, json=(body or {}), headers=headers, timeout=_OTTO_COMPOSIO_UPSTREAM_TIMEOUT)  # noqa: F821
        except Exception:
            raise HTTPException(  # noqa: F821
                status_code=502,
                detail=error_codes.make_error("upstream.unavailable", "Could not reach the app-integration service. Please try again."),  # noqa: F821
            )

    uid = str(q.get("user_id") or (body or {}).get("user_id") or "-")[:18]
    _otto_composio_log.info(
        "composio_proxy user=%s %s %s -> %s (%dms)", uid, method, norm, upstream.status_code,
        int((time.time() - t0) * 1000),
    )

    # 6. Relay upstream status + body verbatim, so the app/agent sees real errors (not lies).
    try:
        return JSONResponse(status_code=upstream.status_code, content=upstream.json())  # noqa: F821
    except Exception:
        return JSONResponse(  # noqa: F821
            status_code=upstream.status_code,
            content=error_codes.make_error("upstream.error", (upstream.text or "")[:300] or "Upstream error."),
        )


# The SPA catch-all (GET /{full_path:path}, registered in an earlier part) is matched before
# this route — FastAPI/Starlette evaluate routes in registration order — so a GET to
# /otto/composio/* would otherwise return index.html instead of reaching this handler. Move
# this route to the front so it wins. It only matches /otto/composio/, so it shadows nothing else.
for _i, _r in enumerate(list(app.router.routes)):  # noqa: F821
    if getattr(_r, "path", None) == "/otto/composio/{path:path}":
        app.router.routes.insert(0, app.router.routes.pop(_i))  # noqa: F821
        break
