# ── Otto proxy ────────────────────────────────────────────────────────────────
# POST /otto/chat — a thin authenticated passthrough to Anthropic's Messages API
# for the Otto desktop app.
#
# The app authenticates with a shared "Otto app" API key
# (Authorization: Bearer az_...). This endpoint forwards the request to Anthropic
# using the SERVER-side ANTHROPIC_API_KEY, so no Anthropic key ever ships inside
# the downloadable app. The app's request/response body is the standard
# /v1/messages shape and is passed through unchanged (tools included).
#
# Spend cap: we meter against the Otto service wallet's BALANCE. Seed that wallet
# with the budget (e.g. $200) and each call is reconciled to its ACTUAL token
# cost, so the balance is the true running spend. When it can't cover the next
# call, pre_call_charge raises InsufficientBalanceError → HTTP 402 ("at capacity")
# for every caller sharing the key. We use balance (not the per-key
# max_spend_cents) because the per-key cap counts gross charges — post_call_refund
# does not carry charged_by_key_id, so it would not net out our estimate refund.
#
# Flow: gate on an upper-bound estimate (never start a call the budget can't
# cover) → forward to Anthropic → on success reconcile to actual usage (refund the
# estimate, charge the actual) → on any upstream failure refund the estimate.
import json
import os

from core.llm.pricing import estimate_cost, estimate_request_cost

_OTTO_AGENT_ID = "otto-proxy"
_OTTO_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_OTTO_DEFAULT_MODEL = "claude-opus-4-8"
_OTTO_FALLBACK_MAX_TOKENS = 4096


@app.post(
    "/otto/chat",
    responses=_error_responses(400, 401, 402, 403, 429, 502, 503),
)
@limiter.limit("120/minute")
def otto_chat(
    request: Request,
    body: dict = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> Response:
    """Authenticated Claude proxy for the Otto desktop app (see module header)."""
    _require_scope(caller, "caller")

    if not isinstance(body, dict) or not body.get("messages"):
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                "Request body must be a Messages API request including 'messages'.",
            ),
        )

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
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

    # 1. Gate: reserve an upper-bound estimate against the wallet budget. If the
    #    balance can't cover it, _pre_call_charge_or_402 raises HTTP 402.
    estimate_cents = estimate_request_cost("anthropic", model, prompt_chars, max_tokens)
    caller_wallet = payments.get_or_create_wallet(_caller_owner_id(request))
    caller_wallet_id = caller_wallet["wallet_id"]
    charge_tx_id = _pre_call_charge_or_402(
        caller=caller,
        caller_wallet_id=caller_wallet_id,
        charge_cents=estimate_cents,
        agent_id=_OTTO_AGENT_ID,
    )

    # 2. Forward to Anthropic with the server-side key. Pass through the
    #    anthropic-version / -beta headers the app sent.
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
        payments.post_call_refund(
            caller_wallet_id, charge_tx_id, estimate_cents, _OTTO_AGENT_ID
        )
        raise HTTPException(
            status_code=502,
            detail=error_codes.make_error(
                "upstream.unavailable",
                "Could not reach the model service. Please try again.",
            ),
        )

    # Upstream returned an error → refund the reservation, surface the real
    # status + body so the app shows the actual Anthropic error.
    if upstream.status_code != 200:
        payments.post_call_refund(
            caller_wallet_id, charge_tx_id, estimate_cents, _OTTO_AGENT_ID
        )
        try:
            err_body = upstream.json()
        except Exception:
            err_body = error_codes.make_error(
                "upstream.unavailable",
                (upstream.text or "")[:500] or "Upstream error.",
            )
        return JSONResponse(status_code=upstream.status_code, content=err_body)

    data = upstream.json()

    # 3. Reconcile the reservation to actual usage so the wallet balance tracks
    #    real spend. Refund the estimate, then charge the actual. Because
    #    actual <= estimate and the estimate already cleared the balance check,
    #    the re-charge clears too. A settlement hiccup must never fail the user's
    #    request — they already have their answer; the estimate just stands.
    try:
        usage = data.get("usage") or {}
        actual_cents = estimate_cost(
            "anthropic",
            str(data.get("model") or model),
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
        )
        payments.post_call_refund(
            caller_wallet_id, charge_tx_id, estimate_cents, _OTTO_AGENT_ID
        )
        if actual_cents > 0:
            payments.pre_call_charge(
                caller_wallet_id,
                actual_cents,
                _OTTO_AGENT_ID,
                charged_by_key_id=str(caller.get("key_id") or "").strip() or None,
                max_spend_cents=_caller_key_spend_cap(caller),
            )
    except Exception:
        pass

    return JSONResponse(status_code=200, content=data)
