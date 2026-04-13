"""
server.py — FastAPI HTTP server for the agentmarket platform

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
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import groq as _groq

import auth as _auth
import payments
import registry
from main import run


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Master key from env — always valid, used by the server itself for agent proxying
_MASTER_KEY = os.environ.get("API_KEY")
if not _MASTER_KEY:
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
    _auth.init_auth_db()
    _register_self()
    yield


# ---------------------------------------------------------------------------
# Rate limiter — keyed per caller identity
# ---------------------------------------------------------------------------

def _key_from_request(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:][:32]   # truncate for safety
    return request.client.host


limiter = Limiter(key_func=_key_from_request)
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
# Auth helpers
# ---------------------------------------------------------------------------

def _resolve_caller(request: Request) -> dict | None:
    """
    Returns a dict describing the caller, or None if the key is invalid.
    - Master key → {"type": "master", "owner_id": <raw_key>}
    - User key   → {"type": "user",   "owner_id": "user:<user_id>", "user": {...}}
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    raw = auth[7:]
    if raw == _MASTER_KEY:
        return {"type": "master", "owner_id": raw}
    user = _auth.verify_api_key(raw)
    if user:
        return {"type": "user", "owner_id": f"user:{user['user_id']}", "user": user}
    return None


def _require_api_key(request: Request) -> dict:
    caller = _resolve_caller(request)
    if caller is None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401,
                detail="Authorization header missing. Expected: Bearer <key>")
        raise HTTPException(status_code=403, detail="Invalid API key.")
    return caller


def _caller_owner_id(request: Request) -> str:
    caller = _resolve_caller(request)
    return caller["owner_id"] if caller else request.headers["Authorization"][7:]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    ticker: str


class AgentRegisterRequest(BaseModel):
    name: str
    description: str
    endpoint_url: str
    price_per_call_usd: float
    tags: list[str] = []


class DepositRequest(BaseModel):
    wallet_id: str
    amount_cents: int
    memo: str = "manual deposit"


class UserRegisterRequest(BaseModel):
    username: str
    email: str
    password: str

    @field_validator("username")
    @classmethod
    def username_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Username cannot be empty")
        return v.strip()

    @field_validator("email")
    @classmethod
    def email_valid(cls, v):
        if "@" not in v or "." not in v:
            raise ValueError("Enter a valid email address")
        return v.strip().lower()

    @field_validator("password")
    @classmethod
    def password_length(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class UserLoginRequest(BaseModel):
    email: str
    password: str


class CreateKeyRequest(BaseModel):
    name: str = "New key"


# ---------------------------------------------------------------------------
# Auth routes  (public — no key required)
# ---------------------------------------------------------------------------

@app.post("/auth/register", status_code=201)
@limiter.limit("10/minute")
def auth_register(request: Request, body: UserRegisterRequest) -> JSONResponse:
    """Create a new user account. Returns the initial API key (shown once)."""
    try:
        result = _auth.register_user(body.username, body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(content=result, status_code=201)


@app.post("/auth/login")
@limiter.limit("20/minute")
def auth_login(request: Request, body: UserLoginRequest) -> JSONResponse:
    """Verify credentials. Returns a fresh API key valid for this session."""
    result = _auth.login_user(body.email, body.password)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return JSONResponse(content=result)


@app.get("/auth/me")
@limiter.limit("60/minute")
def auth_me(request: Request, caller: dict = Depends(_require_api_key)) -> JSONResponse:
    """Return the authenticated user's profile."""
    if caller["type"] == "master":
        return JSONResponse(content={"type": "master", "user_id": None, "username": "admin"})
    user = caller["user"]
    return JSONResponse(content={
        "user_id": user["user_id"],
        "username": user["username"],
        "email": user["email"],
    })


@app.get("/auth/keys")
@limiter.limit("30/minute")
def auth_list_keys(request: Request, caller: dict = Depends(_require_api_key)) -> JSONResponse:
    """List the caller's API keys (metadata only — raw keys never returned after creation)."""
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    keys = _auth.list_api_keys(caller["user"]["user_id"])
    return JSONResponse(content={"keys": keys})


@app.post("/auth/keys", status_code=201)
@limiter.limit("10/minute")
def auth_create_key(
    request: Request,
    body: CreateKeyRequest,
    caller: dict = Depends(_require_api_key),
) -> JSONResponse:
    """Create a new named API key for the authenticated user."""
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    result = _auth.create_api_key(caller["user"]["user_id"], body.name)
    return JSONResponse(content=result, status_code=201)


@app.delete("/auth/keys/{key_id}", status_code=200)
@limiter.limit("10/minute")
def auth_revoke_key(
    request: Request,
    key_id: str,
    caller: dict = Depends(_require_api_key),
) -> JSONResponse:
    """Revoke an API key by ID."""
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    ok = _auth.revoke_api_key(key_id, caller["user"]["user_id"])
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found or already revoked.")
    return JSONResponse(content={"revoked": True})


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
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    ticker = body.ticker.strip().upper()
    if not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(status_code=422, detail=f"Invalid ticker symbol: '{ticker}'")
    try:
        brief = run(ticker)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _groq.RateLimitError as e:
        raise HTTPException(status_code=503, detail=f"LLM rate limit reached. ({e})")
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
    body: AgentRegisterRequest,
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
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
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    agents = registry.get_agents(tag=tag)
    return JSONResponse(content={"agents": agents, "count": len(agents)})


@app.get("/registry/agents/{agent_id}")
@limiter.limit("60/minute")
def registry_get(
    request: Request,
    agent_id: str,
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
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
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    """
    Proxy a call to the registered agent with full payment lifecycle:
      1. Deduct price (402 if broke).
      2. HTTP call to agent endpoint.
      3a. Success → payout 90% agent / 10% platform.
      3b. Failure → full refund to caller.
    """
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    price_cents = round(agent["price_per_call_usd"] * 100)
    caller_wallet   = payments.get_or_create_wallet(_caller_owner_id(request))
    agent_wallet    = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

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

    start = time.monotonic()
    success = False
    try:
        resp = http.post(
            agent["endpoint_url"],
            json=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_MASTER_KEY}",
            },
            timeout=120,
        )
        success = resp.ok
        latency_ms = (time.monotonic() - start) * 1000
        registry.update_call_stats(agent_id, latency_ms, success)

        if success:
            payments.post_call_payout(
                agent_wallet["wallet_id"], platform_wallet["wallet_id"],
                charge_tx_id, price_cents, agent_id,
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
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    try:
        tx_id = payments.deposit(body.wallet_id, body.amount_cents, body.memo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    wallet = payments.get_wallet(body.wallet_id)
    return JSONResponse(content={
        "tx_id": tx_id, "wallet_id": body.wallet_id,
        "balance_cents": wallet["balance_cents"],
    })


@app.get("/wallets/me")
@limiter.limit("60/minute")
def wallet_me(
    request: Request,
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    wallet = payments.get_or_create_wallet(_caller_owner_id(request))
    txs = payments.get_wallet_transactions(wallet["wallet_id"], limit=50)
    return JSONResponse(content={**wallet, "transactions": txs})


@app.get("/wallets/{wallet_id}")
@limiter.limit("60/minute")
def wallet_get(
    request: Request,
    wallet_id: str,
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    wallet = payments.get_wallet(wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail=f"Wallet '{wallet_id}' not found.")
    txs = payments.get_wallet_transactions(wallet_id, limit=50)
    return JSONResponse(content={**wallet, "transactions": txs})


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

@app.get("/runs")
@limiter.limit("30/minute")
def get_runs(
    request: Request,
    limit: int = 50,
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
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
