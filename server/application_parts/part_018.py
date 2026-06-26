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
# Server env:
#   OTTO_APP_TOKEN                shared bearer secret (must match the app's baked token;
#                                 the SAME one /otto/chat + /otto/realtime use)
#   AZURE_RESPONSES_URL          Azure resource base, e.g.
#                                 https://aztea-foundry.services.ai.azure.com
#   AZURE_RESPONSES_KEY          the Azure resource key (server-side ONLY)
#   AZURE_RESPONSES_API_VERSION  api-version query (default 2025-04-01-preview)
#   AZURE_RESPONSES_MODEL        optional deployment pin; overrides the body's model when set
#   OTTO_RESPONSES_BUDGET_CAP_CENTS  spend cap in cents (default 15000 = $150)
#   OTTO_BUDGET_DB               sqlite path (shared with the other Otto proxies)
import hmac
import json
import os
import sqlite3

_OTTO_RESP_FALLBACK_MAX_TOKENS = 4096

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


@app.post(  # noqa: F821  (app/limiter/etc are provided by the shared shard namespace)
    "/otto/responses",
    responses=_error_responses(400, 401, 402, 429, 502, 503),  # noqa: F821
)
@limiter.limit("120/minute")  # noqa: F821
def otto_responses(request: Request, body: dict = Body(...)) -> Response:  # noqa: F821
    """Standalone authenticated Azure Responses (gpt-5.x) proxy for the Otto app."""
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

    # 2. Upstream config must be present.
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

    # 3. Reserve an upper-bound estimate against the shared pool (~4 chars/token in;
    #    max_output_tokens out). 402 if the pool can't cover it.
    estimate_cents = _otto_resp_cost_cents(prompt_chars / 4.0, max_tokens)
    if not _otto_resp_budget_try_reserve(estimate_cents):
        raise HTTPException(  # noqa: F821
            status_code=402,
            detail=error_codes.make_error(
                "payment.spend_limit_exceeded",
                "Otto is at capacity (shared budget reached). Please try again later.",
            ),
        )

    # 4. Forward to Azure with the server-side key.
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

    # 5. Reconcile the reservation to actual usage. Responses API reports
    #    usage.input_tokens / usage.output_tokens. Never fail the request over settlement.
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
