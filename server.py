"""
server.py — FastAPI HTTP server for the agentmarket platform

Exposes the financial research agent over HTTP and hosts the agent registry
and payment system so other agents can discover, call, and pay for listings.

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import requests as http
from dotenv import load_dotenv

load_dotenv()

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import groq as _groq

import payments
import registry
from main import run


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_API_KEY = os.environ.get("API_KEY")
if not _API_KEY:
    raise RuntimeError("API_KEY is not set. Add it to your .env file.")

_SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://localhost:8000")

_FINANCIAL_AGENT_ID = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _register_self() -> None:
    if registry.agent_exists_by_name("Financial Research Agent"):
        return
    registry.register_agent(
        agent_id=_FINANCIAL_AGENT_ID,
        name="Financial Research Agent",
        description=(
            "Fetches the most recent SEC 10-K or 10-Q for any public company "
            "and returns a structured investment brief (signal, risks, highlights) "
            "synthesized by an LLM."
        ),
        endpoint_url=f"{_SERVER_BASE_URL}/analyze",
        price_per_call_usd=0.01,
        tags=["financial-research", "sec-filings", "equity-analysis"],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.init_db()
    payments.init_payments_db()
    _register_self()
    yield


# ---------------------------------------------------------------------------
# Rate limiter — keyed per API key so limits are per caller, not per IP
# ---------------------------------------------------------------------------

def _key_from_api_key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.client.host


limiter = Limiter(key_func=_key_from_api_key)

app = FastAPI(title="agentmarket", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _require_api_key(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header missing or malformed. Expected: Bearer <key>",
        )
    if auth[7:] != _API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key.")


def _caller_owner_id(request: Request) -> str:
    """Extract the bearer token and use it as the stable caller identity."""
    return request.headers["Authorization"][7:]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    ticker: str


class RegisterRequest(BaseModel):
    name: str
    description: str
    endpoint_url: str
    price_per_call_usd: float
    tags: list[str] = []


class DepositRequest(BaseModel):
    wallet_id: str
    amount_cents: int
    memo: str = "manual deposit"


# ---------------------------------------------------------------------------
# Core agent routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
@limiter.limit("10/minute")
def analyze(
    request: Request,
    body: AnalyzeRequest,
    _: None = Depends(_require_api_key),
) -> JSONResponse:
    ticker = body.ticker.strip().upper()
    if not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(status_code=422, detail=f"Invalid ticker symbol: '{ticker}'")
    try:
        brief = run(ticker)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _groq.RateLimitError as e:
        raise HTTPException(status_code=503, detail=f"LLM rate limit reached. Try again shortly. ({e})")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content=brief)


# ---------------------------------------------------------------------------
# Registry routes
# ---------------------------------------------------------------------------

@app.post("/registry/register", status_code=201)
@limiter.limit("20/minute")
def registry_register(
    request: Request,
    body: RegisterRequest,
    _: None = Depends(_require_api_key),
) -> JSONResponse:
    """Register a new agent listing on the marketplace."""
    agent_id = registry.register_agent(
        name=body.name,
        description=body.description,
        endpoint_url=body.endpoint_url,
        price_per_call_usd=body.price_per_call_usd,
        tags=body.tags,
    )
    return JSONResponse(
        content={"agent_id": agent_id, "message": "Agent registered successfully."},
        status_code=201,
    )


@app.get("/registry/agents")
@limiter.limit("60/minute")
def registry_list(
    request: Request,
    tag: str | None = None,
    _: None = Depends(_require_api_key),
) -> JSONResponse:
    """List all registered agents. Filter by tag with ?tag=financial-research."""
    agents = registry.get_agents(tag=tag)
    return JSONResponse(content={"agents": agents, "count": len(agents)})


@app.get("/registry/agents/{agent_id}")
@limiter.limit("60/minute")
def registry_get(
    request: Request,
    agent_id: str,
    _: None = Depends(_require_api_key),
) -> JSONResponse:
    """Get a single agent listing by ID."""
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return JSONResponse(content=agent)


@app.post("/registry/agents/{agent_id}/call")
@limiter.limit("10/minute")
def registry_call(
    request: Request,
    agent_id: str,
    body: Any = Body(default={}),
    _: None = Depends(_require_api_key),
) -> JSONResponse:
    """
    Proxy a call to the agent at its registered endpoint_url.

    Payment lifecycle:
      1. Deduct price from caller wallet (HTTP 402 if insufficient).
      2. Call the agent.
      3a. Success → payout 90% to agent wallet, 10% fee to platform.
      3b. Failure → full refund to caller wallet.

    Also updates total_calls and success_rate on the registry listing.
    """
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    # Convert USD price to integer cents. This is the only place the float crosses
    # the boundary — everything downstream is integer arithmetic.
    price_cents = round(agent["price_per_call_usd"] * 100)

    # Ensure wallets exist for all three parties before touching money.
    caller_wallet = payments.get_or_create_wallet(_caller_owner_id(request))
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    # --- Transaction 1: charge caller (short DB lock, no I/O) ---
    try:
        charge_tx_id = payments.pre_call_charge(
            caller_wallet["wallet_id"], price_cents, agent_id
        )
    except payments.InsufficientBalanceError as e:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_balance",
                "balance_cents": e.balance_cents,
                "required_cents": e.required_cents,
                "wallet_id": caller_wallet["wallet_id"],
            },
        )

    # --- HTTP call (no DB lock held) ---
    start = time.monotonic()
    success = False
    try:
        resp = http.post(
            agent["endpoint_url"],
            json=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_API_KEY}",
            },
            timeout=120,
        )
        success = resp.ok
        latency_ms = (time.monotonic() - start) * 1000
        registry.update_call_stats(agent_id, latency_ms, success)

        # --- Transaction 2a/2b: settle payment ---
        if success:
            payments.post_call_payout(
                agent_wallet["wallet_id"],
                platform_wallet["wallet_id"],
                charge_tx_id,
                price_cents,
                agent_id,
            )
        else:
            payments.post_call_refund(
                caller_wallet["wallet_id"], charge_tx_id, price_cents, agent_id
            )

        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    except http.RequestException as e:
        latency_ms = (time.monotonic() - start) * 1000
        registry.update_call_stats(agent_id, latency_ms, False)
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, price_cents, agent_id
        )
        raise HTTPException(status_code=502, detail=f"Upstream agent unreachable: {e}")


# ---------------------------------------------------------------------------
# Wallet routes
# ---------------------------------------------------------------------------

@app.post("/wallets/deposit")
@limiter.limit("20/minute")
def wallet_deposit(
    request: Request,
    body: DepositRequest,
    _: None = Depends(_require_api_key),
) -> JSONResponse:
    """
    Manually credit a wallet. Used for topping up during development.
    No real money rails — just inserts a deposit transaction.
    """
    try:
        tx_id = payments.deposit(body.wallet_id, body.amount_cents, body.memo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    wallet = payments.get_wallet(body.wallet_id)
    return JSONResponse(content={
        "tx_id": tx_id,
        "wallet_id": body.wallet_id,
        "balance_cents": wallet["balance_cents"],
    })


@app.get("/wallets/me")
@limiter.limit("60/minute")
def wallet_me(
    request: Request,
    _: None = Depends(_require_api_key),
) -> JSONResponse:
    """Return the caller's own wallet (created on first call if needed) and last 20 transactions."""
    wallet = payments.get_or_create_wallet(_caller_owner_id(request))
    txs = payments.get_wallet_transactions(wallet["wallet_id"], limit=20)
    return JSONResponse(content={**wallet, "transactions": txs})


@app.get("/wallets/{wallet_id}")
@limiter.limit("60/minute")
def wallet_get(
    request: Request,
    wallet_id: str,
    _: None = Depends(_require_api_key),
) -> JSONResponse:
    """Return wallet balance and last 20 transactions."""
    wallet = payments.get_wallet(wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail=f"Wallet '{wallet_id}' not found.")
    txs = payments.get_wallet_transactions(wallet_id, limit=20)
    return JSONResponse(content={**wallet, "transactions": txs})


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

@app.get("/runs")
@limiter.limit("30/minute")
def get_runs(
    request: Request,
    limit: int = 50,
    _: None = Depends(_require_api_key),
) -> JSONResponse:
    """Return recent run history from runs.jsonl for dashboard charts."""
    runs_file = os.path.join(os.path.dirname(__file__), "runs.jsonl")
    if not os.path.exists(runs_file):
        return JSONResponse(content={"runs": []})
    with open(runs_file, encoding="utf-8") as f:
        lines = f.readlines()
    runs = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(runs) >= limit:
            break
    return JSONResponse(content={"runs": runs})
