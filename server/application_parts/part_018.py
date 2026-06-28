# ── Otto GPT-5.x proxy ─────────────────────────────────────────────────────────
# POST /otto/responses — a self-contained, authenticated passthrough to Azure
# OpenAI's **Responses API** (gpt-5.x) for the Otto desktop app. The acting twin of
# /otto/chat (part_015): same shared-bearer auth + shared-budget model, but the
# upstream is Azure's /openai/responses instead of Anthropic's /v1/messages, so the
# Azure key NEVER ships in the app and GPT-5.5 works out of the box with no key.
#
#   • Auth:   the same shared bearer secret as /otto/chat. The app sends
#             `Authorization: Bearer <T>`; checked against OTTO_APP_TOKEN. The token
#             is baked into the app and extractable — that's fine; the budget below
#             is the real protection, not the token's secrecy.
#   • Upstream: forwards the Responses body to Azure using the SERVER-side
#             AZURE_RESPONSES_KEY. The endpoint + api-version are server-pinned (env);
#             the deployment can be pinned too (AZURE_RESPONSES_MODEL) so a
#             bearer-holder can't switch to a pricier model.
#   • Budget: a single shared spend pool, SEPARATE from /otto/chat (Anthropic) and
#             /otto/realtime (voice), in the same SQLite db. Each call reserves an
#             upper-bound estimate, then reconciles to the response's actual token
#             usage. Exhausted → HTTP 402 for everyone until reset/raised.
#
# Two upstream paths, selected by OTTO_USE_LITELLM:
#   • LiteLLM gateway (OTTO_USE_LITELLM=1, preferred): forward the Responses body to a
#     local LiteLLM proxy (/v1/responses). LiteLLM holds the Azure key, provides provider
#     routing/fallback + observability, and enforces the $150 cap via the virtual key's
#     max_budget. aztea only validates the app token and maps LiteLLM's budget rejection
#     back to the 402 at-capacity contract. The SQLite reserve below is bypassed here.
#   • Direct Azure (flag off, legacy/rollback): the original passthrough + SQLite cap.
#
# Server env:
#   OTTO_APP_TOKEN                shared bearer secret (must match the app's baked token;
#                                 the SAME one /otto/realtime uses)
#   OTTO_USE_LITELLM             "1" → route via the LiteLLM gateway (below); else direct Azure
#   OTTO_RESPONSES_LITELLM_URL   LiteLLM base, e.g. http://127.0.0.1:4001
#   OTTO_RESPONSES_LITELLM_KEY   LiteLLM virtual key (max_budget=$150; server-side ONLY)
#   OTTO_RESPONSES_LITELLM_MODEL LiteLLM model alias to pin (default otto-responses)
#   AZURE_RESPONSES_URL          [legacy/rollback] Azure resource base
#   AZURE_RESPONSES_KEY          [legacy/rollback] the Azure resource key (server-side ONLY)
#   AZURE_RESPONSES_API_VERSION  [legacy/rollback] api-version query (default 2025-04-01-preview)
#   AZURE_RESPONSES_MODEL        [legacy/rollback] optional deployment pin
#   OTTO_RESPONSES_BUDGET_CAP_CENTS  [legacy/rollback] SQLite spend cap in cents (default 15000 = $150)
#   OTTO_BUDGET_DB               [legacy/rollback] sqlite path (shared with the other Otto proxies)
import asyncio
import hmac
import json
import os
import sqlite3

import httpx

_OTTO_RESP_FALLBACK_MAX_TOKENS = 4096

# Async upstream + concurrency cap. /otto/responses is an ASYNC handler using httpx, so the
# upstream round-trip never blocks a worker threadpool slot — a slow upstream used to drain
# the ~40-thread pool and hang EVERY sync route (/health, /otto, ...) until restart
# (2026-06-27 incident). The semaphore bounds concurrent upstream calls so a burst can't
# exhaust sockets/threads; excess requests await cheaply on the event loop.
_OTTO_RESP_UPSTREAM_TIMEOUT = float(os.environ.get("OTTO_RESPONSES_TIMEOUT_S") or 60)
try:
    _OTTO_RESP_MAX_CONCURRENCY = int(os.environ.get("OTTO_RESPONSES_MAX_CONCURRENCY") or 24)
