"""
server.py — FastAPI HTTP server for the agentmarket platform

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import json
import os
import base64
import math
import hmac
import hashlib
import logging
import ipaddress
import sqlite3
import threading
import time
import uuid
from queue import Empty, Queue
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable
from urllib.parse import urlparse

import requests as http
from dotenv import load_dotenv

load_dotenv()

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import groq as _groq

from agents import codereview as agent_codereview
from agents import textintel as agent_textintel
from agents import wiki as agent_wiki
from core import auth as _auth
from core import onboarding
from core import payments
from core import registry
from core import jobs
from core import disputes
from core import judges
from core import models as core_models
from core import reputation
from main import run as _run_financial
from core.models import (
    AgentRegisterRequest,
    CodeReviewRequest,
    CreateKeyRequest,
    DepositRequest,
    FinancialRequest,
    HookDeliveryProcessRequest,
    JobClaimRequest,
    JobCompleteRequest,
    JobCreateRequest,
    JobEventHookCreateRequest,
    JobFailRequest,
    JobHeartbeatRequest,
    JobDisputeRequest,
    JobMessageRequest,
    JobRateCallerRequest,
    JobRatingRequest,
    JobReleaseRequest,
    JobRetryRequest,
    JobsSweepRequest,
    AdminDisputeRuleRequest,
    OnboardingValidateRequest,
    ReconciliationRunRequest,
    RegistrySearchRequest,
    RotateKeyRequest,
    TextIntelRequest,
    UserLoginRequest,
    UserRegisterRequest,
    WikiRequest,
)

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_MASTER_KEY = os.environ.get("API_KEY")
if not _MASTER_KEY:
    raise RuntimeError("API_KEY is not set. Add it to your .env file.")

_SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")

# Deterministic UUIDs for built-in agents
_FINANCIAL_AGENT_ID  = "00000000-0000-0000-0000-000000000001"
_CODEREVIEW_AGENT_ID = "00000000-0000-0000-0000-000000000002"
_TEXTINTEL_AGENT_ID  = "00000000-0000-0000-0000-000000000003"
_WIKI_AGENT_ID       = "00000000-0000-0000-0000-000000000004"

_BUILTIN_PROXY_ENDPOINTS = {
    _FINANCIAL_AGENT_ID: f"{_SERVER_BASE_URL}/agents/financial",
    _CODEREVIEW_AGENT_ID: f"{_SERVER_BASE_URL}/agents/code-review",
    _TEXTINTEL_AGENT_ID: f"{_SERVER_BASE_URL}/agents/text-intel",
    _WIKI_AGENT_ID: f"{_SERVER_BASE_URL}/agents/wiki",
}
_BUILTIN_PROXY_AUTH_URLS = {
    _FINANCIAL_AGENT_ID: {
        _BUILTIN_PROXY_ENDPOINTS[_FINANCIAL_AGENT_ID],
        f"{_SERVER_BASE_URL}/analyze",  # legacy alias still present in existing registries
    },
    _CODEREVIEW_AGENT_ID: {_BUILTIN_PROXY_ENDPOINTS[_CODEREVIEW_AGENT_ID]},
    _TEXTINTEL_AGENT_ID: {_BUILTIN_PROXY_ENDPOINTS[_TEXTINTEL_AGENT_ID]},
    _WIKI_AGENT_ID: {_BUILTIN_PROXY_ENDPOINTS[_WIKI_AGENT_ID]},
}

_CALLER_CACHE_MISSING = object()
_IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
_MAX_BODY_BYTES = 512 * 1024  # 512 KB
_DEFAULT_LEASE_SECONDS = 300
_DEFAULT_RETRY_DELAY_SECONDS = 30
_DEFAULT_SLA_SECONDS = 900
_DEFAULT_SWEEP_INTERVAL_SECONDS = 30
_DEFAULT_SWEEP_LIMIT = 100
_DEFAULT_HOOK_DELIVERY_INTERVAL_SECONDS = 2
_DEFAULT_HOOK_DELIVERY_BATCH_SIZE = 50
_DEFAULT_HOOK_DELIVERY_MAX_ATTEMPTS = 5
_DEFAULT_HOOK_DELIVERY_BASE_DELAY_SECONDS = 5
_DEFAULT_HOOK_DELIVERY_MAX_DELAY_SECONDS = 300
_DEFAULT_DISPUTE_FILE_WINDOW_SECONDS = 7 * 24 * 3600
_DEFAULT_DISPUTE_JUDGE_INTERVAL_SECONDS = 0


def _env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer, got {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}.")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}, got {value}.")
    return value


def _env_float(name: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None:
        value = float(default)
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a float, got {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}.")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}, got {value}.")
    return value


_SWEEPER_INTERVAL_SECONDS = _env_int(
    "JOB_SWEEP_INTERVAL_SECONDS",
    _DEFAULT_SWEEP_INTERVAL_SECONDS,
    minimum=0,
)
_SWEEPER_SLA_SECONDS = _env_int(
    "JOB_SWEEP_SLA_SECONDS",
    _DEFAULT_SLA_SECONDS,
    minimum=60,
)
_SWEEPER_LIMIT = _env_int(
    "JOB_SWEEP_LIMIT",
    _DEFAULT_SWEEP_LIMIT,
    minimum=1,
    maximum=500,
)
_SWEEPER_RETRY_DELAY_SECONDS = _env_int(
    "JOB_SWEEP_RETRY_DELAY_SECONDS",
    _DEFAULT_RETRY_DELAY_SECONDS,
    minimum=0,
    maximum=3600,
)
_SWEEPER_ENABLED = _SWEEPER_INTERVAL_SECONDS > 0
_HOOK_DELIVERY_INTERVAL_SECONDS = _env_int(
    "HOOK_DELIVERY_INTERVAL_SECONDS",
    _DEFAULT_HOOK_DELIVERY_INTERVAL_SECONDS,
    minimum=0,
)
_HOOK_DELIVERY_BATCH_SIZE = _env_int(
    "HOOK_DELIVERY_BATCH_SIZE",
    _DEFAULT_HOOK_DELIVERY_BATCH_SIZE,
    minimum=1,
    maximum=500,
)
_HOOK_DELIVERY_MAX_ATTEMPTS = _env_int(
    "HOOK_DELIVERY_MAX_ATTEMPTS",
    _DEFAULT_HOOK_DELIVERY_MAX_ATTEMPTS,
    minimum=1,
    maximum=50,
)
_HOOK_DELIVERY_BASE_DELAY_SECONDS = _env_int(
    "HOOK_DELIVERY_BASE_DELAY_SECONDS",
    _DEFAULT_HOOK_DELIVERY_BASE_DELAY_SECONDS,
    minimum=1,
    maximum=3600,
)
_HOOK_DELIVERY_MAX_DELAY_SECONDS = _env_int(
    "HOOK_DELIVERY_MAX_DELAY_SECONDS",
    _DEFAULT_HOOK_DELIVERY_MAX_DELAY_SECONDS,
    minimum=1,
    maximum=24 * 3600,
)
if _HOOK_DELIVERY_MAX_DELAY_SECONDS < _HOOK_DELIVERY_BASE_DELAY_SECONDS:
    raise RuntimeError("HOOK_DELIVERY_MAX_DELAY_SECONDS must be >= HOOK_DELIVERY_BASE_DELAY_SECONDS.")
_HOOK_DELIVERY_ENABLED = _HOOK_DELIVERY_INTERVAL_SECONDS > 0
_DISPUTE_FILE_WINDOW_SECONDS = _env_int(
    "DISPUTE_FILE_WINDOW_SECONDS",
    _DEFAULT_DISPUTE_FILE_WINDOW_SECONDS,
    minimum=3600,
    maximum=30 * 24 * 3600,
)
_DISPUTE_JUDGE_INTERVAL_SECONDS = _env_int(
    "DISPUTE_JUDGE_INTERVAL_SECONDS",
    _DEFAULT_DISPUTE_JUDGE_INTERVAL_SECONDS,
    minimum=0,
    maximum=3600,
)
_DISPUTE_JUDGE_ENABLED = _DISPUTE_JUDGE_INTERVAL_SECONDS > 0
_SLO_CLAIM_P95_TARGET_MS = _env_int(
    "SLO_CLAIM_P95_TARGET_MS",
    60_000,
    minimum=100,
    maximum=3_600_000,
)
_SLO_SETTLEMENT_P95_TARGET_MS = _env_int(
    "SLO_SETTLEMENT_P95_TARGET_MS",
    300_000,
    minimum=100,
    maximum=3_600_000,
)
_SLO_TIMEOUT_RATE_MAX = _env_float(
    "SLO_TIMEOUT_RATE_MAX",
    0.05,
    minimum=0.0,
    maximum=1.0,
)
_SLO_HOOK_SUCCESS_RATE_MIN = _env_float(
    "SLO_HOOK_SUCCESS_RATE_MIN",
    0.95,
    minimum=0.0,
    maximum=1.0,
)
_ALLOW_PRIVATE_OUTBOUND_URLS = os.environ.get("ALLOW_PRIVATE_OUTBOUND_URLS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
_SWEEPER_STATE_LOCK = threading.Lock()
_SWEEPER_STATE = {
    "enabled": _SWEEPER_ENABLED,
    "interval_seconds": _SWEEPER_INTERVAL_SECONDS,
    "sla_seconds": _SWEEPER_SLA_SECONDS,
    "limit": _SWEEPER_LIMIT,
    "retry_delay_seconds": _SWEEPER_RETRY_DELAY_SECONDS,
    "running": False,
    "started_at": None,
    "last_run_at": None,
    "last_summary": None,
    "last_error": None,
}
_HOOK_WORKER_STATE_LOCK = threading.Lock()
_HOOK_WORKER_STATE = {
    "enabled": _HOOK_DELIVERY_ENABLED,
    "interval_seconds": _HOOK_DELIVERY_INTERVAL_SECONDS,
    "batch_size": _HOOK_DELIVERY_BATCH_SIZE,
    "max_attempts": _HOOK_DELIVERY_MAX_ATTEMPTS,
    "base_delay_seconds": _HOOK_DELIVERY_BASE_DELAY_SECONDS,
    "max_delay_seconds": _HOOK_DELIVERY_MAX_DELAY_SECONDS,
    "running": False,
    "started_at": None,
    "last_run_at": None,
    "last_summary": None,
    "last_error": None,
}
_JOB_STREAM_HEARTBEAT_SECONDS = 15
_JOB_TERMINAL_STATUSES = {"complete", "failed"}
_LEGACY_JOB_MESSAGE_TYPES = {
    "question",
    "partial_result",
    "clarification",
    "clarification_needed",
    "final_result",
    "note",
}
_TYPED_JOB_MESSAGE_TYPES = {
    "clarification_request",
    "clarification_response",
    "progress",
    "partial_result",
    "artifact",
    "note",
    "tool_call",
    "tool_result",
}


def _usd_to_cents(usd: float) -> int:
    dec = Decimal(str(usd))
    if dec < 0:
        raise ValueError("Price must be non-negative.")
    cents = int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if dec > 0 and cents == 0:
        return 1  # enforce minimum 1¢ for non-zero prices
    return cents


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_ops_db() -> None:
    with jobs._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
                event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id            TEXT NOT NULL,
                agent_id          TEXT NOT NULL,
                agent_owner_id    TEXT NOT NULL,
                caller_owner_id   TEXT NOT NULL,
                event_type        TEXT NOT NULL,
                actor_owner_id    TEXT,
                payload           TEXT NOT NULL DEFAULT '{}',
                created_at        TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_event_hooks (
                hook_id            TEXT PRIMARY KEY,
                owner_id           TEXT NOT NULL,
                target_url         TEXT NOT NULL,
                secret             TEXT,
                is_active          INTEGER NOT NULL DEFAULT 1,
                created_at         TEXT NOT NULL,
                last_attempt_at    TEXT,
                last_success_at    TEXT,
                last_status_code   INTEGER,
                last_error         TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_event_deliveries (
                delivery_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id            INTEGER NOT NULL,
                hook_id             TEXT NOT NULL,
                owner_id            TEXT NOT NULL,
                target_url          TEXT NOT NULL,
                secret              TEXT,
                payload             TEXT NOT NULL,
                status              TEXT NOT NULL CHECK(status IN ('pending', 'retrying', 'delivered', 'dead_letter')),
                attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                next_attempt_at     TEXT NOT NULL,
                last_attempt_at     TEXT,
                last_success_at     TEXT,
                last_status_code    INTEGER,
                last_error          TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                UNIQUE(event_id, hook_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_owner_created ON job_events(caller_owner_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_agent_owner_created ON job_events(agent_owner_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_job_created ON job_events(job_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_hooks_owner_active ON job_event_hooks(owner_id, is_active)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_event_deliveries_status_due
            ON job_event_deliveries(status, next_attempt_at, delivery_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_event_deliveries_owner_created
            ON job_event_deliveries(owner_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_requests (
                request_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id         TEXT NOT NULL,
                scope            TEXT NOT NULL,
                idempotency_key  TEXT NOT NULL,
                request_hash     TEXT NOT NULL,
                status           TEXT NOT NULL CHECK(status IN ('in_progress', 'completed')),
                response_status  INTEGER,
                response_body    TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                UNIQUE(owner_id, scope, idempotency_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_updated ON idempotency_requests(updated_at DESC)"
        )


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
            "endpoint_url": _BUILTIN_PROXY_ENDPOINTS[_FINANCIAL_AGENT_ID],
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
            "endpoint_url": _BUILTIN_PROXY_ENDPOINTS[_CODEREVIEW_AGENT_ID],
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
            "endpoint_url": _BUILTIN_PROXY_ENDPOINTS[_TEXTINTEL_AGENT_ID],
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
            "endpoint_url": _BUILTIN_PROXY_ENDPOINTS[_WIKI_AGENT_ID],
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
                owner_id="master",
                embed_listing=False,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.init_db()
    payments.init_payments_db()
    _auth.init_auth_db()
    jobs.init_jobs_db()
    disputes.init_disputes_db()
    reputation.init_reputation_db()
    _init_ops_db()
    _register_agents()
    stop_event: threading.Event | None = None
    sweeper_thread: threading.Thread | None = None
    hook_stop_event: threading.Event | None = None
    hook_thread: threading.Thread | None = None
    if _SWEEPER_ENABLED:
        stop_event = threading.Event()
        sweeper_thread = threading.Thread(
            target=_jobs_sweeper_loop,
            args=(stop_event,),
            daemon=True,
            name="agentmarket-job-sweeper",
        )
        sweeper_thread.start()
    else:
        _set_sweeper_state(running=False)

    if _HOOK_DELIVERY_ENABLED:
        hook_stop_event = threading.Event()
        hook_thread = threading.Thread(
            target=_hook_delivery_loop,
            args=(hook_stop_event,),
            daemon=True,
            name="agentmarket-hook-delivery",
        )
        hook_thread.start()
    else:
        _set_hook_worker_state(running=False)
    try:
        yield
    finally:
        if stop_event is not None:
            stop_event.set()
        if sweeper_thread is not None:
            sweeper_thread.join(timeout=2)
        if hook_stop_event is not None:
            hook_stop_event.set()
        if hook_thread is not None:
            hook_thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Rate limiter — keyed per caller identity
# ---------------------------------------------------------------------------

def _key_from_request(request: Request) -> str:
    caller = _resolve_caller(request)
    if caller:
        if caller["type"] == "master":
            return "master"
        return caller["owner_id"]
    return request.client.host if request.client else "unknown"


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
    if cl:
        try:
            content_length = int(cl)
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length header."}, status_code=400)
        if content_length > _MAX_BODY_BYTES:
            return JSONResponse(
                {"detail": f"Request body too large (max {_MAX_BODY_BYTES // 1024} KB)."},
                status_code=413,
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _resolve_caller(request: Request) -> core_models.CallerContext | None:
    cached = getattr(request.state, "_caller", _CALLER_CACHE_MISSING)
    if cached is not _CALLER_CACHE_MISSING:
        return cached

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        request.state._caller = None
        return None

    raw = auth[7:]
    if raw == _MASTER_KEY:
        caller = {
            "type": "master",
            "owner_id": "master",
            "scopes": ["caller", "worker", "admin"],
        }
        request.state._caller = caller
        return caller

    user = _auth.verify_api_key(raw)
    if user:
        scopes = list(user.get("scopes") or [])
        caller = {
            "type": "user",
            "owner_id": f"user:{user['user_id']}",
            "user": user,
            "scopes": scopes,
        }
        request.state._caller = caller
        return caller

    request.state._caller = None
    return None


def _require_api_key(request: Request) -> core_models.CallerContext:
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
    if caller is None:
        raise HTTPException(status_code=403, detail="Invalid API key.")
    return caller["owner_id"]


def _caller_has_scope(caller: core_models.CallerContext, required_scope: str) -> bool:
    if caller["type"] == "master":
        return True
    scopes = {str(scope).strip().lower() for scope in (caller.get("scopes") or []) if str(scope).strip()}
    if "admin" in scopes:
        return True
    return required_scope in scopes


def _require_scope(caller: core_models.CallerContext, required_scope: str, detail: str | None = None) -> None:
    if _caller_has_scope(caller, required_scope):
        return
    scope_name = required_scope.strip().lower()
    raise HTTPException(
        status_code=403,
        detail=detail or f"This endpoint requires an API key with '{scope_name}' scope.",
    )


def _proxy_headers_for_agent(agent: dict) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    allowed_internal_urls = _BUILTIN_PROXY_AUTH_URLS.get(agent["agent_id"], set())
    endpoint_url = str(agent.get("endpoint_url", "")).rstrip("/")
    if endpoint_url in {url.rstrip("/") for url in allowed_internal_urls}:
        headers["Authorization"] = f"Bearer {_MASTER_KEY}"
    return headers


def _proxy_response(resp: http.Response) -> Response:
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type.lower():
        try:
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except ValueError:
            pass

    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    return Response(content=resp.content, status_code=resp.status_code, headers=headers)


def _extract_caller_trust_min(input_schema: dict | None) -> float | None:
    if not isinstance(input_schema, dict):
        return None
    candidate = input_schema.get("min_caller_trust")
    if candidate is None and isinstance(input_schema.get("metadata"), dict):
        candidate = input_schema["metadata"].get("min_caller_trust")
    if candidate is None:
        return None
    try:
        value = float(candidate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value > 1.0 and value <= 100.0:
        value = value / 100.0
    if value < 0.0 or value > 1.0:
        return None
    return value


def _caller_trust_score(owner_id: str) -> float:
    try:
        metrics = reputation.compute_caller_trust_metrics(owner_id)
    except Exception:
        return 0.5
    return float(metrics.get("trust_score_normalized") or 0.5)


def _agent_response(agent: dict, caller: core_models.CallerContext) -> dict:
    min_caller_trust = _extract_caller_trust_min(agent.get("input_schema"))
    if caller.get("type") == "master":
        out = dict(agent)
        out["caller_trust_min"] = min_caller_trust
        return out
    redacted = dict(agent)
    redacted.pop("owner_id", None)
    redacted["caller_trust_min"] = min_caller_trust
    return redacted


def _job_response(job: dict, caller: core_models.CallerContext) -> dict:
    if caller.get("type") == "master":
        return job

    owner_id = caller.get("owner_id")
    result = dict(job)
    hidden = {
        "caller_wallet_id",
        "agent_wallet_id",
        "platform_wallet_id",
        "charge_tx_id",
        "settled_at",
        "agent_owner_id",
    }
    for key in hidden:
        result.pop(key, None)

    if owner_id != job.get("caller_owner_id"):
        result.pop("caller_owner_id", None)
    if owner_id != job.get("claim_owner_id"):
        result.pop("claim_token", None)
    return result


def _caller_can_view_job(caller: core_models.CallerContext, job: dict) -> bool:
    if caller["type"] == "master":
        return True
    owner_id = caller["owner_id"]
    return owner_id == job["caller_owner_id"] or jobs.is_worker_authorized(job, owner_id)


def _caller_can_manage_agent(caller: core_models.CallerContext, agent: dict) -> bool:
    if caller["type"] == "master":
        return True
    return caller["owner_id"] == agent.get("owner_id")


def _assert_worker_claim(job: dict, worker_owner_id: str, claim_token: str | None) -> None:
    if not jobs.is_worker_authorized(job, worker_owner_id):
        raise HTTPException(status_code=403, detail="Not authorized for this agent job.")
    if (job.get("claim_owner_id") or "").strip() != worker_owner_id:
        raise HTTPException(status_code=409, detail="Job is not currently claimed by this worker.")
    stored_token = (job.get("claim_token") or "").strip()
    if not stored_token:
        raise HTTPException(status_code=409, detail="Job claim token is missing.")
    if not claim_token or claim_token != stored_token:
        raise HTTPException(status_code=403, detail="Invalid or missing claim_token.")


def _to_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _job_attempts_remaining(job: dict) -> bool:
    attempt_count = _to_non_negative_int(job.get("attempt_count"), default=0)
    max_attempts = max(1, _to_non_negative_int(job.get("max_attempts"), default=1))
    return attempt_count < max_attempts


def _job_has_stale_active_lease(job: dict) -> bool:
    if job.get("status") not in {"running", "awaiting_clarification"}:
        return False
    if not (job.get("claim_owner_id") or "").strip():
        return False
    lease_expires_at = _parse_iso_datetime(job.get("lease_expires_at"))
    if lease_expires_at is None:
        return False
    return lease_expires_at <= datetime.now(timezone.utc)


def _job_supports_late_worker_grace(job: dict) -> bool:
    if (job.get("claim_owner_id") or "").strip():
        return False
    if job.get("status") not in {"pending", "failed"}:
        return False
    if _to_non_negative_int(job.get("timeout_count"), default=0) <= 0:
        return False
    return _job_attempts_remaining(job)


def _audit_master_claim_bypass(job: dict, action: str, claim_token: str | None) -> None:
    jobs.add_claim_event(
        job["job_id"],
        event_type="master_claim_bypass",
        claim_owner_id=job.get("claim_owner_id"),
        claim_token=claim_token,
        lease_expires_at=job.get("lease_expires_at"),
        actor_id="master",
        metadata={"action": action, "status": job.get("status")},
    )


def _assert_settlement_claim_or_grace(
    job: dict,
    caller: core_models.CallerContext,
    claim_token: str | None,
    action: str,
) -> None:
    actor_owner_id = caller["owner_id"]
    if caller["type"] == "master":
        _audit_master_claim_bypass(job, action=action, claim_token=claim_token)
        return

    if not jobs.is_worker_authorized(job, actor_owner_id):
        raise HTTPException(status_code=403, detail="Not authorized for this agent job.")

    if (job.get("claim_owner_id") or "").strip() == actor_owner_id:
        _assert_worker_claim(job, actor_owner_id, claim_token)
        return

    if not _job_supports_late_worker_grace(job):
        raise HTTPException(status_code=409, detail="Job is not currently claimed by this worker.")
    if not claim_token:
        raise HTTPException(status_code=403, detail="Invalid or missing claim_token.")
    if not jobs.claim_token_was_recently_active(
        job["job_id"],
        claim_owner_id=actor_owner_id,
        claim_token=claim_token,
        within_seconds=_DEFAULT_LEASE_SECONDS,
    ):
        raise HTTPException(status_code=403, detail="Invalid or stale claim_token.")

    jobs.add_claim_event(
        job["job_id"],
        event_type="late_worker_grace",
        claim_owner_id=actor_owner_id,
        claim_token=claim_token,
        lease_expires_at=job.get("lease_expires_at"),
        actor_id=actor_owner_id,
        metadata={"action": action, "status": job.get("status")},
    )


def _timeout_stale_lease_at_touchpoint(job: dict, actor_owner_id: str, touchpoint: str) -> dict | None:
    if not _job_has_stale_active_lease(job):
        return None

    updated = jobs.mark_job_timeout(
        job["job_id"],
        retry_delay_seconds=_SWEEPER_RETRY_DELAY_SECONDS,
    )
    if updated is None:
        return None

    jobs.add_claim_event(
        job["job_id"],
        event_type="touchpoint_timeout",
        claim_owner_id=job.get("claim_owner_id"),
        claim_token=job.get("claim_token"),
        lease_expires_at=job.get("lease_expires_at"),
        actor_id=actor_owner_id,
        metadata={"touchpoint": touchpoint, "status_after": updated.get("status")},
    )

    if updated["status"] == "failed":
        return _settle_failed_job(
            updated,
            actor_owner_id=actor_owner_id,
            event_type="job.timeout_terminal",
        )

    _record_job_event(
        updated,
        "job.timeout_retry_scheduled",
        actor_owner_id=actor_owner_id,
        payload={
            "retry_count": updated["retry_count"],
            "next_retry_at": updated["next_retry_at"],
            "touchpoint": touchpoint,
        },
    )
    return updated


def _job_latency_ms(job: dict) -> float:
    try:
        created = datetime.fromisoformat(job["created_at"])
        completed = datetime.fromisoformat(job["completed_at"])
        return max(0.0, (completed - created).total_seconds() * 1000)
    except Exception:
        return 0.0


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    idx = max(0, min(len(sorted_values) - 1, math.ceil(len(sorted_values) * 0.95) - 1))
    return sorted_values[idx]


def _encode_jobs_cursor(created_at: str, job_id: str) -> str:
    raw = f"{created_at}|{job_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_jobs_cursor(cursor: str | None) -> tuple[str, str] | tuple[None, None]:
    if cursor is None:
        return None, None
    token = cursor.strip()
    if not token:
        raise HTTPException(status_code=422, detail="cursor must not be empty.")
    try:
        padded = token + ("=" * (-len(token) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        created_at, job_id = decoded.split("|", 1)
        datetime.fromisoformat(created_at)
        if not job_id.strip():
            raise ValueError("job_id missing")
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor.") from exc
    return created_at, job_id


def _normalize_job_message_protocol(
    raw_type: str,
    raw_payload: dict,
    correlation_id: str | None = None,
) -> dict:
    msg_type = str(raw_type or "").strip().lower()
    if not msg_type:
        raise ValueError("type must not be empty")
    if not isinstance(raw_payload, dict):
        raise ValueError("payload must be an object.")

    parsed = _parse_job_message_protocol_from_models(msg_type, raw_payload, correlation_id)
    if parsed is None:
        parsed = _parse_job_message_protocol_fallback(msg_type, raw_payload, correlation_id)

    normalized_type = str(parsed.get("type") or "").strip().lower()
    payload = parsed.get("payload", {})
    normalized_correlation = parsed.get("correlation_id")
    if not normalized_type:
        raise ValueError("type must not be empty")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object.")

    if normalized_type in _LEGACY_JOB_MESSAGE_TYPES:
        _LOG.warning(
            "Deprecated legacy job message contract used for type '%s'; prefer typed protocol.",
            normalized_type,
        )
        return {
            "type": normalized_type,
            "payload": payload,
            "correlation_id": normalized_correlation,
            "legacy_type": normalized_type,
        }

    if normalized_type in _TYPED_JOB_MESSAGE_TYPES:
        return {
            "type": normalized_type,
            "payload": payload,
            "correlation_id": normalized_correlation,
            "legacy_type": None,
        }

    return _map_unknown_legacy_message_to_note(normalized_type, payload, normalized_correlation)


def _parse_job_message_protocol_from_models(
    msg_type: str,
    payload: dict,
    correlation_id: str | None,
) -> dict | None:
    normalize_helper = getattr(core_models, "normalize_job_message_body", None)
    if not callable(normalize_helper):
        return None

    try:
        normalized = normalize_helper(
            msg_type=msg_type,
            payload=payload,
            correlation_id=correlation_id,
            allow_legacy=True,
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(str(exc)) from exc

    normalized_type = str(normalized.get("type") or msg_type).strip().lower()
    canonical_type = str(normalized.get("canonical_type") or normalized_type).strip().lower()
    normalized_payload = normalized.get("payload", payload)
    normalized_correlation = normalized.get("correlation_id")
    if not isinstance(normalized_payload, dict):
        raise ValueError("payload must be an object.")

    if normalized_type in _LEGACY_JOB_MESSAGE_TYPES:
        return {
            "type": normalized_type,
            "payload": normalized_payload,
            "correlation_id": normalized_correlation,
        }
    return {
        "type": canonical_type,
        "payload": normalized_payload,
        "correlation_id": normalized_correlation,
    }


def _parse_job_message_protocol_fallback(
    msg_type: str,
    payload: dict,
    correlation_id: str | None,
) -> dict:
    normalized_correlation = None
    if correlation_id is not None:
        text = str(correlation_id).strip()
        normalized_correlation = text or None

    if msg_type in _TYPED_JOB_MESSAGE_TYPES:
        validated_payload = _validate_typed_job_message_payload(msg_type, payload)
        return {
            "type": msg_type,
            "payload": validated_payload,
            "correlation_id": normalized_correlation,
        }

    if msg_type in _LEGACY_JOB_MESSAGE_TYPES:
        return {"type": msg_type, "payload": dict(payload), "correlation_id": normalized_correlation}

    return _map_unknown_legacy_message_to_note(msg_type, dict(payload), normalized_correlation)


def _map_unknown_legacy_message_to_note(
    legacy_type: str,
    payload: dict,
    correlation_id: str | None = None,
) -> dict:
    _LOG.warning(
        "Deprecated unknown legacy job message type '%s' mapped to note.",
        legacy_type,
    )
    note_text = str(
        payload.get("text")
        or payload.get("note")
        or payload.get("message")
        or f"Legacy message type '{legacy_type}'"
    ).strip()
    if not note_text:
        note_text = f"Legacy message type '{legacy_type}'"
    return {
        "type": "note",
        "payload": {
            "text": note_text,
            "legacy_type": legacy_type,
            "legacy_payload": payload,
        },
        "correlation_id": correlation_id,
        "legacy_type": legacy_type,
    }


def _validate_typed_job_message_payload(msg_type: str, payload: dict) -> dict:
    normalized = dict(payload)

    def _required_text(field: str, label: str | None = None) -> str:
        key = label or field
        value = str(normalized.get(field, "")).strip()
        if not value:
            raise ValueError(f"{msg_type} payload.{key} is required.")
        return value

    if msg_type == "clarification_request":
        normalized["question"] = _required_text("question")
        return normalized

    if msg_type == "clarification_response":
        normalized["answer"] = _required_text("answer")
        return normalized

    if msg_type == "note":
        text = str(
            normalized.get("message")
            or normalized.get("note")
            or normalized.get("text")
            or ""
        ).strip()
        if not text:
            raise ValueError("note payload.text is required.")
        normalized["text"] = text
        return normalized

    if msg_type == "progress":
        percent_raw = normalized.get("percent")
        if percent_raw is None:
            raise ValueError("progress payload.percent is required.")
        if percent_raw is not None:
            try:
                percent = int(percent_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("progress payload.percent must be an integer between 0 and 100.") from exc
            if percent < 0 or percent > 100:
                raise ValueError("progress payload.percent must be an integer between 0 and 100.")
            normalized["percent"] = percent
        note = str(normalized.get("note") or "").strip()
        if note:
            normalized["note"] = note
        return normalized

    if msg_type == "tool_call":
        tool_name = str(normalized.get("tool_name") or normalized.get("name") or "").strip()
        if not tool_name:
            raise ValueError("tool_call payload.tool_name is required.")
        normalized["tool_name"] = tool_name
        args = normalized.get("args")
        if args is None:
            normalized["args"] = {}
        elif not isinstance(args, dict):
            raise ValueError("tool_call payload.args must be an object.")
        correlation_id = str(normalized.get("correlation_id") or "").strip()
        if correlation_id:
            normalized["correlation_id"] = correlation_id
        else:
            normalized.pop("correlation_id", None)
        return normalized

    if msg_type == "tool_result":
        correlation_id = str(normalized.get("correlation_id") or "").strip()
        if not correlation_id:
            raise ValueError("tool_result payload.correlation_id is required.")
        normalized["correlation_id"] = correlation_id
        result_payload = normalized.get("payload")
        if result_payload is None:
            normalized["payload"] = {}
        elif not isinstance(result_payload, dict):
            raise ValueError("tool_result payload.payload must be an object.")
        return normalized

    raise ValueError(f"Unsupported typed message type: {msg_type}")


def _job_has_tool_call_correlation(job_id: str, correlation_id: str) -> bool:
    helper = getattr(jobs, "tool_call_correlation_exists", None)
    if callable(helper):
        try:
            return bool(helper(job_id, correlation_id))
        except Exception:
            pass

    since_id: int | None = None
    while True:
        batch = jobs.get_messages(job_id, since_id=since_id, limit=200)
        if not batch:
            return False
        for item in batch:
            if item.get("type") != "tool_call":
                continue
            payload = item.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if str(payload.get("correlation_id") or "").strip() == correlation_id:
                return True
        if len(batch) < 200:
            return False
        since_id = int(batch[-1]["message_id"])


def _subscribe_job_stream(job_id: str) -> Queue:
    return jobs.subscribe_job_messages(job_id)


def _unsubscribe_job_stream(job_id: str, subscriber: Queue) -> None:
    jobs.unsubscribe_job_messages(job_id, subscriber)


def _job_message_to_sse(message: dict) -> str:
    event_id = message.get("message_id")
    payload = json.dumps(message, separators=(",", ":"), default=str)
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("event: message")
    for line in payload.splitlines():
        lines.append(f"data: {line}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _event_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        payload = json.loads(d.get("payload") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    d["payload"] = payload if isinstance(payload, dict) else {}
    return d


def _record_job_event(
    job: dict | None,
    event_type: str,
    actor_owner_id: str | None = None,
    payload: dict | None = None,
) -> dict | None:
    if job is None:
        return None

    try:
        payload_json = json.dumps(payload or {})
    except TypeError:
        payload_json = json.dumps({"value": str(payload)})

    created_at = _utc_now_iso()
    with jobs._conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO job_events
                (job_id, agent_id, agent_owner_id, caller_owner_id,
                 event_type, actor_owner_id, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["job_id"],
                job["agent_id"],
                job["agent_owner_id"],
                job["caller_owner_id"],
                event_type,
                actor_owner_id,
                payload_json,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM job_events WHERE event_id = ?",
            (cur.lastrowid,),
        ).fetchone()

    event = _event_row_to_dict(row)
    _deliver_job_event_hooks(event)
    return event


def _stable_json_text(payload: Any) -> str:
    try:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    except TypeError:
        return json.dumps({"value": str(payload)}, separators=(",", ":"), sort_keys=True)


def _idempotency_begin(
    request: Request,
    caller: core_models.CallerContext,
    scope: str,
    payload: Any,
) -> dict | None:
    idempotency_key = (request.headers.get(_IDEMPOTENCY_KEY_HEADER, "") or "").strip()
    if not idempotency_key:
        return None
    if len(idempotency_key) > 128:
        raise HTTPException(status_code=422, detail=f"{_IDEMPOTENCY_KEY_HEADER} is too long.")

    owner_id = caller["owner_id"]
    request_hash = hashlib.sha256(_stable_json_text(payload).encode("utf-8")).hexdigest()
    now = _utc_now_iso()

    with jobs._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT request_hash, status, response_status, response_body
            FROM idempotency_requests
            WHERE owner_id = ? AND scope = ? AND idempotency_key = ?
            """,
            (owner_id, scope, idempotency_key),
        ).fetchone()
        if row is not None:
            if row["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{_IDEMPOTENCY_KEY_HEADER} was already used for a different request payload."
                    ),
                )
            if row["status"] == "completed":
                try:
                    replay_body = json.loads(row["response_body"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    replay_body = {"detail": "Stored idempotent response is invalid."}
                replay_status = int(row["response_status"] or 200)
                return {
                    "replay": True,
                    "status_code": replay_status,
                    "body": replay_body,
                }
            raise HTTPException(
                status_code=409,
                detail=f"A request with this {_IDEMPOTENCY_KEY_HEADER} is still in progress.",
            )

        conn.execute(
            """
            INSERT INTO idempotency_requests
                (owner_id, scope, idempotency_key, request_hash, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'in_progress', ?, ?)
            """,
            (owner_id, scope, idempotency_key, request_hash, now, now),
        )

    return {
        "replay": False,
        "owner_id": owner_id,
        "scope": scope,
        "idempotency_key": idempotency_key,
    }


def _idempotency_complete(idempotency_state: dict | None, body: Any, status_code: int) -> None:
    if not idempotency_state or idempotency_state.get("replay"):
        return
    now = _utc_now_iso()
    with jobs._conn() as conn:
        conn.execute(
            """
            UPDATE idempotency_requests
            SET status = 'completed',
                response_status = ?,
                response_body = ?,
                updated_at = ?
            WHERE owner_id = ? AND scope = ? AND idempotency_key = ? AND status = 'in_progress'
            """,
            (
                int(status_code),
                _stable_json_text(body),
                now,
                idempotency_state["owner_id"],
                idempotency_state["scope"],
                idempotency_state["idempotency_key"],
            ),
        )


def _idempotency_abort(idempotency_state: dict | None) -> None:
    if not idempotency_state or idempotency_state.get("replay"):
        return
    with jobs._conn() as conn:
        conn.execute(
            """
            DELETE FROM idempotency_requests
            WHERE owner_id = ? AND scope = ? AND idempotency_key = ? AND status = 'in_progress'
            """,
            (
                idempotency_state["owner_id"],
                idempotency_state["scope"],
                idempotency_state["idempotency_key"],
            ),
        )


def _run_idempotent_json_response(
    request: Request,
    caller: core_models.CallerContext,
    scope: str,
    payload: Any,
    operation: Callable[[], tuple[Any, int]],
) -> JSONResponse:
    idempotency_state = _idempotency_begin(request, caller, scope, payload)
    if idempotency_state and idempotency_state.get("replay"):
        return JSONResponse(
            content=idempotency_state["body"],
            status_code=int(idempotency_state["status_code"]),
        )

    try:
        body, status_code = operation()
    except Exception:
        _idempotency_abort(idempotency_state)
        raise

    _idempotency_complete(idempotency_state, body=body, status_code=status_code)
    return JSONResponse(content=body, status_code=status_code)


def _hook_row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _validate_outbound_url(target_url: str, field_name: str) -> str:
    parsed = urlparse(target_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute http(s) URL.")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError(f"{field_name} hostname is missing.")
    if _ALLOW_PRIVATE_OUTBOUND_URLS:
        return target_url.strip()

    if host == "localhost":
        raise ValueError(f"{field_name} cannot target localhost unless ALLOW_PRIVATE_OUTBOUND_URLS=1.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname (non-literal IP) is allowed.
        return target_url.strip()

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise ValueError(
            f"{field_name} cannot target private/loopback/reserved IPs unless ALLOW_PRIVATE_OUTBOUND_URLS=1."
        )
    return target_url.strip()


def _validate_hook_url(target_url: str) -> str:
    return _validate_outbound_url(target_url, "target_url")


def _create_job_event_hook(owner_id: str, target_url: str, secret: str | None = None) -> dict:
    hook_id = str(uuid.uuid4())
    now = _utc_now_iso()
    normalized_secret = secret.strip() if secret else None
    with jobs._conn() as conn:
        conn.execute(
            """
            INSERT INTO job_event_hooks
                (hook_id, owner_id, target_url, secret, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (hook_id, owner_id, _validate_hook_url(target_url), normalized_secret, now),
        )
        row = conn.execute(
            "SELECT * FROM job_event_hooks WHERE hook_id = ?",
            (hook_id,),
        ).fetchone()
    return _hook_row_to_dict(row)


def _list_job_event_hooks(owner_id: str | None = None, include_inactive: bool = False) -> list[dict]:
    clauses = []
    params: list[Any] = []
    if owner_id is not None:
        clauses.append("owner_id = ?")
        params.append(owner_id)
    if not include_inactive:
        clauses.append("is_active = 1")
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM job_event_hooks
            {where_sql}
            ORDER BY created_at DESC
            """,
            tuple(params),
        ).fetchall()
    return [_hook_row_to_dict(r) for r in rows]


def _deactivate_job_event_hook(hook_id: str, owner_id: str | None = None) -> bool:
    with jobs._conn() as conn:
        if owner_id is None:
            result = conn.execute(
                "UPDATE job_event_hooks SET is_active = 0 WHERE hook_id = ?",
                (hook_id,),
            )
        else:
            result = conn.execute(
                "UPDATE job_event_hooks SET is_active = 0 WHERE hook_id = ? AND owner_id = ?",
                (hook_id, owner_id),
            )
    return result.rowcount > 0


def _deliver_job_event_hooks(event: dict) -> None:
    _enqueue_job_event_hook_deliveries(event)


def _set_hook_worker_state(**updates: Any) -> None:
    with _HOOK_WORKER_STATE_LOCK:
        _HOOK_WORKER_STATE.update(updates)


def _enqueue_job_event_hook_deliveries(event: dict) -> None:
    owner_ids = {event.get("caller_owner_id"), event.get("agent_owner_id")}
    owner_ids = {owner_id for owner_id in owner_ids if owner_id}
    if not owner_ids:
        return

    placeholders = ",".join(["?"] * len(owner_ids))
    payload_json = _stable_json_text(event)
    now = _utc_now_iso()
    with jobs._conn() as conn:
        hooks = conn.execute(
            f"""
            SELECT * FROM job_event_hooks
            WHERE is_active = 1 AND owner_id IN ({placeholders})
            """,
            tuple(owner_ids),
        ).fetchall()

    if not hooks:
        return

    for row in hooks:
        hook = _hook_row_to_dict(row)
        with jobs._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO job_event_deliveries
                    (event_id, hook_id, owner_id, target_url, secret, payload,
                     status, attempt_count, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    hook["hook_id"],
                    hook["owner_id"],
                    hook["target_url"],
                    hook.get("secret"),
                    payload_json,
                    now,
                    now,
                    now,
                ),
            )


def _hook_backoff_seconds(attempt_count: int) -> int:
    exponent = max(0, attempt_count - 1)
    delay = _HOOK_DELIVERY_BASE_DELAY_SECONDS * (2 ** exponent)
    return min(delay, _HOOK_DELIVERY_MAX_DELAY_SECONDS)


def _claim_due_hook_delivery(now_iso: str) -> dict | None:
    with jobs._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT *
            FROM job_event_deliveries
            WHERE status IN ('pending', 'retrying')
              AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC, delivery_id ASC
            LIMIT 1
            """,
            (now_iso,),
        ).fetchone()
        if row is None:
            return None

        result = conn.execute(
            """
            UPDATE job_event_deliveries
            SET status = 'retrying',
                attempt_count = attempt_count + 1,
                last_attempt_at = ?,
                updated_at = ?
            WHERE delivery_id = ?
              AND status IN ('pending', 'retrying')
              AND next_attempt_at <= ?
            """,
            (now_iso, now_iso, row["delivery_id"], now_iso),
        )
        if result.rowcount == 0:
            return None

        claimed = conn.execute(
            "SELECT * FROM job_event_deliveries WHERE delivery_id = ?",
            (row["delivery_id"],),
        ).fetchone()
    return dict(claimed) if claimed else None


def _update_hook_attempt_metadata(
    hook_id: str,
    attempted_at: str,
    success: bool,
    status_code: int | None,
    error_text: str | None,
) -> None:
    with jobs._conn() as conn:
        conn.execute(
            """
            UPDATE job_event_hooks
            SET last_attempt_at = ?,
                last_success_at = CASE WHEN ? = 1 THEN ? ELSE last_success_at END,
                last_status_code = ?,
                last_error = ?
            WHERE hook_id = ?
            """,
            (
                attempted_at,
                1 if success else 0,
                attempted_at,
                status_code,
                error_text,
                hook_id,
            ),
        )


def _mark_hook_delivery(
    delivery_id: int,
    *,
    status: str,
    next_attempt_at: str,
    status_code: int | None,
    error_text: str | None,
    now_iso: str,
    mark_success: bool,
) -> None:
    with jobs._conn() as conn:
        conn.execute(
            """
            UPDATE job_event_deliveries
            SET status = ?,
                next_attempt_at = ?,
                last_status_code = ?,
                last_error = ?,
                last_success_at = CASE WHEN ? = 1 THEN ? ELSE last_success_at END,
                updated_at = ?
            WHERE delivery_id = ?
            """,
            (
                status,
                next_attempt_at,
                status_code,
                error_text,
                1 if mark_success else 0,
                now_iso,
                now_iso,
                delivery_id,
            ),
        )


def _process_due_hook_deliveries(limit: int = _HOOK_DELIVERY_BATCH_SIZE) -> dict:
    batch_limit = min(max(1, int(limit)), 500)
    processed = 0
    delivered = 0
    retried = 0
    dead_lettered = 0

    for _ in range(batch_limit):
        now_iso = _utc_now_iso()
        delivery = _claim_due_hook_delivery(now_iso)
        if delivery is None:
            break

        processed += 1
        delivery_id = int(delivery["delivery_id"])
        hook_id = str(delivery["hook_id"])
        attempt_count = int(delivery["attempt_count"])

        with jobs._conn() as conn:
            hook_row = conn.execute(
                "SELECT is_active FROM job_event_hooks WHERE hook_id = ?",
                (hook_id,),
            ).fetchone()

        if hook_row is None or int(hook_row["is_active"]) != 1:
            error_text = "Hook is inactive or deleted."
            _update_hook_attempt_metadata(
                hook_id=hook_id,
                attempted_at=now_iso,
                success=False,
                status_code=None,
                error_text=error_text,
            )
            _mark_hook_delivery(
                delivery_id,
                status="dead_letter",
                next_attempt_at=now_iso,
                status_code=None,
                error_text=error_text,
                now_iso=now_iso,
                mark_success=False,
            )
            dead_lettered += 1
            continue

        try:
            payload = json.loads(delivery["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload_bytes = _stable_json_text(payload).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "X-AgentMarket-Event-Id": str(delivery["event_id"]),
            "X-AgentMarket-Event-Type": str(payload.get("event_type") or "unknown"),
        }
        secret = (delivery.get("secret") or "").strip()
        if secret:
            digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
            headers["X-AgentMarket-Signature"] = f"sha256={digest}"

        status_code = None
        error_text = None
        success = False
        try:
            resp = http.post(
                str(delivery["target_url"]),
                data=payload_bytes,
                headers=headers,
                timeout=5,
            )
            status_code = int(resp.status_code)
            success = 200 <= status_code < 300
            if not success:
                error_text = f"Non-2xx status: {status_code}"
        except http.RequestException as exc:
            error_text = str(exc)

        _update_hook_attempt_metadata(
            hook_id=hook_id,
            attempted_at=now_iso,
            success=success,
            status_code=status_code,
            error_text=error_text,
        )

        if success:
            _mark_hook_delivery(
                delivery_id,
                status="delivered",
                next_attempt_at=now_iso,
                status_code=status_code,
                error_text=None,
                now_iso=now_iso,
                mark_success=True,
            )
            delivered += 1
            continue

        if attempt_count >= _HOOK_DELIVERY_MAX_ATTEMPTS:
            _mark_hook_delivery(
                delivery_id,
                status="dead_letter",
                next_attempt_at=now_iso,
                status_code=status_code,
                error_text=error_text,
                now_iso=now_iso,
                mark_success=False,
            )
            dead_lettered += 1
            continue

        retry_delay = _hook_backoff_seconds(attempt_count)
        next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_delay)).isoformat()
        _mark_hook_delivery(
            delivery_id,
            status="retrying",
            next_attempt_at=next_attempt_at,
            status_code=status_code,
            error_text=error_text,
            now_iso=now_iso,
            mark_success=False,
        )
        retried += 1

    with jobs._conn() as conn:
        pending = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_event_deliveries
            WHERE status IN ('pending', 'retrying')
            """
        ).fetchone()["count"]
        dead = conn.execute(
            "SELECT COUNT(*) AS count FROM job_event_deliveries WHERE status = 'dead_letter'"
        ).fetchone()["count"]

    return {
        "processed": int(processed),
        "delivered": int(delivered),
        "retried": int(retried),
        "dead_lettered": int(dead_lettered),
        "pending": int(pending),
        "dead_letter_total": int(dead),
    }


def _hook_delivery_loop(stop_event: threading.Event) -> None:
    _set_hook_worker_state(running=True, started_at=_utc_now_iso())
    while not stop_event.wait(_HOOK_DELIVERY_INTERVAL_SECONDS):
        started = _utc_now_iso()
        try:
            summary = _process_due_hook_deliveries(limit=_HOOK_DELIVERY_BATCH_SIZE)
            _set_hook_worker_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
        except Exception as exc:
            _set_hook_worker_state(
                last_run_at=started,
                last_error=str(exc),
            )
    _set_hook_worker_state(running=False)


def _list_hook_deliveries(
    owner_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    capped_limit = min(max(1, limit), 500)
    where: list[str] = []
    params: list[Any] = []
    if owner_id is not None:
        where.append("owner_id = ?")
        params.append(owner_id)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(capped_limit)
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM job_event_deliveries
            {where_sql}
            ORDER BY created_at DESC, delivery_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def _list_job_events(caller: core_models.CallerContext, since: int | None = None, limit: int = 100) -> list[dict]:
    limit = min(max(1, limit), 200)
    params: list[Any] = []
    where_clauses = []
    if caller["type"] != "master":
        where_clauses.append("(caller_owner_id = ? OR agent_owner_id = ?)")
        params.extend([caller["owner_id"], caller["owner_id"]])
    if since is not None:
        where_clauses.append("event_id > ?")
        params.append(since)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    params.append(limit)
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM job_events
            {where_sql}
            ORDER BY event_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_event_row_to_dict(r) for r in rows]


def _settle_successful_job(job: dict, actor_owner_id: str) -> dict:
    newly_settled = False
    if disputes.has_dispute_for_job(job["job_id"]):
        return jobs.get_job(job["job_id"]) or job
    if not job["settled_at"]:
        payments.post_call_payout(
            job["agent_wallet_id"],
            job["platform_wallet_id"],
            job["charge_tx_id"],
            job["price_cents"],
            job["agent_id"],
        )
        newly_settled = jobs.mark_settled(job["job_id"])
        if newly_settled:
            registry.update_call_stats(job["agent_id"], latency_ms=_job_latency_ms(job), success=True)
    settled = jobs.get_job(job["job_id"]) or job
    if newly_settled:
        _record_job_event(
            settled,
            "job.completed",
            actor_owner_id=actor_owner_id,
            payload={"status": settled["status"]},
        )
    return settled


def _settle_failed_job(job: dict, actor_owner_id: str, event_type: str = "job.failed") -> dict:
    newly_settled = False
    if not job["settled_at"]:
        payments.post_call_refund(
            job["caller_wallet_id"],
            job["charge_tx_id"],
            job["price_cents"],
            job["agent_id"],
        )
        newly_settled = jobs.mark_settled(job["job_id"])
        if newly_settled:
            registry.update_call_stats(job["agent_id"], latency_ms=_job_latency_ms(job), success=False)
    settled = jobs.get_job(job["job_id"]) or job
    if newly_settled:
        _record_job_event(
            settled,
            event_type,
            actor_owner_id=actor_owner_id,
            payload={"status": settled["status"], "error_message": settled.get("error_message")},
        )
    return settled


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _dispute_view(dispute_row: dict) -> dict:
    payload = dict(dispute_row)
    payload["judgments"] = disputes.get_judgments(payload["dispute_id"])
    return payload


def _dispute_side_for_caller(caller: core_models.CallerContext, job: dict) -> str:
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Master key cannot file disputes.")
    owner_id = caller["owner_id"]
    if owner_id == job["caller_owner_id"]:
        return "caller"
    if jobs.is_worker_authorized(job, owner_id):
        return "agent"
    raise HTTPException(status_code=403, detail="Only the caller or agent owner can file this dispute.")


def _resolve_dispute_with_judges(dispute_id: str, actor_owner_id: str) -> tuple[dict, dict | None]:
    result = judges.run_judgment(dispute_id)
    status = str(result.get("status") or "").strip().lower()
    outcome = result.get("outcome")
    settlement = None

    if status == "consensus" and outcome:
        dispute_row = disputes.get_dispute(dispute_id)
        if dispute_row is None:
            raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
        settlement = payments.post_dispute_settlement(
            dispute_id,
            outcome=outcome,
            split_caller_cents=dispute_row.get("split_caller_cents"),
            split_agent_cents=dispute_row.get("split_agent_cents"),
        )
        disputes.finalize_dispute(
            dispute_id,
            status="resolved",
            outcome=outcome,
            split_caller_cents=dispute_row.get("split_caller_cents"),
            split_agent_cents=dispute_row.get("split_agent_cents"),
        )
    elif status == "tied":
        disputes.set_dispute_status(dispute_id, "tied")

    latest = disputes.get_dispute(dispute_id)
    if latest is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
    job = jobs.get_job(latest["job_id"])
    if job is not None:
        _record_job_event(
            job,
            "job.dispute_judged",
            actor_owner_id=actor_owner_id,
            payload={"dispute_id": dispute_id, "status": latest["status"], "outcome": latest.get("outcome")},
        )
    return _dispute_view(latest), settlement


def _sweep_jobs(
    retry_delay_seconds: int = _DEFAULT_RETRY_DELAY_SECONDS,
    sla_seconds: int = _DEFAULT_SLA_SECONDS,
    limit: int = 100,
    actor_owner_id: str = "system:sweeper",
) -> dict:
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must be >= 0.")
    if sla_seconds <= 0:
        raise ValueError("sla_seconds must be > 0.")
    limit = min(max(1, limit), 500)

    expired = jobs.list_jobs_with_expired_leases(limit=limit)
    timeout_retry_job_ids: list[str] = []
    timeout_failed_job_ids: list[str] = []
    for item in expired:
        updated = jobs.mark_job_timeout(
            item["job_id"],
            retry_delay_seconds=retry_delay_seconds,
        )
        if updated is None:
            continue
        if updated["status"] == "failed":
            settled = _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.timeout_terminal")
            timeout_failed_job_ids.append(settled["job_id"])
        else:
            timeout_retry_job_ids.append(updated["job_id"])
            _record_job_event(
                updated,
                "job.timeout_retry_scheduled",
                actor_owner_id=actor_owner_id,
                payload={"retry_count": updated["retry_count"], "next_retry_at": updated["next_retry_at"]},
            )

    sla_failed_job_ids: list[str] = []
    for item in jobs.list_jobs_past_sla(sla_seconds=sla_seconds, limit=limit):
        updated = jobs.update_job_status(
            item["job_id"],
            "failed",
            error_message="Job exceeded SLA and was automatically failed.",
            completed=True,
        )
        if updated is None:
            continue
        settled = _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.sla_expired")
        sla_failed_job_ids.append(settled["job_id"])

    due_retry = jobs.list_jobs_due_for_retry(limit=limit)
    return {
        "expired_leases_scanned": len(expired),
        "due_retry_count": len(due_retry),
        "timeout_retry_job_ids": timeout_retry_job_ids,
        "timeout_failed_job_ids": timeout_failed_job_ids,
        "sla_failed_job_ids": sla_failed_job_ids,
    }


def _set_sweeper_state(**updates: Any) -> None:
    with _SWEEPER_STATE_LOCK:
        _SWEEPER_STATE.update(updates)


def _jobs_sweeper_loop(stop_event: threading.Event) -> None:
    _set_sweeper_state(running=True, started_at=_utc_now_iso())
    while not stop_event.wait(_SWEEPER_INTERVAL_SECONDS):
        started = _utc_now_iso()
        try:
            summary = _sweep_jobs(
                retry_delay_seconds=_SWEEPER_RETRY_DELAY_SECONDS,
                sla_seconds=_SWEEPER_SLA_SECONDS,
                limit=_SWEEPER_LIMIT,
                actor_owner_id="system:scheduler",
            )
            _set_sweeper_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
        except Exception as exc:
            _set_sweeper_state(
                last_run_at=started,
                last_error=str(exc),
            )
    _set_sweeper_state(running=False)


def _jobs_metrics(sla_seconds: int = _DEFAULT_SLA_SECONDS) -> dict:
    events_since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with jobs._conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        ).fetchall()
        status_counts = {row["status"]: int(row["count"]) for row in rows}
        unsettled = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE settled_at IS NULL"
        ).fetchone()["count"]
        failed_unsettled = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status = 'failed' AND settled_at IS NULL"
        ).fetchone()["count"]
        events_24h = conn.execute(
            "SELECT COUNT(*) AS count FROM job_events WHERE created_at >= ?",
            (events_since,),
        ).fetchone()["count"]
        delivery_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM job_event_deliveries
            GROUP BY status
            """
        ).fetchall()
        delivery_attempted_24h = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_event_deliveries
            WHERE last_attempt_at IS NOT NULL AND last_attempt_at >= ?
            """,
            (events_since,),
        ).fetchone()["count"]
        delivery_success_24h = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_event_deliveries
            WHERE last_success_at IS NOT NULL AND last_success_at >= ?
            """,
            (events_since,),
        ).fetchone()["count"]
        job_window_rows = conn.execute(
            """
            SELECT created_at, claimed_at, settled_at, timeout_count
            FROM jobs
            WHERE created_at >= ?
            """,
            (events_since,),
        ).fetchall()

    expired_leases_count = len(jobs.list_jobs_with_expired_leases(limit=200))
    due_retry_count = len(jobs.list_jobs_due_for_retry(limit=200))
    sla_breach_count = len(jobs.list_jobs_past_sla(sla_seconds=sla_seconds, limit=200))
    delivery_status_counts = {row["status"]: int(row["count"]) for row in delivery_rows}
    delivery_success_rate_24h = (
        round(float(delivery_success_24h) / float(delivery_attempted_24h), 4)
        if delivery_attempted_24h > 0
        else None
    )
    claim_latencies_ms: list[float] = []
    settlement_latencies_ms: list[float] = []
    timeout_jobs_24h = 0
    total_jobs_24h = len(job_window_rows)
    for row in job_window_rows:
        created_at = _parse_iso_datetime(row["created_at"])
        if created_at is None:
            continue

        claimed_at = _parse_iso_datetime(row["claimed_at"])
        if claimed_at is not None and claimed_at >= created_at:
            claim_latencies_ms.append((claimed_at - created_at).total_seconds() * 1000.0)

        settled_at = _parse_iso_datetime(row["settled_at"])
        if settled_at is not None and settled_at >= created_at:
            settlement_latencies_ms.append((settled_at - created_at).total_seconds() * 1000.0)

        if int(row["timeout_count"] or 0) > 0:
            timeout_jobs_24h += 1

    claim_p95_ms = round(_p95(claim_latencies_ms) or 0.0, 3) if claim_latencies_ms else None
    settlement_p95_ms = (
        round(_p95(settlement_latencies_ms) or 0.0, 3)
        if settlement_latencies_ms
        else None
    )
    timeout_rate_24h = (
        round(float(timeout_jobs_24h) / float(total_jobs_24h), 4)
        if total_jobs_24h > 0
        else None
    )
    slo = {
        "window_hours": 24,
        "targets": {
            "claim_latency_p95_ms_max": _SLO_CLAIM_P95_TARGET_MS,
            "settlement_latency_p95_ms_max": _SLO_SETTLEMENT_P95_TARGET_MS,
            "timeout_rate_max": _SLO_TIMEOUT_RATE_MAX,
            "hook_success_rate_min": _SLO_HOOK_SUCCESS_RATE_MIN,
        },
        "claim_latency_p95_ms": claim_p95_ms,
        "settlement_latency_p95_ms": settlement_p95_ms,
        "timeout_rate_last_24h": timeout_rate_24h,
        "hook_success_rate_last_24h": delivery_success_rate_24h,
    }

    alerts = []
    if failed_unsettled > 0:
        alerts.append(f"{failed_unsettled} failed jobs are not settled.")
    if expired_leases_count > 0:
        alerts.append(f"{expired_leases_count} jobs have expired worker leases.")
    if sla_breach_count > 0:
        alerts.append(f"{sla_breach_count} jobs breached SLA.")
    if delivery_status_counts.get("dead_letter", 0) > 0:
        alerts.append(f"{delivery_status_counts.get('dead_letter', 0)} hook deliveries are in dead-letter.")
    if claim_p95_ms is not None and claim_p95_ms > _SLO_CLAIM_P95_TARGET_MS:
        alerts.append(
            f"Claim latency p95 {claim_p95_ms}ms exceeds SLO target {_SLO_CLAIM_P95_TARGET_MS}ms."
        )
    if settlement_p95_ms is not None and settlement_p95_ms > _SLO_SETTLEMENT_P95_TARGET_MS:
        alerts.append(
            "Settlement latency p95 "
            f"{settlement_p95_ms}ms exceeds SLO target {_SLO_SETTLEMENT_P95_TARGET_MS}ms."
        )
    if timeout_rate_24h is not None and timeout_rate_24h > _SLO_TIMEOUT_RATE_MAX:
        alerts.append(
            f"Timeout rate {timeout_rate_24h:.4f} exceeds SLO max {_SLO_TIMEOUT_RATE_MAX:.4f}."
        )
    if (
        delivery_success_rate_24h is not None
        and delivery_success_rate_24h < _SLO_HOOK_SUCCESS_RATE_MIN
    ):
        alerts.append(
            "Hook delivery success rate "
            f"{delivery_success_rate_24h:.4f} is below SLO min {_SLO_HOOK_SUCCESS_RATE_MIN:.4f}."
        )

    with _SWEEPER_STATE_LOCK:
        sweeper_state = dict(_SWEEPER_STATE)
    with _HOOK_WORKER_STATE_LOCK:
        hook_worker_state = dict(_HOOK_WORKER_STATE)

    return {
        "status_counts": status_counts,
        "unsettled_jobs": int(unsettled),
        "failed_unsettled_jobs": int(failed_unsettled),
        "expired_leases": expired_leases_count,
        "due_retries": due_retry_count,
        "sla_breaches": sla_breach_count,
        "events_last_24h": int(events_24h),
        "alerts": alerts,
        "sweeper": sweeper_state,
        "hook_worker": hook_worker_state,
        "hook_delivery": {
            "status_counts": delivery_status_counts,
            "attempted_last_24h": int(delivery_attempted_24h),
            "delivered_last_24h": int(delivery_success_24h),
            "success_rate_last_24h": delivery_success_rate_24h,
        },
        "slo": slo,
    }


def _load_manifest_content(manifest_content: str | None, manifest_url: str | None) -> tuple[str, str]:
    content = (manifest_content or "").strip()
    url = (manifest_url or "").strip()
    if bool(content) == bool(url):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of manifest_content or manifest_url.",
        )
    if content:
        return content, "inline manifest"

    try:
        safe_url = _validate_outbound_url(url, "manifest_url")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        resp = http.get(safe_url, timeout=15)
        resp.raise_for_status()
    except http.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch manifest_url: {exc}")
    if len(resp.content) > _MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Manifest too large (max {_MAX_BODY_BYTES // 1024} KB).",
        )
    text = resp.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Fetched manifest is empty.")
    return text, safe_url


def _sorted_agents(agents: list[dict], rank_by: str | None = None) -> list[dict]:
    if rank_by is None:
        return agents
    mode = rank_by.strip().lower()
    if mode == "trust":
        return sorted(
            agents,
            key=lambda a: (
                float(a.get("trust_score") or 0.0),
                float(a.get("confidence_score") or 0.0),
                int(a.get("total_calls") or 0),
            ),
            reverse=True,
        )
    if mode == "latency":
        return sorted(agents, key=lambda a: float(a.get("avg_latency_ms") or 0.0))
    if mode == "price":
        return sorted(agents, key=lambda a: float(a.get("price_per_call_usd") or 0.0))
    raise HTTPException(status_code=422, detail="rank_by must be one of: trust, latency, price.")


# ---------------------------------------------------------------------------
# OpenAPI response helpers
# ---------------------------------------------------------------------------

_OPENAPI_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    400: {"model": core_models.ErrorResponse, "description": "Bad request."},
    401: {"model": core_models.ErrorResponse, "description": "Missing or invalid authorization header."},
    402: {"model": core_models.ErrorResponse, "description": "Insufficient balance."},
    403: {"model": core_models.ErrorResponse, "description": "Forbidden."},
    404: {"model": core_models.ErrorResponse, "description": "Resource not found."},
    409: {"model": core_models.ErrorResponse, "description": "Conflict."},
    410: {"model": core_models.ErrorResponse, "description": "Lease expired."},
    413: {"model": core_models.ErrorResponse, "description": "Payload too large."},
    422: {"model": core_models.ErrorResponse, "description": "Validation error."},
    429: {"model": core_models.ErrorResponse, "description": "Rate limit exceeded."},
    500: {"model": core_models.ErrorResponse, "description": "Internal server error."},
    502: {"model": core_models.ErrorResponse, "description": "Upstream request failed."},
    503: {"model": core_models.ErrorResponse, "description": "Upstream service unavailable."},
}


def _error_responses(*codes: int) -> dict[int, dict[str, Any]]:
    return {code: _OPENAPI_ERROR_RESPONSES[code] for code in codes if code in _OPENAPI_ERROR_RESPONSES}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=core_models.HealthResponse,
    responses=_error_responses(429, 500),
)
def health() -> core_models.HealthResponse:
    return {"status": "ok", "agents": len(registry.get_agents())}


@app.get(
    "/agent.md",
    response_model=str,
    responses={
        200: {"content": {"text/markdown": {"schema": {"type": "string"}}}},
        **_error_responses(404, 429, 500),
    },
)
def onboarding_manifest_spec() -> Response:
    spec_path = os.path.join(os.path.dirname(__file__), "agent.md")
    if not os.path.exists(spec_path):
        raise HTTPException(status_code=404, detail="agent.md spec not found.")
    with open(spec_path, encoding="utf-8") as f:
        content = f.read()
    return Response(content=content, media_type="text/markdown")


@app.get(
    "/onboarding/spec",
    response_model=str,
    responses={
        200: {"content": {"text/markdown": {"schema": {"type": "string"}}}},
        **_error_responses(404, 429, 500),
    },
)
def onboarding_spec_alias() -> Response:
    return onboarding_manifest_spec()


@app.post(
    "/onboarding/validate",
    response_model=core_models.ManifestValidationResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("20/minute")
def onboarding_validate(
    request: Request,
    body: OnboardingValidateRequest,
    _: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.ManifestValidationResponse:
    manifest_content, source = _load_manifest_content(body.manifest_content, body.manifest_url)
    try:
        validated = onboarding.validate_manifest_content(manifest_content, source=source)
    except onboarding.ManifestValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return JSONResponse(content=validated)


@app.post(
    "/onboarding/ingest",
    status_code=201,
    response_model=core_models.OnboardingIngestResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
)
@limiter.limit("10/minute")
def onboarding_ingest(
    request: Request,
    body: OnboardingValidateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.OnboardingIngestResponse:
    _require_scope(caller, "worker")
    manifest_content, source = _load_manifest_content(body.manifest_content, body.manifest_url)
    try:
        payload = onboarding.build_registration_payload_from_manifest(manifest_content, source=source)
        agent_id = registry.register_agent(
            name=payload["name"],
            description=payload["description"],
            endpoint_url=payload["endpoint_url"],
            price_per_call_usd=payload["price_per_call_usd"],
            tags=payload["tags"],
            input_schema=payload["input_schema"],
            owner_id=caller["owner_id"],
        )
    except onboarding.ManifestValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    agent = registry.get_agent_with_reputation(agent_id) or registry.get_agent(agent_id)
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "source": source,
            "registration_payload": payload,
            "agent": _agent_response(agent, caller),
            "message": "Manifest validated and agent registered.",
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# Auth routes  (public — no key required)
# ---------------------------------------------------------------------------

@app.post(
    "/auth/register",
    status_code=201,
    response_model=core_models.AuthRegisterResponse,
    responses=_error_responses(400, 429, 500),
)
@limiter.limit("10/minute")
def auth_register(request: Request, body: UserRegisterRequest) -> core_models.AuthRegisterResponse:
    """Create a new user account. Returns the initial API key (shown once)."""
    try:
        result = _auth.register_user(body.username, body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(content=result, status_code=201)


@app.post(
    "/auth/login",
    response_model=core_models.AuthLoginResponse,
    responses=_error_responses(401, 429, 500),
)
@limiter.limit("20/minute")
def auth_login(request: Request, body: UserLoginRequest) -> core_models.AuthLoginResponse:
    """Verify credentials. Returns a fresh API key valid for this session."""
    result = _auth.login_user(body.email, body.password)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return JSONResponse(content=result)


@app.get(
    "/auth/me",
    response_model=core_models.AuthMeResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def auth_me(request: Request, caller: core_models.CallerContext = Depends(_require_api_key)) -> core_models.AuthMeResponse:
    """Return the authenticated user's profile."""
    if caller["type"] == "master":
        return JSONResponse(content={
            "type": "master",
            "user_id": None,
            "username": "admin",
            "scopes": ["caller", "worker", "admin"],
        })
    user = caller["user"]
    return JSONResponse(content={
        "user_id": user["user_id"],
        "username": user["username"],
        "email": user["email"],
        "scopes": caller.get("scopes") or [],
    })


