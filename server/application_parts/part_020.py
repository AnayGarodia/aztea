# ── Otto embeddings proxy ──────────────────────────────────────────────────────
# POST /otto/embeddings — a self-contained, authenticated passthrough to Azure
# OpenAI's **Embeddings API** (text-embedding-3-large) for the Otto desktop app's
# semantic RECALL (procedural / corrective memory). The embeddings twin of
# /otto/responses (part_018): same shared-bearer auth + LiteLLM-or-direct upstream
# model, so the Azure key NEVER ships in the app and cloud RECALL works out of the
# box for every signed-in user — not just power users with their own Azure key.
#
#   • Auth:   the same shared bearer secret as /otto/responses + /otto/realtime. The
#             app sends `Authorization: Bearer <T>`; checked against OTTO_APP_TOKEN.
#   • Upstream: forwards the embeddings body (`input` (+ optional `dimensions`)) to a
#             private LiteLLM gateway (/v1/embeddings) which holds the Azure key, pins
#             the deployment, and enforces a budget via the virtual key's max_budget.
#   • Budget: the embeddings virtual key's max_budget on the LiteLLM path; a small
#             SQLite cap on the legacy/rollback direct-Azure path. Exhausted → HTTP 402.
#
# Two upstream paths, selected by OTTO_EMBEDDINGS_USE_LITELLM (OTTO_USE_LITELLM fallback):
#   • LiteLLM gateway (preferred): POST the body to the local LiteLLM /v1/embeddings.
#   • Direct Azure (flag off, legacy/rollback): the original passthrough + SQLite cap.
#
# Server env:
#   OTTO_APP_TOKEN                  shared bearer secret (same one /otto/responses uses)
#   OTTO_EMBEDDINGS_USE_LITELLM     "1" → route via the LiteLLM gateway; else direct Azure
#   OTTO_EMBEDDINGS_LITELLM_URL     LiteLLM base, e.g. http://127.0.0.1:4001
#   OTTO_EMBEDDINGS_LITELLM_KEY     LiteLLM virtual key (max_budget; server-side ONLY)
#   OTTO_EMBEDDINGS_LITELLM_MODEL   LiteLLM model alias to pin (default otto-embeddings)
#   AZURE_EMBEDDINGS_URL            [legacy/rollback] Azure resource base
#   AZURE_EMBEDDINGS_KEY            [legacy/rollback] the Azure resource key (server-side ONLY)
#   AZURE_EMBEDDINGS_API_VERSION    [legacy/rollback] api-version (default 2024-12-01-preview)
#   AZURE_EMBEDDINGS_MODEL          [legacy/rollback] deployment name (default text-embedding-3-large)
#   OTTO_EMBEDDINGS_BUDGET_CAP_CENTS [legacy/rollback] SQLite spend cap in cents (default 5000 = $50)
#   OTTO_BUDGET_DB                  [legacy/rollback] sqlite path (shared with the other Otto proxies)
import asyncio
import json
import os
import sqlite3

# Async upstream + concurrency cap, same rationale as /otto/responses (part_018): a slow upstream
# must never drain the worker threadpool and wedge every other route. Embeddings calls are short and
# batchy, so the cap can be a touch higher than responses.
_OTTO_EMB_UPSTREAM_TIMEOUT = float(os.environ.get("OTTO_EMBEDDINGS_TIMEOUT_S") or 30)
try:
    _OTTO_EMB_MAX_CONCURRENCY = int(os.environ.get("OTTO_EMBEDDINGS_MAX_CONCURRENCY") or 32)
except (TypeError, ValueError):
    _OTTO_EMB_MAX_CONCURRENCY = 32
_OTTO_EMB_SEM = asyncio.Semaphore(_OTTO_EMB_MAX_CONCURRENCY)

# Approximate Azure text-embedding-3-large price, cents per 1,000,000 tokens. Only sizes the legacy
# SQLite cap — TUNE to your actual Azure rate card. Rounding UP trips the cap slightly early.
_OTTO_EMB_RATE = 13.0   # ~$0.13 / 1M tokens


def _otto_emb_budget_cap_cents() -> float:
    try:
        return float(os.environ.get("OTTO_EMBEDDINGS_BUDGET_CAP_CENTS") or 5000)
    except (TypeError, ValueError):
        return 5000.0


def _otto_emb_budget_conn() -> sqlite3.Connection:
    # Reuses the shared Otto budget db + WAL/busy_timeout discipline from part_018.
    conn = sqlite3.connect(_otto_resp_budget_db(), timeout=10)  # noqa: F821 (from part_018)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS otto_embeddings_budget ("
        "  id INTEGER PRIMARY KEY CHECK(id = 1),"
        "  spent_cents REAL NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT OR IGNORE INTO otto_embeddings_budget (id, spent_cents) VALUES (1, 0)")
    conn.commit()
    return conn


