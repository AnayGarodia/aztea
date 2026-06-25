# ── Otto proxy ────────────────────────────────────────────────────────────────
# POST /otto/chat — a self-contained, authenticated passthrough to Anthropic's
# Messages API for the Otto desktop app. Deliberately does NOT use aztea's
# user/wallet/payments framework — it is a standalone shortcut.
#
#   • Auth:   a single shared secret. The app sends `Authorization: Bearer <T>`;
#             this checks T against the OTTO_APP_TOKEN env var. (The token is
#             baked into the app and therefore extractable — that's fine; the
#             budget below is the real protection, not the token's secrecy.)
#   • Upstream: forwards the /v1/messages body unchanged to Anthropic using the
#             SERVER-side ANTHROPIC_API_KEY, so no Anthropic key ships in the app.
#   • Budget: a single shared spend pool, tracked in a tiny SQLite counter and
#             priced at real Anthropic rates. Each call reserves an upper-bound
#             estimate, then reconciles to actual token cost, so the cap maps to
#             real dollars. When the pool is exhausted → HTTP 402 for everyone
#             until it's reset/raised.
#
# Server env:
#   OTTO_APP_TOKEN          shared bearer secret (must match the app's baked-in token)
#   ANTHROPIC_API_KEY       the real Anthropic key (already used by aztea)
#   OTTO_BUDGET_CAP_CENTS   spend cap in cents (default 20000 = $200)
#   OTTO_BUDGET_DB          sqlite path (default ~/.otto-proxy-budget.sqlite3)
import hmac
import json
import os
import sqlite3

_OTTO_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_OTTO_DEFAULT_MODEL = "claude-opus-4-8"
_OTTO_FALLBACK_MAX_TOKENS = 4096

# Real Anthropic list prices, (input, output) cents per 1,000,000 tokens.
# Approximate + adjustable — this only sizes the shared spend cap.
_OTTO_RATES = {
    "fable": (1000, 5000),
    "opus": (500, 2500),
    "sonnet": (300, 1500),
    "haiku": (100, 500),
}
_OTTO_DEFAULT_RATE = _OTTO_RATES["opus"]


def _otto_rate(model: str) -> tuple[int, int]:
    m = (model or "").lower()
    for key, rate in _OTTO_RATES.items():
        if key in m:
            return rate
    return _OTTO_DEFAULT_RATE


def _otto_cost_cents(model: str, input_tokens: float, output_tokens: float) -> float:
    in_rate, out_rate = _otto_rate(model)
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0


def _otto_budget_db() -> str:
    return os.environ.get("OTTO_BUDGET_DB") or os.path.expanduser(
        "~/.otto-proxy-budget.sqlite3"
    )


def _otto_budget_cap_cents() -> float:
    try:
        return float(os.environ.get("OTTO_BUDGET_CAP_CENTS") or 20000)
    except (TypeError, ValueError):
        return 20000.0


def _otto_budget_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_otto_budget_db(), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS otto_budget ("
        "  id INTEGER PRIMARY KEY CHECK(id = 1),"
        "  spent_cents REAL NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT OR IGNORE INTO otto_budget (id, spent_cents) VALUES (1, 0)")
    conn.commit()
    return conn


