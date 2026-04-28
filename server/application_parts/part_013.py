# server.application shard 12 — wallet routes (top-up, deposit, withdraw,
# connect onboard, wallet read), run history, and the catch-all SPA
# fallback that serves frontend/dist/ for non-API URLs. MUST remain the
# last shard so the SPA catch-all route is registered after every API
# route.


@app.post(
    "/wallets/topup/session",
    tags=["wallet"],
    summary="Create a Stripe Checkout session for wallet top-up.",
    responses=_error_responses(400, 401, 403, 404, 422, 429, 500, 503),
)
@limiter.limit("20/minute")
def create_topup_session(
    request: Request,
    body: core_models.TopupSessionRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    if not _STRIPE_AVAILABLE or not _STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment processing is not configured on this server.")
    _require_scope(caller, "caller")
    wallet = payments.get_wallet(body.wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail=f"Wallet '{body.wallet_id}' not found.")
    if caller["type"] != "master" and wallet["owner_id"] != caller["owner_id"]:
        raise HTTPException(status_code=403, detail="Not authorized to top up this wallet.")
    if int(body.amount_cents) < MINIMUM_DEPOSIT_CENTS:
        raise _deposit_below_minimum_error(int(body.amount_cents))
    if not (100 <= body.amount_cents <= 50000):
        raise HTTPException(status_code=400, detail="Amount must be between $1.00 and $500.00.")
    if _TOPUP_DAILY_LIMIT_CENTS > 0:
        used_last_24h = _wallet_stripe_topup_total_last_24h(body.wallet_id)
        projected_total = used_last_24h + int(body.amount_cents)
        if projected_total > _TOPUP_DAILY_LIMIT_CENTS:
            limit_usd = _TOPUP_DAILY_LIMIT_CENTS / 100
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "payment.topup_daily_limit_exceeded",
                    "message": f"Daily top-up limit exceeded (${limit_usd:,.2f}/24h).",
                    "data": {
                        "limit_cents": _TOPUP_DAILY_LIMIT_CENTS,
                        "used_cents_last_24h": used_last_24h,
                        "requested_cents": int(body.amount_cents),
                    },
                },
            )

    _stripe_lib.api_key = _STRIPE_SECRET_KEY
    try:
        session = _stripe_lib.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "Aztea wallet top-up",
                        "description": f"Add ${body.amount_cents / 100:.2f} to your Aztea wallet.",
                    },
                    "unit_amount": body.amount_cents,
                },
                "quantity": 1,
            }],
            mode="payment",
            client_reference_id=body.wallet_id,
            metadata={
                "wallet_id": body.wallet_id,
                "owner_id": caller["owner_id"],
            },
            success_url=f"{_FRONTEND_BASE_URL}/wallet?payment=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{_FRONTEND_BASE_URL}/wallet?payment=cancelled",
        )
    except Exception as exc:
        status_code, payload = _stripe_http_error("topup_session", exc)
        raise HTTPException(status_code=status_code, detail=payload)
    return JSONResponse({"checkout_url": session.url, "session_id": session.id})


