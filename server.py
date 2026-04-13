"""
server.py — FastAPI HTTP server for the agentmarket platform

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
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

import agent_codereview
import agent_textintel
import agent_wiki
import auth as _auth
import payments
import registry
from main import run as _run_financial


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_MASTER_KEY = os.environ.get("API_KEY")
if not _MASTER_KEY:
    raise RuntimeError("API_KEY is not set. Add it to your .env file.")

_SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://localhost:8000")

# Deterministic UUIDs for built-in agents
_FINANCIAL_AGENT_ID  = "00000000-0000-0000-0000-000000000001"
_CODEREVIEW_AGENT_ID = "00000000-0000-0000-0000-000000000002"
_TEXTINTEL_AGENT_ID  = "00000000-0000-0000-0000-000000000003"
_WIKI_AGENT_ID       = "00000000-0000-0000-0000-000000000004"

_MAX_BODY_BYTES = 512 * 1024  # 512 KB


# ---------------------------------------------------------------------------
# Startup — register built-in agents
# ---------------------------------------------------------------------------

def _register_agents() -> None:
    agents = [
        {
            "agent_id": _FINANCIAL_AGENT_ID,
            "name": "Financial Research Agent",
            "description": (
                "Fetches the most recent SEC 10-K or 10-Q for any public company "
                "and returns a structured investment brief (signal, risks, highlights) "
                "synthesized by an LLM."
            ),
            "endpoint_url": f"{_SERVER_BASE_URL}/agents/financial",
            "price_per_call_usd": 0.01,
            "tags": ["financial-research", "sec-filings", "equity-analysis"],
            "input_schema": {
                "fields": [
                    {
                        "name": "ticker",
                        "type": "text",
                        "label": "Ticker symbol",
                        "placeholder": "AAPL",
                        "required": True,
                        "max_length": 5,
                        "transform": "uppercase",
                        "hint": "Any NYSE or NASDAQ ticker (e.g. AAPL, MSFT, TSLA)",
                    }
                ]
            },
        },
        {
            "agent_id": _CODEREVIEW_AGENT_ID,
            "name": "Code Review Agent",
            "description": (
                "Reviews any code snippet for bugs, security vulnerabilities, "
                "performance issues, and style problems. Returns a scored report "
                "with specific, actionable fixes."
            ),
            "endpoint_url": f"{_SERVER_BASE_URL}/agents/code-review",
            "price_per_call_usd": 0.005,
            "tags": ["code-review", "security", "developer-tools"],
            "input_schema": {
                "fields": [
                    {
                        "name": "code",
                        "type": "textarea",
                        "label": "Code",
                        "placeholder": "Paste your code here…",
                        "required": True,
                        "hint": "Up to ~12,000 characters",
                    },
                    {
                        "name": "language",
                        "type": "select",
                        "label": "Language",
                        "required": False,
                        "default": "auto",
                        "options": [
                            "auto", "python", "javascript", "typescript",
                            "go", "rust", "java", "cpp", "c", "ruby", "php",
                            "swift", "kotlin", "sql",
                        ],
                    },
                    {
                        "name": "focus",
                        "type": "select",
                        "label": "Review focus",
                        "required": False,
                        "default": "all",
                        "options": ["all", "security", "performance", "bugs", "style"],
                    },
                ]
            },
        },
        {
            "agent_id": _TEXTINTEL_AGENT_ID,
            "name": "Text Intelligence Agent",
            "description": (
                "Analyzes any text for sentiment, key entities, topics, and readability. "
                "Returns a structured NLP brief with summary, quotes, and scores. "
                "Works on articles, reviews, reports, emails, or any prose."
            ),
            "endpoint_url": f"{_SERVER_BASE_URL}/agents/text-intel",
            "price_per_call_usd": 0.003,
            "tags": ["nlp", "sentiment-analysis", "text-analytics"],
            "input_schema": {
                "fields": [
                    {
                        "name": "text",
                        "type": "textarea",
                        "label": "Text to analyze",
                        "placeholder": "Paste any text here — article, review, report…",
                        "required": True,
                        "hint": "Up to ~10,000 characters",
                    },
                    {
                        "name": "mode",
                        "type": "select",
                        "label": "Analysis depth",
                        "required": False,
                        "default": "full",
                        "options": ["full", "quick"],
                        "hint": "quick = sentiment + summary only",
                    },
                ]
            },
        },
        {
            "agent_id": _WIKI_AGENT_ID,
            "name": "Wikipedia Research Agent",
            "description": (
                "Fetches the Wikipedia article for any topic and returns a "
                "structured research brief: summary, key facts, related topics, "
                "and content classification."
            ),
            "endpoint_url": f"{_SERVER_BASE_URL}/agents/wiki",
            "price_per_call_usd": 0.003,
            "tags": ["research", "knowledge-base", "wikipedia"],
            "input_schema": {
                "fields": [
                    {
                        "name": "topic",
                        "type": "text",
                        "label": "Topic",
                        "placeholder": "e.g. Quantum computing",
                        "required": True,
                        "hint": "Any Wikipedia-resolvable topic",
                    }
                ]
            },
        },
    ]

    for a in agents:
        if not registry.agent_exists_by_name(a["name"]):
            registry.register_agent(
                agent_id=a["agent_id"],
                name=a["name"],
                description=a["description"],
                endpoint_url=a["endpoint_url"],
                price_per_call_usd=a["price_per_call_usd"],
                tags=a["tags"],
                input_schema=a["input_schema"],
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.init_db()
    payments.init_payments_db()
    _auth.init_auth_db()
    _register_agents()
    yield


# ---------------------------------------------------------------------------
# Rate limiter — keyed per caller identity
# ---------------------------------------------------------------------------

def _key_from_request(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:][:32]
    host = request.client.host if request.client else "unknown"
    return host


limiter = Limiter(key_func=_key_from_request)
app = FastAPI(title="agentmarket", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — allow Vite dev server and common local ports
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


# ---------------------------------------------------------------------------
# Middleware — security headers + request size cap
# ---------------------------------------------------------------------------

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and int(cl) > _MAX_BODY_BYTES:
        return JSONResponse(
            {"detail": f"Request body too large (max {_MAX_BODY_BYTES // 1024} KB)."},
            status_code=413,
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _resolve_caller(request: Request) -> dict | None:
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
            raise HTTPException(
                status_code=401,
                detail="Authorization header missing. Expected: Bearer <key>",
            )
        raise HTTPException(status_code=403, detail="Invalid API key.")
    return caller


def _caller_owner_id(request: Request) -> str:
    caller = _resolve_caller(request)
    return caller["owner_id"] if caller else request.headers["Authorization"][7:]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class FinancialRequest(BaseModel):
    ticker: str


class CodeReviewRequest(BaseModel):
    code: str
    language: str = "auto"
    focus: str = "all"

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v):
        if not v.strip():
            raise ValueError("code must not be empty")
        return v

    @field_validator("focus")
    @classmethod
    def focus_valid(cls, v):
        valid = {"all", "security", "performance", "bugs", "style"}
        if v not in valid:
            raise ValueError(f"focus must be one of: {', '.join(sorted(valid))}")
        return v


class TextIntelRequest(BaseModel):
    text: str
    mode: str = "full"

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v):
        if not v.strip():
            raise ValueError("text must not be empty")
        return v

    @field_validator("mode")
    @classmethod
    def mode_valid(cls, v):
        if v not in ("full", "quick"):
            raise ValueError("mode must be 'full' or 'quick'")
        return v


class WikiRequest(BaseModel):
    topic: str

    @field_validator("topic")
    @classmethod
    def topic_not_empty(cls, v):
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v.strip()


class AgentRegisterRequest(BaseModel):
    name: str
    description: str
    endpoint_url: str
    price_per_call_usd: float
    tags: list[str] = []
    input_schema: dict = {}


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
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "agents": len(registry.get_agents())}


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
# Agent endpoints — direct calls (used by proxy and CLI client)
# ---------------------------------------------------------------------------

@app.post("/agents/financial")
@limiter.limit("10/minute")
def agent_financial(
    request: Request,
    body: FinancialRequest,
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    ticker = body.ticker.strip().upper()
    if not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(status_code=422, detail=f"Invalid ticker symbol: '{ticker}'")
    try:
        brief = _run_financial(ticker)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _groq.RateLimitError as e:
        raise HTTPException(status_code=503, detail=f"All LLM models rate-limited. ({e})")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content=brief)


# Keep /analyze as an alias for backwards compatibility
@app.post("/analyze")
@limiter.limit("10/minute")
def analyze_alias(
    request: Request,
    body: FinancialRequest,
    caller: dict = Depends(_require_api_key),
) -> JSONResponse:
    return agent_financial(request, body, caller)


@app.post("/agents/code-review")
@limiter.limit("10/minute")
def agent_code_review(
    request: Request,
    body: CodeReviewRequest,
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    try:
        result = agent_codereview.run(body.code, body.language, body.focus)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _groq.RateLimitError as e:
        raise HTTPException(status_code=503, detail=f"All LLM models rate-limited. ({e})")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content=result)


@app.post("/agents/text-intel")
@limiter.limit("15/minute")
def agent_text_intel(
    request: Request,
    body: TextIntelRequest,
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    try:
        result = agent_textintel.run(body.text, body.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _groq.RateLimitError as e:
        raise HTTPException(status_code=503, detail=f"All LLM models rate-limited. ({e})")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content=result)


@app.post("/agents/wiki")
@limiter.limit("15/minute")
def agent_wiki_endpoint(
    request: Request,
    body: WikiRequest,
    _: dict = Depends(_require_api_key),
) -> JSONResponse:
    try:
        result = agent_wiki.run(body.topic)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _groq.RateLimitError as e:
        raise HTTPException(status_code=503, detail=f"All LLM models rate-limited. ({e})")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content=result)


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
        input_schema=body.input_schema,
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
    caller: dict = Depends(_require_api_key),
) -> JSONResponse:
    """
    Proxy a call to the registered agent with full payment lifecycle:
      1. Deduct price (402 if broke).
      2. HTTP POST to agent endpoint.
      3a. Success → payout 90% agent / 10% platform.
      3b. Failure → full refund to caller.
    """
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    price_cents     = round(agent["price_per_call_usd"] * 100)
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
    limit = min(max(1, limit), 200)
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