def _otto_emb_budget_try_reserve(cost_cents: float) -> bool:
    cap = _otto_emb_budget_cap_cents()
    conn = _otto_emb_budget_conn()
    try:
        cur = conn.execute(
            "UPDATE otto_embeddings_budget SET spent_cents = spent_cents + ? "
            "WHERE id = 1 AND spent_cents + ? <= ?",
            (cost_cents, cost_cents, cap),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _otto_emb_budget_adjust(delta_cents: float) -> None:
    conn = _otto_emb_budget_conn()
    try:
        conn.execute(
            "UPDATE otto_embeddings_budget SET spent_cents = MAX(0, spent_cents + ?) WHERE id = 1",
            (delta_cents,),
        )
        conn.commit()
    finally:
        conn.close()


def _otto_emb_use_litellm() -> bool:
    # Per-path flag with OTTO_USE_LITELLM as the shared fallback, so embeddings can cut over to the
    # gateway independently of responses/realtime.
    v = (
        os.environ.get("OTTO_EMBEDDINGS_USE_LITELLM")
        or os.environ.get("OTTO_USE_LITELLM")
        or ""
    ).strip().lower()
    return v in ("1", "true", "yes", "on")


async def _otto_emb_via_litellm(body: dict) -> Response:  # noqa: F821
    """Forward the embeddings body to the local LiteLLM gateway (async; preferred path).

    LiteLLM holds the Azure key and enforces the cap via the virtual key's max_budget — so the
    SQLite reserve is bypassed here. A budget rejection maps to the app's 402 at-capacity contract.
    """
    gw_base = (os.environ.get("OTTO_EMBEDDINGS_LITELLM_URL") or "http://127.0.0.1:4001").strip().rstrip("/")
    gw_key = (os.environ.get("OTTO_EMBEDDINGS_LITELLM_KEY") or "").strip()
    if not gw_base or not gw_key:
        raise HTTPException(  # noqa: F821
            status_code=503,
            detail=error_codes.make_error("server.unavailable", "Otto service is not configured (no gateway)."),  # noqa: F821
        )
    # Pin to the budgeted virtual key's model alias so routing + spend attribution resolve.
    body["model"] = (os.environ.get("OTTO_EMBEDDINGS_LITELLM_MODEL") or "otto-embeddings").strip()
    headers = {"authorization": f"Bearer {gw_key}", "content-type": "application/json"}
    try:
        gw = await _otto_http().post(  # noqa: F821 (shared async client from part_018)
            f"{gw_base}/v1/embeddings", json=body, headers=headers, timeout=_OTTO_EMB_UPSTREAM_TIMEOUT
        )
    except Exception:
        raise HTTPException(  # noqa: F821
            status_code=502,
            detail=error_codes.make_error("upstream.unavailable", "Could not reach the model service. Please try again."),  # noqa: F821
        )
    if gw.status_code == 200:
        try:
            return JSONResponse(status_code=200, content=gw.json())  # noqa: F821
        except Exception:
            raise HTTPException(  # noqa: F821
                status_code=502,
                detail=error_codes.make_error("upstream.unavailable", "Malformed response from the model service. Please try again."),  # noqa: F821
            )
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


def _otto_emb_via_azure_sync(body: dict) -> Response:  # noqa: F821
    """Legacy/rollback path: direct Azure passthrough + the SQLite shared-budget cap.

    Blocking (requests + sqlite), so the async handler runs it via asyncio.to_thread. The deployment
    is server-pinned so a bearer-holder can't switch to a pricier embedding model.
    """
    azure_base = (os.environ.get("AZURE_EMBEDDINGS_URL") or "").strip().rstrip("/")
    azure_key = (os.environ.get("AZURE_EMBEDDINGS_KEY") or "").strip()
    api_version = (os.environ.get("AZURE_EMBEDDINGS_API_VERSION") or "2024-12-01-preview").strip()
    deployment = (os.environ.get("AZURE_EMBEDDINGS_MODEL") or "text-embedding-3-large").strip()
    if not azure_base or not azure_key:
        raise HTTPException(  # noqa: F821
            status_code=503,
            detail=error_codes.make_error("server.unavailable", "Otto service is not configured (no upstream model key)."),  # noqa: F821
        )

    # Reserve an upper-bound estimate (~chars/4 tokens) against the shared pool. 402 if it can't cover it.
    input_chars = len(json.dumps(body.get("input") or [], default=str))
    estimate_cents = (input_chars / 4.0) * _OTTO_EMB_RATE / 1_000_000.0
    if not _otto_emb_budget_try_reserve(estimate_cents):
        raise HTTPException(  # noqa: F821
            status_code=402,
            detail=error_codes.make_error(
                "payment.spend_limit_exceeded",
                "Otto is at capacity (shared budget reached). Please try again later.",
            ),
        )

    # Azure embeddings carry the deployment in the path; strip any client-sent model so it can't override.
    body.pop("model", None)
    url = f"{azure_base}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
    headers = {"api-key": azure_key, "content-type": "application/json"}
    try:
        upstream = http.post(url, json=body, headers=headers, timeout=60)  # noqa: F821
    except Exception:
        _otto_emb_budget_adjust(-estimate_cents)  # refund the reservation
        raise HTTPException(  # noqa: F821
            status_code=502,
            detail=error_codes.make_error("upstream.unavailable", "Could not reach the model service. Please try again."),  # noqa: F821
        )

    if upstream.status_code != 200:
        _otto_emb_budget_adjust(-estimate_cents)
        try:
            err_body = upstream.json()
        except Exception:
            err_body = error_codes.make_error("upstream.unavailable", (upstream.text or "")[:500] or "Upstream error.")  # noqa: F821
        return JSONResponse(status_code=upstream.status_code, content=err_body)  # noqa: F821

    data = upstream.json()
    # Reconcile the reservation to actual usage. Never fail the request over settlement.
    # Guard the usage-less 200: if the response omits/zeros token counts, do NOT refund the
    # reservation down to ~0 (that would record a real call as free and let a caller who can
    # induce usage-less 200s drain the pool). Keep the conservative estimate in that case.
    try:
        usage = data.get("usage") or {}
        tokens = float(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
        if tokens > 0:
            actual_cents = tokens * _OTTO_EMB_RATE / 1_000_000.0
            _otto_emb_budget_adjust(actual_cents - estimate_cents)
    except Exception:
        pass

    return JSONResponse(status_code=200, content=data)  # noqa: F821


@app.post(  # noqa: F821  (app/limiter/etc are provided by the shared shard namespace)
    "/otto/embeddings",
    responses=_error_responses(400, 401, 402, 429, 502, 503),  # noqa: F821
)
@limiter.limit("240/minute")  # noqa: F821
async def otto_embeddings(request: Request, body: dict = Body(...)) -> Response:  # noqa: F821
    """Authenticated Azure Embeddings proxy for the Otto app's semantic RECALL.

    ASYNC + concurrency-bounded like /otto/responses so a slow upstream can't wedge the process.
    Auth + validation happen BEFORE taking a slot.
    """
    # 1. Auth: shared bearer secret.
    expected = os.environ.get("OTTO_APP_TOKEN", "").strip()
    if not expected:
        raise HTTPException(  # noqa: F821
            status_code=503,
            detail=error_codes.make_error("server.unavailable", "Otto service is not configured (no app token)."),  # noqa: F821
        )
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
    if not _otto_proxy_auth_ok(token):  # noqa: F821 (from part_016)
        raise HTTPException(  # noqa: F821
            status_code=401,
            detail=error_codes.make_error("auth.invalid_or_expired_token", "Invalid Otto app token."),  # noqa: F821
        )

    # Embeddings requests carry `input` (a string or list of strings).
    if not isinstance(body, dict) or not body.get("input"):
        raise HTTPException(  # noqa: F821
            status_code=400,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,  # noqa: F821
                "Request body must be an Embeddings API request including 'input'.",
            ),
        )

    # Cap the input so an authenticated bearer-holder can't POST a multi-megabyte batch (memory
    # recall sends a handful of short strings). Reject oversized batches/payloads BEFORE reserving
    # budget or taking a concurrency slot. Bounds: <= 512 items and <= 256 KB of input JSON.
    _inp = body.get("input")
    _items = len(_inp) if isinstance(_inp, list) else 1
    if _items > 512 or len(json.dumps(_inp, default=str)) > 262_144:
        raise HTTPException(  # noqa: F821
            status_code=400,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,  # noqa: F821
                "Embeddings 'input' is too large (max 512 items / 256 KB).",
            ),
        )

    # 2. Upstream call, concurrency-bounded.
    async with _OTTO_EMB_SEM:
        if _otto_emb_use_litellm():
            return await _otto_emb_via_litellm(body)
        return await asyncio.to_thread(_otto_emb_via_azure_sync, body)