@app.post(
    "/stripe/webhook",
    tags=["wallet"],
    summary="Stripe webhook receiver: credits wallet on successful checkout.",
    include_in_schema=False,
)
@limiter.limit("300/minute")
async def stripe_webhook(request: Request) -> JSONResponse:
    if not _STRIPE_AVAILABLE or not _STRIPE_SECRET_KEY or not _STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Stripe not configured.")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        _stripe_lib.api_key = _STRIPE_SECRET_KEY
        event = _stripe_lib.Webhook.construct_event(payload, sig_header, _STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature.")

    if event["type"] == "checkout.session.completed":
        # Stripe SDK v15 returns StripeObjects, not plain dicts — use attribute
        # access and fall back via getattr to avoid KeyError / AttributeError.
        session_obj = event["data"]["object"]
        _meta = _stripe_obj_get(session_obj, "metadata", None) or {}
        wallet_id = (
            _stripe_obj_get(session_obj, "client_reference_id", None)
            or _stripe_obj_get(_meta, "wallet_id", None)
        )
        amount_cents = _stripe_obj_get(session_obj, "amount_total", None)
        session_id = _stripe_obj_id(session_obj)

        if not wallet_id or not amount_cents or not session_id:
            _LOG.warning("Stripe webhook: missing wallet_id/amount/session_id in %s", session_id)
            return JSONResponse({"received": True, "status": "skipped"})

        idempotency_state = _stripe_begin_checkout_webhook_event(
            session_id=session_id,
            wallet_id=str(wallet_id),
            amount_cents=int(amount_cents),
        )
        if idempotency_state == "already_processed":
            return JSONResponse({"received": True, "status": "already_processed"})
        if idempotency_state == "already_processing":
            return JSONResponse({"received": True, "status": "processing"})

        try:
            payments.deposit(str(wallet_id), int(amount_cents), f"Stripe payment [{session_id[:12]}]")
        except Exception as exc:
            _stripe_mark_checkout_webhook_failed(
                session_id=session_id,
                error_message=str(exc),
            )
            _LOG.exception("Failed to deposit Stripe payment for session %s wallet %s", session_id, wallet_id)
            return JSONResponse({"received": True, "status": "deposit_failed"}, status_code=500)
        _stripe_mark_checkout_webhook_processed(
            session_id=session_id,
            wallet_id=str(wallet_id),
            amount_cents=int(amount_cents),
        )

        _LOG.info("Stripe top-up: %d cents → wallet %s (session %s)", amount_cents, wallet_id, session_id)
        # Notify wallet owner
        try:
            _wallet_row = payments.get_wallet(str(wallet_id))
            if _wallet_row:
                _deposit_email = _get_owner_email(_wallet_row.get("owner_id", ""))
                if _deposit_email:
                    _email.send_deposit_confirmed(_deposit_email, int(amount_cents))
        except Exception:
            _LOG.warning("Failed to send deposit email for wallet %s", wallet_id)

    if event["type"] == "account.updated":
        # Stripe Connect: account completed onboarding or details changed
        account_obj = event["data"]["object"]
        account_id = _stripe_obj_id(account_obj)
        charges_enabled = bool(_stripe_obj_get(account_obj, "charges_enabled", False))
        payouts_enabled = bool(_stripe_obj_get(account_obj, "payouts_enabled", False))
        fully_enabled = bool(charges_enabled and payouts_enabled)
        if account_id:
            with get_db_connection() as _ac_conn:
                _ac_conn.execute(
                    "UPDATE wallets SET stripe_connect_enabled = ? WHERE stripe_connect_account_id = ?",
                    (1 if fully_enabled else 0, account_id),
                )
                _ac_conn.commit()
            _LOG.info(
                "Stripe Connect account.updated: %s charges_enabled=%s payouts_enabled=%s",
                account_id, charges_enabled, payouts_enabled,
            )

    return JSONResponse({"received": True, "status": "ok"})


# Prefer Accounts v2 for new Connect integrations; keep v1 fallback for SDK
# compatibility in environments where v2 resources are not yet available.
def _create_connect_account() -> str:
    v2 = _stripe_obj_get(_stripe_lib, "v2", None)
    core = _stripe_obj_get(v2, "core", None) if v2 is not None else None
    accounts = _stripe_obj_get(core, "accounts", None) if core is not None else None
    create_v2 = _stripe_obj_get(accounts, "create", None) if accounts is not None else None
    if callable(create_v2):
        try:
            account_v2 = create_v2(
                controller={
                    "losses": {"payments": "application"},
                    "fees": {"payer": "application"},
                    "stripe_dashboard": {"type": "express"},
                    "requirement_collection": "stripe",
                }
            )
            account_id = _stripe_obj_id(account_v2)
            if account_id:
                return account_id
        except Exception as exc:
            _LOG.warning("Stripe Accounts v2 account creation failed, falling back to v1: %s", exc)

    account_v1 = _stripe_lib.Account.create(
        type="express",
        capabilities={"transfers": {"requested": True}},
    )
    account_id = _stripe_obj_id(account_v1)
    if not account_id:
        raise RuntimeError("Stripe account creation returned no account id.")
    return account_id


# ---------------------------------------------------------------------------
# Stripe Connect — onboard, status, withdraw
# ---------------------------------------------------------------------------


@app.post(
    "/wallets/connect/onboard",
    tags=["wallet"],
    summary="Create a Stripe connected account and return an onboarding URL.",
    responses=_error_responses(400, 401, 403, 503),
)
@limiter.limit("10/minute")
def connect_onboard(
    request: Request,
    body: core_models.ConnectOnboardRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    if not _STRIPE_AVAILABLE or not _STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment processing is not configured on this server.")
    _require_scope(caller, "caller")

    wallet = payments.get_wallet_by_owner(caller["owner_id"])
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found.")

    _stripe_lib.api_key = _STRIPE_SECRET_KEY

    # Reuse existing Connect account if one already exists
    existing_account_id = wallet.get("stripe_connect_account_id")
    if not existing_account_id:
        try:
            existing_account_id = _create_connect_account()
        except Exception as exc:
            status_code, payload = _stripe_http_error("connect_onboard_account_create", exc)
            raise HTTPException(status_code=status_code, detail=payload)
        with get_db_connection() as _ac_conn:
            _ac_conn.execute(
                "UPDATE wallets SET stripe_connect_account_id = ? WHERE wallet_id = ?",
                (existing_account_id, wallet["wallet_id"]),
            )
            _ac_conn.commit()

    return_url = (body.return_url or "").strip() or f"{_FRONTEND_BASE_URL}/wallet?connect=success"
    refresh_url = (body.refresh_url or "").strip() or f"{_FRONTEND_BASE_URL}/wallet?connect=refresh"

    try:
        link = _stripe_lib.AccountLink.create(
            account=existing_account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
    except Exception as exc:
        status_code, payload = _stripe_http_error("connect_onboard_link_create", exc)
        raise HTTPException(status_code=status_code, detail=payload)
    return JSONResponse({"onboarding_url": link.url, "account_id": existing_account_id})


@app.get(
    "/wallets/connect/status",
    tags=["wallet"],
    summary="Get Stripe Connect account status for the authenticated user.",
    responses=_error_responses(401, 403, 503),
)
@limiter.limit("30/minute")
def connect_status(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    if not _STRIPE_AVAILABLE or not _STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment processing is not configured on this server.")
    _require_scope(caller, "caller")

    wallet = payments.get_wallet_by_owner(caller["owner_id"])
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found.")

    account_id = wallet.get("stripe_connect_account_id")
    if not account_id:
        return JSONResponse({"connected": False, "charges_enabled": False, "account_id": None})

    _stripe_lib.api_key = _STRIPE_SECRET_KEY
    try:
        account = _stripe_lib.Account.retrieve(account_id)
        charges_enabled = bool(getattr(account, "charges_enabled", False))
    except Exception:
        charges_enabled = bool(wallet.get("stripe_connect_enabled", 0))

    # Keep local cache in sync
    if charges_enabled != bool(wallet.get("stripe_connect_enabled", 0)):
        with get_db_connection() as _ac_conn:
            _ac_conn.execute(
                "UPDATE wallets SET stripe_connect_enabled = ? WHERE wallet_id = ?",
                (1 if charges_enabled else 0, wallet["wallet_id"]),
            )
            _ac_conn.commit()

    return JSONResponse({
        "connected": True,
        "charges_enabled": charges_enabled,
        "account_id": account_id,
    })


@app.post(
    "/wallets/withdraw",
    tags=["wallet"],
    summary="Withdraw funds from wallet to connected Stripe account.",
    responses=_error_responses(400, 401, 403, 503),
)
@limiter.limit("10/minute")
def withdraw(
    request: Request,
    body: core_models.WithdrawRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    if not _STRIPE_AVAILABLE or not _STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment processing is not configured on this server.")
    _require_scope(caller, "caller")
    def _operation() -> tuple[dict[str, Any], int]:
        if body.amount_cents < 100:
            raise HTTPException(status_code=400, detail="Minimum withdrawal is $1.00.")
        if body.amount_cents > 1_000_000:
            raise HTTPException(status_code=400, detail="Maximum withdrawal is $10,000.00.")

        wallet = payments.get_wallet_by_owner(caller["owner_id"])
        if wallet is None:
            raise HTTPException(status_code=404, detail="Wallet not found.")

        account_id = str(wallet.get("stripe_connect_account_id") or "").strip()
        if not account_id:
            raise HTTPException(
                status_code=400,
                detail="No bank account connected. Use POST /wallets/connect/onboard first.",
            )

        if not wallet.get("stripe_connect_enabled"):
            raise HTTPException(
                status_code=400,
                detail="Your Stripe Connect account is not yet active. Complete onboarding first.",
            )

        if wallet["balance_cents"] < body.amount_cents:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient balance: have {wallet['balance_cents']}¢, need {body.amount_cents}¢.",
            )

        _stripe_lib.api_key = _STRIPE_SECRET_KEY
        request_idempotency_key = (request.headers.get(_IDEMPOTENCY_KEY_HEADER, "") or "").strip()
        stripe_idempotency_basis = request_idempotency_key or str(uuid.uuid4())
        stripe_idempotency_key = "aztea-withdraw-" + hashlib.sha256(
            f"{caller['owner_id']}:{wallet['wallet_id']}:{body.amount_cents}:{stripe_idempotency_basis}".encode(
                "utf-8"
            )
        ).hexdigest()

        # Debit wallet first (raises InsufficientBalanceError if something changed).
        try:
            payments.charge(
                wallet["wallet_id"],
                body.amount_cents,
                memo=f"Withdrawal to Stripe Connect [{account_id[:12]}]",
            )
        except payments.InsufficientBalanceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        try:
            transfer = _stripe_lib.Transfer.create(
                amount=body.amount_cents,
                currency="usd",
                destination=account_id,
                idempotency_key=stripe_idempotency_key,
            )
        except Exception as exc:
            # Refund the wallet charge on Stripe failure.
            try:
                payments.deposit(
                    wallet["wallet_id"],
                    body.amount_cents,
                    memo=f"Withdrawal refund (Stripe error): {exc}",
                )
            except Exception:
                _LOG.exception("Critical: failed to refund withdrawal for wallet %s", wallet["wallet_id"])
            status_code, payload = _stripe_http_error("withdraw_transfer", exc)
            raise HTTPException(status_code=status_code, detail=payload)

        transfer_id = _stripe_obj_id(transfer)
        if not transfer_id:
            raise HTTPException(status_code=502, detail="Stripe transfer response did not include an ID.")

        # Record the transfer for audit.
        with get_db_connection() as _tr_conn:
            _tr_conn.execute(
                "INSERT INTO stripe_connect_transfers (transfer_id, wallet_id, amount_cents, stripe_tx_id, memo, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    wallet["wallet_id"],
                    body.amount_cents,
                    transfer_id,
                    f"Withdrawal to {account_id[:12]}",
                    _utc_now_iso(),
                ),
            )
            _tr_conn.commit()

        _LOG.info(
            "Stripe Connect withdrawal: %d¢ from wallet %s → account %s (transfer %s)",
            body.amount_cents, wallet["wallet_id"], account_id, transfer_id,
        )
        try:
            _withdraw_email = _get_owner_email(caller.get("owner_id", ""))
            if _withdraw_email:
                _email.send_withdrawal_processed(_withdraw_email, body.amount_cents)
        except Exception:
            _LOG.warning("Failed to send withdrawal email for owner %s", caller.get("owner_id", ""))
        return {
            "status": "ok",
            "transfer_id": transfer_id,
            "amount_cents": body.amount_cents,
        }, 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope="wallets.withdraw",
        payload=body.model_dump(),
        operation=_operation,
    )


@app.get(
    "/wallets/withdrawals",
    response_model=core_models.WalletWithdrawalsResponse,
    tags=["wallet"],
    summary="List withdrawal audit history for the authenticated caller wallet.",
    responses=_error_responses(401, 403, 404, 422, 429, 500),
)
@limiter.limit("30/minute")
def wallet_withdrawals(
    request: Request,
    limit: int = 20,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletWithdrawalsResponse:
    _require_scope(caller, "caller")
    if limit <= 0:
        raise HTTPException(status_code=422, detail="limit must be > 0.")
    wallet = payments.get_wallet_by_owner(caller["owner_id"])
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found.")
    withdrawals = payments.list_connect_withdrawals(wallet["wallet_id"], limit=limit)
    return JSONResponse(content={"withdrawals": withdrawals, "count": len(withdrawals)})


@app.get(
    "/wallets/{wallet_id}",
    response_model=core_models.WalletResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def wallet_get(
    request: Request,
    wallet_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletResponse:
    wallet = payments.get_wallet(wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail=f"Wallet '{wallet_id}' not found.")
    if caller["type"] != "master" and wallet["owner_id"] != caller["owner_id"]:
        raise HTTPException(status_code=403, detail="Not authorized to view this wallet.")
    txs = payments.get_wallet_transactions(wallet_id, limit=50)
    return JSONResponse(content={**wallet, "transactions": txs})


# ---------------------------------------------------------------------------
# Billing: top-up history + saved Stripe payment methods. Top-up history is
# read directly from the existing ``stripe_sessions`` bookkeeping table. Saved
# cards live on a per-user Stripe Customer that we create lazily on the first
# SetupIntent and persist on ``users.stripe_customer_id``.
# ---------------------------------------------------------------------------


def _require_user_caller(caller: core_models.CallerContext) -> dict:
    if caller["type"] != "user":
        raise HTTPException(status_code=403, detail="Not available for master or agent-scoped keys.")
    return caller["user"]


def _ensure_stripe_customer(user: dict) -> str:
    customer_id = _auth.get_stripe_customer_id(user["user_id"])
    if customer_id:
        return customer_id
    _stripe_lib.api_key = _STRIPE_SECRET_KEY
    try:
        customer = _stripe_lib.Customer.create(
            email=user.get("email"),
            name=user.get("full_name") or user.get("username"),
            metadata={"user_id": user["user_id"]},
        )
    except Exception as exc:
        status_code, payload = _stripe_http_error("billing_customer_create", exc)
        raise HTTPException(status_code=status_code, detail=payload)
    customer_id = _stripe_obj_id(customer)
    if not customer_id:
        raise HTTPException(status_code=502, detail="Stripe did not return a customer id.")
    _auth.set_stripe_customer_id(user["user_id"], customer_id)
    return customer_id


@app.get(
    "/billing/topups",
    tags=["billing"],
    summary="List the authenticated user's Stripe wallet top-ups.",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def list_billing_topups(
    request: Request,
    limit: int = 25,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    user = _require_user_caller(caller)
    _require_scope(caller, "caller")
    wallet = payments.get_or_create_wallet(user["user_id"])
    safe_limit = max(1, min(int(limit or 25), 100))
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT session_id, wallet_id, amount_cents, processed_at
            FROM stripe_sessions
            WHERE wallet_id = ?
            ORDER BY processed_at DESC
            LIMIT ?
            """,
            (wallet["wallet_id"], safe_limit),
        ).fetchall()
    return JSONResponse({
        "wallet_id": wallet["wallet_id"],
        "topups": [
            {
                "session_id": r["session_id"],
                "wallet_id": r["wallet_id"],
                "amount_cents": int(r["amount_cents"] or 0),
                "processed_at": r["processed_at"],
            }
            for r in rows
        ],
    })


@app.post(
    "/billing/setup-session",
    tags=["billing"],
    summary="Create a Stripe Checkout setup session so the user can save a card on file.",
    responses=_error_responses(401, 403, 429, 500, 503),
)
@limiter.limit("20/minute")
def create_billing_setup_session(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    if not _STRIPE_AVAILABLE or not _STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment processing is not configured on this server.")
    user = _require_user_caller(caller)
    _require_scope(caller, "caller")
    customer_id = _ensure_stripe_customer(user)
    _stripe_lib.api_key = _STRIPE_SECRET_KEY
    try:
        session = _stripe_lib.checkout.Session.create(
            mode="setup",
            customer=customer_id,
            payment_method_types=["card"],
            success_url=f"{_FRONTEND_BASE_URL}/settings?card_added=1",
            cancel_url=f"{_FRONTEND_BASE_URL}/settings?card_added=cancelled",
        )
    except Exception as exc:
        status_code, payload = _stripe_http_error("billing_setup_session", exc)
        raise HTTPException(status_code=status_code, detail=payload)
    return JSONResponse({
        "checkout_url": _stripe_obj_get(session, "url", None),
        "session_id": _stripe_obj_id(session),
    })


@app.get(
    "/billing/payment-methods",
    tags=["billing"],
    summary="List the user's saved Stripe payment methods.",
    responses=_error_responses(401, 403, 429, 500, 503),
)
@limiter.limit("60/minute")
def list_billing_payment_methods(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    if not _STRIPE_AVAILABLE or not _STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment processing is not configured on this server.")
    user = _require_user_caller(caller)
    _require_scope(caller, "caller")
    customer_id = _auth.get_stripe_customer_id(user["user_id"])
    if not customer_id:
        return JSONResponse({"payment_methods": []})
    _stripe_lib.api_key = _STRIPE_SECRET_KEY
    try:
        result = _stripe_lib.PaymentMethod.list(customer=customer_id, type="card")
    except Exception as exc:
        status_code, payload = _stripe_http_error("billing_payment_methods_list", exc)
        raise HTTPException(status_code=status_code, detail=payload)
    cards = []
    for pm in _stripe_obj_get(result, "data", []) or []:
        card = _stripe_obj_get(pm, "card", None)
        cards.append({
            "id": _stripe_obj_id(pm),
            "brand": _stripe_obj_get(card, "brand", None) if card else None,
            "last4": _stripe_obj_get(card, "last4", None) if card else None,
            "exp_month": _stripe_obj_get(card, "exp_month", None) if card else None,
            "exp_year": _stripe_obj_get(card, "exp_year", None) if card else None,
        })
    return JSONResponse({"payment_methods": cards})


@app.delete(
    "/billing/payment-methods/{payment_method_id}",
    tags=["billing"],
    summary="Detach a saved card from the user's Stripe customer.",
    responses=_error_responses(401, 403, 404, 429, 500, 503),
)
@limiter.limit("30/minute")
def delete_billing_payment_method(
    request: Request,
    payment_method_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    if not _STRIPE_AVAILABLE or not _STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment processing is not configured on this server.")
    user = _require_user_caller(caller)
    _require_scope(caller, "caller")
    customer_id = _auth.get_stripe_customer_id(user["user_id"])
    if not customer_id:
        raise HTTPException(status_code=404, detail="No saved payment methods.")
    _stripe_lib.api_key = _STRIPE_SECRET_KEY
    try:
        pm = _stripe_lib.PaymentMethod.retrieve(payment_method_id)
    except Exception as exc:
        status_code, payload = _stripe_http_error("billing_payment_method_retrieve", exc)
        raise HTTPException(status_code=status_code, detail=payload)
    pm_customer = _stripe_obj_get(pm, "customer", None)
    if pm_customer != customer_id:
        raise HTTPException(status_code=404, detail="Payment method not found.")
    try:
        _stripe_lib.PaymentMethod.detach(payment_method_id)
    except Exception as exc:
        status_code, payload = _stripe_http_error("billing_payment_method_detach", exc)
        raise HTTPException(status_code=status_code, detail=payload)
    return JSONResponse({"ok": True, "payment_method_id": payment_method_id})


# ---------------------------------------------------------------------------
# SPA fallback: serve the built React app for non-API routes.
#
# Keeps the site working even when an upstream proxy forwards "/" to FastAPI
# (e.g. nginx misconfig or missing frontend/dist short-circuit), and replaces
# Starlette's default `{"detail": "Not Found"}` with either the SPA or a
# structured, user-actionable 404 payload.
# ---------------------------------------------------------------------------

from pathlib import Path as _SpaPath
from fastapi.responses import FileResponse as _SpaFileResponse

_FRONTEND_DIST_DIR = _SpaPath(_REPO_ROOT) / "frontend" / "dist"
_SPA_API_PREFIXES: tuple[str, ...] = (
    "api/",
    "auth/",
    "admin/",
    "agents/",
    "billing/",
    "builtin/",
    "config/",
    "disputes/",
    "health",
    "jobs",
    "llm/",
    "mcp/",
    "metrics",
    "onboarding/",
    "ops/",
    "openapi.json",
    "public/",
    "registry/",
    "reputation/",
    "runs",
    "skills",
    "stripe/",
    "wallets/",
    "webhooks/",
)


def _path_is_api(path_fragment: str) -> bool:
    normalized = path_fragment.lstrip("/").lower()
    if not normalized:
        return False
    return normalized.startswith(_SPA_API_PREFIXES)


def _resolved_under(parent: _SpaPath, candidate: _SpaPath) -> bool:
    try:
        candidate_resolved = candidate.resolve()
        parent_resolved = parent.resolve()
    except (OSError, RuntimeError):
        return False
    return parent_resolved in candidate_resolved.parents or candidate_resolved == parent_resolved


def _pipeline_visible_to_caller(caller: core_models.CallerContext, pipeline_row: dict) -> bool:
    if caller.get("type") == "master":
        return True
    owner_id = str(pipeline_row.get("owner_id") or "").strip()
    if owner_id and owner_id == caller.get("owner_id"):
        return True
    return bool(pipeline_row.get("is_public"))


def _pipeline_response(pipeline_row: dict) -> dict:
    return {
        "pipeline_id": pipeline_row["pipeline_id"],
        "owner_id": pipeline_row.get("owner_id"),
        "name": pipeline_row.get("name"),
        "description": pipeline_row.get("description"),
        "definition": pipeline_row.get("definition") or {},
        "is_public": bool(pipeline_row.get("is_public")),
        "created_at": pipeline_row.get("created_at"),
        "updated_at": pipeline_row.get("updated_at"),
    }


def _recipe_catalog_entry(pipeline_row: dict) -> dict:
    recipe_meta = next(
        (item for item in recipes.BUILTIN_RECIPES if item.get("recipe_id") == pipeline_row.get("pipeline_id")),
        None,
    )
    payload = _pipeline_response(pipeline_row)
    if recipe_meta is not None:
        payload["default_input_schema"] = recipe_meta.get("default_input_schema") or {}
    return payload


@app.post(
    "/pipelines",
    status_code=201,
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
    tags=["Pipelines"],
    summary="Create a pipeline DAG definition.",
)
@limiter.limit("20/minute")
def pipelines_create(
    request: Request,
    body: dict[str, Any] = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required.")
    # Accept both canonical {"definition": {"nodes": [...]}} and the shorthand
    # form where nodes are at the body root: {"name": "...", "nodes": [...]}.
    definition = body.get("definition")
    if not isinstance(definition, dict):
        if isinstance(body.get("nodes"), list):
            definition = {"nodes": body["nodes"]}
        else:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "pipeline.invalid_definition",
                    "message": "definition must be an object with a nodes array.",
                },
            )
    try:
        validated = pipelines.validate_definition(definition)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "pipeline.node_invalid", "message": str(exc)},
        )
    created = pipelines.create_pipeline(
        caller["owner_id"],
        name,
        {"nodes": validated["nodes"]},
        description=str(body.get("description") or "").strip(),
        is_public=bool(body.get("is_public")),
        pipeline_id=body.get("pipeline_id"),
    )
    return JSONResponse(content=_pipeline_response(created), status_code=201)


@app.get(
    "/pipelines",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
    tags=["Pipelines"],
    summary="List pipelines visible to the caller.",
)
@limiter.limit("60/minute")
def pipelines_list(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    rows = pipelines.list_pipelines(caller["owner_id"], include_public=False)
    visible = [_pipeline_response(row) for row in rows if row is not None and _pipeline_visible_to_caller(caller, row)]
    return JSONResponse(content={"pipelines": visible, "count": len(visible)})


@app.get(
    "/pipelines/{pipeline_id}",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Pipelines"],
    summary="Get a pipeline definition.",
)
@limiter.limit("60/minute")
def pipelines_get(
    request: Request,
    pipeline_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    pipeline_row = pipelines.get_pipeline(pipeline_id)
    if pipeline_row is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found.")
    if not _pipeline_visible_to_caller(caller, pipeline_row):
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found.")
    return JSONResponse(content=_pipeline_response(pipeline_row))


@app.post(
    "/pipelines/{pipeline_id}/run",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 422, 429, 500),
    tags=["Pipelines"],
    summary="Execute a pipeline asynchronously.",
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def pipelines_run(
    request: Request,
    pipeline_id: str,
    body: dict[str, Any] = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    pipeline_row = pipelines.get_pipeline(pipeline_id)
    if pipeline_row is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found.")
    if not _pipeline_visible_to_caller(caller, pipeline_row):
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found.")
    input_payload = body.get("input_payload") or {}
    if not isinstance(input_payload, dict):
        raise HTTPException(status_code=422, detail="input_payload must be an object.")
    caller_wallet = payments.get_or_create_wallet(caller["owner_id"])
    try:
        run_id = pipelines.run_pipeline(
            pipeline_id,
            input_payload,
            caller["owner_id"],
            caller_wallet["wallet_id"],
            client_id=_request_client_id(request, body.get("client_id")),
            execute_builtin_agent=_execute_builtin_agent,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "pipeline.node_invalid", "message": str(exc)},
        )
    run = pipelines.get_run(run_id)
    return JSONResponse(
        content={
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "status": (run or {}).get("status", "running"),
        }
    )


@app.get(
    "/pipelines/{pipeline_id}/runs/{run_id}",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Pipelines"],
    summary="Get pipeline run status and results.",
)
@limiter.limit("60/minute")
def pipelines_run_get(
    request: Request,
    pipeline_id: str,
    run_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    pipeline_row = pipelines.get_pipeline(pipeline_id)
    if pipeline_row is None:
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found.")
    if not _pipeline_visible_to_caller(caller, pipeline_row):
        raise HTTPException(status_code=404, detail=f"Pipeline '{pipeline_id}' not found.")
    run = pipelines.get_run(run_id)
    if run is None or str(run.get("pipeline_id") or "") != pipeline_id:
        raise HTTPException(status_code=404, detail=f"Pipeline run '{run_id}' not found.")
    if caller.get("type") != "master" and caller["owner_id"] != run.get("caller_owner_id") and caller["owner_id"] != pipeline_row.get("owner_id"):
        raise HTTPException(status_code=403, detail="Not authorized to view this pipeline run.")
    return JSONResponse(
        content={
            "run_id": run["run_id"],
            "pipeline_id": run.get("pipeline_id"),
            "caller_owner_id": run.get("caller_owner_id"),
            "status": run.get("status"),
            "input_payload": run.get("input_payload") or {},
            "output_payload": run.get("output_payload"),
            "error_message": run.get("error_message"),
            "step_results": run.get("step_results") or {},
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "completed_at": run.get("completed_at"),
        }
    )


@app.get(
    "/recipes",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
    tags=["Pipelines"],
    summary="List built-in public pipeline recipes plus the caller's own recipes.",
)
@limiter.limit("60/minute")
def recipes_list(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    rows = pipelines.list_pipelines(caller["owner_id"], include_public=True)
    recipe_rows = [
        row for row in rows
        if row is not None and (
            str(row.get("owner_id") or "") == recipes.PLATFORM_RECIPES_OWNER_ID
            or str(row.get("owner_id") or "") == caller["owner_id"]
        )
    ]
    return JSONResponse(
        content={
            "recipes": [_recipe_catalog_entry(row) for row in recipe_rows],
            "count": len(recipe_rows),
        }
    )


@app.post(
    "/recipes",
    status_code=201,
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
    tags=["Pipelines"],
    summary="Create a user-owned pipeline recipe.",
)
@limiter.limit("20/minute")
def recipes_create(
    request: Request,
    body: dict[str, Any] = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    """Create a recipe owned by the authenticated user.

    Body:
      - ``name`` (required) — slug-style name unique to the caller
      - ``description`` (optional)
      - ``definition`` (required) — pipeline definition with a ``nodes`` array

    Returns the newly created recipe row. Recipes are stored in the same
    ``pipelines`` table as platform recipes; ownership is tracked on
    ``owner_id``. To run, POST to ``/recipes/{recipe_id}/run``.
    """
    _require_scope(caller, "caller")
    name = str((body or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="'name' is required.")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="'name' must be at most 80 characters.")
    definition = (body or {}).get("definition")
    if not isinstance(definition, dict):
        raise HTTPException(
            status_code=422,
            detail="'definition' must be an object with a 'nodes' array.",
        )
    description = str((body or {}).get("description") or "").strip()
    try:
        row = pipelines.create_pipeline(
            owner_id=caller["owner_id"],
            name=name,
            definition=definition,
            description=description or None,
            is_public=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return JSONResponse(content={"recipe": _recipe_catalog_entry(row)}, status_code=201)


@app.post(
    "/recipes/{recipe_id}/run",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 422, 429, 500),
    tags=["Pipelines"],
    summary="Run a built-in public recipe.",
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def recipes_run(
    request: Request,
    recipe_id: str,
    body: dict[str, Any] = Body(...),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    pipeline_row = pipelines.get_pipeline(recipe_id)
    if pipeline_row is None:
        raise HTTPException(status_code=404, detail=f"Recipe '{recipe_id}' not found.")
    owner_id = str(pipeline_row.get("owner_id") or "")
    is_platform = owner_id == recipes.PLATFORM_RECIPES_OWNER_ID
    is_owner = owner_id == caller["owner_id"]
    if not (is_platform or is_owner):
        raise HTTPException(status_code=404, detail=f"Recipe '{recipe_id}' not found.")
    input_payload = body.get("input_payload") or {}
    if not isinstance(input_payload, dict):
        raise HTTPException(status_code=422, detail="input_payload must be an object.")
    caller_wallet = payments.get_or_create_wallet(caller["owner_id"])
    try:
        run_id = pipelines.run_pipeline(
            recipe_id,
            input_payload,
            caller["owner_id"],
            caller_wallet["wallet_id"],
            client_id=_request_client_id(request, body.get("client_id")),
            execute_builtin_agent=_execute_builtin_agent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    run = pipelines.get_run(run_id)
    return JSONResponse(
        content={
            "run_id": run_id,
            "pipeline_id": recipe_id,
            "recipe_id": recipe_id,
            "status": (run or {}).get("status", "running"),
        }
    )


@app.get("/", include_in_schema=False)
def spa_root() -> _SpaFileResponse:
    """Serve ``frontend/dist/index.html`` at the site root.

    Without this route an unmatched request for ``/`` would fall through to
    Starlette's default ``{"detail": "Not Found"}`` handler, producing a
    broken public URL whenever nginx forwards ``/`` to FastAPI. If the SPA
    has not been built yet we surface an actionable 404 that tells the
    operator exactly what to run.
    """
    index_file = _FRONTEND_DIST_DIR / "index.html"
    if index_file.is_file():
        return _SpaFileResponse(str(index_file))
    raise HTTPException(
        status_code=404,
        detail=(
            "Frontend is not built on this server. "
            "Run `cd frontend && npm ci && npm run build`, then restart the API."
        ),
    )


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str) -> _SpaFileResponse:
    """Serve static assets or the React SPA shell for any non-API path.

    Because this route is registered last, every concrete API route (``/auth``,
    ``/jobs``, ``/wallets``, …) wins during FastAPI's sequential matching and
    this handler only fires for paths that would otherwise 404. Resolution
    order for the requested fragment:

    1. If the fragment looks like an API prefix (see ``_SPA_API_PREFIXES``),
       return a structured 404 so clients do not receive an HTML page when
       they meant to hit JSON.
    2. If ``frontend/dist`` is missing (frontend not yet built), return a
       human-readable 404 telling the operator how to build the SPA.
    3. If the fragment maps to an existing file inside ``frontend/dist`` (and
       path traversal is blocked by ``_resolved_under``), stream that file —
       this is how hashed assets under ``/assets/...`` are served.
    4. Otherwise fall back to ``index.html`` so React Router can resolve the
       URL on the client.
    """
    if _path_is_api(full_path):
        raise HTTPException(status_code=404, detail=f"Not Found: /{full_path}")

    if not _FRONTEND_DIST_DIR.is_dir():
        raise HTTPException(
            status_code=404,
            detail=(
                "Frontend assets are not available on this server. "
                "Build the React app (`cd frontend && npm ci && npm run build`) and restart."
            ),
        )

    safe_fragment = full_path.lstrip("/")
    if safe_fragment:
        candidate = _FRONTEND_DIST_DIR / safe_fragment
        if candidate.is_file() and _resolved_under(_FRONTEND_DIST_DIR, candidate):
            return _SpaFileResponse(str(candidate))

    index_file = _FRONTEND_DIST_DIR / "index.html"
    if index_file.is_file():
        return _SpaFileResponse(str(index_file))

    raise HTTPException(
        status_code=404,
        detail="index.html missing from frontend/dist. Rebuild the frontend and restart.",
    )