except (TypeError, ValueError):
    _OTTO_RESP_MAX_CONCURRENCY = 24
_OTTO_RESP_SEM = asyncio.Semaphore(_OTTO_RESP_MAX_CONCURRENCY)

# ── Shared httpx client (one per worker process) ──────────────────────────────
# All Otto async proxies (responses, composio) reuse ONE AsyncClient per worker with explicit
# connection limits, instead of building/tearing a client per request (which churns TCP/TLS pools
# and FDs under bursts). Created + closed by the lifespan (part_001); _otto_http() lazily creates
# one if the lifespan hasn't (tests / standalone). Per-request `timeout=` keeps the per-path caps.
_OTTO_HTTP_CLIENT = None
_OTTO_HTTP_CLOSED = False


def _otto_http():  # -> httpx.AsyncClient
    global _OTTO_HTTP_CLIENT
    if _OTTO_HTTP_CLIENT is None:
        if _OTTO_HTTP_CLOSED:
            # Shutdown already closed the shared client — do NOT spawn a replacement that
            # would never be aclose()d (orphaned sockets/FDs). Surface as unavailable; the
            # caller's try/except turns it into the normal 502/503 path.
            raise RuntimeError("otto http client is closed (server shutting down)")
        try:
            _mc = int(os.environ.get("OTTO_HTTP_MAX_CONNECTIONS") or 100)
            _mk = int(os.environ.get("OTTO_HTTP_MAX_KEEPALIVE") or 20)
        except (TypeError, ValueError):
            _mc, _mk = 100, 20
        _OTTO_HTTP_CLIENT = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=_mc, max_keepalive_connections=_mk)
        )
    return _OTTO_HTTP_CLIENT


async def _otto_http_close() -> None:
    """Close the shared client on shutdown (called from the lifespan finally, AFTER the
    in-flight drain). Sets a sticky closed flag so _otto_http() won't re-create an orphan."""
    global _OTTO_HTTP_CLIENT, _OTTO_HTTP_CLOSED
    _OTTO_HTTP_CLOSED = True
    client = _OTTO_HTTP_CLIENT
    _OTTO_HTTP_CLIENT = None
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass

# Approximate Azure gpt-5.x list prices, (input, output) cents per 1,000,000 tokens.
# This only sizes the shared spend cap — TUNE to your actual Azure rate card. Rounding
# UP means the cap trips slightly early rather than overshooting the dollar budget.
_OTTO_RESP_RATE_IN = 200.0     # $2 / 1M input
_OTTO_RESP_RATE_OUT = 1000.0   # $10 / 1M output


def _otto_resp_cost_cents(input_tokens: float, output_tokens: float) -> float:
    return (input_tokens * _OTTO_RESP_RATE_IN + output_tokens * _OTTO_RESP_RATE_OUT) / 1_000_000.0


def _otto_resp_budget_db() -> str:
    return os.environ.get("OTTO_BUDGET_DB") or os.path.expanduser("~/.otto-proxy-budget.sqlite3")


def _otto_resp_budget_cap_cents() -> float:
    try:
        return float(os.environ.get("OTTO_RESPONSES_BUDGET_CAP_CENTS") or 15000)
    except (TypeError, ValueError):
        return 15000.0


def _otto_resp_budget_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_otto_resp_budget_db(), timeout=10)
    # WAL + a short busy_timeout: writes are fast and a contended lock waits at most 2s instead
    # of holding a threadpool worker for 10s (see the composio proxy for the full rationale).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS otto_responses_budget ("
        "  id INTEGER PRIMARY KEY CHECK(id = 1),"
        "  spent_cents REAL NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT OR IGNORE INTO otto_responses_budget (id, spent_cents) VALUES (1, 0)")
    conn.commit()
    return conn