@app.get(
    "/auth/keys",
    response_model=core_models.ApiKeyListResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def auth_list_keys(request: Request, caller: core_models.CallerContext = Depends(_require_api_key)) -> core_models.ApiKeyListResponse:
    """List the caller's API keys (metadata only — raw keys never returned after creation)."""
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    keys = _auth.list_api_keys(caller["user"]["user_id"])
    return JSONResponse(content={"keys": keys})


@app.post(
    "/auth/keys",
    status_code=201,
    response_model=core_models.ApiKeyCreateResponse,
    responses=_error_responses(400, 401, 403, 429, 500),
)
@limiter.limit("10/minute")
def auth_create_key(
    request: Request,
    body: CreateKeyRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.ApiKeyCreateResponse:
    """Create a new named API key for the authenticated user."""
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    try:
        result = _auth.create_api_key(caller["user"]["user_id"], body.name, scopes=body.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content=result, status_code=201)


@app.post(
    "/auth/keys/{key_id}/rotate",
    status_code=201,
    response_model=core_models.ApiKeyRotateResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("10/minute")
def auth_rotate_key(
    request: Request,
    key_id: str,
    body: RotateKeyRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.ApiKeyRotateResponse:
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    try:
        result = _auth.rotate_api_key(
            key_id=key_id,
            user_id=caller["user"]["user_id"],
            name=body.name,
            scopes=body.scopes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="Key not found or already revoked.")
    return JSONResponse(content=result, status_code=201)


@app.delete(
    "/auth/keys/{key_id}",
    status_code=200,
    response_model=core_models.ApiKeyRevokeResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("10/minute")
def auth_revoke_key(
    request: Request,
    key_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.ApiKeyRevokeResponse:
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

@app.post(
    "/agents/financial",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500, 503),
)
@limiter.limit("10/minute")
def agent_financial(
    request: Request,
    body: FinancialRequest,
    _: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
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
@app.post(
    "/analyze",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500, 503),
)
@limiter.limit("10/minute")
def analyze_alias(
    request: Request,
    body: FinancialRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    return agent_financial(request, body, caller)


@app.post(
    "/agents/code-review",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 429, 500, 503),
)
@limiter.limit("10/minute")
def agent_code_review(
    request: Request,
    body: CodeReviewRequest,
    _: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    try:
        result = agent_codereview.run(body.code, body.language, body.focus)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _groq.RateLimitError as e:
        raise HTTPException(status_code=503, detail=f"All LLM models rate-limited. ({e})")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content=result)


@app.post(
    "/agents/text-intel",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 429, 500, 503),
)
@limiter.limit("15/minute")
def agent_text_intel(
    request: Request,
    body: TextIntelRequest,
    _: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    try:
        result = agent_textintel.run(body.text, body.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _groq.RateLimitError as e:
        raise HTTPException(status_code=503, detail=f"All LLM models rate-limited. ({e})")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content=result)


@app.post(
    "/agents/wiki",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 429, 500, 503),
)
@limiter.limit("15/minute")
def agent_wiki_endpoint(
    request: Request,
    body: WikiRequest,
    _: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
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

@app.post(
    "/registry/register",
    status_code=201,
    response_model=core_models.RegistryRegisterResponse,
    responses=_error_responses(400, 401, 403, 409, 429, 500),
)
@limiter.limit("20/minute")
def registry_register(
    request: Request,
    body: AgentRegisterRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistryRegisterResponse:
    _require_scope(caller, "worker")
    try:
        agent_id = registry.register_agent(
            name=body.name,
            description=body.description,
            endpoint_url=body.endpoint_url,
            price_per_call_usd=body.price_per_call_usd,
            tags=body.tags,
            input_schema=body.input_schema,
            owner_id=caller["owner_id"],
        )
        agent = registry.get_agent_with_reputation(agent_id) or registry.get_agent(agent_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "message": "Agent registered successfully.",
            "agent": _agent_response(agent, caller) if agent else None,
        },
        status_code=201,
    )


@app.get(
    "/registry/agents",
    response_model=core_models.RegistryAgentsResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def registry_list(
    request: Request,
    tag: str | None = None,
    rank_by: str | None = None,
    include_reputation: bool = True,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistryAgentsResponse:
    agents = registry.get_agents_with_reputation(tag=tag) if include_reputation else registry.get_agents(tag=tag)
    agents = _sorted_agents(agents, rank_by=rank_by)
    return JSONResponse(content={"agents": [_agent_response(a, caller) for a in agents], "count": len(agents)})


@app.post(
    "/registry/search",
    response_model=core_models.RegistrySearchResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def registry_search(
    request: Request,
    body: RegistrySearchRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistrySearchResponse:
    """
    Recommended discovery endpoint.
    Performs semantic natural-language matching with trust, pricing, and input-schema compatibility checks.
    The legacy GET /registry/agents?tag=... route remains supported for backward compatibility.
    """
    try:
        caller_trust = None
        if body.respect_caller_trust_min and caller["type"] != "master":
            caller_trust = _caller_trust_score(caller["owner_id"])
        ranked = registry.search_agents(
            query=body.query,
            limit=body.limit,
            min_trust=body.min_trust,
            max_price_cents=body.max_price_cents,
            required_input_fields=body.required_input_fields,
            caller_trust=caller_trust,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    results = [
        {
            "agent": _agent_response(item["agent"], caller),
            "similarity": item["similarity"],
            "trust": item["trust"],
            "blended_score": item["blended_score"],
            "match_reasons": item["match_reasons"],
        }
        for item in ranked
    ]
    return JSONResponse(content={"results": results, "count": len(results)})


@app.get(
    "/registry/agents/{agent_id}",
    response_model=core_models.AgentResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def registry_get(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentResponse:
    agent = registry.get_agent_with_reputation(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return JSONResponse(content=_agent_response(agent, caller))


@app.post(
    "/registry/agents/{agent_id}/call",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 429, 500, 502, 503),
)
@limiter.limit("10/minute")
def registry_call(
    request: Request,
    agent_id: str,
    body: core_models.RegistryCallRequest | None = Body(default=None),
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> Response:
    """
    Proxy a call to the registered agent with full payment lifecycle:
      1. Deduct price (402 if broke).
      2. HTTP POST to agent endpoint.
      3a. Success → payout 90% agent / 10% platform.
      3b. Failure → full refund to caller.
    """
    _require_scope(caller, "caller")
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    price_cents     = _usd_to_cents(agent["price_per_call_usd"])
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
    try:
        resp = http.post(
            agent["endpoint_url"],
            json=(body.root if body is not None else {}),
            headers=_proxy_headers_for_agent(agent),
            timeout=120,
        )
    except http.RequestException as e:
        latency_ms = (time.monotonic() - start) * 1000
        registry.update_call_stats(agent_id, latency_ms, False)
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, price_cents, agent_id
        )
        raise HTTPException(status_code=502, detail=f"Upstream agent unreachable: {e}")

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

    return _proxy_response(resp)


# ---------------------------------------------------------------------------
# Jobs routes
# ---------------------------------------------------------------------------

@app.post(
    "/jobs",
    status_code=201,
    response_model=core_models.JobResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def jobs_create(
    request: Request,
    body: JobCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "caller")
    agent = registry.get_agent(body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{body.agent_id}' not found.")

    caller_owner_id = _caller_owner_id(request)
    min_caller_trust = _extract_caller_trust_min(agent.get("input_schema"))
    if min_caller_trust is not None and caller["type"] != "master":
        caller_trust = _caller_trust_score(caller_owner_id)
        if caller_trust < min_caller_trust:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "CALLER_TRUST_BELOW_MINIMUM",
                    "caller_trust": round(caller_trust, 6),
                    "required_min_caller_trust": round(min_caller_trust, 6),
                    "agent_id": agent["agent_id"],
                },
            )

    price_cents = _usd_to_cents(agent["price_per_call_usd"])
    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    try:
        charge_tx_id = payments.pre_call_charge(
            caller_wallet["wallet_id"], price_cents, agent["agent_id"]
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

    try:
        job = jobs.create_job(
            agent_id=agent["agent_id"],
            caller_owner_id=caller_owner_id,
            caller_wallet_id=caller_wallet["wallet_id"],
            agent_wallet_id=agent_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            price_cents=price_cents,
            charge_tx_id=charge_tx_id,
            input_payload=body.input_payload,
            agent_owner_id=agent.get("owner_id"),
            max_attempts=body.max_attempts,
        )
    except Exception as e:
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, price_cents, agent["agent_id"]
        )
        raise HTTPException(status_code=500, detail=f"Failed to create job: {e}")

    _record_job_event(
        job,
        "job.created",
        actor_owner_id=caller["owner_id"],
        payload={"max_attempts": body.max_attempts},
    )
    return JSONResponse(content=_job_response(job, caller), status_code=201)


@app.get(
    "/jobs",
    response_model=core_models.JobsListResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def jobs_list(
    request: Request,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobsListResponse:
    if status and status not in jobs.VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
    page_size = min(max(1, limit), 200)
    before_created_at, before_job_id = _decode_jobs_cursor(cursor)
    owner_id = _caller_owner_id(request)
    items = jobs.list_jobs_for_owner(
        owner_id,
        limit=page_size + 1,
        status=status,
        before_created_at=before_created_at,
        before_job_id=before_job_id,
    )
    next_cursor = None
    if len(items) > page_size:
        page_items = items[:page_size]
        last = page_items[-1]
        next_cursor = _encode_jobs_cursor(last["created_at"], last["job_id"])
    else:
        page_items = items
    return JSONResponse(
        content={
            "jobs": [_job_response(j, caller) for j in page_items],
            "next_cursor": next_cursor,
        }
    )


@app.get(
    "/jobs/{job_id}",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_get(
    request: Request,
    job_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view this job.")
    response = _job_response(job, caller)
    response["latest_message_id"] = jobs.get_latest_message_id(job_id)
    return JSONResponse(content=response)


@app.get(
    "/jobs/agent/{agent_id}",
    response_model=core_models.JobsListResponse,
    responses=_error_responses(401, 403, 404, 422, 429, 500),
)
@limiter.limit("60/minute")
def jobs_list_for_agent(
    request: Request,
    agent_id: str,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobsListResponse:
    _require_scope(caller, "worker")
    agent = registry.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    if status and status not in jobs.VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
    page_size = min(max(1, limit), 200)
    before_created_at, before_job_id = _decode_jobs_cursor(cursor)
    items = jobs.list_jobs_for_agent(
        agent_id,
        limit=page_size + 1,
        status=status,
        before_created_at=before_created_at,
        before_job_id=before_job_id,
    )
    next_cursor = None
    if len(items) > page_size:
        page_items = items[:page_size]
        last = page_items[-1]
        next_cursor = _encode_jobs_cursor(last["created_at"], last["job_id"])
    else:
        page_items = items
    return JSONResponse(
        content={
            "jobs": [_job_response(j, caller) for j in page_items],
            "next_cursor": next_cursor,
        }
    )


@app.post(
    "/jobs/{job_id}/claim",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 429, 500),
)
@limiter.limit("60/minute")
def jobs_claim(
    request: Request,
    job_id: str,
    body: JobClaimRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    worker_owner_id = caller["owner_id"]
    require_auth = caller["type"] != "master"
    claimed = jobs.claim_job(
        job_id,
        claim_owner_id=worker_owner_id,
        lease_seconds=body.lease_seconds,
        require_authorized_owner=require_auth,
    )
    if claimed is None:
        raise HTTPException(status_code=409, detail="Job is not claimable.")

    _record_job_event(
        claimed,
        "job.claimed",
        actor_owner_id=worker_owner_id,
        payload={
            "lease_seconds": body.lease_seconds,
            "attempt_count": claimed["attempt_count"],
        },
    )
    return JSONResponse(content=_job_response(claimed, caller))


@app.post(
    "/jobs/{job_id}/heartbeat",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 410, 429, 500),
)
@limiter.limit("120/minute")
def jobs_heartbeat(
    request: Request,
    job_id: str,
    body: JobHeartbeatRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    worker_owner_id = caller["owner_id"]
    timed_out = _timeout_stale_lease_at_touchpoint(
        job,
        actor_owner_id=worker_owner_id,
        touchpoint="heartbeat",
    )
    if timed_out is not None:
        return JSONResponse(content=_job_response(timed_out, caller), status_code=410)

    if caller["type"] != "master":
        _assert_worker_claim(job, worker_owner_id, body.claim_token)

    heartbeat = jobs.heartbeat_job_lease(
        job_id,
        claim_owner_id=worker_owner_id,
        lease_seconds=body.lease_seconds,
        claim_token=body.claim_token,
        require_authorized_owner=(caller["type"] != "master"),
    )
    if heartbeat is None:
        raise HTTPException(status_code=409, detail="Unable to heartbeat this job claim.")

    _record_job_event(
        heartbeat,
        "job.heartbeat",
        actor_owner_id=worker_owner_id,
        payload={"lease_seconds": body.lease_seconds},
    )
    return JSONResponse(content=_job_response(heartbeat, caller))


@app.post(
    "/jobs/{job_id}/release",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 410, 429, 500),
)
@limiter.limit("60/minute")
def jobs_release(
    request: Request,
    job_id: str,
    body: JobReleaseRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    worker_owner_id = caller["owner_id"]
    timed_out = _timeout_stale_lease_at_touchpoint(
        job,
        actor_owner_id=worker_owner_id,
        touchpoint="release",
    )
    if timed_out is not None:
        return JSONResponse(content=_job_response(timed_out, caller), status_code=410)

    if caller["type"] != "master":
        _assert_worker_claim(job, worker_owner_id, body.claim_token)

    released = jobs.release_job_claim(
        job_id,
        claim_owner_id=worker_owner_id,
        claim_token=body.claim_token,
        require_authorized_owner=(caller["type"] != "master"),
    )
    if released is None:
        raise HTTPException(status_code=409, detail="Unable to release this job claim.")

    _record_job_event(
        released,
        "job.released",
        actor_owner_id=worker_owner_id,
        payload={},
    )
    return JSONResponse(content=_job_response(released, caller))


@app.post(
    "/jobs/{job_id}/complete",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 410, 429, 500),
)
@limiter.limit("30/minute")
def jobs_complete(
    request: Request,
    job_id: str,
    body: JobCompleteRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")
    def _operation() -> tuple[dict, int]:
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

        actor_owner_id = caller["owner_id"]
        if caller["type"] != "master" and not jobs.is_worker_authorized(job, actor_owner_id):
            raise HTTPException(status_code=403, detail="Not authorized for this agent job.")
        timed_out = _timeout_stale_lease_at_touchpoint(
            job,
            actor_owner_id=actor_owner_id,
            touchpoint="complete",
        )
        if timed_out is not None:
            return _job_response(timed_out, caller), 410

        if job["settled_at"]:
            return _job_response(job, caller), 200
        if job["status"] == "complete" and job.get("completed_at"):
            settled = _settle_successful_job(job, actor_owner_id=actor_owner_id)
            return _job_response(settled, caller), 200

        _assert_settlement_claim_or_grace(
            job,
            caller=caller,
            claim_token=body.claim_token,
            action="complete",
        )

        updated = jobs.update_job_status(
            job_id, "complete", output_payload=body.output_payload, completed=True
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="Unable to update job status.")
        settled = _settle_successful_job(updated, actor_owner_id=actor_owner_id)
        return _job_response(settled, caller), 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.complete:{job_id}",
        payload={"output_payload": body.output_payload, "claim_token": body.claim_token},
        operation=_operation,
    )


@app.post(
    "/jobs/{job_id}/fail",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 410, 429, 500),
)
@limiter.limit("30/minute")
def jobs_fail(
    request: Request,
    job_id: str,
    body: JobFailRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")
    def _operation() -> tuple[dict, int]:
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

        actor_owner_id = caller["owner_id"]
        if caller["type"] != "master" and not jobs.is_worker_authorized(job, actor_owner_id):
            raise HTTPException(status_code=403, detail="Not authorized for this agent job.")
        timed_out = _timeout_stale_lease_at_touchpoint(
            job,
            actor_owner_id=actor_owner_id,
            touchpoint="fail",
        )
        if timed_out is not None:
            return _job_response(timed_out, caller), 410

        if job["settled_at"]:
            return _job_response(job, caller), 200
        if job["status"] == "failed" and job.get("error_message") == body.error_message:
            settled = _settle_failed_job(
                job,
                actor_owner_id=actor_owner_id,
                event_type="job.failed",
            )
            return _job_response(settled, caller), 200

        _assert_settlement_claim_or_grace(
            job,
            caller=caller,
            claim_token=body.claim_token,
            action="fail",
        )

        updated = jobs.update_job_status(
            job_id, "failed", error_message=body.error_message, completed=True
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="Unable to update job status.")
        settled = _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.failed")
        return _job_response(settled, caller), 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.fail:{job_id}",
        payload={"error_message": body.error_message, "claim_token": body.claim_token},
        operation=_operation,
    )


@app.post(
    "/jobs/{job_id}/retry",
    response_model=core_models.JobResponse,
    responses=_error_responses(401, 403, 404, 409, 422, 429, 500),
)
@limiter.limit("30/minute")
def jobs_retry(
    request: Request,
    job_id: str,
    body: JobRetryRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "worker")
    def _operation() -> tuple[dict, int]:
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

        actor_owner_id = caller["owner_id"]
        require_auth = caller["type"] != "master"
        claim_owner_id = actor_owner_id if require_auth else (job.get("claim_owner_id") or actor_owner_id)
        if require_auth:
            _assert_worker_claim(job, actor_owner_id, body.claim_token)

        try:
            updated = jobs.schedule_job_retry(
                job_id,
                retry_delay_seconds=body.retry_delay_seconds,
                error_message=body.error_message,
                claim_owner_id=claim_owner_id,
                claim_token=body.claim_token,
                require_authorized_owner=require_auth,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if updated is None:
            raise HTTPException(status_code=409, detail="Unable to schedule retry for this job.")

        if updated["status"] == "failed":
            settled = _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.retry_exhausted")
            return _job_response(settled, caller), 200

        _record_job_event(
            updated,
            "job.retry_scheduled",
            actor_owner_id=actor_owner_id,
            payload={
                "retry_delay_seconds": body.retry_delay_seconds,
                "retry_count": updated["retry_count"],
                "next_retry_at": updated["next_retry_at"],
            },
        )
        return _job_response(updated, caller), 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.retry:{job_id}",
        payload=body.model_dump(),
        operation=_operation,
    )


@app.post(
    "/jobs/{job_id}/messages",
    response_model=core_models.JobMessageResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_message_create(
    request: Request,
    job_id: str,
    body: JobMessageRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobMessageResponse:
    """
    Post a message to a job thread.

    Deprecated: the legacy free-form contract (`question`, `partial_result`,
    `clarification`, `clarification_needed`, `final_result`, `note`) remains
    accepted for one compatibility window. New integrations should use the
    typed protocol message shapes.
    """
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to post to this job.")

    raw_type = body.type
    raw_payload = body.payload
    raw_correlation_id = body.correlation_id
    raw_from_id = body.from_id
    from_id_override = None
    if raw_from_id is not None:
        from_id_override = str(raw_from_id).strip() or None

    try:
        parsed = _normalize_job_message_protocol(
            raw_type,
            raw_payload,
            correlation_id=raw_correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    msg_type = parsed["type"]
    payload = parsed["payload"]

    if caller["type"] == "master":
        from_id = from_id_override or f"agent:{job['agent_id']}"
    elif caller["owner_id"] == job["caller_owner_id"]:
        from_id = from_id_override or job["caller_owner_id"]
    else:
        from_id = from_id_override or caller["owner_id"]

    if msg_type == "tool_call":
        correlation_id = str(payload.get("correlation_id") or "").strip()
        if not correlation_id:
            payload["correlation_id"] = str(uuid.uuid4())
    elif msg_type == "tool_result":
        correlation_id = str(payload.get("correlation_id") or "").strip()
        if not correlation_id:
            raise HTTPException(status_code=400, detail="tool_result payload.correlation_id is required.")
        if not _job_has_tool_call_correlation(job_id, correlation_id):
            raise HTTPException(
                status_code=400,
                detail=f"Unknown tool_result correlation_id '{correlation_id}'.",
            )

    msg = jobs.add_message(
        job_id,
        from_id,
        msg_type,
        payload,
        lease_seconds=_DEFAULT_LEASE_SECONDS,
    )
    updated_job = jobs.get_job(job_id) or job
    _record_job_event(
        updated_job,
        "job.message_added",
        actor_owner_id=caller["owner_id"],
        payload={"type": msg_type, "message_id": msg["message_id"]},
    )

    return JSONResponse(content=msg, status_code=201)


@app.get(
    "/jobs/{job_id}/messages",
    response_model=core_models.JobMessagesResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_message_list(
    request: Request,
    job_id: str,
    since: int | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobMessagesResponse:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view messages.")
    items = jobs.get_messages(job_id, since_id=since)
    return JSONResponse(content={"messages": items})


@app.get(
    "/jobs/{job_id}/stream",
    response_model=str,
    responses={
        200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
        **_error_responses(401, 403, 404, 429, 500),
    },
)
@limiter.limit("60/minute")
def jobs_message_stream(
    request: Request,
    job_id: str,
    since: int | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> StreamingResponse:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view messages.")

    def _iter_events():
        subscriber = _subscribe_job_stream(job_id)
        last_seen = since
        try:
            yield ": heartbeat\n\n"
            while True:
                batch = jobs.get_messages(job_id, since_id=last_seen, limit=200)
                if batch:
                    for item in batch:
                        message_id = int(item["message_id"])
                        if last_seen is not None and message_id <= last_seen:
                            continue
                        last_seen = message_id
                        yield _job_message_to_sse(item)
                    continue

                latest_job = jobs.get_job(job_id)
                if latest_job is None or latest_job.get("status") in _JOB_TERMINAL_STATUSES:
                    break

                try:
                    queued = subscriber.get(timeout=_JOB_STREAM_HEARTBEAT_SECONDS)
                except Empty:
                    yield ": heartbeat\n\n"
                    latest_job = jobs.get_job(job_id)
                    if latest_job is None or latest_job.get("status") in _JOB_TERMINAL_STATUSES:
                        break
                    continue

                queued_id = int(queued.get("message_id") or 0)
                if last_seen is not None and queued_id <= last_seen:
                    continue
                last_seen = queued_id
                yield _job_message_to_sse(queued)
        finally:
            _unsubscribe_job_stream(job_id, subscriber)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(_iter_events(), media_type="text/event-stream", headers=headers)


# ---------------------------------------------------------------------------
# Reputation + operations routes
# ---------------------------------------------------------------------------

@app.post(
    "/jobs/{job_id}/rating",
    status_code=201,
    response_model=core_models.JobRatingResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def jobs_rate(
    request: Request,
    job_id: str,
    body: JobRatingRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobRatingResponse:
    _require_scope(caller, "caller")
    def _operation() -> tuple[dict, int]:
        if caller["type"] == "master":
            raise HTTPException(status_code=403, detail="Master key cannot submit quality ratings.")

        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        if job["caller_owner_id"] != caller["owner_id"]:
            raise HTTPException(status_code=403, detail="Only the original caller can rate this job.")
        if disputes.has_dispute_for_job(job_id):
            raise HTTPException(status_code=409, detail="Ratings are locked once a dispute is filed.")

        try:
            rating = reputation.record_job_quality_rating(job_id, caller["owner_id"], body.rating)
        except ValueError as exc:
            message = str(exc)
            if "already has a quality rating" in message:
                raise HTTPException(status_code=409, detail=message)
            raise HTTPException(status_code=400, detail=message)

        metrics = reputation.compute_trust_metrics(job["agent_id"])
        _record_job_event(
            job,
            "job.rated",
            actor_owner_id=caller["owner_id"],
            payload={"rating": body.rating},
        )
        return {"rating": rating, "agent_reputation": metrics}, 201

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.rating:{job_id}",
        payload={"rating": body.rating},
        operation=_operation,
    )


@app.post(
    "/jobs/{job_id}/rate-caller",
    status_code=201,
    response_model=core_models.JobCallerRatingResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def jobs_rate_caller(
    request: Request,
    job_id: str,
    body: JobRateCallerRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobCallerRatingResponse:
    _require_scope(caller, "worker")
    if caller["type"] == "master":
        raise HTTPException(status_code=403, detail="Master key cannot submit caller ratings.")

    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not jobs.is_worker_authorized(job, caller["owner_id"]):
        raise HTTPException(status_code=403, detail="Only the job's agent owner can rate the caller.")

    try:
        rating = reputation.record_caller_rating(
            job_id=job_id,
            agent_owner_id=caller["owner_id"],
            rating=body.rating,
            comment=body.comment,
        )
    except ValueError as exc:
        message = str(exc)
        if "already has a caller rating" in message:
            raise HTTPException(status_code=409, detail=message)
        raise HTTPException(status_code=400, detail=message)

    caller_reputation = reputation.compute_caller_trust_metrics(job["caller_owner_id"])
    _record_job_event(
        job,
        "job.caller_rated",
        actor_owner_id=caller["owner_id"],
        payload={"rating": body.rating},
    )
    return JSONResponse(content={"rating": rating, "caller_reputation": caller_reputation}, status_code=201)


@app.post(
    "/jobs/{job_id}/dispute",
    status_code=201,
    response_model=core_models.DisputeResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("20/minute")
def jobs_dispute(
    request: Request,
    job_id: str,
    body: JobDisputeRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeResponse:
    if not (_caller_has_scope(caller, "caller") or _caller_has_scope(caller, "worker")):
        raise HTTPException(status_code=403, detail="This endpoint requires caller or worker scope.")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if job.get("status") != "complete" or not job.get("completed_at"):
        raise HTTPException(status_code=400, detail="Disputes can only be filed for completed jobs.")

    completed_at = _parse_iso_datetime(job.get("completed_at"))
    if completed_at is None:
        raise HTTPException(status_code=400, detail="Job completion timestamp is invalid.")
    if (datetime.now(timezone.utc) - completed_at) > timedelta(seconds=_DISPUTE_FILE_WINDOW_SECONDS):
        raise HTTPException(status_code=400, detail="Dispute window has expired for this job.")

    side = _dispute_side_for_caller(caller, job)
    if reputation.get_job_quality_rating(job_id) is not None:
        raise HTTPException(status_code=409, detail="Disputes must be filed before the caller submits a rating.")
    if disputes.has_dispute_for_job(job_id):
        raise HTTPException(status_code=409, detail="A dispute already exists for this job.")

    try:
        created = disputes.create_dispute(
            job_id=job_id,
            filed_by_owner_id=caller["owner_id"],
            side=side,
            reason=body.reason,
            evidence=body.evidence,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="A dispute already exists for this job.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        lock_summary = payments.lock_dispute_funds(created["dispute_id"])
    except payments.InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "DISPUTE_CLAWBACK_INSUFFICIENT_BALANCE",
                "balance_cents": exc.balance_cents,
                "required_cents": exc.required_cents,
            },
        )
    _record_job_event(
        job,
        "job.dispute_filed",
        actor_owner_id=caller["owner_id"],
        payload={"dispute_id": created["dispute_id"], "side": side, "lock": lock_summary},
    )
    return JSONResponse(content=_dispute_view(created), status_code=201)


@app.post(
    "/ops/disputes/{dispute_id}/judge",
    response_model=core_models.DisputeJudgeResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("30/minute")
def disputes_judge(
    request: Request,
    dispute_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeJudgeResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    if disputes.get_dispute(dispute_id) is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
    try:
        dispute_payload, settlement = _resolve_dispute_with_judges(dispute_id, actor_owner_id=caller["owner_id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(content={"dispute": dispute_payload, "settlement": settlement})


@app.post(
    "/admin/disputes/{dispute_id}/rule",
    response_model=core_models.DisputeJudgeResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def disputes_admin_rule(
    request: Request,
    dispute_id: str,
    body: AdminDisputeRuleRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeJudgeResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    dispute_row = disputes.get_dispute(dispute_id)
    if dispute_row is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")

    if dispute_row["status"] in {"resolved", "consensus"}:
        disputes.set_dispute_status(dispute_id, "appealed")

    admin_user_id = None
    if caller["type"] == "user":
        admin_user_id = caller["user"]["user_id"]

    try:
        disputes.record_judgment(
            dispute_id,
            judge_kind="human_admin",
            verdict=body.outcome,
            reasoning=body.reasoning,
            admin_user_id=admin_user_id,
        )
        settlement = payments.post_dispute_settlement(
            dispute_id,
            outcome=body.outcome,
            split_caller_cents=body.split_caller_cents,
            split_agent_cents=body.split_agent_cents,
        )
        finalized = disputes.finalize_dispute(
            dispute_id,
            status="final",
            outcome=body.outcome,
            split_caller_cents=body.split_caller_cents,
            split_agent_cents=body.split_agent_cents,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except payments.InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "DISPUTE_SETTLEMENT_INSUFFICIENT_BALANCE",
                "balance_cents": exc.balance_cents,
                "required_cents": exc.required_cents,
            },
        )

    if finalized is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")

    job = jobs.get_job(finalized["job_id"])
    if job is not None:
        _record_job_event(
            job,
            "job.dispute_finalized",
            actor_owner_id=caller["owner_id"],
            payload={"dispute_id": dispute_id, "outcome": body.outcome},
        )
    return JSONResponse(content={"dispute": _dispute_view(finalized), "settlement": settlement})


@app.get(
    "/ops/jobs/{job_id}/settlement-trace",
    response_model=core_models.JobSettlementTraceResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_settlement_trace(
    request: Request,
    job_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobSettlementTraceResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    txs = payments.get_settlement_transactions(job["charge_tx_id"])
    fee_cents = job["price_cents"] * payments.PLATFORM_FEE_PCT // 100
    return JSONResponse(
        content={
            "job_id": job["job_id"],
            "agent_id": job["agent_id"],
            "status": job["status"],
            "charge_tx_id": job["charge_tx_id"],
            "price_cents": job["price_cents"],
            "expected_agent_payout_cents": job["price_cents"] - fee_cents,
            "expected_platform_fee_cents": fee_cents,
            "settled_at": job["settled_at"],
            "transactions": txs,
        }
    )


@app.get(
    "/ops/jobs/events",
    response_model=core_models.JobEventsResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def jobs_events(
    request: Request,
    since: int | None = None,
    limit: int = 100,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobEventsResponse:
    return JSONResponse(content={"events": _list_job_events(caller, since=since, limit=limit)})


@app.post(
    "/ops/jobs/hooks",
    status_code=201,
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 409, 422, 429, 500),
)
@limiter.limit("20/minute")
def job_event_hook_create(
    request: Request,
    body: JobEventHookCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    try:
        hook = _create_job_event_hook(caller["owner_id"], body.target_url, body.secret)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return JSONResponse(content=hook, status_code=201)


@app.get(
    "/ops/jobs/hooks",
    response_model=core_models.JobEventHookListResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def job_event_hook_list(
    request: Request,
    include_inactive: bool = False,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobEventHookListResponse:
    owner_id = None if caller["type"] == "master" else caller["owner_id"]
    hooks = _list_job_event_hooks(owner_id=owner_id, include_inactive=include_inactive)
    return JSONResponse(content={"hooks": hooks})


@app.delete(
    "/ops/jobs/hooks/{hook_id}",
    response_model=core_models.JobEventHookDeleteResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def job_event_hook_delete(
    request: Request,
    hook_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobEventHookDeleteResponse:
    owner_id = None if caller["type"] == "master" else caller["owner_id"]
    ok = _deactivate_job_event_hook(hook_id, owner_id=owner_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Hook not found.")
    return JSONResponse(content={"deleted": True, "hook_id": hook_id})


@app.post(
    "/ops/jobs/hooks/process",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def job_event_hook_process(
    request: Request,
    body: HookDeliveryProcessRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    summary = _process_due_hook_deliveries(limit=body.limit)
    return JSONResponse(content=summary)


@app.get(
    "/ops/jobs/hooks/dead-letter",
    response_model=core_models.JobEventHookDeadLetterResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def job_event_hook_dead_letter(
    request: Request,
    limit: int = 100,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobEventHookDeadLetterResponse:
    owner_id = None if _caller_has_scope(caller, "admin") else caller["owner_id"]
    deliveries = _list_hook_deliveries(owner_id=owner_id, status="dead_letter", limit=limit)
    return JSONResponse(content={"deliveries": deliveries, "count": len(deliveries)})


@app.post(
    "/ops/jobs/sweep",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("20/minute")
def jobs_sweep(
    request: Request,
    body: JobsSweepRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    try:
        summary = _sweep_jobs(
            retry_delay_seconds=body.retry_delay_seconds,
            sla_seconds=body.sla_seconds,
            limit=body.limit,
            actor_owner_id=caller["owner_id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return JSONResponse(content=summary)


@app.get(
    "/ops/jobs/metrics",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def jobs_metrics(
    request: Request,
    sla_seconds: int = _DEFAULT_SLA_SECONDS,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    if sla_seconds <= 0:
        raise HTTPException(status_code=422, detail="sla_seconds must be > 0.")
    return JSONResponse(content=_jobs_metrics(sla_seconds=sla_seconds))


@app.get(
    "/ops/jobs/slo",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def jobs_slo(
    request: Request,
    sla_seconds: int = _DEFAULT_SLA_SECONDS,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    if sla_seconds <= 0:
        raise HTTPException(status_code=422, detail="sla_seconds must be > 0.")
    metrics = _jobs_metrics(sla_seconds=sla_seconds)
    return JSONResponse(content={"slo": metrics["slo"], "alerts": metrics["alerts"]})


# ---------------------------------------------------------------------------
# Payments ops routes
# ---------------------------------------------------------------------------

@app.get(
    "/ops/payments/reconcile",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def payments_reconcile_preview(
    request: Request,
    max_mismatches: int = 100,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    if max_mismatches <= 0:
        raise HTTPException(status_code=422, detail="max_mismatches must be > 0.")
    summary = payments.compute_ledger_invariants(max_mismatches=max_mismatches)
    return JSONResponse(content=summary)


@app.post(
    "/ops/payments/reconcile",
    status_code=201,
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def payments_reconcile_run(
    request: Request,
    body: ReconciliationRunRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    summary = payments.record_reconciliation_run(max_mismatches=body.max_mismatches)
    return JSONResponse(content=summary, status_code=201)


@app.get(
    "/ops/payments/reconcile/runs",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 422, 429, 500),
)
@limiter.limit("60/minute")
def payments_reconcile_runs(
    request: Request,
    limit: int = 20,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    if limit <= 0:
        raise HTTPException(status_code=422, detail="limit must be > 0.")
    runs = payments.list_reconciliation_runs(limit=limit)
    return JSONResponse(content={"runs": runs, "count": len(runs)})


# ---------------------------------------------------------------------------
# Wallet routes
# ---------------------------------------------------------------------------

@app.post(
    "/wallets/deposit",
    response_model=core_models.WalletDepositResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def wallet_deposit(
    request: Request,
    body: DepositRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletDepositResponse:
    _require_scope(caller, "caller")
    wallet = payments.get_wallet(body.wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail=f"Wallet '{body.wallet_id}' not found.")
    if caller["type"] != "master" and wallet["owner_id"] != caller["owner_id"]:
        raise HTTPException(status_code=403, detail="Not authorized to deposit into this wallet.")
    try:
        tx_id = payments.deposit(body.wallet_id, body.amount_cents, body.memo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    wallet = payments.get_wallet(body.wallet_id)
    return JSONResponse(content={
        "tx_id": tx_id, "wallet_id": body.wallet_id,
        "balance_cents": wallet["balance_cents"],
    })


@app.get(
    "/wallets/me",
    response_model=core_models.WalletResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def wallet_me(
    request: Request,
    _: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletResponse:
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    txs = payments.get_wallet_transactions(wallet["wallet_id"], limit=50)
    caller_trust = _caller_trust_score(owner_id)
    return JSONResponse(content={**wallet, "caller_trust": caller_trust, "transactions": txs})


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
# Run history
# ---------------------------------------------------------------------------

@app.get(
    "/runs",
    response_model=core_models.RunsResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def get_runs(
    request: Request,
    limit: int = 50,
    _: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RunsResponse:
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