def _otto_budget_try_reserve(cost_cents: float) -> bool:
    """Atomically add cost_cents iff it keeps the pool within the cap."""
    cap = _otto_budget_cap_cents()
    conn = _otto_budget_conn()
    try:
        cur = conn.execute(
            "UPDATE otto_budget SET spent_cents = spent_cents + ? "
            "WHERE id = 1 AND spent_cents + ? <= ?",
            (cost_cents, cost_cents, cap),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _otto_budget_adjust(delta_cents: float) -> None:
    """Apply a delta (e.g. refund a reservation, or reconcile est → actual)."""
    conn = _otto_budget_conn()
    try:
        conn.execute(
            "UPDATE otto_budget SET spent_cents = MAX(0, spent_cents + ?) WHERE id = 1",
            (delta_cents,),
        )
        conn.commit()
    finally:
        conn.close()


@app.post(
    "/otto/chat",
    responses=_error_responses(400, 401, 402, 429, 502, 503),
)
@limiter.limit("120/minute")
def otto_chat(request: Request, body: dict = Body(...)) -> Response:
    """Standalone authenticated Claude proxy for the Otto desktop app."""
    # 1. Auth: shared bearer secret (constant-time compare).
    expected = os.environ.get("OTTO_APP_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=error_codes.make_error(
                "server.unavailable", "Otto service is not configured (no app token)."
            ),
        )
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail=error_codes.make_error(
                "auth.invalid_or_expired_token", "Invalid Otto app token."
            ),
        )

    if not isinstance(body, dict) or not body.get("messages"):
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                "Request body must be a Messages API request including 'messages'.",
            ),
        )

    # Dedicated key for the Otto proxy so the $150 Otto budget stays isolated from the
    # main app's own Anthropic usage (core/llm/providers/anthropic_provider.py also reads
    # ANTHROPIC_API_KEY). Falls back to ANTHROPIC_API_KEY for back-compat.
    anthropic_key = (
        os.environ.get("OTTO_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    ).strip()
    if not anthropic_key:
        raise HTTPException(
            status_code=503,
            detail=error_codes.make_error(
                "server.unavailable",
                "Otto service is not configured (no upstream model key).",
            ),
        )

    model = str(body.get("model") or _OTTO_DEFAULT_MODEL)
    try:
        max_tokens = int(body.get("max_tokens") or _OTTO_FALLBACK_MAX_TOKENS)
    except (TypeError, ValueError):
        max_tokens = _OTTO_FALLBACK_MAX_TOKENS
    prompt_chars = (
        len(json.dumps(body.get("system") or "", default=str))
        + len(json.dumps(body.get("messages") or [], default=str))
        + len(json.dumps(body.get("tools") or [], default=str))
    )

    # 2. Reserve an upper-bound estimate against the shared pool (~4 chars/token
    #    in; max_tokens out). 402 if the pool can't cover it.
    estimate_cents = _otto_cost_cents(model, prompt_chars / 4.0, max_tokens)
    if not _otto_budget_try_reserve(estimate_cents):
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                "payment.spend_limit_exceeded",
                "Otto is at capacity (shared budget reached). Please try again later.",
            ),
        )

    # 3. Forward to Anthropic with the server-side key.
    headers = {
        "x-api-key": anthropic_key,
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        "content-type": "application/json",
    }
    beta_header = request.headers.get("anthropic-beta")
    if beta_header:
        headers["anthropic-beta"] = beta_header

    try:
        upstream = http.post(
            _OTTO_ANTHROPIC_URL, json=body, headers=headers, timeout=120
        )
    except Exception:
        _otto_budget_adjust(-estimate_cents)  # refund the reservation
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                "upstream.unavailable",
                "Could not reach the model service. Please try again.",
            ),
        )

    if upstream.status_code != 200:
        _otto_budget_adjust(-estimate_cents)
        try:
            err_body = upstream.json()
        except Exception:
            err_body = error_codes.make_error(
                "upstream.unavailable",
                (upstream.text or "")[:500] or "Upstream error.",
            )
        return JSONResponse(status_code=upstream.status_code, content=err_body)

    data = upstream.json()

    # 4. Reconcile the reservation to actual usage (estimate → actual), so the
    #    pool tracks real spend. Never fail the user's request over settlement.
    try:
        usage = data.get("usage") or {}
        actual_cents = _otto_cost_cents(
            str(data.get("model") or model),
            float(usage.get("input_tokens") or 0),
            float(usage.get("output_tokens") or 0),
        )
        _otto_budget_adjust(actual_cents - estimate_cents)
    except Exception:
        pass

    return JSONResponse(status_code=200, content=data)