def _otto_resp_budget_try_reserve(cost_cents: float) -> bool:
    """Atomically add cost_cents iff it keeps the pool within the cap."""
    cap = _otto_resp_budget_cap_cents()
    conn = _otto_resp_budget_conn()
    try:
        cur = conn.execute(
            "UPDATE otto_responses_budget SET spent_cents = spent_cents + ? "
            "WHERE id = 1 AND spent_cents + ? <= ?",
            (cost_cents, cost_cents, cap),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _otto_resp_budget_adjust(delta_cents: float) -> None:
    """Apply a delta (refund a reservation, or reconcile est → actual)."""
    conn = _otto_resp_budget_conn()
    try:
        conn.execute(
            "UPDATE otto_responses_budget SET spent_cents = MAX(0, spent_cents + ?) WHERE id = 1",
            (delta_cents,),
        )
        conn.commit()
    finally:
        conn.close()


def _otto_resp_use_litellm() -> bool:
    # Per-path flag (OTTO_RESPONSES_USE_LITELLM) with OTTO_USE_LITELLM as a shared fallback,
    # so responses can cut over to the gateway independently of realtime.
    v = (
        os.environ.get("OTTO_RESPONSES_USE_LITELLM")
        or os.environ.get("OTTO_USE_LITELLM")
        or ""
    ).strip().lower()
    return v in ("1", "true", "yes", "on")


async def _otto_resp_via_litellm(body: dict) -> Response:  # noqa: F821
    """Forward the Responses body to the local LiteLLM gateway (async; OTTO_USE_LITELLM path).

    LiteLLM holds the Azure key, handles routing/fallback, and enforces the $150 cap via the
    virtual key's max_budget — so the SQLite reserve is bypassed here. A budget rejection is
    mapped back to the app's 402 at-capacity contract; other upstream errors pass through.
    Async httpx so a slow gateway never blocks the worker's threadpool.
    """
    gw_base = (os.environ.get("OTTO_RESPONSES_LITELLM_URL") or "").strip().rstrip("/")
    gw_key = (os.environ.get("OTTO_RESPONSES_LITELLM_KEY") or "").strip()
    if not gw_base or not gw_key:
        raise HTTPException(  # noqa: F821
            status_code=503,
            detail=error_codes.make_error("server.unavailable", "Otto service is not configured (no gateway)."),  # noqa: F821
        )
    # Pin to the budgeted virtual key's model alias so routing + spend attribution resolve.
    body["model"] = (os.environ.get("OTTO_RESPONSES_LITELLM_MODEL") or "otto-responses").strip()
    headers = {"authorization": f"Bearer {gw_key}", "content-type": "application/json"}
    try:
        gw = await _otto_http().post(
            f"{gw_base}/v1/responses", json=body, headers=headers, timeout=_OTTO_RESP_UPSTREAM_TIMEOUT
        )
    except Exception:
        raise HTTPException(  # noqa: F821
            status_code=502,
            detail=error_codes.make_error("upstream.unavailable", "Could not reach the model service. Please try again."),  # noqa: F821
        )
    if gw.status_code == 200:
        return JSONResponse(status_code=200, content=gw.json())  # noqa: F821
    try:
        gw_err = gw.json()
    except Exception:
        gw_err = error_codes.make_error("upstream.unavailable", (gw.text or "")[:500] or "Upstream error.")  # noqa: F821
    blob = json.dumps(gw_err, default=str).lower()
    if gw.status_code in (400, 402, 429) and "budget" in blob and ("exceed" in blob or "limit" in blob):
        raise HTTPException(  # noqa: F821
            status_code=402,
            detail=error_codes.make_error(
                "payment.spend_limit_exceeded",
                "Otto is at capacity (shared budget reached). Please try again later.",
            ),
        )
    return JSONResponse(status_code=gw.status_code, content=gw_err)  # noqa: F821


def _otto_resp_via_azure_sync(body: dict) -> Response:  # noqa: F821
    """Legacy/rollback path: direct Azure passthrough + the SQLite shared-budget cap.

    Blocking (requests + sqlite), so the async handler runs it via asyncio.to_thread. Kept only
    for OTTO_USE_LITELLM rollback; the live path is the async _otto_resp_via_litellm.
    """
    azure_base = (os.environ.get("AZURE_RESPONSES_URL") or "").strip().rstrip("/")
    azure_key = (os.environ.get("AZURE_RESPONSES_KEY") or "").strip()
    api_version = (os.environ.get("AZURE_RESPONSES_API_VERSION") or "2025-04-01-preview").strip()
    if not azure_base or not azure_key:
        raise HTTPException(  # noqa: F821
            status_code=503,
            detail=error_codes.make_error("server.unavailable", "Otto service is not configured (no upstream model key)."),  # noqa: F821
        )

    # Optionally pin the deployment server-side so a bearer-holder can't request a pricier model.
    pinned = (os.environ.get("AZURE_RESPONSES_MODEL") or "").strip()
    if pinned:
        body["model"] = pinned

    try:
        max_tokens = int(body.get("max_output_tokens") or _OTTO_RESP_FALLBACK_MAX_TOKENS)
    except (TypeError, ValueError):
        max_tokens = _OTTO_RESP_FALLBACK_MAX_TOKENS
    prompt_chars = (
        len(json.dumps(body.get("instructions") or "", default=str))
        + len(json.dumps(body.get("input") or [], default=str))
        + len(json.dumps(body.get("tools") or [], default=str))
    )

    # Reserve an upper-bound estimate against the shared pool. 402 if the pool can't cover it.
    estimate_cents = _otto_resp_cost_cents(prompt_chars / 4.0, max_tokens)
    if not _otto_resp_budget_try_reserve(estimate_cents):
        raise HTTPException(  # noqa: F821
            status_code=402,
            detail=error_codes.make_error(
                "payment.spend_limit_exceeded",
                "Otto is at capacity (shared budget reached). Please try again later.",
            ),
        )

    url = f"{azure_base}/openai/responses?api-version={api_version}"
    headers = {"api-key": azure_key, "content-type": "application/json"}
    try:
        upstream = http.post(url, json=body, headers=headers, timeout=120)  # noqa: F821
    except Exception:
        _otto_resp_budget_adjust(-estimate_cents)  # refund the reservation
        raise HTTPException(  # noqa: F821
            status_code=502,
            detail=error_codes.make_error("upstream.unavailable", "Could not reach the model service. Please try again."),  # noqa: F821
        )

    if upstream.status_code != 200:
        _otto_resp_budget_adjust(-estimate_cents)
        try:
            err_body = upstream.json()
        except Exception:
            err_body = error_codes.make_error("upstream.unavailable", (upstream.text or "")[:500] or "Upstream error.")  # noqa: F821
        return JSONResponse(status_code=upstream.status_code, content=err_body)  # noqa: F821

    data = upstream.json()

    # Reconcile the reservation to actual usage. Never fail the request over settlement.
    try:
        usage = data.get("usage") or {}
        actual_cents = _otto_resp_cost_cents(
            float(usage.get("input_tokens") or 0),
            float(usage.get("output_tokens") or 0),
        )
        _otto_resp_budget_adjust(actual_cents - estimate_cents)
    except Exception:
        pass

    return JSONResponse(status_code=200, content=data)  # noqa: F821


@app.post(  # noqa: F821  (app/limiter/etc are provided by the shared shard namespace)
    "/otto/responses",
    responses=_error_responses(400, 401, 402, 429, 502, 503),  # noqa: F821
)
@limiter.limit("120/minute")  # noqa: F821
async def otto_responses(request: Request, body: dict = Body(...)) -> Response:  # noqa: F821
    """Authenticated Azure Responses (gpt-5.x) proxy for the Otto app.

    ASYNC so the upstream round-trip never blocks the worker's threadpool, and bounded by a
    concurrency semaphore so a slow upstream can't exhaust sockets/threads and wedge the whole
    process (the 2026-06-27 hang). Auth + validation happen BEFORE taking a slot.
    """
    # 1. Auth: shared bearer secret (constant-time compare).
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

    # Responses API requests carry `input` (not `messages`).
    if not isinstance(body, dict) or not body.get("input"):
        raise HTTPException(  # noqa: F821
            status_code=400,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,  # noqa: F821
                "Request body must be a Responses API request including 'input'.",
            ),
        )

    # 2. Upstream call, concurrency-bounded. LiteLLM path is fully async; the legacy Azure path
    #    runs in a worker thread. Either way the semaphore caps in-flight upstream work so one
    #    slow upstream can't starve the threadpool/sockets and hang every other route.
    async with _OTTO_RESP_SEM:
        if _otto_resp_use_litellm():
            return await _otto_resp_via_litellm(body)
        return await asyncio.to_thread(_otto_resp_via_azure_sync, body)
