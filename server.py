"""
server.py — FastAPI HTTP server for the agentmarket platform

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import json
import os
import asyncio
import base64
import math
import hmac
import hashlib
import logging
import re
import ipaddress
import collections
import sqlite3
import threading
import time
import uuid
from contextvars import Token
from queue import Empty, Queue
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable
from urllib.parse import urlparse
try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl unavailable on some platforms
    fcntl = None

import requests as http
from dotenv import load_dotenv

load_dotenv()

_SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            integrations=[StarletteIntegration(), FastApiIntegration()],
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            environment=os.environ.get("ENVIRONMENT", "production"),
            send_default_pii=False,
        )
    except Exception as _sentry_exc:
        logging.warning("Sentry init failed: %s", _sentry_exc)

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import groq as _groq

from agents import codereview as agent_codereview
from agents import cve_lookup as agent_cve_lookup
from agents import datainsights as agent_datainsights
from agents import dependency_scanner as agent_dependency_scanner
from agents import healthcare_expert as agent_healthcare_expert
from agents import image_generator as agent_image_generator
from agents import incident_response as agent_incident_response
from agents import negotiation as agent_negotiation
from agents import portfolio as agent_portfolio
from agents import product as agent_product
from agents import scenario as agent_scenario
from agents import secrets_detection as agent_secrets_detection
from agents import sqlbuilder as agent_sqlbuilder
from agents import static_analysis as agent_static_analysis
from agents import system_design as agent_system_design
from agents import textintel as agent_textintel
from agents import video_storyboard as agent_video_storyboard
from agents import wiki as agent_wiki
from agents import arxiv_research as agent_arxiv_research
from agents import python_executor as agent_python_executor
from agents import web_researcher as agent_web_researcher
from core import auth as _auth
from core import embeddings
from core import onboarding
from core import payments
from core.db import close_all_connections as _close_all_db_connections
from core import registry
from core import jobs
from core import disputes
from core import judges
from core import models as core_models
from core import reputation
from core import error_codes
from core.migrate import apply_migrations
from core import logging_utils
from core import email as _email
from core import url_security as _url_security
from scripts.financial_cli import run as _run_financial
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
    JobVerificationDecisionRequest,
    JobMessageRequest,
    JobRateCallerRequest,
    JobRatingRequest,
    JobReleaseRequest,
    JobRetryRequest,
    JobsSweepRequest,
    MCPInvokeRequest,
    NegotiationRequest,
    PortfolioRequest,
    ProductStrategyRequest,
    ScenarioRequest,
    AdminDisputeRuleRequest,
    OnboardingValidateRequest,
    ReconciliationRunRequest,
    RegistrySearchRequest,
    RotateKeyRequest,
    AgentKeyCreateRequest,
    AgentReviewDecisionRequest,
    AuthLegalAcceptRequest,
    TextIntelRequest,
    UserLoginRequest,
    UserRegisterRequest,
    WikiRequest,
)

_LOG_LEVEL_NAME = (os.environ.get("LOG_LEVEL", "INFO") or "INFO").strip().upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
if not isinstance(_LOG_LEVEL, int):
    _LOG_LEVEL = logging.INFO
logging_utils.configure_json_logging(_LOG_LEVEL)
_LOG = logging.getLogger(__name__)


class _SecretRedactFilter(logging.Filter):
    """Strip API key values and sensitive env-var patterns from log records."""
    _PATTERNS = re.compile(
        r'((?:am_|amk_|sk_live_|sk_test_|Bearer\s+)[A-Za-z0-9_\-]{8,})',
        re.IGNORECASE,
    )

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._PATTERNS.sub('[REDACTED]', str(record.msg))
        record.args = tuple(
            self._PATTERNS.sub('[REDACTED]', str(a)) if isinstance(a, str) else a
            for a in (record.args or ())
        )
        return True


logging.getLogger().addFilter(_SecretRedactFilter())
_BACKGROUND_WORKER_LOCK_PATH = os.environ.get("BACKGROUND_WORKER_LOCK_PATH", "").strip()
_background_worker_lock_handle: Any | None = None


def _background_worker_lock_path() -> str:
    configured = str(_BACKGROUND_WORKER_LOCK_PATH or "").strip()
    if configured:
        return configured
    db_ref = str(getattr(jobs, "DB_PATH", "") or "")
    digest = hashlib.sha256(db_ref.encode("utf-8")).hexdigest()[:16]
    return f"/tmp/aztea-background-worker-{digest}.lock"


def _acquire_background_worker_lock() -> bool:
    global _background_worker_lock_handle
    if _background_worker_lock_handle is not None:
        return True
    if fcntl is None:
        return True
    lock_path = _background_worker_lock_path()
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        _LOG.warning("Failed to open background worker lock file %s: %s", lock_path, exc)
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    except OSError as exc:
        _LOG.warning("Failed to acquire background worker lock %s: %s", lock_path, exc)
        handle.close()
        return True
    _background_worker_lock_handle = handle
    return True


def _release_background_worker_lock() -> None:
    global _background_worker_lock_handle
    handle = _background_worker_lock_handle
    if handle is None:
        return
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
    try:
        handle.close()
    except OSError:
        pass
    _background_worker_lock_handle = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_MASTER_KEY = os.environ.get("API_KEY")
if not _MASTER_KEY:
    raise RuntimeError("API_KEY is not set. Add it to your .env file.")

_SERVER_BASE_URL   = os.environ.get("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")
_FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", _SERVER_BASE_URL).rstrip("/")

# Stripe
_STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "").strip()
_STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
_STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()
try:
    import stripe as _stripe_lib
    _STRIPE_AVAILABLE = True
except ImportError:
    _stripe_lib = None
    _STRIPE_AVAILABLE = False

# Deterministic UUIDs for built-in agents
_FINANCIAL_AGENT_ID        = "b7741251-d7ac-5423-b57d-8e12cd80885f"
_CODEREVIEW_AGENT_ID       = "8cea848f-a165-5d6c-b1a0-7d14fff77d14"
_TEXTINTEL_AGENT_ID        = "3daebf56-1873-5e7c-ba4f-7e69c51aefac"
_WIKI_AGENT_ID             = "9a175aa2-8ffd-52f7-aae0-5a33fc88db83"
_NEGOTIATION_AGENT_ID      = "39b2867f-4910-5b5b-9492-bbb3f4ae4a06"
_SCENARIO_AGENT_ID         = "d2e672ae-2a2e-52f9-8e60-10a644ba49bb"
_PRODUCT_AGENT_ID          = "6dd1d7ff-d838-5d35-b633-da89602fea7e"
_PORTFOLIO_AGENT_ID        = "4fa63abf-fea3-513b-9203-bc09ff668a44"
_RESUME_AGENT_ID           = "17076c9b-ae5c-534d-9054-705fc9afc4b3"
_SQLBUILDER_AGENT_ID       = "b1de8c04-f82c-506c-9305-d67dfaea2e4f"
_DATAINSIGHTS_AGENT_ID     = "51214278-5a31-5de8-8514-5a2c07ccfa4d"
_EMAILWRITER_AGENT_ID      = "07891578-d49e-54b2-9297-db4a453f1fbb"
_SECRETS_AGENT_ID          = "b52b13ea-d7f7-5030-89b7-eed22dc0a9fa"
_STATICANALYSIS_AGENT_ID   = "e2b69985-1f53-5ae3-aba6-df38d5f024da"
_DEPSCANNER_AGENT_ID       = "9adba8e2-fa19-5160-9e67-143e0811ba91"
_CVELOOKUP_AGENT_ID        = "a3e239dd-ea92-556b-9c95-0a213a3daf59"
_QUALITY_JUDGE_AGENT_ID    = "9cf0d9d0-4a10-58c9-b97a-6b5f81b1cf33"
_SYSTEM_DESIGN_AGENT_ID    = "eda2e80c-78a1-5a94-ae2b-e450858a7efa"
_INCIDENT_RESPONSE_AGENT_ID = "5cceca4c-85f2-5b2d-bc06-3b352aaf0c33"
_HEALTHCARE_EXPERT_AGENT_ID = "40d9012b-f611-502f-a73b-ef631efed163"
_IMAGE_GENERATOR_AGENT_ID  = "4fb167bd-b474-5ea5-bd5c-8976dfe799ae"
_VIDEO_STORYBOARD_AGENT_ID = "c12994de-cde9-514a-9c07-a3833b25bb1f"
_ARXIV_RESEARCH_AGENT_ID   = "9e673f6e-9115-516f-b41b-5af8bcbf15bd"
_PYTHON_EXECUTOR_AGENT_ID  = "040dc3f5-afe7-5db7-b253-4936090cc7af"
_WEB_RESEARCHER_AGENT_ID   = "32cd7b5c-44d0-5259-bb02-1bbc612e92d7"

def _normalize_endpoint_ref(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


_BUILTIN_INTERNAL_ENDPOINTS = {
    _FINANCIAL_AGENT_ID: "internal://financial",
    _CODEREVIEW_AGENT_ID: "internal://code-review",
    _TEXTINTEL_AGENT_ID: "internal://text-intel",
    _WIKI_AGENT_ID: "internal://wiki",
    _NEGOTIATION_AGENT_ID: "internal://negotiation",
    _SCENARIO_AGENT_ID: "internal://scenario",
    _PRODUCT_AGENT_ID: "internal://product-strategy",
    _PORTFOLIO_AGENT_ID: "internal://portfolio",
    _QUALITY_JUDGE_AGENT_ID: "internal://quality-judge",
    _SQLBUILDER_AGENT_ID: "internal://sql-builder",
    _DATAINSIGHTS_AGENT_ID: "internal://data-insights",
    _SECRETS_AGENT_ID: "internal://secrets-detection",
    _STATICANALYSIS_AGENT_ID: "internal://static-analysis",
    _DEPSCANNER_AGENT_ID: "internal://dependency-scanner",
    _CVELOOKUP_AGENT_ID: "internal://cve-lookup",
    _SYSTEM_DESIGN_AGENT_ID: "internal://system-design-reviewer",
    _INCIDENT_RESPONSE_AGENT_ID: "internal://incident-response-commander",
    _HEALTHCARE_EXPERT_AGENT_ID: "internal://healthcare-expert",
    _IMAGE_GENERATOR_AGENT_ID: "internal://image-generator",
    _VIDEO_STORYBOARD_AGENT_ID: "internal://video-storyboard-generator",
    _ARXIV_RESEARCH_AGENT_ID:  "internal://arxiv-research",
    _PYTHON_EXECUTOR_AGENT_ID: "internal://python-executor",
    _WEB_RESEARCHER_AGENT_ID:  "internal://web-researcher",
}
_BUILTIN_LEGACY_ROUTE_ENDPOINTS = {
    _FINANCIAL_AGENT_ID: f"{_SERVER_BASE_URL}/agents/financial",
    _CODEREVIEW_AGENT_ID: f"{_SERVER_BASE_URL}/agents/code-review",
    _TEXTINTEL_AGENT_ID: f"{_SERVER_BASE_URL}/agents/text-intel",
    _WIKI_AGENT_ID: f"{_SERVER_BASE_URL}/agents/wiki",
    _NEGOTIATION_AGENT_ID: f"{_SERVER_BASE_URL}/agents/negotiation",
    _SCENARIO_AGENT_ID: f"{_SERVER_BASE_URL}/agents/scenario",
    _PRODUCT_AGENT_ID: f"{_SERVER_BASE_URL}/agents/product-strategy",
    _PORTFOLIO_AGENT_ID: f"{_SERVER_BASE_URL}/agents/portfolio",
    _QUALITY_JUDGE_AGENT_ID: f"{_SERVER_BASE_URL}/agents/quality-judge",
    _SQLBUILDER_AGENT_ID: f"{_SERVER_BASE_URL}/agents/sql-builder",
    _DATAINSIGHTS_AGENT_ID: f"{_SERVER_BASE_URL}/agents/data-insights",
    _SECRETS_AGENT_ID: f"{_SERVER_BASE_URL}/agents/secrets-detection",
    _STATICANALYSIS_AGENT_ID: f"{_SERVER_BASE_URL}/agents/static-analysis",
    _DEPSCANNER_AGENT_ID: f"{_SERVER_BASE_URL}/agents/dependency-scanner",
    _CVELOOKUP_AGENT_ID: f"{_SERVER_BASE_URL}/agents/cve-lookup",
    _SYSTEM_DESIGN_AGENT_ID: f"{_SERVER_BASE_URL}/agents/system-design-reviewer",
    _INCIDENT_RESPONSE_AGENT_ID: f"{_SERVER_BASE_URL}/agents/incident-response-commander",
    _HEALTHCARE_EXPERT_AGENT_ID: f"{_SERVER_BASE_URL}/agents/healthcare-expert",
    _IMAGE_GENERATOR_AGENT_ID: f"{_SERVER_BASE_URL}/agents/image-generator",
    _VIDEO_STORYBOARD_AGENT_ID: f"{_SERVER_BASE_URL}/agents/video-storyboard-generator",
    _ARXIV_RESEARCH_AGENT_ID:  f"{_SERVER_BASE_URL}/agents/arxiv-research",
    _PYTHON_EXECUTOR_AGENT_ID: f"{_SERVER_BASE_URL}/agents/python-executor",
    _WEB_RESEARCHER_AGENT_ID:  f"{_SERVER_BASE_URL}/agents/web-researcher",
}
_BUILTIN_ENDPOINT_TO_AGENT_ID: dict[str, str] = {}
for _agent_id, _endpoint in _BUILTIN_INTERNAL_ENDPOINTS.items():
    _BUILTIN_ENDPOINT_TO_AGENT_ID[_normalize_endpoint_ref(_endpoint)] = _agent_id
    _legacy = _BUILTIN_LEGACY_ROUTE_ENDPOINTS.get(_agent_id)
    if _legacy:
        _BUILTIN_ENDPOINT_TO_AGENT_ID[_normalize_endpoint_ref(_legacy)] = _agent_id
_BUILTIN_ENDPOINT_TO_AGENT_ID[_normalize_endpoint_ref(f"{_SERVER_BASE_URL}/analyze")] = _FINANCIAL_AGENT_ID
_BUILTIN_AGENT_IDS = frozenset(_BUILTIN_INTERNAL_ENDPOINTS.keys())
_CURATED_PUBLIC_BUILTIN_AGENT_IDS = frozenset(
    {
        # Real tool use — fetch live data, execute code, or call external APIs
        _FINANCIAL_AGENT_ID,       # SEC EDGAR API
        _WIKI_AGENT_ID,            # Wikipedia API
        _CVELOOKUP_AGENT_ID,       # NIST NVD API
        _ARXIV_RESEARCH_AGENT_ID,  # arXiv API
        _PYTHON_EXECUTOR_AGENT_ID,  # subprocess sandbox
        _WEB_RESEARCHER_AGENT_ID,   # HTTP fetch + parse
        _IMAGE_GENERATOR_AGENT_ID,  # OpenAI / Replicate API
        _CODEREVIEW_AGENT_ID,      # structured expert output, high quality prompt
    }
)
_CURATED_BUILTIN_AGENT_IDS = frozenset(set(_CURATED_PUBLIC_BUILTIN_AGENT_IDS) | {_QUALITY_JUDGE_AGENT_ID})
_BUILTIN_WORKER_OWNER_ID = "system:builtin-worker"
_SYSTEM_USERNAME = "system"
_SYSTEM_USER_EMAIL = "system@aztea.internal"

_CALLER_CACHE_MISSING = object()
_IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
_MAX_BODY_BYTES = 512 * 1024  # 512 KB
_DEFAULT_LEASE_SECONDS = 300
_DEFAULT_RETRY_DELAY_SECONDS = 30
_DEFAULT_SLA_SECONDS = 900
_DEFAULT_SWEEP_INTERVAL_SECONDS = 60
_DEFAULT_SWEEP_LIMIT = 100
_DEFAULT_HOOK_DELIVERY_INTERVAL_SECONDS = 2
_DEFAULT_HOOK_DELIVERY_BATCH_SIZE = 50
_DEFAULT_HOOK_DELIVERY_MAX_ATTEMPTS = 3
_DEFAULT_HOOK_DELIVERY_BASE_DELAY_SECONDS = 5
_DEFAULT_HOOK_DELIVERY_MAX_DELAY_SECONDS = 300
_DEFAULT_HOOK_DELIVERY_CLAIM_LEASE_SECONDS = 30
_DEFAULT_DISPUTE_FILE_WINDOW_SECONDS = 7 * 24 * 3600
_DEFAULT_DISPUTE_WINDOW_HOURS = 72
_DEFAULT_DISPUTE_FILING_DEPOSIT_BPS = 500
_DEFAULT_DISPUTE_FILING_DEPOSIT_MIN_CENTS = 5
_DEFAULT_DISPUTE_JUDGE_INTERVAL_SECONDS = 60  # auto-resolve pending disputes every 60s
_DEFAULT_BUILTIN_JOB_WORKER_INTERVAL_SECONDS = 2
_DEFAULT_BUILTIN_JOB_WORKER_BATCH_SIZE = 20
_DEFAULT_TOPUP_DAILY_LIMIT_CENTS = 100_000
_DEFAULT_PAYMENTS_RECONCILIATION_INTERVAL_SECONDS = 3600
_DEFAULT_PAYMENTS_RECONCILIATION_MAX_MISMATCHES = 100
_DEFAULT_ENDPOINT_MONITOR_BATCH_SIZE = 100
_DEFAULT_ENDPOINT_MONITOR_TIMEOUT_SECONDS = 3
_DEFAULT_ENDPOINT_MONITOR_FAILURE_THRESHOLD = 3
MINIMUM_DEPOSIT_CENTS = int(os.getenv("MINIMUM_DEPOSIT_CENTS", "500"))
_PROTOCOL_VERSION = "1.0"
_PROTOCOL_VERSION_HEADER = "X-Aztea-Version"
_LEGACY_PROTOCOL_VERSION_HEADER = "X-AgentMarket-Version"
# $0.001 cannot be represented in integer cents; keep ledger integer-safe until millicent support exists.
_DEFAULT_JUDGE_FEE_CENTS = 0
_REPUTATION_DECAY_GRACE_DAYS = 30
_REPUTATION_DECAY_DAILY_RATE = 0.005
AUTO_SUSPEND_FAILURE_RATE_THRESHOLD = 0.6
AUTO_SUSPEND_MIN_CALLS = 10
_DEFAULT_RATE_LIMIT = "60/minute"
_AUTH_RATE_LIMIT = "10/minute"
_SEARCH_RATE_LIMIT = "30/minute"
_JOBS_CREATE_RATE_LIMIT = "20/minute"

# In-memory sliding-window rate limiter for /mcp/invoke (per API key).
_MCP_RATE_LIMIT_PER_MIN = 60
_mcp_rate_windows: dict[str, collections.deque] = {}
_mcp_rate_lock = threading.Lock()


def _mcp_check_rate_limit(key_id: str) -> bool:
    """Return True if the key is within limit, False if exceeded."""
    now = time.monotonic()
    window = 60.0
    with _mcp_rate_lock:
        dq = _mcp_rate_windows.setdefault(key_id, collections.deque())
        while dq and now - dq[0] >= window:
            dq.popleft()
        if len(dq) >= _MCP_RATE_LIMIT_PER_MIN:
            return False
        dq.append(now)
        return True


def _mcp_log_invocation(
    agent_id: str,
    caller_key_id: str,
    tool_name: str,
    input_json: str,
    duration_ms: int,
    success: bool,
) -> None:
    input_hash = hashlib.sha256(input_json.encode("utf-8", errors="replace")).hexdigest()
    row_id = str(uuid.uuid4())
    now = _utc_now_iso()
    try:
        with jobs._conn() as conn:
            conn.execute(
                """
                INSERT INTO mcp_invocation_log
                    (id, agent_id, caller_key_id, tool_name, input_hash, invoked_at, duration_ms, success)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row_id, agent_id, caller_key_id, tool_name, input_hash, now, duration_ms, int(success)),
            )
    except Exception:
        _LOG.warning("Failed to write MCP invocation log for tool '%s'", tool_name)


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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean, got {raw!r}.")


def _parse_ip_allowlist(name: str, raw: str | None) -> list[Any]:
    value = str(raw or "").strip()
    if not value:
        return []
    networks: list[Any] = []
    for token in value.split(","):
        candidate = token.strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                network = ipaddress.ip_network(candidate, strict=False)
            else:
                ip_obj = ipaddress.ip_address(candidate)
                prefix = 32 if ip_obj.version == 4 else 128
                network = ipaddress.ip_network(f"{candidate}/{prefix}", strict=False)
        except ValueError as exc:
            raise RuntimeError(f"{name} contains invalid IP/CIDR value {candidate!r}.") from exc
        networks.append(network)
    return networks


_JUDGE_FEE_CENTS = _env_int(
    "JUDGE_FEE_CENTS",
    _DEFAULT_JUDGE_FEE_CENTS,
    minimum=0,
    maximum=10_000,
)


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
_HOOK_DELIVERY_CLAIM_LEASE_SECONDS = _env_int(
    "HOOK_DELIVERY_CLAIM_LEASE_SECONDS",
    _DEFAULT_HOOK_DELIVERY_CLAIM_LEASE_SECONDS,
    minimum=5,
    maximum=300,
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
_DEFAULT_JOB_DISPUTE_WINDOW_HOURS = _env_int(
    "DEFAULT_JOB_DISPUTE_WINDOW_HOURS",
    _DEFAULT_DISPUTE_WINDOW_HOURS,
    minimum=1,
    maximum=24 * 30,
)
_DISPUTE_JUDGE_INTERVAL_SECONDS = _env_int(
    "DISPUTE_JUDGE_INTERVAL_SECONDS",
    _DEFAULT_DISPUTE_JUDGE_INTERVAL_SECONDS,
    minimum=0,
    maximum=3600,
)
_DISPUTE_JUDGE_ENABLED = _DISPUTE_JUDGE_INTERVAL_SECONDS > 0
_AGENT_HEALTH_CHECK_INTERVAL_SECONDS = _env_int(
    "AGENT_HEALTH_CHECK_INTERVAL_SECONDS",
    300,
    minimum=0,
)
_AGENT_HEALTH_CHECK_ENABLED = _AGENT_HEALTH_CHECK_INTERVAL_SECONDS > 0
_DISPUTE_FILING_DEPOSIT_BPS = _env_int(
    "DISPUTE_FILING_DEPOSIT_BPS",
    _DEFAULT_DISPUTE_FILING_DEPOSIT_BPS,
    minimum=0,
    maximum=10_000,
)
_DISPUTE_FILING_DEPOSIT_MIN_CENTS = _env_int(
    "DISPUTE_FILING_DEPOSIT_MIN_CENTS",
    _DEFAULT_DISPUTE_FILING_DEPOSIT_MIN_CENTS,
    minimum=0,
    maximum=10_000,
)
_builtin_worker_interval = _env_int(
    "BUILTIN_JOB_WORKER_INTERVAL_SECONDS",
    _DEFAULT_BUILTIN_JOB_WORKER_INTERVAL_SECONDS,
    minimum=0,
    maximum=300,
)
_BUILTIN_JOB_WORKER_INTERVAL_SECONDS = (
    _builtin_worker_interval
    if _builtin_worker_interval > 0
    else _DEFAULT_BUILTIN_JOB_WORKER_INTERVAL_SECONDS
)
_BUILTIN_JOB_WORKER_BATCH_SIZE = _env_int(
    "BUILTIN_JOB_WORKER_BATCH_SIZE",
    _DEFAULT_BUILTIN_JOB_WORKER_BATCH_SIZE,
    minimum=1,
    maximum=500,
)
_BUILTIN_JOB_WORKER_ENABLED = _env_bool(
    "BUILTIN_JOB_WORKER_ENABLED",
    default=_builtin_worker_interval > 0,
)
_TOPUP_DAILY_LIMIT_CENTS = _env_int(
    "TOPUP_DAILY_LIMIT_CENTS",
    _DEFAULT_TOPUP_DAILY_LIMIT_CENTS,
    minimum=0,
    maximum=5_000_000,
)
_PAYMENTS_RECONCILIATION_INTERVAL_SECONDS = _env_int(
    "PAYMENTS_RECONCILIATION_INTERVAL_SECONDS",
    _DEFAULT_PAYMENTS_RECONCILIATION_INTERVAL_SECONDS,
    minimum=0,
    maximum=24 * 3600,
)
_PAYMENTS_RECONCILIATION_MAX_MISMATCHES = _env_int(
    "PAYMENTS_RECONCILIATION_MAX_MISMATCHES",
    _DEFAULT_PAYMENTS_RECONCILIATION_MAX_MISMATCHES,
    minimum=1,
    maximum=1000,
)
_PAYMENTS_RECONCILIATION_ENABLED = _PAYMENTS_RECONCILIATION_INTERVAL_SECONDS > 0
_ENDPOINT_MONITOR_BATCH_SIZE = _env_int(
    "ENDPOINT_MONITOR_BATCH_SIZE",
    _DEFAULT_ENDPOINT_MONITOR_BATCH_SIZE,
    minimum=1,
    maximum=500,
)
_ENDPOINT_MONITOR_TIMEOUT_SECONDS = _env_int(
    "ENDPOINT_MONITOR_TIMEOUT_SECONDS",
    _DEFAULT_ENDPOINT_MONITOR_TIMEOUT_SECONDS,
    minimum=1,
    maximum=30,
)
_ENDPOINT_MONITOR_FAILURE_THRESHOLD = _env_int(
    "ENDPOINT_MONITOR_FAILURE_THRESHOLD",
    _DEFAULT_ENDPOINT_MONITOR_FAILURE_THRESHOLD,
    minimum=1,
    maximum=20,
)
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
_ENVIRONMENT = str(os.environ.get("ENVIRONMENT", "development") or "development").strip().lower()
_ALLOW_PRIVATE_OUTBOUND_URLS = os.environ.get("ALLOW_PRIVATE_OUTBOUND_URLS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
_ADMIN_IP_ALLOWLIST_NETWORKS = _parse_ip_allowlist(
    "ADMIN_IP_ALLOWLIST",
    os.environ.get("ADMIN_IP_ALLOWLIST"),
)
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
_BUILTIN_WORKER_STATE_LOCK = threading.Lock()
_BUILTIN_WORKER_STATE = {
    "enabled": _BUILTIN_JOB_WORKER_ENABLED,
    "interval_seconds": _BUILTIN_JOB_WORKER_INTERVAL_SECONDS,
    "batch_size": _BUILTIN_JOB_WORKER_BATCH_SIZE,
    "running": False,
    "started_at": None,
    "last_run_at": None,
    "last_summary": None,
    "last_error": None,
}
_DISPUTE_JUDGE_STATE_LOCK = threading.Lock()
_DISPUTE_JUDGE_STATE = {
    "enabled": _DISPUTE_JUDGE_ENABLED,
    "interval_seconds": _DISPUTE_JUDGE_INTERVAL_SECONDS,
    "running": False,
    "started_at": None,
    "last_run_at": None,
    "last_summary": None,
    "last_error": None,
}
_PAYMENTS_RECONCILIATION_STATE_LOCK = threading.Lock()
_PAYMENTS_RECONCILIATION_STATE = {
    "enabled": _PAYMENTS_RECONCILIATION_ENABLED,
    "interval_seconds": _PAYMENTS_RECONCILIATION_INTERVAL_SECONDS,
    "max_mismatches": _PAYMENTS_RECONCILIATION_MAX_MISMATCHES,
    "running": False,
    "started_at": None,
    "last_run_at": None,
    "last_summary": None,
    "last_error": None,
}
_INFLIGHT_REQUESTS_LOCK = threading.Lock()
_INFLIGHT_REQUESTS = 0
_SERVER_SHUTTING_DOWN = False
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = _env_int(
    "SHUTDOWN_DRAIN_TIMEOUT_SECONDS",
    15,
    minimum=0,
    maximum=120,
)
_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS = _env_int(
    "SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS",
    10,
    minimum=1,
    maximum=120,
)
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
    "agent_message",
    "note",
    "tool_call",
    "tool_result",
}
_AGENT_WORK_EXAMPLES_MAX = _env_int(
    "AGENT_WORK_EXAMPLES_MAX",
    20,
    minimum=1,
    maximum=100,
)
_AGENT_WORK_EXAMPLE_MAX_STRING_LEN = _env_int(
    "AGENT_WORK_EXAMPLE_MAX_STRING_LEN",
    500,
    minimum=64,
    maximum=4000,
)


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


def _set_server_shutting_down(value: bool) -> None:
    global _SERVER_SHUTTING_DOWN
    with _INFLIGHT_REQUESTS_LOCK:
        _SERVER_SHUTTING_DOWN = bool(value)


def _server_is_shutting_down() -> bool:
    with _INFLIGHT_REQUESTS_LOCK:
        return bool(_SERVER_SHUTTING_DOWN)


def _inc_inflight_requests() -> int:
    global _INFLIGHT_REQUESTS
    with _INFLIGHT_REQUESTS_LOCK:
        _INFLIGHT_REQUESTS += 1
        return _INFLIGHT_REQUESTS


def _dec_inflight_requests() -> int:
    global _INFLIGHT_REQUESTS
    with _INFLIGHT_REQUESTS_LOCK:
        _INFLIGHT_REQUESTS = max(0, _INFLIGHT_REQUESTS - 1)
        return _INFLIGHT_REQUESTS


def _inflight_requests_count() -> int:
    with _INFLIGHT_REQUESTS_LOCK:
        return int(_INFLIGHT_REQUESTS)


def _compute_dispute_filing_deposit_cents(price_cents: int) -> int:
    normalized_price = max(0, int(price_cents))
    if normalized_price <= 0 or _DISPUTE_FILING_DEPOSIT_BPS <= 0:
        return 0
    computed = (normalized_price * _DISPUTE_FILING_DEPOSIT_BPS) // 10_000
    return max(_DISPUTE_FILING_DEPOSIT_MIN_CENTS, computed)


def _migrate_job_event_deliveries_status_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_event_deliveries'"
    ).fetchone()
    if row is None:
        return
    table_sql = str(row["sql"] or "").lower()
    if (
        "dead_letter" not in table_sql
        and "retrying" not in table_sql
        and "'failed'" in table_sql
        and "'cancelled'" in table_sql
    ):
        return

    conn.execute(
        """
        CREATE TABLE job_event_deliveries_new (
            delivery_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id            INTEGER NOT NULL,
            hook_id             TEXT NOT NULL,
            owner_id            TEXT NOT NULL,
            target_url          TEXT NOT NULL,
            secret              TEXT,
            payload             TEXT NOT NULL,
            status              TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'failed', 'cancelled')),
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
        """
        INSERT INTO job_event_deliveries_new (
            delivery_id, event_id, hook_id, owner_id, target_url, secret, payload, status,
            attempt_count, next_attempt_at, last_attempt_at, last_success_at, last_status_code,
            last_error, created_at, updated_at
        )
        SELECT
            delivery_id,
            event_id,
            hook_id,
            owner_id,
            target_url,
            secret,
            payload,
            CASE
                WHEN status = 'retrying' THEN 'pending'
                WHEN status = 'dead_letter' THEN 'failed'
                WHEN status IN ('pending', 'delivered', 'failed', 'cancelled') THEN status
                ELSE 'pending'
            END AS status,
            attempt_count,
            next_attempt_at,
            last_attempt_at,
            last_success_at,
            last_status_code,
            last_error,
            created_at,
            updated_at
        FROM job_event_deliveries
        """
    )
    conn.execute("DROP TABLE job_event_deliveries")
    conn.execute("ALTER TABLE job_event_deliveries_new RENAME TO job_event_deliveries")


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
                status              TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'failed', 'cancelled')),
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
        _migrate_job_event_deliveries_status_schema(conn)
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



def _init_stripe_db() -> None:
    """Create Stripe bookkeeping tables used for top-ups and webhook idempotency."""
    with sqlite3.connect(jobs.DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_sessions (
                session_id    TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL,
                processed_at  TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stripe_webhook_events (
                session_id    TEXT PRIMARY KEY,
                wallet_id     TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL,
                status        TEXT NOT NULL CHECK(status IN ('processing', 'processed', 'failed')),
                attempts      INTEGER NOT NULL DEFAULT 0,
                last_error    TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stripe_webhook_events_status_updated "
            "ON stripe_webhook_events(status, updated_at DESC)"
        )


# ---------------------------------------------------------------------------
# Startup — register built-in agents
# ---------------------------------------------------------------------------

def _output_schema_object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties)}
    if required:
        schema["required"] = list(required)
    return schema


def _quality_judge_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "input_payload": {"type": "object"},
            "output_payload": {"type": "object"},
            "agent_description": {"type": "string"},
        },
        "required": ["input_payload", "output_payload"],
    }


def _builtin_agent_specs() -> list[dict[str, Any]]:
    specs = [
        {
            "agent_id": _FINANCIAL_AGENT_ID,
            "name": "Financial Research Agent",
            "description": "Fetches the latest SEC filing and returns a structured investment brief.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_FINANCIAL_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["financial-research", "sec-filings", "equity-analysis"],
            "input_schema": FinancialRequest.model_json_schema(),
            "output_schema": _output_schema_object(
                {
                    "ticker": {"type": "string"},
                    "company_name": {"type": "string"},
                    "filing_type": {"type": "string"},
                    "filing_date": {"type": "string"},
                    "business_summary": {"type": "string"},
                    "recent_financial_highlights": {"type": "array", "items": {"type": "string"}},
                    "key_risks": {"type": "array", "items": {"type": "string"}},
                    "signal": {"type": "string"},
                    "signal_reasoning": {"type": "string"},
                    "generated_at": {"type": "string"},
                },
                required=["ticker", "signal"],
            ),
            "output_examples": [
                {
                    "input": {"ticker": "AAPL"},
                    "output": {
                        "ticker": "AAPL",
                        "company_name": "Apple Inc.",
                        "filing_type": "10-Q",
                        "filing_date": "2026-01-31",
                        "business_summary": "Consumer hardware and services ecosystem.",
                        "recent_financial_highlights": ["Revenue growth in Services", "Stable gross margin"],
                        "key_risks": ["Regulatory pressure", "Supply chain concentration"],
                        "signal": "positive",
                        "signal_reasoning": "Recurring revenue expansion offsets hardware cyclicality.",
                        "generated_at": "2026-02-01T00:00:00+00:00",
                    },
                },
                {
                    "input": {"ticker": "TSLA"},
                    "output": {
                        "ticker": "TSLA",
                        "company_name": "Tesla, Inc.",
                        "filing_type": "10-Q",
                        "filing_date": "2026-02-05",
                        "business_summary": "EV manufacturing and energy storage business.",
                        "recent_financial_highlights": ["Automotive margin compression", "Energy growth"],
                        "key_risks": ["Price competition", "Execution risk on new models"],
                        "signal": "neutral",
                        "signal_reasoning": "Growth opportunities remain, but profitability volatility is elevated.",
                        "generated_at": "2026-02-06T00:00:00+00:00",
                    },
                },
            ],
        },
        {
            "agent_id": _CODEREVIEW_AGENT_ID,
            "name": "Code Review Agent",
            "description": "Staff-engineer-quality code review: OWASP Top 10 vulnerabilities with CWE IDs, performance anti-patterns, complexity scoring, test recommendations, and copy-paste-ready fixes.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_CODEREVIEW_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["code-review", "security", "developer-tools"],
            "input_schema": CodeReviewRequest.model_json_schema(),
            "output_schema": _output_schema_object(
                {
                    "language_detected": {"type": "string"},
                    "score": {"type": "integer"},
                    "issues": {"type": "array", "items": {"type": "object"}},
                    "positive_aspects": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
                required=["score", "summary"],
            ),
            "output_examples": [
                {
                    "input": {
                        "code": "def divide(a, b):\n    return a / b\n",
                        "language": "python",
                        "focus": "bugs",
                    },
                    "output": {
                        "language_detected": "python",
                        "score": 78,
                        "issues": [
                            {
                                "severity": "medium",
                                "title": "Missing zero-division guard",
                                "suggestion": "Handle b == 0 before division.",
                            }
                        ],
                        "positive_aspects": ["Function is concise and readable."],
                        "summary": "Core logic is correct but missing input safety checks.",
                    },
                },
                {
                    "input": {
                        "code": "const token = req.headers.authorization;\nconsole.log(token);",
                        "language": "javascript",
                        "focus": "security",
                    },
                    "output": {
                        "language_detected": "javascript",
                        "score": 62,
                        "issues": [
                            {
                                "severity": "high",
                                "title": "Sensitive token logging",
                                "suggestion": "Remove token logging or redact before logging.",
                            }
                        ],
                        "positive_aspects": ["Simple extraction flow."],
                        "summary": "Avoid exposing secrets in logs.",
                    },
                },
            ],
        },
        {
            "agent_id": _TEXTINTEL_AGENT_ID,
            "name": "Text Intelligence Agent",
            "description": "Deep NLP analysis: sentiment + objectivity scoring, named entity extraction with roles, logical fallacy detection, rhetorical device identification, bias indicators, and claim extraction. Modes: full | quick | claims | rhetoric.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_TEXTINTEL_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["nlp", "sentiment-analysis", "text-analytics"],
            "input_schema": TextIntelRequest.model_json_schema(),
            "output_schema": _output_schema_object(
                {
                    "word_count": {"type": "integer"},
                    "reading_time_seconds": {"type": "integer"},
                    "language": {"type": "string"},
                    "sentiment": {"type": "string"},
                    "sentiment_score": {"type": "number"},
                    "summary": {"type": "string"},
                    "key_entities": {"type": "array", "items": {"type": "string"}},
                    "main_topics": {"type": "array", "items": {"type": "string"}},
                    "key_quotes": {"type": "array", "items": {"type": "string"}},
                },
                required=["word_count", "summary"],
            ),
            "output_examples": [
                {
                    "input": {
                        "text": "Revenue rose 18% year over year while operating margin fell 2 points.",
                        "mode": "quick",
                    },
                    "output": {
                        "word_count": 13,
                        "reading_time_seconds": 4,
                        "language": "en",
                        "sentiment": "mixed",
                        "sentiment_score": 0.12,
                        "summary": "Strong growth paired with margin pressure.",
                        "key_entities": ["Revenue", "Operating margin"],
                        "main_topics": ["earnings", "profitability"],
                        "key_quotes": ["Revenue rose 18% year over year"],
                    },
                },
                {
                    "input": {
                        "text": "Customer satisfaction improved after response times dropped below two hours.",
                        "mode": "full",
                    },
                    "output": {
                        "word_count": 11,
                        "reading_time_seconds": 3,
                        "language": "en",
                        "sentiment": "positive",
                        "sentiment_score": 0.71,
                        "summary": "Faster support correlated with better satisfaction.",
                        "key_entities": ["Customer satisfaction", "response times"],
                        "main_topics": ["support operations", "customer experience"],
                        "key_quotes": ["response times dropped below two hours"],
                    },
                },
            ],
        },
        {
            "agent_id": _WIKI_AGENT_ID,
            "name": "Wikipedia Research Agent",
            "description": "Deep research synthesis from Wikipedia: dense fact extraction, chronological timelines, notable figures, statistics with source notes, controversies and debates, knowledge gaps, and primary sources worth following up. Modes: standard | deep.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_WIKI_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["research", "knowledge-base", "wikipedia"],
            "input_schema": WikiRequest.model_json_schema(),
            "output_schema": _output_schema_object(
                {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "summary": {"type": "string"},
                    "key_facts": {"type": "array", "items": {"type": "string"}},
                    "related_topics": {"type": "array", "items": {"type": "string"}},
                    "content_type": {"type": "string"},
                },
                required=["title", "summary"],
            ),
            "output_examples": [
                {
                    "input": {"topic": "Discounted cash flow"},
                    "output": {
                        "title": "Discounted cash flow",
                        "url": "https://en.wikipedia.org/wiki/Discounted_cash_flow",
                        "summary": "Valuation method based on present value of expected future cash flows.",
                        "key_facts": [
                            "Uses a discount rate to reflect risk and time value.",
                            "Common in equity and project valuation.",
                        ],
                        "related_topics": ["Net present value", "Weighted average cost of capital"],
                        "content_type": "encyclopedia_article",
                    },
                },
                {
                    "input": {"topic": "Porter's five forces"},
                    "output": {
                        "title": "Porter's five forces analysis",
                        "url": "https://en.wikipedia.org/wiki/Porter%27s_five_forces_analysis",
                        "summary": "Framework for analyzing competition and profitability drivers in an industry.",
                        "key_facts": ["Covers supplier power, buyer power, rivalry, substitutes, and entrants."],
                        "related_topics": ["Competitive strategy", "Industry analysis"],
                        "content_type": "encyclopedia_article",
                    },
                },
            ],
        },
        {
            "agent_id": _NEGOTIATION_AGENT_ID,
            "name": "Negotiation Strategist Agent",
            "description": "Harvard-method negotiation strategy: ZOPA/BATNA analysis, power dynamics scoring, verbatim scripts, concession sequencing plan, tactic counterplay, and timeline leverage. Grounded in Fisher & Ury and behavioral economics.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_NEGOTIATION_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["negotiation", "strategy", "operations"],
            "input_schema": NegotiationRequest.model_json_schema(),
            "output_schema": _output_schema_object(
                {
                    "opening_position": {"type": "string"},
                    "must_haves": {"type": "array", "items": {"type": "string"}},
                    "tradeables": {"type": "array", "items": {"type": "string"}},
                    "red_lines": {"type": "array", "items": {"type": "string"}},
                    "tactics": {"type": "array", "items": {"type": "object"}},
                    "fallback_plan": {"type": "string"},
                    "risk_flags": {"type": "array", "items": {"type": "string"}},
                },
                required=["opening_position", "fallback_plan"],
            ),
            "output_examples": [
                {
                    "input": {
                        "objective": "Renew enterprise contract at +12% ARR with annual prepay.",
                        "counterparty_profile": "Procurement-led team",
                        "constraints": ["No discount above 8%"],
                        "context": "Incumbent vendor with strong adoption.",
                    },
                    "output": {
                        "opening_position": "Propose multi-year renewal with premium support add-on.",
                        "must_haves": ["Price uplift near target", "Annual prepay"],
                        "tradeables": ["Seat ramp schedule", "Training credits"],
                        "red_lines": ["Discount above 8%"],
                        "tactics": [{"name": "anchoring", "description": "Lead with value-backed anchor"}],
                        "fallback_plan": "Offer term extension in exchange for lower uplift.",
                        "risk_flags": ["Budget freeze risk", "Competitive quotes late in cycle"],
                    },
                },
                {
                    "input": {
                        "objective": "Secure vendor SLA concessions without price increase.",
                        "counterparty_profile": "Relationship-focused account team",
                        "constraints": ["No budget increase"],
                        "context": "Recent outage impacted trust.",
                    },
                    "output": {
                        "opening_position": "Tie SLA upgrades to renewal certainty and reference commitment.",
                        "must_haves": ["Response-time SLA improvements"],
                        "tradeables": ["Public case study participation"],
                        "red_lines": ["Any net new cost"],
                        "tactics": [{"name": "package swap", "description": "Exchange non-cash concessions"}],
                        "fallback_plan": "Escalate to pilot extension with explicit SLA checkpoints.",
                        "risk_flags": ["Vendor legal delays", "Scope ambiguity in SLA wording"],
                    },
                },
            ],
        },
        {
            "agent_id": _SCENARIO_AGENT_ID,
            "name": "Scenario Simulator Agent",
            "description": "5-scenario strategic foresight (crash/downside/base/upside/moonshot) with calibrated probabilities, sensitivity analysis, pre-mortem, monitoring dashboard, and early signal detection. GBN/Shell methodology.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SCENARIO_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["forecasting", "strategy", "decision-making"],
            "input_schema": ScenarioRequest.model_json_schema(),
            "output_schema": _output_schema_object(
                {
                    "decision": {"type": "string"},
                    "horizon": {"type": "string"},
                    "risk_tolerance": {"type": "string"},
                    "scenarios": {"type": "array", "items": {"type": "object"}},
                    "recommended_plan": {"type": "object"},
                    "confidence": {"type": "number"},
                },
                required=["decision", "scenarios", "recommended_plan"],
            ),
            "output_examples": [
                {
                    "input": {
                        "decision": "Expand to EU via direct sales team",
                        "assumptions": "ARR 5M with 30% growth",
                        "horizon": "18 months",
                        "risk_tolerance": "balanced",
                    },
                    "output": {
                        "decision": "Expand to EU via direct sales team",
                        "horizon": "18 months",
                        "risk_tolerance": "balanced",
                        "scenarios": [
                            {"name": "base", "probability": 0.5, "result": "moderate growth"},
                            {"name": "upside", "probability": 0.25, "result": "accelerated pipeline"},
                        ],
                        "recommended_plan": {
                            "phases": ["pilot in 2 countries", "scale after KPI validation"]
                        },
                        "confidence": 0.67,
                    },
                },
                {
                    "input": {
                        "decision": "Delay expansion and deepen US upsell",
                        "assumptions": "Strong NRR but slowing top-of-funnel",
                        "horizon": "12 months",
                        "risk_tolerance": "conservative",
                    },
                    "output": {
                        "decision": "Delay expansion and deepen US upsell",
                        "horizon": "12 months",
                        "risk_tolerance": "conservative",
                        "scenarios": [{"name": "base", "probability": 0.6, "result": "higher cash efficiency"}],
                        "recommended_plan": {"focus": ["enterprise expansion", "churn prevention"]},
                        "confidence": 0.72,
                    },
                },
            ],
        },
        {
            "agent_id": _PRODUCT_AGENT_ID,
            "name": "Product Strategy Lab Agent",
            "description": "VP-level product strategy: Jobs To Be Done analysis, RICE-scored roadmap, competitive moat assessment, unit economics (CAC/LTV), hypothesis-driven experiments, and phased go-to-market. Honest about weak spots.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PRODUCT_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["product", "go-to-market", "experimentation"],
            "input_schema": ProductStrategyRequest.model_json_schema(),
            "output_schema": _output_schema_object(
                {
                    "positioning_statement": {"type": "string"},
                    "user_personas": {"type": "array", "items": {"type": "string"}},
                    "roadmap": {"type": "array", "items": {"type": "object"}},
                    "experiments": {"type": "array", "items": {"type": "object"}},
                    "risks": {"type": "array", "items": {"type": "string"}},
                },
                required=["positioning_statement", "roadmap"],
            ),
            "output_examples": [
                {
                    "input": {
                        "product_idea": "AI copilot for customer success teams",
                        "target_users": "Mid-market B2B SaaS CSMs",
                        "market_context": "Crowded tooling category",
                        "horizon_quarters": 3,
                    },
                    "output": {
                        "positioning_statement": "Proactive churn prevention assistant for high-volume CSM workflows.",
                        "user_personas": ["Scaled CSM", "CS leader"],
                        "roadmap": [
                            {"quarter": "Q1", "milestone": "risk scoring MVP"},
                            {"quarter": "Q2", "milestone": "playbook automation"},
                        ],
                        "experiments": [{"name": "churn model A/B", "metric": "retention lift"}],
                        "risks": ["Data quality variance", "Integration complexity"],
                    },
                },
                {
                    "input": {
                        "product_idea": "Automated onboarding coach for PLG products",
                        "target_users": "SMB product teams",
                        "market_context": "High trial-to-paid drop-off",
                        "horizon_quarters": 2,
                    },
                    "output": {
                        "positioning_statement": "Guided activation coach that shortens time-to-value for new users.",
                        "user_personas": ["Growth PM", "Lifecycle marketer"],
                        "roadmap": [{"quarter": "Q1", "milestone": "in-app assistant + milestone tracking"}],
                        "experiments": [{"name": "activation checklist personalization", "metric": "activation rate"}],
                        "risks": ["Over-personalization fatigue"],
                    },
                },
            ],
        },
        {
            "agent_id": _PORTFOLIO_AGENT_ID,
            "name": "Portfolio Planner Agent",
            "description": "CFA-level portfolio planning: mean-variance optimization concepts, factor exposure, Sharpe/Sortino estimates, inflation-adjusted return ranges, tax efficiency notes, specific ETF examples (VTI/BND/VXUS), phased deployment plan, and realistic red flags.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PORTFOLIO_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["portfolio", "allocation", "wealth-planning"],
            "input_schema": PortfolioRequest.model_json_schema(),
            "output_schema": _output_schema_object(
                {
                    "goal_summary": {"type": "string"},
                    "allocation": {"type": "array", "items": {"type": "object"}},
                    "rebalancing_plan": {"type": "string"},
                    "watch_metrics": {"type": "array", "items": {"type": "string"}},
                    "disclaimer": {"type": "string"},
                },
                required=["goal_summary", "allocation"],
            ),
            "output_examples": [
                {
                    "input": {
                        "investment_goal": "Long-term wealth growth",
                        "risk_profile": "balanced",
                        "time_horizon_years": 10,
                        "capital_usd": 50000,
                    },
                    "output": {
                        "goal_summary": "Balanced growth allocation for long-term horizon.",
                        "allocation": [
                            {"asset_class": "US equities", "weight_pct": 45},
                            {"asset_class": "International equities", "weight_pct": 20},
                            {"asset_class": "Bonds", "weight_pct": 30},
                            {"asset_class": "Cash", "weight_pct": 5},
                        ],
                        "rebalancing_plan": "Rebalance semi-annually or at 5% drift.",
                        "watch_metrics": ["volatility", "drawdown", "allocation drift"],
                        "disclaimer": "Educational output, not investment advice.",
                    },
                },
                {
                    "input": {
                        "investment_goal": "Capital preservation",
                        "risk_profile": "conservative",
                        "time_horizon_years": 5,
                        "capital_usd": 120000,
                    },
                    "output": {
                        "goal_summary": "Conservative allocation prioritizing downside protection.",
                        "allocation": [
                            {"asset_class": "Investment-grade bonds", "weight_pct": 55},
                            {"asset_class": "Dividend equities", "weight_pct": 25},
                            {"asset_class": "Cash equivalents", "weight_pct": 20},
                        ],
                        "rebalancing_plan": "Quarterly review with annual tax-aware rebalance.",
                        "watch_metrics": ["income yield", "duration risk", "inflation sensitivity"],
                        "disclaimer": "Educational output, not investment advice.",
                    },
                },
            ],
        },
        {
            "agent_id": _QUALITY_JUDGE_AGENT_ID,
            "name": "Quality Judge Agent",
            "description": "Internal verification worker that scores completed outputs before settlement.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_QUALITY_JUDGE_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["quality", "internal"],
            "input_schema": _quality_judge_input_schema(),
            "output_schema": _output_schema_object(
                {
                    "verdict": {"type": "string"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                required=["verdict", "score", "reason"],
            ),
            "output_examples": [
                {
                    "input": {
                        "input_payload": {"task": "Summarize filing risks"},
                        "output_payload": {"summary": "Identified debt covenant and supply-chain risks."},
                        "agent_description": "SEC filing analyst",
                    },
                    "output": {
                        "verdict": "pass",
                        "score": 86,
                        "reason": "Output is relevant, structured, and addresses requested risk focus.",
                    },
                },
                {
                    "input": {
                        "input_payload": {"task": "Provide concise bug report"},
                        "output_payload": {"text": "Looks good."},
                        "agent_description": "Code review specialist",
                    },
                    "output": {
                        "verdict": "fail",
                        "score": 22,
                        "reason": "Response is too generic and lacks actionable findings.",
                    },
                },
            ],
            "internal_only": True,
        },
        {
            "agent_id": _RESUME_AGENT_ID,
            "name": "Resume Analyzer Agent",
            "description": "Staff-recruiter-quality resume analysis: ATS score, keyword gap detection, line-by-line rewrites, section audit, and a verdict. Optionally matches against a specific job description.",
            "endpoint_url": "internal://resume",
            "price_per_call_usd": 0.02,
            "tags": ["career", "recruiting", "writing"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "resume_text": {
                        "type": "string",
                        "description": "Full resume text (plain text or lightly formatted)",
                        "example": "Jane Doe\njane@email.com\n\nExperience:\nSoftware Engineer at Acme Corp...",
                    },
                    "job_description": {
                        "type": "string",
                        "description": "Job description to match against (optional)",
                        "default": "",
                    },
                    "role_level": {
                        "type": "string",
                        "enum": ["junior", "mid", "senior", "executive"],
                        "default": "mid",
                        "description": "Target seniority level",
                    },
                },
                "required": ["resume_text"],
            },
            "output_schema": _output_schema_object(
                {
                    "overall_score": {"type": "integer"},
                    "ats_score": {"type": "integer"},
                    "verdict": {"type": "string"},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "critical_gaps": {"type": "array", "items": {"type": "string"}},
                    "line_edits": {"type": "array", "items": {"type": "object"}},
                    "one_line_summary": {"type": "string"},
                },
                required=["overall_score", "verdict", "one_line_summary"],
            ),
            "output_examples": [
                {
                    "input": {"resume_text": "John Smith\njohn@email.com\n\nExperience:\nJr Dev at StartupXYZ 2022-2024...", "role_level": "mid"},
                    "output": {
                        "overall_score": 62,
                        "ats_score": 71,
                        "verdict": "needs_work",
                        "strengths": ["Consistent employment history", "Relevant tech stack listed"],
                        "critical_gaps": ["No quantified impact in any bullet", "Missing summary section", "Skills section is disorganized"],
                        "line_edits": [{"original": "Worked on features", "improved": "Shipped 8 product features serving 12K users, reducing support tickets 22%", "reason": "Quantified impact outperforms vague ownership"}],
                        "one_line_summary": "Solid background but resume undersells their work — needs rewriting before senior roles.",
                    },
                },
            ],
        },
        {
            "agent_id": _SQLBUILDER_AGENT_ID,
            "name": "SQL Query Builder Agent",
            "description": "Natural language to production SQL across PostgreSQL, MySQL, SQLite, BigQuery, and Snowflake. Includes explanation, edge case handling, performance notes, and dialect-specific guidance.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SQLBUILDER_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["sql", "data-engineering", "developer-tools"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Natural language question to answer with SQL",
                        "example": "What are the top 10 customers by total spend in the last 90 days?",
                    },
                    "schema": {
                        "type": "string",
                        "description": "Database schema as DDL or table descriptions (optional)",
                        "default": "",
                        "example": "CREATE TABLE orders (id INT, customer_id INT, amount DECIMAL, created_at TIMESTAMP);",
                    },
                    "dialect": {
                        "type": "string",
                        "enum": ["postgresql", "mysql", "sqlite", "bigquery", "snowflake"],
                        "default": "postgresql",
                    },
                    "context": {
                        "type": "string",
                        "description": "Additional context: data volumes, performance requirements",
                        "default": "",
                    },
                },
                "required": ["question"],
            },
            "output_schema": _output_schema_object(
                {
                    "sql": {"type": "string"},
                    "explanation": {"type": "string"},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                    "performance_notes": {"type": "array", "items": {"type": "string"}},
                    "estimated_complexity": {"type": "string"},
                },
                required=["sql", "explanation"],
            ),
            "output_examples": [
                {
                    "input": {
                        "question": "Top 5 products by revenue last quarter",
                        "schema": "CREATE TABLE orders (id INT, product_id INT, amount DECIMAL, created_at TIMESTAMP);\nCREATE TABLE products (id INT, name TEXT);",
                        "dialect": "postgresql",
                    },
                    "output": {
                        "sql": "WITH last_q AS (\n  SELECT product_id, SUM(amount) AS revenue\n  FROM orders\n  WHERE created_at >= date_trunc('quarter', CURRENT_DATE) - INTERVAL '3 months'\n    AND created_at < date_trunc('quarter', CURRENT_DATE)\n  GROUP BY product_id\n)\nSELECT p.name, lq.revenue\nFROM last_q lq JOIN products p ON p.id = lq.product_id\nORDER BY lq.revenue DESC\nLIMIT 5;",
                        "explanation": "Uses date_trunc to isolate the previous calendar quarter, aggregates order revenue per product, joins to get names, and returns top 5.",
                        "assumptions": ["'Last quarter' means previous full calendar quarter", "amount column is already in the same currency"],
                        "performance_notes": ["Add index on orders(created_at, product_id) for large tables"],
                        "estimated_complexity": "moderate",
                    },
                },
            ],
        },
        {
            "agent_id": _DATAINSIGHTS_AGENT_ID,
            "name": "Data Insights Agent",
            "description": "Analyzes JSON, CSV, or structured text data: descriptive statistics, anomaly detection, trend identification, direct answers to specific questions, and visualization recommendations.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_DATAINSIGHTS_AGENT_ID],
            "price_per_call_usd": 0.02,
            "tags": ["data-analysis", "analytics", "statistics"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": "Raw data to analyze: JSON array, CSV text, or key:value pairs",
                        "example": '[{"month":"Jan","revenue":42000,"users":1200},{"month":"Feb","revenue":51000,"users":1450}]',
                    },
                    "question": {
                        "type": "string",
                        "description": "Specific question to answer, or 'general' for open-ended analysis",
                        "default": "general",
                        "example": "Which month had the highest revenue per user?",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["json", "csv", "text"],
                        "default": "json",
                    },
                },
                "required": ["data"],
            },
            "output_schema": _output_schema_object(
                {
                    "row_count": {"type": "integer"},
                    "key_findings": {"type": "array", "items": {"type": "string"}},
                    "anomalies": {"type": "array", "items": {"type": "object"}},
                    "answer_to_question": {"type": "string"},
                    "recommendations": {"type": "array", "items": {"type": "string"}},
                },
                required=["key_findings", "answer_to_question"],
            ),
            "output_examples": [
                {
                    "input": {
                        "data": '[{"month":"Jan","revenue":42000},{"month":"Feb","revenue":51000},{"month":"Mar","revenue":38000}]',
                        "question": "Is revenue trending up or down?",
                        "format": "json",
                    },
                    "output": {
                        "row_count": 3,
                        "key_findings": ["Feb was peak revenue at $51K", "March dropped 25.5% from Feb — significant decline", "Jan-Feb shows growth but Feb-Mar reversal"],
                        "anomalies": [{"description": "March revenue drop of 25.5% from Feb — unusually large swing", "severity": "medium", "affected_rows": "row 3"}],
                        "answer_to_question": "Mixed — revenue grew 21% Jan to Feb, then dropped 25.5% in March. No clear trend in 3 data points; more history needed.",
                        "recommendations": ["Investigate March drop cause before drawing conclusions", "Collect at least 6 months of data for trend analysis"],
                    },
                },
            ],
        },
        {
            "agent_id": _EMAILWRITER_AGENT_ID,
            "name": "Email Sequence Writer Agent",
            "description": "Writes professional emails and multi-email sequences for outreach, follow-ups, proposals, announcements, and support. Generates 3 subject line A/B variants, preview text, and personalization hooks per email.",
            "endpoint_url": "internal://email-writer",
            "price_per_call_usd": 0.02,
            "tags": ["writing", "marketing", "sales"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "What this email (or sequence) needs to achieve",
                        "example": "Book a 20-minute demo with a VP of Engineering at a Series B startup",
                    },
                    "tone": {
                        "type": "string",
                        "enum": ["formal", "professional", "friendly", "direct", "persuasive"],
                        "default": "professional",
                    },
                    "email_type": {
                        "type": "string",
                        "enum": ["outreach", "follow_up", "proposal", "rejection", "announcement", "support", "sequence"],
                        "default": "outreach",
                    },
                    "recipient_context": {
                        "type": "string",
                        "description": "Who you're writing to",
                        "default": "",
                        "example": "VP Engineering at a 50-person Series B SaaS startup in fintech",
                    },
                    "sender_context": {
                        "type": "string",
                        "description": "Who you are / your company",
                        "default": "",
                    },
                    "key_points": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key points to include",
                        "default": [],
                    },
                    "sequence_length": {
                        "type": "integer",
                        "description": "Number of emails in the sequence (1-5)",
                        "default": 1,
                        "minimum": 1,
                        "maximum": 5,
                    },
                },
                "required": ["goal"],
            },
            "output_schema": _output_schema_object(
                {
                    "emails": {"type": "array", "items": {"type": "object"}},
                    "strategy_notes": {"type": "string"},
                    "personalization_hooks": {"type": "array", "items": {"type": "string"}},
                },
                required=["emails", "strategy_notes"],
            ),
            "output_examples": [
                {
                    "input": {
                        "goal": "Get a product manager to respond to a demo request",
                        "tone": "friendly",
                        "email_type": "outreach",
                        "recipient_context": "Senior PM at a mid-size B2B SaaS company",
                        "sequence_length": 1,
                    },
                    "output": {
                        "emails": [{
                            "sequence_position": 1,
                            "subject_lines": ["Quick question about [Company]'s onboarding flow", "The problem most PMs have with {metric}", "15 minutes — is this relevant to you?"],
                            "body": "Hi [Name],\n\nI noticed [Company] recently launched [feature] — that usually means onboarding optimization becomes a real priority.\n\nWe help teams like yours cut time-to-value by 40% without touching the engineering backlog.\n\nWorth 15 minutes next week?\n\n[Your name]",
                            "preview_text": "Quick question about your onboarding flow at [Company]...",
                            "send_timing": "Day 0",
                            "word_count": 58,
                            "cta": "Book a 15-minute call",
                        }],
                        "strategy_notes": "Single email focused on a specific trigger event to establish relevance before the ask.",
                        "personalization_hooks": ["Reference a recent product launch or announcement", "Mention a job posting that reveals a pain point", "Cite a public metric or company news"],
                    },
                },
            ],
        },
        {
            "agent_id": _SECRETS_AGENT_ID,
            "name": "Secrets Detection Agent",
            "description": "Scans GitHub repositories for exposed API keys, credentials, tokens, and secrets in source code and git history. Detects Stripe keys, AWS credentials, JWT secrets, and database passwords.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SECRETS_AGENT_ID],
            "price_per_call_usd": 0.04,
            "tags": ["security", "secrets", "git-history", "credentials"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "GitHub repo (owner/repo or full URL)"},
                    "scan": {"type": "string", "enum": ["full", "shallow"], "default": "full", "description": "full scans git history; shallow scans current HEAD only"},
                    "branch": {"type": "string", "default": "main"},
                },
                "required": ["repo"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "secrets": {"type": "array", "items": {"type": "object"}},
                    "git_history_secrets": {"type": "array", "items": {"type": "object"}},
                    "total_critical": {"type": "integer"},
                    "summary": {"type": "string"},
                },
            },
            "output_examples": [
                {
                    "input": {"repo": "acme/payments-api", "scan": "full"},
                    "output": {
                        "repo": "acme/payments-api",
                        "secrets": [{"file": "src/config/keys.js", "line": 12, "type": "stripe_key", "description": "Hardcoded Stripe live secret key", "confidence": "high"}],
                        "git_history_secrets": [{"commit": "a3f9b12", "file": ".env.backup", "type": "aws_credentials", "description": "AWS credentials committed to git history"}],
                        "total_critical": 2,
                        "summary": "Found 2 critical credential exposures.",
                        "scan_duration_ms": 1100,
                    },
                },
            ],
        },
        {
            "agent_id": _STATICANALYSIS_AGENT_ID,
            "name": "Static Analysis Agent",
            "description": "Performs static security analysis on GitHub repositories. Detects SQL injection (CWE-89), authentication bypass (CWE-306), XSS (CWE-79), path traversal (CWE-22), and SSRF (CWE-918) with copy-paste-ready fixes.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_STATICANALYSIS_AGENT_ID],
            "price_per_call_usd": 0.09,
            "tags": ["security", "sast", "code-analysis", "cwe", "owasp"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "GitHub repo (owner/repo or full URL)"},
                    "focus": {"type": "string", "default": "all", "description": "Comma-separated: injection,auth,xss,path_traversal,all"},
                    "language": {"type": "string", "default": "auto"},
                },
                "required": ["repo"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "findings": {"type": "array", "items": {"type": "object"}},
                    "total_critical": {"type": "integer"},
                    "total_high": {"type": "integer"},
                    "summary": {"type": "string"},
                },
            },
            "output_examples": [
                {
                    "input": {"repo": "acme/payments-api", "focus": "injection,auth"},
                    "output": {
                        "repo": "acme/payments-api",
                        "findings": [{"file": "src/db/query.js", "line": 47, "severity": "critical", "type": "sql_injection", "cwe": "CWE-89", "description": "Unsanitized input in SQL query"}],
                        "total_critical": 1,
                        "total_high": 1,
                        "summary": "Found 1 critical SQL injection (CWE-89).",
                        "scan_duration_ms": 2300,
                    },
                },
            ],
        },
        {
            "agent_id": _DEPSCANNER_AGENT_ID,
            "name": "Dependency Scanner Agent",
            "description": "Scans npm, pip, Maven, Cargo, and Go module dependencies against the NIST NVD and GitHub Advisory Database. Returns CVEs with CVSS scores, affected version ranges, and upgrade paths.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_DEPSCANNER_AGENT_ID],
            "price_per_call_usd": 0.11,
            "tags": ["security", "dependencies", "cve", "supply-chain", "npm"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "GitHub repo to scan"},
                    "ecosystem": {"type": "string", "enum": ["npm", "pip", "maven", "cargo", "go"], "default": "npm"},
                },
                "required": ["repo"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "vulnerabilities": {"type": "array", "items": {"type": "object"}},
                    "total": {"type": "integer"},
                    "summary": {"type": "string"},
                },
            },
            "output_examples": [
                {
                    "input": {"repo": "acme/payments-api", "ecosystem": "npm"},
                    "output": {
                        "repo": "acme/payments-api",
                        "ecosystem": "npm",
                        "vulnerabilities": [{"package": "lodash", "version": "4.17.20", "cve": "CVE-2021-23337", "cvss": 7.2, "severity": "high"}],
                        "total": 2,
                        "summary": "Found 2 CVEs across 1 vulnerable package.",
                        "scan_duration_ms": 3100,
                    },
                },
            ],
        },
        {
            "agent_id": _CVELOOKUP_AGENT_ID,
            "name": "CVE Lookup Agent",
            "description": "Real-time CVE intelligence for specific package versions. Cross-references NIST NVD, MITRE CVE, and GitHub Advisory Database. Returns CVSS scores, exploit availability, affected version ranges, and recommended upgrade paths.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_CVELOOKUP_AGENT_ID],
            "price_per_call_usd": 0.06,
            "tags": ["security", "cve", "vulnerability-intel", "nvd", "packages"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "packages": {"type": "array", "items": {"type": "string"}, "description": "Array of package@version strings", "example": ["express@4.17.1", "lodash@4.17.20"]},
                    "include_patched": {"type": "boolean", "default": False},
                },
                "required": ["packages"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "results": {"type": "array", "items": {"type": "object"}},
                    "total_vulnerable": {"type": "integer"},
                    "summary": {"type": "string"},
                },
            },
            "output_examples": [
                {
                    "input": {"packages": ["lodash@4.17.20", "express@4.17.1"]},
                    "output": {
                        "results": [{"package": "lodash", "version": "4.17.20", "cve": "CVE-2019-10744", "cvss": 9.1, "severity": "critical"}],
                        "total_vulnerable": 2,
                        "total_packages_checked": 2,
                        "summary": "lodash@4.17.20 has 2 known CVEs including CVE-2019-10744 (prototype pollution, CVSS 9.1).",
                    },
                },
            ],
        },
    ]
    specs.extend(
        [
            {
                "agent_id": _SYSTEM_DESIGN_AGENT_ID,
                "name": "System Design Reviewer Agent",
                "description": "Principal-level architecture planning with request flows, data models, tradeoff matrices, phased rollout plans, and explicit risk ownership.",
                "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SYSTEM_DESIGN_AGENT_ID],
                "price_per_call_usd": 0.08,
                "tags": ["architecture", "system-design", "scalability", "reliability"],
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "context": {"type": "string", "description": "Product/service context and objective."},
                        "requirements": {"type": "array", "items": {"type": "string"}},
                        "constraints": {"type": "array", "items": {"type": "string"}},
                        "scale_assumptions": {"type": "array", "items": {"type": "string"}},
                        "stack": {"type": "array", "items": {"type": "string"}},
                        "non_functional_requirements": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["context", "requirements"],
                },
                "output_schema": _output_schema_object(
                    {
                        "architecture_summary": {"type": "string"},
                        "components": {"type": "array", "items": {"type": "object"}},
                        "request_flow": {"type": "array", "items": {"type": "string"}},
                        "tradeoff_matrix": {"type": "array", "items": {"type": "object"}},
                        "scaling_plan": {"type": "object"},
                        "phase_plan": {"type": "array", "items": {"type": "object"}},
                        "top_risks": {"type": "array", "items": {"type": "object"}},
                    },
                    required=["architecture_summary", "components", "phase_plan"],
                ),
                "output_examples": [
                    {
                        "input": {
                            "context": "Realtime fraud scoring API for card transactions.",
                            "requirements": ["p95 under 120ms", "99.95% availability", "full auditability"],
                            "constraints": ["single region initially", "lean infra team"],
                        },
                        "output": {
                            "architecture_summary": "Event-driven scoring service with low-latency cache and async model updates.",
                            "components": [
                                {"name": "gateway", "responsibility": "auth + rate limit"},
                                {"name": "scoring-service", "responsibility": "rules + model inference"},
                            ],
                            "request_flow": ["gateway validates request", "scoring-service reads feature cache", "decision emitted to ledger"],
                            "tradeoff_matrix": [
                                {
                                    "decision": "cache strategy",
                                    "option_a": "global cache",
                                    "option_b": "service-local cache",
                                    "chosen": "service-local cache",
                                    "rationale": "lower latency and reduced blast radius",
                                }
                            ],
                            "scaling_plan": {"hotspots": ["feature lookup"], "mitigations": ["read-through cache"]},
                            "phase_plan": [{"phase": "phase-1", "goal": "stabilize core path"}],
                            "top_risks": [{"risk": "feature staleness", "impact": "medium", "owner": "platform"}],
                        },
                    }
                ],
            },
            {
                "agent_id": _INCIDENT_RESPONSE_AGENT_ID,
                "name": "Incident Response Commander Agent",
                "description": "SRE-grade incident command: likely root causes with confidence, first-15-minute actions, stabilization plan, communication templates, and 30/60/90-minute timeline.",
                "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_INCIDENT_RESPONSE_AGENT_ID],
                "price_per_call_usd": 0.08,
                "tags": ["incident-response", "sre", "operations", "reliability"],
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "incident_title": {"type": "string"},
                        "severity": {"type": "string", "default": "unknown"},
                        "symptoms": {"type": "array", "items": {"type": "string"}},
                        "service_map": {"type": "array", "items": {"type": "string"}},
                        "recent_changes": {"type": "array", "items": {"type": "string"}},
                        "telemetry": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["incident_title", "symptoms"],
                },
                "output_schema": _output_schema_object(
                    {
                        "severity_assessment": {"type": "object"},
                        "probable_root_causes": {"type": "array", "items": {"type": "object"}},
                        "first_15_min_actions": {"type": "array", "items": {"type": "string"}},
                        "stabilization_plan": {"type": "array", "items": {"type": "string"}},
                        "communications": {"type": "object"},
                        "timeline_30_60_90": {"type": "object"},
                        "postmortem_followups": {"type": "array", "items": {"type": "string"}},
                    },
                    required=["severity_assessment", "first_15_min_actions", "communications"],
                ),
                "output_examples": [
                    {
                        "input": {
                            "incident_title": "Checkout latency spike",
                            "symptoms": ["p95 jumped from 180ms to 1.9s", "timeouts from payment provider"],
                            "recent_changes": ["new retry policy deployed"],
                        },
                        "output": {
                            "severity_assessment": {"level": "sev-1", "justification": "Revenue path degraded globally"},
                            "probable_root_causes": [
                                {
                                    "cause": "retry amplification to payment upstream",
                                    "confidence": "high",
                                    "evidence": ["timeout volume aligns with deploy"],
                                }
                            ],
                            "first_15_min_actions": ["freeze deploys", "disable aggressive retries", "enable load shedding"],
                            "stabilization_plan": ["rollback retry policy", "drain failing worker pool"],
                            "communications": {
                                "internal_update": "Mitigating checkout latency via retry rollback.",
                                "status_page_update": "Investigating elevated checkout errors.",
                                "next_update_eta": "15 minutes",
                            },
                            "timeline_30_60_90": {"30": "service stabilized", "60": "error budget impact estimated", "90": "postmortem owner assigned"},
                            "postmortem_followups": ["retry policy guardrail tests", "per-provider circuit breaker thresholds"],
                        },
                    }
                ],
            },
            {
                "agent_id": _HEALTHCARE_EXPERT_AGENT_ID,
                "name": "Healthcare Expert Agent",
                "description": "Clinical triage copilot for symptom assessment, urgency flags, and clinician-ready visit prep. Educational-only guidance with explicit emergency escalation.",
                "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_HEALTHCARE_EXPERT_AGENT_ID],
                "price_per_call_usd": 0.03,
                "tags": ["healthcare", "triage", "clinical-support", "patient-education"],
                "input_schema": _output_schema_object(
                    {
                        "symptoms": {"type": "array", "items": {"type": "string"}},
                        "age_years": {"type": "integer", "minimum": 0},
                        "sex": {"type": "string"},
                        "medical_history": {"type": "array", "items": {"type": "string"}},
                        "medications": {"type": "array", "items": {"type": "string"}},
                        "duration": {"type": "string"},
                        "urgency_context": {"type": "string"},
                        "goal": {"type": "string"},
                    },
                    required=["symptoms"],
                ),
                "output_schema": _output_schema_object(
                    {
                        "triage_level": {"type": "string"},
                        "possible_considerations": {"type": "array", "items": {"type": "object"}},
                        "red_flags": {"type": "array", "items": {"type": "object"}},
                        "next_steps": {"type": "array", "items": {"type": "string"}},
                        "questions_for_clinician": {"type": "array", "items": {"type": "string"}},
                        "disclaimer": {"type": "string"},
                    },
                    required=["triage_level", "next_steps", "disclaimer"],
                ),
                "output_examples": [
                    {
                        "input": {
                            "symptoms": ["fever", "sore throat", "fatigue"],
                            "age_years": 29,
                            "duration": "2 days",
                            "medical_history": ["asthma"],
                        },
                        "output": {
                            "triage_level": "primary_care_24h",
                            "possible_considerations": [
                                {"condition": "viral upper respiratory infection", "confidence": "medium"}
                            ],
                            "red_flags": [
                                {"flag": "trouble breathing", "why_urgent": "possible respiratory compromise"}
                            ],
                            "next_steps": ["Hydrate and rest", "Schedule clinician visit within 24 hours if symptoms persist"],
                            "questions_for_clinician": ["Should I adjust asthma meds while sick?"],
                            "disclaimer": "Educational guidance only, not a diagnosis or treatment plan.",
                        },
                    }
                ],
            },
            {
                "agent_id": _IMAGE_GENERATOR_AGENT_ID,
                "name": "Image Generator Agent",
                "description": "Generates production image artifacts with real model backends (OpenAI gpt-image-1 or configured Replicate model), with optional reference-image guidance.",
                "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_IMAGE_GENERATOR_AGENT_ID],
                "price_per_call_usd": 0.02,
                "tags": ["image-generation", "creative", "multimodal", "design"],
                "input_schema": _output_schema_object(
                    {
                        "prompt": {"type": "string"},
                        "style": {"type": "string"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "output_format": {"type": "string"},
                        "input_images": {"type": "array", "items": {"type": "object"}},
                    },
                    required=["prompt"],
                ),
                "output_schema": _output_schema_object(
                    {
                        "summary": {"type": "string"},
                        "generation_prompt": {"type": "string"},
                        "artifacts": {"type": "array", "items": {"type": "object"}},
                        "input_images_used": {"type": "integer"},
                        "warnings": {"type": "array", "items": {"type": "string"}},
                    },
                    required=["summary", "artifacts"],
                ),
                "output_examples": [
                    {
                        "input": {
                            "prompt": "A retro-futurist skyline at sunrise, cinematic lighting",
                            "style": "synthwave",
                            "width": 1024,
                            "height": 1024,
                        },
                        "output": {
                            "summary": "Generated one image artifact using a live model backend.",
                            "generation_prompt": "A retro-futurist skyline at sunrise, cinematic lighting",
                            "artifacts": [
                                {
                                    "name": "generated.png",
                                    "mime": "image/png",
                                    "url_or_base64": "data:image/png;base64,...",
                                    "size_bytes": 245801,
                                }
                            ],
                            "input_images_used": 0,
                            "warnings": [],
                        },
                    }
                ],
            },
            {
                "agent_id": _VIDEO_STORYBOARD_AGENT_ID,
                "name": "Video Storyboard Generator Agent",
                "description": "Turns a creative brief into production-ready shot plans and generates an actual video artifact through a configured video model backend.",
                "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_VIDEO_STORYBOARD_AGENT_ID],
                "price_per_call_usd": 0.03,
                "tags": ["video-generation", "storyboarding", "multimodal", "creative"],
                "input_schema": _output_schema_object(
                    {
                        "brief": {"type": "string"},
                        "duration_seconds": {"type": "integer"},
                        "aspect_ratio": {"type": "string"},
                        "style": {"type": "string"},
                        "reference_images": {"type": "array", "items": {"type": "object"}},
                    },
                    required=["brief"],
                ),
                "output_schema": _output_schema_object(
                    {
                        "title": {"type": "string"},
                        "duration_seconds": {"type": "integer"},
                        "aspect_ratio": {"type": "string"},
                        "style": {"type": "string"},
                        "shot_plan": {"type": "array", "items": {"type": "object"}},
                        "voiceover_script": {"type": "string"},
                        "render_recipe": {"type": "object"},
                        "artifacts": {"type": "array", "items": {"type": "object"}},
                    },
                    required=["title", "shot_plan", "voiceover_script", "artifacts"],
                ),
                "output_examples": [
                    {
                        "input": {
                            "brief": "Launch teaser for an AI healthcare assistant for busy parents.",
                            "duration_seconds": 30,
                            "aspect_ratio": "16:9",
                            "style": "clean cinematic",
                        },
                        "output": {
                            "title": "Launch teaser: AI healthcare assistant for busy parents",
                            "duration_seconds": 30,
                            "aspect_ratio": "16:9",
                            "style": "clean cinematic",
                            "shot_plan": [
                                {
                                    "shot_id": 1,
                                    "start_second": 0,
                                    "end_second": 6,
                                    "visual_prompt": "Morning rush at home, parent checking phone for care guidance",
                                }
                            ],
                            "voiceover_script": "When every minute matters, trusted care guidance should be one tap away.",
                            "render_recipe": {"target_fps": 24, "color_profile": "rec709", "provider": "replicate"},
                            "artifacts": [
                                {
                                    "name": "generated.mp4",
                                    "mime": "video/mp4",
                                    "url_or_base64": "https://cdn.example.com/generated.mp4",
                                    "size_bytes": 0,
                                }
                            ],
                        },
                    }
                ],
            },
        {
            "agent_id": _ARXIV_RESEARCH_AGENT_ID,
            "name": "arXiv Research Agent",
            "description": "Search real academic papers on arXiv and get an expert synthesis: key themes, seminal works, open questions, and suggested follow-ups. Pulls live data from arXiv.org — not LLM hallucinations.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_ARXIV_RESEARCH_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["research", "academic", "arxiv", "papers", "science"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (keywords, author name, topic)", "example": "diffusion models image generation"},
                    "max_results": {"type": "integer", "default": 8, "minimum": 1, "maximum": 20, "description": "Number of papers to fetch"},
                    "sort_by": {"type": "string", "enum": ["relevance", "lastUpdatedDate", "submittedDate"], "default": "relevance"},
                    "categories": {"type": "array", "items": {"type": "string"}, "description": "arXiv category filters e.g. cs.AI, stat.ML", "example": ["cs.LG", "cs.AI"]},
                },
                "required": ["query"],
            },
            "output_schema": _output_schema_object(
                {
                    "query": {"type": "string"},
                    "total_found": {"type": "integer"},
                    "papers": {"type": "array", "items": {"type": "object"}},
                    "synthesis": {"type": "string"},
                    "key_themes": {"type": "array", "items": {"type": "string"}},
                    "seminal_papers": {"type": "array", "items": {"type": "string"}},
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                    "suggested_follow_ups": {"type": "array", "items": {"type": "string"}},
                },
                required=["query", "papers", "synthesis"],
            ),
            "output_examples": [
                {
                    "input": {"query": "transformer attention self-supervised", "max_results": 5},
                    "output": {
                        "query": "transformer attention self-supervised",
                        "total_found": 5,
                        "papers": [
                            {
                                "arxiv_id": "2010.11929",
                                "title": "An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale",
                                "authors": ["Alexey Dosovitskiy", "Lucas Beyer"],
                                "abstract": "While the Transformer architecture has become the de-facto standard for NLP tasks, its applications to computer vision remain limited...",
                                "categories": ["cs.CV"],
                                "published": "2020-10-22",
                                "updated": "2021-06-03",
                                "pdf_url": "https://arxiv.org/pdf/2010.11929",
                                "abstract_url": "https://arxiv.org/abs/2010.11929",
                            }
                        ],
                        "synthesis": "The literature shows a clear convergence on attention mechanisms replacing convolutional backbones for vision tasks, with self-supervised pre-training bridging the gap between label-efficient and high-performance models.",
                        "key_themes": ["vision transformers", "self-supervised pre-training", "attention at scale"],
                        "seminal_papers": ["2010.11929"],
                        "open_questions": ["Can attention fully replace inductive biases from CNNs?", "Scaling limits of self-supervised vision models"],
                        "suggested_follow_ups": ["masked autoencoders MAE", "CLIP vision language pretraining"],
                    },
                }
            ],
        },
        {
            "agent_id": _PYTHON_EXECUTOR_AGENT_ID,
            "name": "Python Code Executor",
            "description": "Execute Python code in a sandboxed environment and get stdout, stderr, exit code, execution time, and an expert explanation. Supports math, data manipulation, algorithms, and any pure-Python computation.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_PYTHON_EXECUTOR_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["code-execution", "python", "developer-tools", "compute"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute", "example": "print(sum(i**2 for i in range(10)))"},
                    "stdin": {"type": "string", "default": "", "description": "Optional input data fed to stdin"},
                    "timeout": {"type": "integer", "default": 10, "minimum": 1, "maximum": 30, "description": "Execution timeout in seconds"},
                    "explain": {"type": "boolean", "default": True, "description": "Whether to include an expert explanation of the output"},
                },
                "required": ["code"],
            },
            "output_schema": _output_schema_object(
                {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "timed_out": {"type": "boolean"},
                    "execution_time_ms": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "variables_captured": {"type": "object"},
                },
                required=["stdout", "exit_code"],
            ),
            "output_examples": [
                {
                    "input": {"code": "import math\nresult = [math.factorial(n) for n in range(1, 11)]\nprint(result)", "explain": True},
                    "output": {
                        "stdout": "[1, 2, 6, 24, 120, 720, 5040, 40320, 362880, 3628800]\n",
                        "stderr": "",
                        "exit_code": 0,
                        "timed_out": False,
                        "execution_time_ms": 28,
                        "explanation": "The code computes factorials 1! through 10! using a list comprehension over math.factorial. Output is correct — factorials grow rapidly and 10! = 3,628,800 as expected.",
                        "variables_captured": {"result": [1, 2, 6, 24, 120, 720, 5040, 40320, 362880, 3628800]},
                    },
                }
            ],
        },
        {
            "agent_id": _WEB_RESEARCHER_AGENT_ID,
            "name": "Web Researcher Agent",
            "description": "Fetch any public URL and return a structured analysis: dense summary, key points, direct answers to your question, verbatim supporting quotes, and extracted links. Reads the actual page — not a cached version.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_WEB_RESEARCHER_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["web", "research", "summarization", "content-extraction"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public URL to fetch and analyze", "example": "https://en.wikipedia.org/wiki/Large_language_model"},
                    "question": {"type": "string", "default": "", "description": "Specific question to answer from the page content"},
                    "mode": {"type": "string", "enum": ["summary", "extract", "qa"], "default": "summary", "description": "Analysis mode"},
                },
                "required": ["url"],
            },
            "output_schema": _output_schema_object(
                {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "word_count": {"type": "integer"},
                    "fetched_at": {"type": "string"},
                    "summary": {"type": "string"},
                    "key_points": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "string"},
                    "quotes": {"type": "array", "items": {"type": "string"}},
                    "links": {"type": "array", "items": {"type": "object"}},
                    "content_type": {"type": "string"},
                },
                required=["url", "summary"],
            ),
            "output_examples": [
                {
                    "input": {"url": "https://en.wikipedia.org/wiki/Transformer_(machine_learning_model)", "question": "What is the key innovation of transformers?"},
                    "output": {
                        "url": "https://en.wikipedia.org/wiki/Transformer_(machine_learning_model)",
                        "title": "Transformer (machine learning model)",
                        "word_count": 4200,
                        "fetched_at": "2026-01-15T10:30:00+00:00",
                        "summary": "The Transformer is a deep learning architecture introduced in the 2017 paper 'Attention Is All You Need'. It relies entirely on self-attention mechanisms, eliminating recurrence and enabling massive parallelization during training.",
                        "key_points": [
                            "Introduced by Vaswani et al. (2017) at Google Brain",
                            "Self-attention enables O(1) path length between any two tokens",
                            "Forms the basis of GPT, BERT, and most modern LLMs",
                        ],
                        "answer": "The key innovation is replacing recurrence entirely with self-attention, which allows all token positions to attend to each other simultaneously, enabling parallelization and capturing long-range dependencies more effectively.",
                        "quotes": ["Attention is All You Need", "The model architecture avoids recurrence"],
                        "links": [{"text": "Attention Is All You Need", "href": "https://arxiv.org/abs/1706.03762"}],
                        "content_type": "article",
                    },
                }
            ],
        },
        ]
    )
    return [spec for spec in specs if spec.get("agent_id") in _CURATED_BUILTIN_AGENT_IDS]


def _ensure_system_user() -> str:
    with _auth._conn() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE username = ? ORDER BY created_at ASC LIMIT 1",
            (_SYSTEM_USERNAME,),
        ).fetchone()
        if existing is not None:
            user_id = str(existing["user_id"])
            conn.execute("UPDATE users SET status = 'suspended' WHERE user_id = ?", (user_id,))
            return user_id

        user_id = str(uuid.uuid4())
        now = _utc_now_iso()
        email = _SYSTEM_USER_EMAIL
        if conn.execute("SELECT 1 FROM users WHERE email = ? LIMIT 1", (email,)).fetchone() is not None:
            email = f"system-{user_id[:8]}@aztea.internal"
        salt = "system-account-disabled"
        password_hash = hashlib.sha256(f"{user_id}:{salt}".encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO users (user_id, username, email, password_hash, salt, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'suspended')
            """,
            (user_id, _SYSTEM_USERNAME, email, password_hash, salt, now),
        )
        return user_id


def ensure_builtin_agents_registered() -> None:
    system_user_id = _ensure_system_user()
    system_owner_id = f"user:{system_user_id}"
    specs = _builtin_agent_specs()
    managed_ids = {str(spec.get("agent_id") or "").strip() for spec in specs if str(spec.get("agent_id") or "").strip()}
    now = _utc_now_iso()

    for spec in specs:
        existing = registry.get_agent(spec["agent_id"])
        output_examples = spec.get("output_examples")
        output_examples_json = None
        if isinstance(output_examples, list):
            output_examples_json = json.dumps([item for item in output_examples if isinstance(item, dict)]) or None
        if existing is None:
            if registry.agent_exists_by_name(spec["name"]):
                continue
            registry.register_agent(
                agent_id=spec["agent_id"],
                name=spec["name"],
                description=spec["description"],
                endpoint_url=spec["endpoint_url"],
                price_per_call_usd=float(spec.get("price_per_call_usd", 0.01)),
                tags=spec["tags"],
                input_schema=spec["input_schema"],
                output_schema=spec["output_schema"],
                output_verifier_url=None,
                output_examples=output_examples,
                internal_only=bool(spec.get("internal_only", False)),
                status="active",
                owner_id=system_owner_id,
                embed_listing=False,
                model_provider="groq",
                model_id="llama-3.3-70b-versatile",
            )
            continue

        with registry._conn() as conn:
            conn.execute(
                """
                UPDATE agents
                SET owner_id = ?,
                    name = ?,
                    description = ?,
                    endpoint_url = ?,
                    price_per_call_usd = ?,
                    tags = ?,
                    input_schema = ?,
                    output_schema = ?,
                    output_examples = ?,
                    internal_only = ?,
                    status = 'active',
                    review_status = 'approved',
                    reviewed_by = ?,
                    reviewed_at = ?,
                    model_provider = ?,
                    model_id = ?
                WHERE agent_id = ?
                """,
                (
                    system_owner_id,
                    spec["name"],
                    spec["description"],
                    spec["endpoint_url"],
                    float(spec.get("price_per_call_usd", 0.01)),
                    json.dumps(spec.get("tags") or []),
                    json.dumps(spec.get("input_schema") or {}, sort_keys=True),
                    json.dumps(spec.get("output_schema") or {}, sort_keys=True),
                    output_examples_json,
                    1 if bool(spec.get("internal_only", False)) else 0,
                    _SYSTEM_USERNAME,
                    now,
                    "groq",
                    "llama-3.3-70b-versatile",
                    spec["agent_id"],
                ),
            )

    deprecated_ids = _BUILTIN_AGENT_IDS - managed_ids
    for agent_id in deprecated_ids:
        stale = registry.get_agent(agent_id, include_unapproved=True)
        if stale is not None and str(stale.get("status") or "").strip().lower() != "suspended":
            registry.set_agent_status(agent_id, "suspended")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _ENVIRONMENT == "production" and not _ADMIN_IP_ALLOWLIST_NETWORKS:
        _LOG.warning(
            "ADMIN_IP_ALLOWLIST is not set. Admin routes are accessible from any IP. "
            "Set ADMIN_IP_ALLOWLIST=<cidr>,... to restrict access in production."
        )
    apply_migrations(jobs.DB_PATH)
    registry.init_db()
    payments.init_payments_db()
    _auth.init_auth_db()
    jobs.init_jobs_db()
    disputes.init_disputes_db()
    reputation.init_reputation_db()
    _init_ops_db()
    _init_stripe_db()
    ensure_builtin_agents_registered()
    _set_server_shutting_down(False)
    stop_event: threading.Event | None = None
    sweeper_thread: threading.Thread | None = None
    hook_stop_event: threading.Event | None = None
    hook_thread: threading.Thread | None = None
    builtin_stop_event: threading.Event | None = None
    builtin_thread: threading.Thread | None = None
    dispute_judge_stop_event: threading.Event | None = None
    dispute_judge_thread: threading.Thread | None = None
    payments_reconciliation_stop_event: threading.Event | None = None
    payments_reconciliation_thread: threading.Thread | None = None
    is_background_worker_leader = _acquire_background_worker_lock()
    if not is_background_worker_leader:
        _LOG.info("Background workers disabled in this process; another worker owns the lock.")

    if is_background_worker_leader and _SWEEPER_ENABLED:
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

    if is_background_worker_leader and _HOOK_DELIVERY_ENABLED:
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

    if is_background_worker_leader and _BUILTIN_JOB_WORKER_ENABLED:
        builtin_stop_event = threading.Event()
        builtin_thread = threading.Thread(
            target=_builtin_worker_loop,
            args=(builtin_stop_event,),
            daemon=True,
            name="agentmarket-builtin-worker",
        )
        builtin_thread.start()
    else:
        _set_builtin_worker_state(running=False)

    if is_background_worker_leader and _DISPUTE_JUDGE_ENABLED:
        dispute_judge_stop_event = threading.Event()
        dispute_judge_thread = threading.Thread(
            target=_dispute_judge_loop,
            args=(dispute_judge_stop_event,),
            daemon=True,
            name="agentmarket-dispute-judge",
        )
        dispute_judge_thread.start()
    else:
        _set_dispute_judge_state(running=False)

    agent_health_stop_event: threading.Event | None = None
    agent_health_thread: threading.Thread | None = None
    if is_background_worker_leader and _AGENT_HEALTH_CHECK_ENABLED:
        agent_health_stop_event = threading.Event()
        agent_health_thread = threading.Thread(
            target=_agent_health_loop,
            args=(agent_health_stop_event,),
            daemon=True,
            name="agentmarket-agent-health",
        )
        agent_health_thread.start()

    if is_background_worker_leader and _PAYMENTS_RECONCILIATION_ENABLED:
        payments_reconciliation_stop_event = threading.Event()
        payments_reconciliation_thread = threading.Thread(
            target=_payments_reconciliation_loop,
            args=(payments_reconciliation_stop_event,),
            daemon=True,
            name="agentmarket-payments-reconciliation",
        )
        payments_reconciliation_thread.start()
    else:
        _set_payments_reconciliation_state(running=False)

    if os.environ.get("OPENAI_API_KEY"):
        try:
            embeddings.embed_text("warmup")
        except Exception as exc:
            _LOG.warning("Embedding warmup failed: %s", exc)

    try:
        yield
    finally:
        _set_server_shutting_down(True)
        drain_deadline = time.monotonic() + _SHUTDOWN_DRAIN_TIMEOUT_SECONDS
        while time.monotonic() < drain_deadline:
            if _inflight_requests_count() <= 0:
                break
            await asyncio.sleep(0.05)
        if stop_event is not None:
            stop_event.set()
        if sweeper_thread is not None:
            sweeper_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if hook_stop_event is not None:
            hook_stop_event.set()
        if hook_thread is not None:
            hook_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if builtin_stop_event is not None:
            builtin_stop_event.set()
        if builtin_thread is not None:
            builtin_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if dispute_judge_stop_event is not None:
            dispute_judge_stop_event.set()
        if dispute_judge_thread is not None:
            dispute_judge_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if payments_reconciliation_stop_event is not None:
            payments_reconciliation_stop_event.set()
        if payments_reconciliation_thread is not None:
            payments_reconciliation_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if agent_health_stop_event is not None:
            agent_health_stop_event.set()
        if agent_health_thread is not None:
            agent_health_thread.join(timeout=_SHUTDOWN_THREAD_JOIN_TIMEOUT_SECONDS)
        if is_background_worker_leader:
            _release_background_worker_lock()
        _close_all_db_connections()


# ---------------------------------------------------------------------------
# Rate limiter — keyed per caller identity
# ---------------------------------------------------------------------------

def _key_from_request(request: Request) -> str:
    caller = _resolve_caller(request)
    if caller:
        if caller["type"] == "master":
            return "master"
        key_id = str(caller.get("key_id") or "").strip()
        if key_id:
            return f"key:{key_id}"
        return caller["owner_id"]
    client_ip = _request_client_ip(request)
    return str(client_ip) if client_ip is not None else "unknown"


limiter = Limiter(key_func=_key_from_request, default_limits=[_DEFAULT_RATE_LIMIT])
app = FastAPI(title="agentmarket v1", lifespan=lifespan)
app.state.limiter = limiter

# CORS — origins come from CORS_ALLOW_ORIGINS env var (comma-separated).
# Defaults include common local dev ports.  In production, set the env var
# to your deployed frontend origin(s), e.g.:
#   CORS_ALLOW_ORIGINS=https://aztea.dev,https://www.aztea.dev
_cors_env = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
_cors_origins: list[str] = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env
    else [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ]
)
# Production safety: refuse wildcard CORS in production deployments.
if _ENVIRONMENT == "production" and "*" in _cors_origins:
    raise RuntimeError("CORS_ALLOW_ORIGINS must not contain '*' when ENVIRONMENT=production.")
# Always include the configured frontend base URL so Stripe redirects work.
if _FRONTEND_BASE_URL and _FRONTEND_BASE_URL not in _cors_origins:
    _cors_origins.append(_FRONTEND_BASE_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    max_age=600,
)

# Trust X-Forwarded-For only from explicitly trusted proxy networks.
# TRUSTED_PROXY_IPS defaults to loopback for local reverse-proxy setups.
_TRUSTED_PROXY_NETWORKS = _parse_ip_allowlist(
    "TRUSTED_PROXY_IPS",
    os.environ.get("TRUSTED_PROXY_IPS", "127.0.0.1"),
)


# ---------------------------------------------------------------------------
# Middleware — security headers + request size cap
# ---------------------------------------------------------------------------

@app.middleware("http")
async def shutdown_draining(request: Request, call_next):
    _inc_inflight_requests()
    try:
        return await call_next(request)
    finally:
        _dec_inflight_requests()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    has_primary = (request.headers.get(_PROTOCOL_VERSION_HEADER, "") or "").strip()
    has_legacy = (request.headers.get(_LEGACY_PROTOCOL_VERSION_HEADER, "") or "").strip()
    if not (has_primary or has_legacy):
        logging_utils.log_event(
            _LOG,
            logging.WARNING,
            "request.missing_protocol_header",
            {
                "header": _PROTOCOL_VERSION_HEADER,
                "method": request.method,
                "path": request.url.path,
            },
        )
    response = await call_next(request)
    response.headers[_PROTOCOL_VERSION_HEADER] = _PROTOCOL_VERSION
    response.headers[_LEGACY_PROTOCOL_VERSION_HEADER] = _PROTOCOL_VERSION
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
            return JSONResponse(
                content=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    "Invalid Content-Length header.",
                ),
                status_code=400,
            )
        if content_length > _MAX_BODY_BYTES:
            return JSONResponse(
                content=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    f"Request body too large (max {_MAX_BODY_BYTES // 1024} KB).",
                ),
                status_code=413,
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

try:
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, REGISTRY
    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False

if _PROM_AVAILABLE:
    _prom_requests_total = Counter(
        "agentmarket_http_requests_total",
        "Total HTTP requests",
        ["method", "path", "status"],
    )
    _prom_request_latency = Histogram(
        "agentmarket_http_request_duration_seconds",
        "HTTP request latency",
        ["method", "path"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )

    def _metrics_path_label(request: Request) -> str:
        route = request.scope.get("route")
        route_path = getattr(route, "path", None)
        if isinstance(route_path, str) and route_path.strip():
            return route_path
        return request.url.path

    @app.middleware("http")
    async def prometheus_middleware(request: Request, call_next):
        raw_path = request.url.path
        # Don't instrument /metrics itself to avoid recursion noise
        if raw_path == "/metrics":
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        latency = time.perf_counter() - start
        path = _metrics_path_label(request)
        _prom_requests_total.labels(
            method=request.method, path=path, status=response.status_code
        ).inc()
        _prom_request_latency.labels(method=request.method, path=path).observe(latency)
        return response


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint(request: Request):
    """Prometheus-compatible metrics. Restricted to internal/admin callers."""
    allow_cidr = os.environ.get("METRICS_ALLOW_CIDR", "127.0.0.1/32")
    client_ip = _request_client_ip(request)
    try:
        network = ipaddress.ip_network(allow_cidr, strict=False)
        if client_ip is None or client_ip not in network:
            raise HTTPException(status_code=403, detail="Forbidden")
    except ValueError:
        pass
    if not _PROM_AVAILABLE:
        return JSONResponse({"error": "prometheus_client not installed"}, status_code=503)
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.middleware("http")
async def request_tracing(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    token: Token = logging_utils.set_request_id(request_id)
    start = time.monotonic()
    response: Response | None = None
    response = await call_next(request)
    try:
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration_ms = round((time.monotonic() - start) * 1000, 3)
        logging_utils.log_event(
            _LOG,
            logging.INFO,
            "http.request.completed",
            {
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
                "status_code": response.status_code if response is not None else 500,
                "client_ip": str(_request_client_ip(request) or ""),
            },
        )
        logging_utils.reset_request_id(token)


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
    if hmac.compare_digest(raw, _MASTER_KEY):
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
            "key_id": str(user.get("key_id") or ""),
        }
        request.state._caller = caller
        return caller

    agent_key = _auth.verify_agent_api_key(raw)
    if agent_key:
        caller = {
            "type": "agent_key",
            "owner_id": f"agent_key:{agent_key['agent_id']}",
            "scopes": ["worker"],
            "agent_id": str(agent_key["agent_id"]),
            "key_id": str(agent_key["key_id"]),
        }
        request.state._caller = caller
        return caller

    request.state._caller = None
    return None


_PUBLIC_FRONTEND_URL = (
    os.environ.get("AZTEA_FRONTEND_URL")
    or os.environ.get("AGENTMARKET_FRONTEND_URL")
    or "https://aztea.dev"
).rstrip("/")
_SIGNUP_URL = f"{_PUBLIC_FRONTEND_URL}/signup"
_DOCS_URL = f"{_PUBLIC_FRONTEND_URL}/docs"

_PUBLIC_DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")
_PUBLIC_DOCS_PRIORITY = {
    "quickstart.md": 0,
    "auth-onboarding.md": 1,
    "api-reference.md": 2,
}


def _public_docs_entries() -> list[dict[str, str]]:
    if not os.path.isdir(_PUBLIC_DOCS_DIR):
        return []

    filenames = [
        name for name in os.listdir(_PUBLIC_DOCS_DIR)
        if name.endswith(".md") and os.path.isfile(os.path.join(_PUBLIC_DOCS_DIR, name))
    ]
    filenames.sort(key=lambda name: (_PUBLIC_DOCS_PRIORITY.get(name, 100), name))

    entries: list[dict[str, str]] = []
    for filename in filenames:
        slug = filename[:-3].strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            continue
        title = slug.replace("-", " ").title()
        full_path = os.path.join(_PUBLIC_DOCS_DIR, filename)
        try:
            with open(full_path, encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        heading = stripped[2:].strip()
                        if heading:
                            title = heading
                        break
                    if stripped:
                        break
        except OSError:
            continue
        entries.append({
            "slug": slug,
            "title": title,
            "filename": filename,
            "full_path": full_path,
        })

    return entries


def _find_public_doc(doc_slug: str) -> dict[str, str] | None:
    normalized_slug = str(doc_slug or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", normalized_slug):
        return None
    for entry in _public_docs_entries():
        if entry["slug"] == normalized_slug:
            return entry
    return None


def _require_api_key(request: Request) -> core_models.CallerContext:
    caller = _resolve_caller(request)
    if caller is None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "AUTHENTICATION_REQUIRED",
                    "message": "No API key provided. Sign up to get one — it comes with $1 free credit.",
                    "signup_url": _SIGNUP_URL,
                    "docs_url": _DOCS_URL,
                },
            )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "INVALID_API_KEY",
                "message": "API key is invalid or expired.",
                "signup_url": _SIGNUP_URL,
                "docs_url": _DOCS_URL,
            },
        )
    return caller


def _caller_owner_id(request: Request) -> str:
    caller = _resolve_caller(request)
    if caller is None:
        raise HTTPException(status_code=403, detail="Invalid API key.")
    return caller["owner_id"]


def _caller_key_spend_cap(caller: core_models.CallerContext) -> int | None:
    if caller.get("type") != "user":
        return None
    user = caller.get("user") or {}
    raw = user.get("max_spend_cents")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _caller_key_per_job_cap(caller: core_models.CallerContext) -> int | None:
    if caller.get("type") != "user":
        return None
    user = caller.get("user") or {}
    raw = user.get("per_job_cap_cents")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _pre_call_charge_or_402(
    *,
    caller: core_models.CallerContext,
    caller_wallet_id: str,
    charge_cents: int,
    agent_id: str,
) -> str:
    try:
        return payments.pre_call_charge(
            caller_wallet_id,
            charge_cents,
            agent_id,
            charged_by_key_id=str(caller.get("key_id") or "").strip() or None,
            max_spend_cents=_caller_key_spend_cap(caller),
        )
    except payments.InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.INSUFFICIENT_FUNDS,
                "Insufficient wallet balance.",
                {
                    "balance_cents": exc.balance_cents,
                    "required_cents": exc.required_cents,
                    "wallet_id": caller_wallet_id,
                },
            ),
        )
    except payments.KeySpendLimitExceededError as exc:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.SPEND_LIMIT_EXCEEDED,
                "API key spend cap exceeded.",
                {
                    "scope": "api_key",
                    "key_id": str(caller.get("key_id") or "").strip() or None,
                    "limit_cents": exc.limit_cents,
                    "spent_cents": exc.spent_cents,
                    "attempted_cents": exc.attempted_cents,
                },
            ),
        )
    except payments.WalletDailySpendLimitExceededError as exc:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.SPEND_LIMIT_EXCEEDED,
                "Wallet daily spend cap exceeded.",
                {
                    "scope": "wallet_daily",
                    "wallet_id": caller_wallet_id,
                    "limit_cents": exc.limit_cents,
                    "spent_last_24h_cents": exc.spent_last_24h_cents,
                    "attempted_cents": exc.attempted_cents,
                },
            ),
        )


def _agent_has_verified_contract(agent: dict) -> bool:
    if "verified_contract" in agent:
        try:
            return bool(int(agent.get("verified_contract") or 0))
        except (TypeError, ValueError):
            return bool(agent.get("verified_contract"))
    try:
        return bool(int(agent.get("verified") or 0))
    except (TypeError, ValueError):
        return bool(agent.get("verified"))


def _deposit_below_minimum_error(attempted_cents: int) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=error_codes.make_error(
            error_codes.DEPOSIT_BELOW_MINIMUM,
            f"Minimum deposit is {MINIMUM_DEPOSIT_CENTS} cents.",
            {
                "minimum_cents": MINIMUM_DEPOSIT_CENTS,
                "attempted_cents": int(attempted_cents),
            },
        ),
    )


def _request_client_ip(request: Request) -> Any | None:
    host = (request.client.host if request.client else "") or ""
    try:
        direct_ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if any(direct_ip in network for network in _TRUSTED_PROXY_NETWORKS):
        forwarded_for = (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
        if forwarded_for:
            try:
                return ipaddress.ip_address(forwarded_for)
            except ValueError:
                pass
        real_ip = (request.headers.get("x-real-ip", "") or "").strip()
        if real_ip:
            try:
                return ipaddress.ip_address(real_ip)
            except ValueError:
                pass
    return direct_ip


def _require_admin_ip_allowlist(request: Request) -> None:
    if not _ADMIN_IP_ALLOWLIST_NETWORKS:
        return
    client_ip = _request_client_ip(request)
    if client_ip is None:
        raise HTTPException(status_code=403, detail="Admin endpoint access denied from this network.")
    if any(client_ip in network for network in _ADMIN_IP_ALLOWLIST_NETWORKS):
        return
    raise HTTPException(status_code=403, detail="Admin endpoint access denied from this network.")


def _get_owner_email(owner_id: str) -> str | None:
    """Return email address for a user owner_id (user:<uuid>), or None."""
    if not isinstance(owner_id, str) or not owner_id.startswith("user:"):
        return None
    user_id = owner_id[len("user:"):]
    try:
        user = _auth.get_user_by_id(user_id)
        return user.get("email") if user else None
    except Exception:
        return None


def _caller_has_scope(caller: core_models.CallerContext, required_scope: str) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return required_scope == "worker"
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
        detail=error_codes.make_error(
            error_codes.INSUFFICIENT_SCOPE,
            detail or f"This endpoint requires an API key with '{scope_name}' scope.",
        ),
    )


def _require_any_scope(caller: core_models.CallerContext, *scopes: str) -> None:
    """Raise 403 if the caller has none of the given scopes."""
    if any(_caller_has_scope(caller, s) for s in scopes):
        return
    joined = " or ".join(f"'{s}'" for s in scopes)
    raise HTTPException(
        status_code=403,
        detail=error_codes.make_error(
            error_codes.INSUFFICIENT_SCOPE,
            f"This endpoint requires {joined} scope.",
        ),
    )


def _proxy_headers_for_agent(agent: dict) -> dict[str, str]:
    return {"Content-Type": "application/json"}


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


def _extract_judge_agent_id(input_schema: dict | None) -> str | None:
    if not isinstance(input_schema, dict):
        return None
    candidate = input_schema.get("judge_agent_id")
    if candidate is None and isinstance(input_schema.get("metadata"), dict):
        candidate = input_schema["metadata"].get("judge_agent_id")
    text = str(candidate or "").strip()
    return text or None


def _caller_trust_score(owner_id: str) -> float:
    try:
        return payments.get_caller_trust(owner_id)
    except Exception:
        return 0.5


def _compute_bulk_agent_stats(agent_ids: list[str]) -> dict:
    """
    Returns {agent_id: {jobs_last_30_days, job_completion_rate, median_latency_seconds}}
    for all supplied agent IDs in a single pass.
    """
    if not agent_ids:
        return {}
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    placeholders = ",".join("?" * len(agent_ids))
    with jobs._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT agent_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status = 'failed'   THEN 1 ELSE 0 END) AS failed
            FROM jobs
            WHERE agent_id IN ({placeholders}) AND created_at >= ?
            GROUP BY agent_id
            """,
            (*agent_ids, since),
        ).fetchall()
        latency_rows = conn.execute(
            f"""
            SELECT agent_id,
                   (julianday(completed_at) - julianday(claimed_at)) * 86400 AS latency_s
            FROM jobs
            WHERE agent_id IN ({placeholders})
              AND status = 'complete'
              AND claimed_at IS NOT NULL
              AND completed_at IS NOT NULL
              AND created_at >= ?
            ORDER BY agent_id, latency_s
            """,
            (*agent_ids, since),
        ).fetchall()
    stats: dict[str, dict] = {aid: {"jobs_last_30_days": 0, "job_completion_rate": None, "median_latency_seconds": None} for aid in agent_ids}
    for r in rows:
        aid = r["agent_id"]
        total = int(r["total"] or 0)
        completed = int(r["completed"] or 0)
        failed = int(r["failed"] or 0)
        denom = completed + failed
        stats[aid]["jobs_last_30_days"] = total
        stats[aid]["job_completion_rate"] = round(completed / denom, 4) if denom > 0 else None
    by_agent: dict[str, list] = {}
    for lr in latency_rows:
        by_agent.setdefault(lr["agent_id"], []).append(float(lr["latency_s"]))
    for aid, lats in by_agent.items():
        if lats:
            mid = len(lats) // 2
            stats[aid]["median_latency_seconds"] = round(
                lats[mid] if len(lats) % 2 else (lats[mid - 1] + lats[mid]) / 2, 2
            )
    return stats


def _agent_response(agent: dict, caller: core_models.CallerContext, stats: dict | None = None) -> dict:
    min_caller_trust = _extract_caller_trust_min(agent.get("input_schema"))
    price_cents = _usd_to_cents(agent.get("price_per_call_usd") or 0.0)
    caller_charge_cents = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=payments.PLATFORM_FEE_PCT,
        fee_bearer_policy="caller",
    )["caller_charge_cents"]
    is_internal = bool(agent.get("internal_only")) or str(agent.get("endpoint_url", "")).startswith("internal://")
    out = dict(agent) if caller.get("type") == "master" else dict(agent)
    if caller.get("type") != "master":
        out.pop("owner_id", None)
    out["caller_trust_min"] = min_caller_trust
    out["caller_charge_cents"] = caller_charge_cents
    if is_internal:
        out["last_health_status"] = "healthy"
        out["last_health_check_at"] = _utc_now_iso()
    if stats is not None:
        out["jobs_last_30_days"] = stats.get("jobs_last_30_days", 0)
        out["job_completion_rate"] = stats.get("job_completion_rate")
        out["median_latency_seconds"] = stats.get("median_latency_seconds")
    return out


def _job_response(job: dict, caller: core_models.CallerContext) -> dict:
    if caller.get("type") == "master":
        out = dict(job)
        if out.get("caller_charge_cents") is None:
            out["caller_charge_cents"] = int(out.get("price_cents") or 0)
        return out

    owner_id = caller.get("owner_id")
    result = dict(job)
    if result.get("caller_charge_cents") is None:
        result["caller_charge_cents"] = int(result.get("price_cents") or 0)
    hidden = {
        "caller_wallet_id",
        "agent_wallet_id",
        "platform_wallet_id",
        "charge_tx_id",
        "agent_owner_id",
    }
    for key in hidden:
        result.pop(key, None)

    if owner_id != job.get("caller_owner_id") and owner_id != job.get("claim_owner_id"):
        result.pop("caller_owner_id", None)
        result.pop("output_verification_decision_owner_id", None)
    if owner_id != job.get("claim_owner_id"):
        result.pop("claim_token", None)
    return result


def _caller_can_view_job(caller: core_models.CallerContext, job: dict) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return str(caller.get("agent_id") or "").strip() == str(job.get("agent_id") or "").strip()
    owner_id = caller["owner_id"]
    return owner_id == job["caller_owner_id"] or jobs.is_worker_authorized(job, owner_id)


def _resolve_parent_job_for_creation(
    caller: core_models.CallerContext,
    parent_job_id: str | None,
    *,
    parent_cascade_policy: str,
) -> dict | None:
    normalized_parent_job_id = str(parent_job_id or "").strip()
    normalized_policy = str(parent_cascade_policy or "").strip().lower() or "detach"
    if not normalized_parent_job_id:
        if normalized_policy != "detach":
            raise HTTPException(
                status_code=422,
                detail="parent_cascade_policy requires parent_job_id.",
            )
        return None

    parent = jobs.get_job(normalized_parent_job_id)
    if parent is None:
        raise HTTPException(status_code=404, detail=f"Parent job '{normalized_parent_job_id}' not found.")

    if caller["type"] == "master":
        return parent

    owner_id = caller["owner_id"]
    if owner_id not in {parent.get("caller_owner_id"), parent.get("agent_owner_id")}:
        raise HTTPException(
            status_code=403,
            detail="Not authorized to link jobs to this parent_job_id.",
        )
    return parent


def _caller_can_manage_agent(caller: core_models.CallerContext, agent: dict) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return str(caller.get("agent_id") or "").strip() == str(agent.get("agent_id") or "").strip()
    return caller["owner_id"] == agent.get("owner_id")


def _caller_is_admin(caller: core_models.CallerContext) -> bool:
    if caller.get("type") == "master":
        return True
    scopes = caller.get("scopes") or []
    return "admin" in scopes


def _caller_can_access_agent(caller: core_models.CallerContext, agent: dict) -> bool:
    if _caller_is_admin(caller):
        return True
    if bool(agent.get("internal_only")):
        return _caller_can_manage_agent(caller, agent)
    review_status = str(agent.get("review_status") or "approved").strip().lower()
    if review_status != "approved":
        return False
    if str(agent.get("status") or "").strip().lower() == "banned":
        return False
    return True


def _assert_agent_callable(agent_id: str, agent: dict) -> None:
    if agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if agent.get("status") == "suspended":
        raise HTTPException(
            status_code=503,
            detail=error_codes.make_error(
                error_codes.AGENT_SUSPENDED,
                f"Agent '{agent_id}' is suspended.",
                {"agent_id": agent_id},
            ),
        )


def _caller_worker_authorized_for_job(caller: core_models.CallerContext, job: dict) -> bool:
    if caller["type"] == "master":
        return True
    if caller["type"] == "agent_key":
        return str(caller.get("agent_id") or "").strip() == str(job.get("agent_id") or "").strip()
    return jobs.is_worker_authorized(job, caller["owner_id"])


def _assert_worker_claim(
    job: dict,
    caller: core_models.CallerContext,
    worker_owner_id: str,
    claim_token: str | None,
) -> None:
    if not _caller_worker_authorized_for_job(caller, job):
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

    if not _caller_worker_authorized_for_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized for this agent job.")

    if (job.get("claim_owner_id") or "").strip() == actor_owner_id:
        _assert_worker_claim(job, caller, actor_owner_id, claim_token)
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
        allow_retry=True,
    )
    if updated is None:
        return None

    metadata: dict[str, Any] = {
        "touchpoint": touchpoint,
        "status_after": updated.get("status"),
    }
    if updated.get("status") == "pending":
        metadata["next_retry_at"] = updated.get("next_retry_at")

    jobs.add_claim_event(
        job["job_id"],
        event_type="touchpoint_timeout",
        claim_owner_id=job.get("claim_owner_id"),
        claim_token=job.get("claim_token"),
        lease_expires_at=job.get("lease_expires_at"),
        actor_id=actor_owner_id,
        metadata=metadata,
    )

    if updated.get("status") == "pending":
        _record_job_event(
            updated,
            "job.timeout_retry_scheduled",
            actor_owner_id=actor_owner_id,
            payload={
                "touchpoint": touchpoint,
                "retry_count": updated.get("retry_count"),
                "next_retry_at": updated.get("next_retry_at"),
            },
        )
        return updated

    return _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.timeout_terminal")


def _job_latency_ms(job: dict) -> float:
    try:
        created = datetime.fromisoformat(job["created_at"])
        completed = datetime.fromisoformat(job["completed_at"])
        return max(0.0, (completed - created).total_seconds() * 1000)
    except Exception:
        return 0.0


def _validate_json_schema_subset(payload: Any, schema: dict, path: str = "$") -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return []

    errors: list[str] = []
    schema_type = str(schema.get("type") or "").strip().lower()
    if not schema_type and isinstance(schema.get("properties"), dict):
        schema_type = "object"

    def _is_type(value: Any, expected: str) -> bool:
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "null":
            return value is None
        return True

    if schema_type:
        if not _is_type(payload, schema_type):
            errors.append(f"{path}: expected type '{schema_type}'")
            return errors

    if schema_type == "object":
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for field in required:
            key = str(field)
            if key not in payload:
                errors.append(f"{path}.{key}: required field missing")
        if isinstance(properties, dict):
            for key, field_schema in properties.items():
                if key in payload and isinstance(field_schema, dict):
                    errors.extend(
                        _validate_json_schema_subset(payload[key], field_schema, path=f"{path}.{key}")
                    )
        additional_properties = schema.get("additionalProperties")
        if additional_properties is False and isinstance(properties, dict):
            allowed = set(properties.keys())
            for key in payload.keys():
                if key not in allowed:
                    errors.append(f"{path}.{key}: additional property not allowed")
    elif schema_type == "array" and isinstance(payload, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, value in enumerate(payload):
                errors.extend(_validate_json_schema_subset(value, item_schema, path=f"{path}[{idx}]"))

    return errors


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


def _normalize_protocol_artifact_list(
    raw_value: Any,
    *,
    field_name: str,
    strict: bool = True,
) -> list[dict[str, Any]]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        if strict:
            raise ValueError(f"{field_name} must be an array of artifact objects.")
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_value):
        if not isinstance(item, dict):
            if strict:
                raise ValueError(f"{field_name}[{index}] must be an object.")
            continue
        artifact = dict(item)
        name = str(artifact.get("name") or "").strip()
        mime = str(artifact.get("mime") or "").strip().lower()
        locator = str(artifact.get("url_or_base64") or "").strip()
        size_raw = artifact.get("size_bytes")
        try:
            size_bytes = int(size_raw)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f"{field_name}[{index}].size_bytes must be a non-negative integer.")
            continue
        if strict and (not name or not mime or not locator or size_bytes < 0):
            raise ValueError(
                f"{field_name}[{index}] must include non-empty name/mime/url_or_base64 and non-negative size_bytes."
            )
        if not name or not mime or not locator or size_bytes < 0:
            continue
        artifact["name"] = name
        artifact["mime"] = mime
        artifact["url_or_base64"] = locator
        artifact["size_bytes"] = size_bytes
        normalized.append(artifact)
    return normalized


def _normalize_format_preferences(raw_value: Any, *, field_name: str) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise ValueError(f"{field_name} must be an array of MIME-like format strings.")
    normalized: list[str] = []
    for item in raw_value:
        text = str(item).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normalize_protocol_channel(raw_value: Any, *, field_name: str) -> str | None:
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    if len(text) > 128:
        raise ValueError(f"{field_name} must be <= 128 characters.")
    return text


def _normalize_protocol_metadata(raw_value: Any, *, field_name: str) -> dict[str, Any]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise ValueError(f"{field_name} must be an object.")
    return dict(raw_value)


def _normalize_optional_bool(raw_value: Any, *, field_name: str) -> bool | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        lowered = raw_value.strip().lower()
        if not lowered:
            return None
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    if isinstance(raw_value, (int, float)):
        if int(raw_value) == 1:
            return True
        if int(raw_value) == 0:
            return False
    raise ValueError(f"{field_name} must be a boolean.")


def _merge_protocol_input_envelope(
    payload: dict[str, Any],
    *,
    input_artifacts: list[dict[str, Any]] | None = None,
    preferred_input_formats: list[str] | None = None,
    preferred_output_formats: list[str] | None = None,
    communication_channel: str | None = None,
    protocol_metadata: dict[str, Any] | None = None,
    private_task: bool | None = None,
) -> dict[str, Any]:
    updated = dict(payload or {})
    current_protocol = updated.get("protocol")
    protocol = dict(current_protocol) if isinstance(current_protocol, dict) else {}
    if input_artifacts:
        protocol["input_artifacts"] = list(input_artifacts)
    if preferred_input_formats:
        protocol["preferred_input_formats"] = list(preferred_input_formats)
    if preferred_output_formats:
        protocol["preferred_output_formats"] = list(preferred_output_formats)
    if communication_channel:
        protocol["communication_channel"] = communication_channel
    if private_task is not None:
        protocol["private_task"] = bool(private_task)
    if protocol_metadata:
        existing_metadata = protocol.get("metadata")
        merged_metadata = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
        merged_metadata.update(protocol_metadata)
        protocol["metadata"] = merged_metadata
    if protocol:
        updated["protocol"] = protocol
    return updated


def _merge_protocol_output_envelope(
    payload: dict[str, Any],
    *,
    output_artifacts: list[dict[str, Any]] | None = None,
    output_format: str | None = None,
    protocol_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updated = dict(payload or {})
    current_protocol = updated.get("protocol")
    protocol = dict(current_protocol) if isinstance(current_protocol, dict) else {}
    if output_artifacts:
        protocol["output_artifacts"] = list(output_artifacts)
    if output_format:
        protocol["output_format"] = output_format
    if protocol_metadata:
        existing_metadata = protocol.get("metadata")
        merged_metadata = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
        merged_metadata.update(protocol_metadata)
        protocol["metadata"] = merged_metadata
    if protocol:
        updated["protocol"] = protocol
    return updated


def _normalize_input_protocol_from_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    protocol = payload.get("protocol")
    if not isinstance(protocol, dict):
        private_task = _normalize_optional_bool(payload.get("private_task"), field_name="private_task")
        if private_task is None:
            return dict(payload), []
        normalized_payload = _merge_protocol_input_envelope(
            payload,
            private_task=private_task,
        )
        return normalized_payload, []
    input_artifacts = _normalize_protocol_artifact_list(
        protocol.get("input_artifacts"),
        field_name="protocol.input_artifacts",
    )
    preferred_input_formats = _normalize_format_preferences(
        protocol.get("preferred_input_formats"),
        field_name="protocol.preferred_input_formats",
    )
    preferred_output_formats = _normalize_format_preferences(
        protocol.get("preferred_output_formats"),
        field_name="protocol.preferred_output_formats",
    )
    communication_channel = _normalize_protocol_channel(
        protocol.get("communication_channel"),
        field_name="protocol.communication_channel",
    )
    private_task = _normalize_optional_bool(
        protocol.get("private_task", payload.get("private_task")),
        field_name="protocol.private_task",
    )
    metadata = _normalize_protocol_metadata(protocol.get("metadata"), field_name="protocol.metadata")
    normalized_payload = _merge_protocol_input_envelope(
        payload,
        input_artifacts=input_artifacts,
        preferred_input_formats=preferred_input_formats,
        preferred_output_formats=preferred_output_formats,
        communication_channel=communication_channel,
        protocol_metadata=metadata,
        private_task=private_task,
    )
    return normalized_payload, preferred_output_formats


def _normalize_output_protocol_for_response(
    response_payload: Any,
    *,
    requested_output_formats: list[str] | None = None,
) -> Any:
    if not isinstance(response_payload, dict):
        return response_payload
    normalized = dict(response_payload)
    protocol = normalized.get("protocol")
    protocol_dict = dict(protocol) if isinstance(protocol, dict) else {}
    artifact_candidates = protocol_dict.get("output_artifacts")
    if not artifact_candidates:
        artifact_candidates = normalized.get("artifacts")
    output_artifacts = _normalize_protocol_artifact_list(
        artifact_candidates,
        field_name="protocol.output_artifacts",
        strict=False,
    )
    output_format = str(protocol_dict.get("output_format") or "").strip().lower() or None
    if output_format is None and output_artifacts:
        output_format = str(output_artifacts[0].get("mime") or "").strip().lower() or None
    metadata = _normalize_protocol_metadata(protocol_dict.get("metadata"), field_name="protocol.metadata")
    if requested_output_formats:
        metadata.setdefault("requested_output_formats", list(requested_output_formats))
    return _merge_protocol_output_envelope(
        normalized,
        output_artifacts=output_artifacts,
        output_format=output_format,
        protocol_metadata=metadata,
    )


def _is_private_task_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        private_top_level = _normalize_optional_bool(payload.get("private_task"), field_name="private_task")
    except ValueError:
        private_top_level = None
    if private_top_level is True:
        return True
    protocol = payload.get("protocol")
    if not isinstance(protocol, dict):
        return False
    try:
        private_protocol = _normalize_optional_bool(
            protocol.get("private_task"),
            field_name="protocol.private_task",
        )
    except ValueError:
        private_protocol = None
    return bool(private_protocol)


def _truncate_example_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= _AGENT_WORK_EXAMPLE_MAX_STRING_LEN:
            return value
        return value[:_AGENT_WORK_EXAMPLE_MAX_STRING_LEN] + "...<truncated>"
    if isinstance(value, list):
        return [_truncate_example_value(item) for item in value[:20]]
    if isinstance(value, dict):
        truncated: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            key_text = str(key)
            if key_text == "url_or_base64" and isinstance(item, str) and item.startswith("data:"):
                truncated[key_text] = "<inline-data-uri-omitted>"
                continue
            truncated[key_text] = _truncate_example_value(item)
        return truncated
    return value


def _extract_protocol_output_artifacts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    protocol = payload.get("protocol")
    protocol_dict = dict(protocol) if isinstance(protocol, dict) else {}
    artifact_candidates = protocol_dict.get("output_artifacts")
    if artifact_candidates is None:
        artifact_candidates = payload.get("artifacts")
    return _normalize_protocol_artifact_list(
        artifact_candidates,
        field_name="output_artifacts",
        strict=False,
    )


def _record_public_work_example(
    agent: dict,
    input_payload: Any,
    output_payload: Any,
    *,
    job_id: str | None = None,
    latency_ms: float | None = None,
    quality_score: int | None = None,
    rating: int | None = None,
) -> None:
    if not isinstance(agent, dict):
        return
    if _is_private_task_payload(input_payload):
        return
    if not isinstance(input_payload, dict) or not isinstance(output_payload, dict):
        return
    agent_id = str(agent.get("agent_id") or "").strip()
    if not agent_id:
        return
    artifacts = _extract_protocol_output_artifacts(output_payload)
    example: dict[str, Any] = {
        "created_at": _utc_now_iso(),
        "input": _truncate_example_value(input_payload),
        "output": _truncate_example_value(output_payload),
        "model_provider": str(agent.get("model_provider") or "").strip().lower() or None,
        "model_id": str(agent.get("model_id") or "").strip() or None,
    }
    if job_id:
        example["job_id"] = str(job_id)
    if latency_ms is not None:
        example["latency_ms"] = round(float(latency_ms), 1)
    if quality_score is not None:
        example["quality_score"] = int(quality_score)
    if rating is not None:
        example["rating"] = int(rating)
    if artifacts:
        example["artifacts"] = _truncate_example_value(artifacts)
    try:
        registry.append_agent_output_example(
            agent_id,
            example,
            max_examples=_AGENT_WORK_EXAMPLES_MAX,
        )
    except Exception:
        _LOG.exception("Failed to append output example for agent %s.", agent_id)


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

    raise ValueError(f"Unsupported job message type: {normalized_type}")


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

    raise ValueError(f"Unsupported job message type: {msg_type}")


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

    if msg_type == "agent_message":
        channel = str(normalized.get("channel") or "").strip()
        if not channel:
            raise ValueError("agent_message payload.channel is required.")
        normalized["channel"] = channel
        body = normalized.get("body")
        if isinstance(body, str):
            body_text = body.strip()
            if not body_text:
                raise ValueError("agent_message payload.body must not be empty.")
            normalized["body"] = body_text
        elif isinstance(body, dict):
            normalized["body"] = dict(body)
        else:
            raise ValueError("agent_message payload.body must be an object or non-empty string.")
        to_id = str(normalized.get("to_id") or "").strip()
        if to_id:
            normalized["to_id"] = to_id
        else:
            normalized.pop("to_id", None)
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
        except Exception as exc:
            _LOG.warning(
                "Failed to query tool-call correlation index for job %s correlation %s: %s",
                job_id,
                correlation_id,
                exc,
            )

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
    logging_utils.log_event(
        _LOG,
        logging.INFO,
        "job.state_transition",
        {
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "job_id": event.get("job_id"),
            "agent_id": event.get("agent_id"),
            "actor_owner_id": event.get("actor_owner_id"),
            "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
        },
    )
    _deliver_job_event_hooks(event)
    if event.get("event_type") in {"job.completed", "job.failed", "job.failed_quality"} and (job or {}).get("callback_url"):
        _enqueue_job_callback(job, event["event_id"])
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
                    replay_body = error_codes.make_error(
                        error_codes.INVALID_INPUT,
                        "Stored idempotent response is invalid.",
                    )
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
    return _url_security.validate_outbound_url(
        target_url,
        field_name,
        allow_private=_ALLOW_PRIVATE_OUTBOUND_URLS,
    )


def _validate_hook_url(target_url: str) -> str:
    return _validate_outbound_url(target_url, "target_url")


def _effective_port(scheme: str, port: int | None) -> int:
    if port is not None:
        return port
    return 443 if scheme == "https" else 80


def _allow_loopback_same_origin(request: Request, target_url: str) -> bool:
    parsed = urlparse(target_url.strip())
    target_host = (parsed.hostname or "").strip().lower()
    if target_host not in {"localhost", "127.0.0.1", "::1"}:
        return False

    request_host = (request.url.hostname or "").strip().lower()
    if request_host not in {"localhost", "127.0.0.1", "::1"}:
        return False

    target_scheme = (parsed.scheme or "").strip().lower()
    request_scheme = (request.url.scheme or "").strip().lower()
    if target_scheme != request_scheme:
        return False

    target_port = _effective_port(target_scheme, parsed.port)
    request_port = _effective_port(request_scheme, request.url.port)
    return target_port == request_port


def _validate_agent_endpoint_url(request: Request, endpoint_url: str) -> str:
    normalized = endpoint_url.strip()
    if _allow_loopback_same_origin(request, normalized):
        parsed = urlparse(normalized)
        if parsed.username or parsed.password:
            raise ValueError("endpoint_url must not include username or password.")
        if parsed.fragment:
            raise ValueError("endpoint_url must not include URL fragments.")
        return normalized
    return _validate_outbound_url(normalized, "endpoint_url")


def _probe_register_endpoint_or_400(url: str) -> None:
    """
    Best-effort liveness probe for an agent endpoint_url at registration time.
    Rejects URLs that fail to connect or return 5xx. 4xx responses are accepted
    (the endpoint is reachable; auth/route shape is validated elsewhere).
    """
    try:
        resp = http.head(url, timeout=5.0, allow_redirects=False)
        status = resp.status_code
        if status in (405, 501) or status >= 500:
            resp = http.get(url, timeout=5.0, allow_redirects=False)
            status = resp.status_code
        if status >= 500:
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.REGISTRY_ENDPOINT_UNREACHABLE,
                    f"Endpoint responded with status {status}. Provide a reachable URL.",
                ),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.REGISTRY_ENDPOINT_UNREACHABLE,
                f"Could not reach endpoint: {exc}",
            ),
        )


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
    now = _utc_now_iso()
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
        if result.rowcount <= 0:
            return False
        conn.execute(
            """
            UPDATE job_event_deliveries
            SET status = 'cancelled',
                next_attempt_at = ?,
                updated_at = ?,
                last_error = COALESCE(last_error, 'hook deactivated')
            WHERE hook_id = ?
              AND status = 'pending'
            """,
            (now, now, hook_id),
        )
    return True


def _deliver_job_event_hooks(event: dict) -> None:
    _enqueue_job_event_hook_deliveries(event)


def _set_hook_worker_state(**updates: Any) -> None:
    with _HOOK_WORKER_STATE_LOCK:
        _HOOK_WORKER_STATE.update(updates)


def _set_builtin_worker_state(**updates: Any) -> None:
    with _BUILTIN_WORKER_STATE_LOCK:
        _BUILTIN_WORKER_STATE.update(updates)


def _set_dispute_judge_state(**updates: Any) -> None:
    with _DISPUTE_JUDGE_STATE_LOCK:
        _DISPUTE_JUDGE_STATE.update(updates)


def _set_payments_reconciliation_state(**updates: Any) -> None:
    with _PAYMENTS_RECONCILIATION_STATE_LOCK:
        _PAYMENTS_RECONCILIATION_STATE.update(updates)


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _resolve_builtin_agent_id(agent: dict[str, Any]) -> str | None:
    endpoint = _normalize_endpoint_ref(str(agent.get("endpoint_url") or ""))
    matched = _BUILTIN_ENDPOINT_TO_AGENT_ID.get(endpoint)
    if matched:
        return matched
    agent_id = str(agent.get("agent_id") or "").strip()
    if agent_id in _BUILTIN_AGENT_IDS and endpoint.startswith("internal://"):
        return agent_id
    return None


def _execute_builtin_agent(agent_id: str, input_payload: dict[str, Any]) -> dict:
    payload = input_payload or {}
    if agent_id == _FINANCIAL_AGENT_ID:
        body = FinancialRequest.model_validate(payload)
        return _invoke_financial_agent(body)
    if agent_id == _CODEREVIEW_AGENT_ID:
        body = CodeReviewRequest.model_validate(payload)
        return _invoke_code_review_agent(body)
    if agent_id == _TEXTINTEL_AGENT_ID:
        body = TextIntelRequest.model_validate(payload)
        return _invoke_text_intel_agent(body)
    if agent_id == _WIKI_AGENT_ID:
        body = WikiRequest.model_validate(payload)
        return _invoke_wiki_agent(body)
    if agent_id == _NEGOTIATION_AGENT_ID:
        body = NegotiationRequest.model_validate(payload)
        return _invoke_negotiation_agent(body)
    if agent_id == _SCENARIO_AGENT_ID:
        body = ScenarioRequest.model_validate(payload)
        return _invoke_scenario_agent(body)
    if agent_id == _PRODUCT_AGENT_ID:
        body = ProductStrategyRequest.model_validate(payload)
        return _invoke_product_strategy_agent(body)
    if agent_id == _PORTFOLIO_AGENT_ID:
        body = PortfolioRequest.model_validate(payload)
        return _invoke_portfolio_agent(body)
    if agent_id == _QUALITY_JUDGE_AGENT_ID:
        return judges.run_quality_judgment(
            input_payload=payload.get("input_payload") if isinstance(payload, dict) else {},
            output_payload=payload.get("output_payload") if isinstance(payload, dict) else {},
            agent_description=str(payload.get("agent_description") or "") if isinstance(payload, dict) else "",
        )
    if agent_id == _SQLBUILDER_AGENT_ID:
        return agent_sqlbuilder.run(payload)
    if agent_id == _DATAINSIGHTS_AGENT_ID:
        return agent_datainsights.run(payload)
    if agent_id == _SECRETS_AGENT_ID:
        return agent_secrets_detection.run(payload)
    if agent_id == _STATICANALYSIS_AGENT_ID:
        return agent_static_analysis.run(payload)
    if agent_id == _DEPSCANNER_AGENT_ID:
        return agent_dependency_scanner.run(payload)
    if agent_id == _CVELOOKUP_AGENT_ID:
        return agent_cve_lookup.run(payload)
    if agent_id == _SYSTEM_DESIGN_AGENT_ID:
        return agent_system_design.run(payload)
    if agent_id == _INCIDENT_RESPONSE_AGENT_ID:
        return agent_incident_response.run(payload)
    if agent_id == _HEALTHCARE_EXPERT_AGENT_ID:
        return agent_healthcare_expert.run(payload)
    if agent_id == _IMAGE_GENERATOR_AGENT_ID:
        return agent_image_generator.run(payload)
    if agent_id == _VIDEO_STORYBOARD_AGENT_ID:
        return agent_video_storyboard.run(payload)
    if agent_id == _ARXIV_RESEARCH_AGENT_ID:
        return agent_arxiv_research.run(payload)
    if agent_id == _PYTHON_EXECUTOR_AGENT_ID:
        return agent_python_executor.run(payload)
    if agent_id == _WEB_RESEARCHER_AGENT_ID:
        return agent_web_researcher.run(payload)
    raise ValueError(f"Unsupported built-in agent '{agent_id}'.")


def _process_pending_builtin_job(job: dict) -> bool:
    claimed = jobs.claim_job(
        job["job_id"],
        claim_owner_id=_BUILTIN_WORKER_OWNER_ID,
        lease_seconds=_DEFAULT_LEASE_SECONDS,
        require_authorized_owner=False,
    )
    if claimed is None:
        return False

    _record_job_event(
        claimed,
        "job.claimed",
        actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
        payload={
            "lease_seconds": _DEFAULT_LEASE_SECONDS,
            "attempt_count": claimed["attempt_count"],
            "auto_worker": True,
        },
    )
    jobs.add_message(
        claimed["job_id"],
        from_id=_BUILTIN_WORKER_OWNER_ID,
        msg_type="progress",
        payload={"message": "Built-in worker started processing.", "percent": 5},
    )

    try:
        output = _execute_builtin_agent(
            str(claimed["agent_id"]),
            claimed.get("input_payload") or {},
        )
    except _groq.RateLimitError as exc:
        retried = jobs.schedule_job_retry(
            claimed["job_id"],
            retry_delay_seconds=_SWEEPER_RETRY_DELAY_SECONDS,
            error_message=f"Built-in worker rate-limited: {exc}",
            claim_owner_id=_BUILTIN_WORKER_OWNER_ID,
            claim_token=claimed.get("claim_token"),
            require_authorized_owner=False,
        )
        if retried is not None and retried["status"] == "pending":
            _record_job_event(
                retried,
                "job.retry_scheduled",
                actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                payload={"retry_count": retried["retry_count"], "next_retry_at": retried["next_retry_at"]},
            )
            return True
        updated = retried or jobs.update_job_status(
            claimed["job_id"],
            "failed",
            error_message=f"Built-in worker rate-limited: {exc}",
            completed=True,
        )
        if updated is not None:
            _settle_failed_job(
                updated,
                actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                event_type="job.failed_builtin",
            )
        return True
    except Exception as exc:
        updated = jobs.update_job_status(
            claimed["job_id"],
            "failed",
            error_message=f"Built-in execution failed: {exc}",
            completed=True,
        )
        if updated is not None:
            _settle_failed_job(
                updated,
                actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                event_type="job.failed_builtin",
            )
        return True

    jobs.add_message(
        claimed["job_id"],
        from_id=_BUILTIN_WORKER_OWNER_ID,
        msg_type="final_result",
        payload={"message": "Built-in worker completed successfully."},
    )
    agent = registry.get_agent(claimed["agent_id"], include_unapproved=True)
    if agent is not None:
        output_schema = agent.get("output_schema")
        if isinstance(output_schema, dict) and output_schema:
            mismatches = _validate_json_schema_subset(output, output_schema)
            if mismatches:
                updated = jobs.update_job_status(
                    claimed["job_id"],
                    "failed",
                    error_message=f"Output schema mismatch: {', '.join(mismatches[:3])}",
                    completed=True,
                )
                if updated is not None:
                    _settle_failed_job(
                        updated,
                        actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                        event_type="job.failed_schema",
                    )
                return True
        quality = _run_quality_gate(claimed, agent, output)
        jobs.set_job_quality_result(
            claimed["job_id"],
            judge_verdict=quality["judge_verdict"],
            quality_score=quality["quality_score"],
            judge_agent_id=quality["judge_agent_id"],
        )
        if not quality["passed"]:
            updated = jobs.update_job_status(
                claimed["job_id"],
                "failed",
                error_message=f"Quality judge failed: {quality['reason']}",
                completed=True,
            )
            if updated is not None:
                _settle_failed_job(
                    updated,
                    actor_owner_id=_BUILTIN_WORKER_OWNER_ID,
                    event_type="job.failed_quality",
                )
            return True
    completed = jobs.update_job_status(
        claimed["job_id"],
        "complete",
        output_payload=output,
        completed=True,
    )
    if completed is not None:
        settled = _settle_successful_job(completed, actor_owner_id=_BUILTIN_WORKER_OWNER_ID)
        if agent is not None:
            distribution = payments.compute_success_distribution(
                int(completed.get("price_cents") or 0),
                platform_fee_pct=completed.get("platform_fee_pct_at_create"),
                fee_bearer_policy=completed.get("fee_bearer_policy"),
            )
            platform_fee_cents = int(distribution["platform_fee_cents"])
            judge_fee_cents = min(_JUDGE_FEE_CENTS, platform_fee_cents)
            if judge_fee_cents > 0:
                judge_agent_id = str(settled.get("judge_agent_id") or _QUALITY_JUDGE_AGENT_ID)
                judge_wallet = payments.get_or_create_wallet(f"agent:{judge_agent_id}")
                payments.record_judge_fee(
                    completed["platform_wallet_id"],
                    judge_wallet["wallet_id"],
                    charge_tx_id=completed["charge_tx_id"],
                    agent_id=completed["agent_id"],
                    fee_cents=judge_fee_cents,
                )
    return True


def _process_pending_builtin_jobs(limit_per_agent: int = _BUILTIN_JOB_WORKER_BATCH_SIZE) -> dict[str, int]:
    batch_limit = min(max(1, int(limit_per_agent)), 500)
    scanned = 0
    processed = 0
    for agent_id in _BUILTIN_AGENT_IDS:
        pending = jobs.list_jobs_for_agent(
            agent_id,
            status="pending",
            limit=batch_limit,
        )
        scanned += len(pending)
        for job in pending:
            if _process_pending_builtin_job(job):
                processed += 1
    return {"scanned": scanned, "processed": processed}


def _builtin_worker_loop(stop_event: threading.Event) -> None:
    _set_builtin_worker_state(running=True, started_at=_utc_now_iso())
    while not stop_event.wait(_BUILTIN_JOB_WORKER_INTERVAL_SECONDS):
        started = _utc_now_iso()
        try:
            summary = _process_pending_builtin_jobs(limit_per_agent=_BUILTIN_JOB_WORKER_BATCH_SIZE)
            _set_builtin_worker_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
        except Exception as exc:
            _LOG.exception("Built-in worker loop failed.")
            _set_builtin_worker_state(
                last_run_at=started,
                last_error=str(exc),
            )
    _set_builtin_worker_state(running=False)


_TIE_TIMEOUT_HOURS = 48
_TIE_TIMEOUT_REASONING = (
    "Judges tied after two rounds. Defaulting to caller per platform policy."
)


def _run_pending_dispute_judgments(limit: int = 100, actor_owner_id: str = "system:dispute-judge") -> dict:
    capped = min(max(1, int(limit)), 500)
    pending = disputes.list_disputes(status="pending", limit=capped)
    judged_count = 0
    resolved_count = 0
    tied_count = 0
    tie_timeout_count = 0
    errors: list[dict[str, str]] = []
    processed_ids: list[str] = []
    resolved_ids: list[str] = []
    tied_ids: list[str] = []

    for dispute_row in pending:
        dispute_id = str(dispute_row.get("dispute_id") or "").strip()
        if not dispute_id:
            continue
        try:
            latest, _ = _resolve_dispute_with_judges(dispute_id, actor_owner_id=actor_owner_id)
        except Exception as exc:
            errors.append({"dispute_id": dispute_id, "error": str(exc)})
            continue
        judged_count += 1
        processed_ids.append(dispute_id)
        status = str(latest.get("status") or "").strip().lower()
        if status == "resolved":
            resolved_count += 1
            resolved_ids.append(dispute_id)
        elif status == "tied":
            tied_count += 1
            tied_ids.append(dispute_id)

    # Auto-rule tied disputes older than _TIE_TIMEOUT_HOURS in favour of caller.
    stale_tied = disputes.get_stale_tied_disputes(older_than_hours=_TIE_TIMEOUT_HOURS, limit=capped)
    for dispute_row in stale_tied:
        dispute_id = str(dispute_row.get("dispute_id") or "").strip()
        if not dispute_id:
            continue
        try:
            disputes.record_judgment(
                dispute_id,
                judge_kind="human_admin",
                verdict="caller_wins",
                reasoning=_TIE_TIMEOUT_REASONING,
                model=None,
                admin_user_id="system_tie_timeout",
            )
            payments.post_dispute_settlement(
                dispute_id,
                outcome="caller_wins",
            )
            finalized = disputes.finalize_dispute(
                dispute_id,
                status="final",
                outcome="caller_wins",
            )
            if finalized is not None:
                _apply_dispute_effects(finalized, "caller_wins")
                job = jobs.get_job(finalized["job_id"])
                if job is not None:
                    _record_job_event(
                        job,
                        "job.dispute_finalized",
                        actor_owner_id=actor_owner_id,
                        payload={"dispute_id": dispute_id, "outcome": "caller_wins", "reason": "tie_timeout"},
                    )
            tie_timeout_count += 1
            _LOG.warning(
                "Tie-timeout auto-ruling: dispute %s → caller_wins after %dh",
                dispute_id,
                _TIE_TIMEOUT_HOURS,
            )
        except Exception as exc:
            errors.append({"dispute_id": dispute_id, "error": f"tie_timeout: {exc}"})

    return {
        "pending_scanned": len(pending),
        "judged_count": judged_count,
        "resolved_count": resolved_count,
        "tied_count": tied_count,
        "tie_timeout_count": tie_timeout_count,
        "failed_count": len(errors),
        "processed_dispute_ids": processed_ids,
        "resolved_dispute_ids": resolved_ids,
        "tied_dispute_ids": tied_ids,
        "errors": errors,
    }


def _dispute_judge_loop(stop_event: threading.Event) -> None:
    _set_dispute_judge_state(running=True, started_at=_utc_now_iso())
    while not stop_event.wait(_DISPUTE_JUDGE_INTERVAL_SECONDS):
        started = _utc_now_iso()
        try:
            summary = _run_pending_dispute_judgments(actor_owner_id="system:dispute-judge")
            _set_dispute_judge_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
        except Exception as exc:
            _LOG.exception("Dispute judge loop failed.")
            _set_dispute_judge_state(
                last_run_at=started,
                last_error=str(exc),
            )
    _set_dispute_judge_state(running=False)


def _run_agent_health_checks() -> dict:
    """Check health endpoints of all external agents that have a healthcheck_url."""
    agents_to_check = registry.get_agents(include_internal=False)
    checked = 0
    healthy = 0
    unhealthy = 0
    for agent in agents_to_check:
        url = (agent.get("healthcheck_url") or "").strip()
        if not url:
            continue
        try:
            validated_url = _validate_outbound_url(url, "healthcheck_url")
            import httpx as _httpx
            resp = _httpx.get(validated_url, timeout=10, follow_redirects=True)
            status = "healthy" if 200 <= resp.status_code < 300 else "unhealthy"
        except Exception:
            status = "unhealthy"
        registry.update_agent_health(agent["agent_id"], status, _utc_now_iso())
        checked += 1
        if status == "healthy":
            healthy += 1
        else:
            unhealthy += 1
    return {"checked": checked, "healthy": healthy, "unhealthy": unhealthy}


def _agent_health_loop(stop_event: threading.Event) -> None:
    while not stop_event.wait(_AGENT_HEALTH_CHECK_INTERVAL_SECONDS):
        try:
            _run_agent_health_checks()
        except Exception:
            _LOG.exception("Agent health check loop failed.")


def _payments_reconciliation_loop(stop_event: threading.Event) -> None:
    _set_payments_reconciliation_state(running=True, started_at=_utc_now_iso())
    while not stop_event.is_set():
        started = _utc_now_iso()
        try:
            summary = payments.record_reconciliation_run(
                max_mismatches=_PAYMENTS_RECONCILIATION_MAX_MISMATCHES
            )
            _set_payments_reconciliation_state(
                last_run_at=started,
                last_summary=summary,
                last_error=None,
            )
            if not bool(summary.get("invariant_ok")):
                logging_utils.log_event(
                    _LOG,
                    logging.ERROR,
                    "payments.reconciliation_invariant_failed",
                    {
                        "run_id": summary.get("run_id"),
                        "drift_cents": summary.get("drift_cents"),
                        "mismatch_count": summary.get("mismatch_count"),
                    },
                )
        except Exception as exc:
            _LOG.exception("Payments reconciliation loop failed.")
            _set_payments_reconciliation_state(
                last_run_at=started,
                last_error=str(exc),
            )
        if stop_event.wait(_PAYMENTS_RECONCILIATION_INTERVAL_SECONDS):
            break
    _set_payments_reconciliation_state(running=False)


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


_JOB_CALLBACK_HOOK_PREFIX = "callback:"


def _enqueue_job_callback(job: dict, event_id: int) -> None:
    """Enqueue a one-time push delivery to job.callback_url on terminal state."""
    callback_url = (job.get("callback_url") or "").strip()
    if not callback_url:
        return
    try:
        safe_url = _validate_hook_url(callback_url)
    except ValueError:
        return

    hook_id = f"{_JOB_CALLBACK_HOOK_PREFIX}{job['job_id']}"
    payload = {
        "job_id": job["job_id"],
        "agent_id": job.get("agent_id"),
        "status": job.get("status"),
        "output_payload": job.get("output_payload"),
        "error_message": job.get("error_message"),
        "completed_at": job.get("completed_at"),
        "settled_at": job.get("settled_at"),
        "price_cents": job.get("price_cents"),
    }
    now = _utc_now_iso()
    with jobs._conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO job_event_deliveries
                (event_id, hook_id, owner_id, target_url, secret, payload,
                 status, attempt_count, next_attempt_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (
                event_id,
                hook_id,
                job.get("caller_owner_id", ""),
                safe_url,
                (job.get("callback_secret") or "").strip() or None,
                json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str),
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
            WHERE status = 'pending'
              AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC, delivery_id ASC
            LIMIT 1
            """,
            (now_iso,),
        ).fetchone()
        if row is None:
            return None

        claim_until_iso = (
            datetime.fromisoformat(now_iso) + timedelta(seconds=_HOOK_DELIVERY_CLAIM_LEASE_SECONDS)
        ).isoformat()
        result = conn.execute(
            """
            UPDATE job_event_deliveries
            SET next_attempt_at = ?,
                last_attempt_at = ?,
                updated_at = ?
            WHERE delivery_id = ?
              AND status = 'pending'
              AND next_attempt_at <= ?
            """,
            (claim_until_iso, now_iso, now_iso, row["delivery_id"], now_iso),
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
    attempt_count: int | None = None,
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
                attempt_count = COALESCE(?, attempt_count),
                last_status_code = ?,
                last_error = ?,
                last_success_at = CASE WHEN ? = 1 THEN ? ELSE last_success_at END,
                updated_at = ?
            WHERE delivery_id = ?
            """,
            (
                status,
                next_attempt_at,
                attempt_count,
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
    failed = 0
    cancelled = 0

    for _ in range(batch_limit):
        now_iso = _utc_now_iso()
        delivery = _claim_due_hook_delivery(now_iso)
        if delivery is None:
            break

        processed += 1
        delivery_id = int(delivery["delivery_id"])
        hook_id = str(delivery["hook_id"])
        attempt_count = int(delivery["attempt_count"])

        is_job_callback = hook_id.startswith(_JOB_CALLBACK_HOOK_PREFIX)
        if not is_job_callback:
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
                    status="cancelled",
                    next_attempt_at=now_iso,
                    attempt_count=attempt_count,
                    status_code=None,
                    error_text=error_text,
                    now_iso=now_iso,
                    mark_success=False,
                )
                cancelled += 1
                continue

        try:
            safe_target_url = _validate_hook_url(str(delivery["target_url"]))
        except ValueError as exc:
            error_text = f"Blocked unsafe hook target: {exc}"
            if not is_job_callback:
                _update_hook_attempt_metadata(
                    hook_id=hook_id,
                    attempted_at=now_iso,
                    success=False,
                    status_code=None,
                    error_text=error_text,
                )
            _mark_hook_delivery(
                delivery_id,
                status="failed" if (attempt_count + 1) >= _HOOK_DELIVERY_MAX_ATTEMPTS else "pending",
                next_attempt_at=(
                    now_iso
                    if (attempt_count + 1) >= _HOOK_DELIVERY_MAX_ATTEMPTS
                    else (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=_hook_backoff_seconds(attempt_count + 1))
                    ).isoformat()
                ),
                attempt_count=attempt_count + 1,
                status_code=None,
                error_text=error_text,
                now_iso=now_iso,
                mark_success=False,
            )
            if (attempt_count + 1) >= _HOOK_DELIVERY_MAX_ATTEMPTS:
                failed += 1
            else:
                retried += 1
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
            "X-Aztea-Event-Id": str(delivery["event_id"]),
            "X-Aztea-Event-Type": str(payload.get("event_type") or "unknown"),
        }
        secret = (delivery.get("secret") or "").strip()
        if secret:
            digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
            headers["X-Aztea-Signature"] = f"sha256={digest}"

        status_code = None
        error_text = None
        success = False
        try:
            resp = http.post(
                safe_target_url,
                data=payload_bytes,
                headers=headers,
                timeout=5,
                allow_redirects=False,
            )
            status_code = int(resp.status_code)
            success = 200 <= status_code < 300
            if not success:
                error_text = f"Non-2xx status: {status_code}"
        except http.RequestException as exc:
            error_text = str(exc)

        if not is_job_callback:
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
                attempt_count=attempt_count,
                status_code=status_code,
                error_text=None,
                now_iso=now_iso,
                mark_success=True,
            )
            delivered += 1
            continue

        next_attempt_count = attempt_count + 1
        if next_attempt_count >= _HOOK_DELIVERY_MAX_ATTEMPTS:
            _mark_hook_delivery(
                delivery_id,
                status="failed",
                next_attempt_at=now_iso,
                attempt_count=next_attempt_count,
                status_code=status_code,
                error_text=error_text,
                now_iso=now_iso,
                mark_success=False,
            )
            failed += 1
            continue

        retry_delay = _hook_backoff_seconds(next_attempt_count)
        next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_delay)).isoformat()
        _mark_hook_delivery(
            delivery_id,
            status="pending",
            next_attempt_at=next_attempt_at,
            attempt_count=next_attempt_count,
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
            WHERE status = 'pending'
            """
        ).fetchone()["count"]
        failed_total = conn.execute(
            "SELECT COUNT(*) AS count FROM job_event_deliveries WHERE status = 'failed'"
        ).fetchone()["count"]

    return {
        "processed": int(processed),
        "delivered": int(delivered),
        "retried": int(retried),
        "failed": int(failed),
        "cancelled": int(cancelled),
        "dead_lettered": int(failed),
        "pending": int(pending),
        "failed_total": int(failed_total),
        "dead_letter_total": int(failed_total),
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
            _LOG.exception("Hook delivery loop failed.")
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


def _run_output_verifier(
    verifier_url: str | None,
    *,
    job: dict,
    output_payload: dict,
    timeout_seconds: int = 10,
) -> tuple[bool, str]:
    target = str(verifier_url or "").strip()
    if not target:
        return True, "no external verifier configured"
    try:
        safe_url = _validate_outbound_url(target, "output_verifier_url")
    except ValueError as exc:
        return False, f"invalid verifier url: {exc}"
    payload = {
        "job_id": job["job_id"],
        "agent_id": job["agent_id"],
        "input_payload": job.get("input_payload") or {},
        "output_payload": output_payload,
    }
    try:
        response = http.post(
            safe_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_seconds,
            allow_redirects=False,
        )
        if 300 <= int(response.status_code) < 400:
            return False, "external verifier redirects are not allowed"
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        _LOG.warning("External verifier failed for job %s: %s", job.get("job_id"), exc)
        return False, "external verifier request failed"
    if not isinstance(body, dict):
        return False, "external verifier returned non-object response"
    if bool(body.get("verified")):
        return True, "external verifier passed"
    return False, str(body.get("reason") or "external verifier returned verified=false")


def _run_registration_verifier(
    verifier_url: str | None,
    *,
    registration_payload: dict[str, Any],
    timeout_seconds: int = 10,
) -> tuple[bool, str]:
    target = str(verifier_url or "").strip()
    if not target:
        return False, "no verifier configured"
    try:
        safe_url = _validate_outbound_url(target, "output_verifier_url")
    except ValueError as exc:
        return False, f"invalid verifier url: {exc}"
    payload = {
        "event_type": "agent_registration_verification",
        "agent": registration_payload,
    }
    try:
        response = http.post(
            safe_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_seconds,
            allow_redirects=False,
        )
        if 300 <= int(response.status_code) < 400:
            return False, "registration verifier redirects are not allowed"
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        _LOG.warning("Agent registration verifier request failed for %s: %s", registration_payload.get("name"), exc)
        return False, "registration verifier request failed"
    if not isinstance(body, dict):
        return False, "registration verifier returned non-object response"
    if bool(body.get("verified")):
        return True, str(body.get("reason") or "registration verifier passed")
    return False, str(body.get("reason") or "registration verifier returned verified=false")


def _timeout_error_payload(job_payload: dict) -> dict:
    return error_codes.make_error(
        error_codes.AGENT_TIMEOUT,
        "Job lease expired before completion.",
        {"job": job_payload},
    )


def _run_quality_gate(job: dict, agent: dict, output_payload: dict) -> dict[str, Any]:
    judge_agent_id = str(job.get("judge_agent_id") or _QUALITY_JUDGE_AGENT_ID).strip() or _QUALITY_JUDGE_AGENT_ID
    judge_job_id: str | None = None
    try:
        judge_agent = registry.get_agent(judge_agent_id)
        platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
        judge_wallet = payments.get_or_create_wallet(f"agent:{judge_agent_id}")
        child_charge_tx = payments.pre_call_charge(platform_wallet["wallet_id"], 0, judge_agent_id)
        child = jobs.create_job(
            agent_id=judge_agent_id,
            caller_owner_id="system:quality-judge",
            caller_wallet_id=platform_wallet["wallet_id"],
            agent_wallet_id=judge_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            price_cents=0,
            charge_tx_id=child_charge_tx,
            input_payload={
                "parent_job_id": job["job_id"],
                "input_payload": job.get("input_payload") or {},
                "output_payload": output_payload,
                "agent_description": str(agent.get("description") or ""),
            },
            agent_owner_id=(judge_agent or {}).get("owner_id") or "master",
            max_attempts=1,
            parent_job_id=job["job_id"],
            parent_cascade_policy="detach",
            dispute_window_hours=1,
            judge_agent_id=None,
        )
        judge_job_id = child["job_id"]
    except Exception:
        judge_job_id = None

    output_schema = agent.get("output_schema")
    has_output_schema = output_schema is not None
    live_quality_toggle = (
        os.environ.get("AZTEA_ENABLE_LIVE_QUALITY_JUDGE")
        or os.environ.get("AGENTMARKET_ENABLE_LIVE_QUALITY_JUDGE")
        or ""
    )
    live_quality_enabled = (
        str(live_quality_toggle).strip().lower() in {"1", "true", "yes", "on"}
        and bool(str(os.environ.get("GROQ_API_KEY", "")).strip())
    )

    verdict = "pass"
    score = 5
    reason = "No output contract defined — structural check passed."
    parsed_output: Any
    try:
        parsed_output = json.loads(_stable_json_text(output_payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed_output = None
        verdict = "fail"
        score = 0
        reason = "Output payload was not valid JSON."

    if verdict == "pass" and (parsed_output is None or parsed_output == {}):
        verdict = "fail"
        score = 0
        reason = "Output payload must not be null or an empty object."

    if verdict == "pass" and has_output_schema and isinstance(output_schema, dict):
        schema_errors = _validate_json_schema_subset(parsed_output, output_schema)
        if schema_errors:
            verdict = "fail"
            score = 0
            reason = f"Output did not match declared schema: {schema_errors[0]}"
        else:
            reason = "Output matched declared schema and structural checks."

    if verdict == "pass" and live_quality_enabled:
        try:
            judge_result = judges.run_quality_judgment(
                input_payload=job.get("input_payload") or {},
                output_payload=output_payload,
                agent_description=str(agent.get("description") or ""),
            )
            judge_verdict = str(judge_result.get("verdict") or "").strip().lower()
            if judge_verdict in {"pass", "fail"}:
                verdict = judge_verdict
            else:
                verdict = "fail"
            try:
                score = int(judge_result.get("score"))
            except (TypeError, ValueError):
                score = 1 if verdict == "fail" else 5
            score = max(0, min(10, score))
            reason = str(judge_result.get("reason") or "").strip() or "Quality judge returned no reason."
        except Exception as exc:
            verdict = "fail"
            score = 0
            reason = f"quality judge error: {exc}"

    verifier_passed, verifier_reason = _run_output_verifier(
        agent.get("output_verifier_url"),
        job=job,
        output_payload=output_payload,
    )
    if verdict == "pass" and not verifier_passed:
        verdict = "fail"
        reason = f"{reason} External verifier: {verifier_reason}"

    if judge_job_id is not None:
        child_output = {"verdict": verdict, "score": score, "reason": reason}
        child_complete = jobs.update_job_status(judge_job_id, "complete", output_payload=child_output, completed=True)
        if child_complete is not None:
            jobs.mark_settled(judge_job_id)

    passed = verdict == "pass"
    return {
        "judge_agent_id": judge_agent_id,
        "judge_job_id": judge_job_id,
        "judge_verdict": verdict,
        "quality_score": score,
        "reason": reason,
        "passed": passed,
        "verifier_reason": verifier_reason,
    }


def _apply_dispute_effects(dispute: dict, outcome: str) -> None:
    normalized_outcome = str(outcome or "").strip().lower()
    current_job = jobs.get_job(dispute["job_id"])
    was_settled = bool((current_job or {}).get("settled_at"))
    previous_outcome = str((current_job or {}).get("dispute_outcome") or "").strip().lower()
    job = jobs.set_job_dispute_outcome(dispute["job_id"], normalized_outcome)
    if job is None:
        return
    if not was_settled:
        jobs.mark_settled(dispute["job_id"])
        job = jobs.get_job(dispute["job_id"]) or job
    if normalized_outcome == "caller_wins" and previous_outcome != "caller_wins":
        registry.update_call_stats(job["agent_id"], latency_ms=0.0, success=False)
    elif normalized_outcome in {"agent_wins", "split", "void"} and not was_settled:
        registry.update_call_stats(job["agent_id"], latency_ms=_job_latency_ms(job), success=True)

    filed_by = str(dispute.get("filed_by_owner_id") or "").strip()
    if filed_by.startswith("user:") and dispute.get("side") == "caller" and normalized_outcome == "agent_wins":
        payments.adjust_caller_trust_once(
            filed_by,
            delta=-0.05,
            reason="dispute_loss",
            related_id=dispute["dispute_id"],
        )


def _fail_open_jobs_for_agent(agent_id: str, actor_owner_id: str, reason: str) -> dict[str, int]:
    affected = 0
    refunded = 0
    for status in ("pending", "running", "awaiting_clarification"):
        open_jobs = jobs.list_jobs_for_agent(agent_id, status=status, limit=500)
        for item in open_jobs:
            updated = jobs.update_job_status(
                item["job_id"],
                "failed",
                error_message=reason,
                completed=True,
            )
            if updated is None:
                continue
            affected += 1
            settled = _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.failed_agent_banned")
            if settled.get("settled_at"):
                refunded += 1
    return {"affected_jobs": affected, "refunded_jobs": refunded}


def _normalize_output_verification_status(job: dict) -> str:
    status = str(job.get("output_verification_status") or "").strip().lower()
    if status in {"pending", "accepted", "rejected", "expired"}:
        return status
    return "not_required"


def _ensure_output_rejection_dispute(
    job: dict,
    *,
    filed_by_owner_id: str,
    reason: str,
    evidence: str | None = None,
) -> dict:
    existing = disputes.get_dispute_by_job(job["job_id"])
    if existing is not None:
        return existing

    conn = payments._conn()
    filing_deposit_cents = _compute_dispute_filing_deposit_cents(int(job.get("price_cents") or 0))
    insufficient_phase = "dispute_create"
    try:
        conn.execute("BEGIN IMMEDIATE")
        created = disputes.create_dispute(
            job_id=job["job_id"],
            filed_by_owner_id=filed_by_owner_id,
            side="caller",
            reason=reason,
            evidence=evidence,
            filing_deposit_cents=filing_deposit_cents,
            conn=conn,
        )
        insufficient_phase = "filing_deposit"
        payments.collect_dispute_filing_deposit(
            created["dispute_id"],
            filed_by_owner_id=filed_by_owner_id,
            amount_cents=filing_deposit_cents,
            conn=conn,
        )
        insufficient_phase = "clawback_lock"
        payments.lock_dispute_funds(created["dispute_id"], conn=conn)
        conn.execute("COMMIT")
        return created
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        existing = disputes.get_dispute_by_job(job["job_id"])
        if existing is not None:
            return existing
        raise HTTPException(status_code=409, detail="A dispute already exists for this job.")
    except ValueError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=400, detail=str(exc))
    except payments.InsufficientBalanceError as exc:
        conn.execute("ROLLBACK")
        error_code = (
            error_codes.DISPUTE_FILING_DEPOSIT_INSUFFICIENT_BALANCE
            if insufficient_phase == "filing_deposit"
            else error_codes.DISPUTE_CLAWBACK_INSUFFICIENT_BALANCE
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": error_code,
                "balance_cents": exc.balance_cents,
                "required_cents": exc.required_cents,
            },
        )


def _cascade_fail_active_child_jobs(parent_job: dict, actor_owner_id: str) -> dict[str, Any]:
    active_children = jobs.list_child_jobs(
        parent_job["job_id"],
        statuses=("pending", "running", "awaiting_clarification"),
        limit=500,
    )
    failed_child_job_ids: list[str] = []
    for child in active_children:
        policy = str(child.get("parent_cascade_policy") or "").strip().lower() or "detach"
        if policy != "fail_children_on_parent_fail":
            continue
        updated = jobs.update_job_status(
            child["job_id"],
            "failed",
            error_message=f"Parent job {parent_job['job_id']} failed; child was cascaded.",
            completed=True,
        )
        if updated is None:
            continue
        settled_child = _settle_failed_job(
            updated,
            actor_owner_id=actor_owner_id,
            event_type="job.failed_parent_cascade",
            refund_fraction=1.0,
        )
        failed_child_job_ids.append(settled_child["job_id"])
    return {
        "scanned_children": len(active_children),
        "failed_children": len(failed_child_job_ids),
        "failed_child_job_ids": failed_child_job_ids,
    }


def _effective_dispute_window_seconds(job: dict) -> int:
    dispute_window_hours = _to_non_negative_int(
        job.get("dispute_window_hours"),
        default=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
    )
    if dispute_window_hours < 1:
        dispute_window_hours = _DEFAULT_JOB_DISPUTE_WINDOW_HOURS
    configured_window_seconds = dispute_window_hours * 3600
    return min(configured_window_seconds, _DISPUTE_FILE_WINDOW_SECONDS)


def _dispute_window_deadline(job: dict) -> datetime | None:
    completed_at = _parse_iso_datetime(job.get("completed_at"))
    if completed_at is None:
        return None
    return completed_at + timedelta(seconds=_effective_dispute_window_seconds(job))


def _is_dispute_window_open(job: dict, *, now_dt: datetime | None = None) -> bool:
    deadline = _dispute_window_deadline(job)
    if deadline is None:
        return False
    current = now_dt or datetime.now(timezone.utc)
    return current <= deadline


def _settle_successful_job(
    job: dict,
    actor_owner_id: str,
    *,
    require_dispute_window_expiry: bool = True,
) -> dict:
    newly_settled = False
    refreshed = jobs.initialize_output_verification_state(job["job_id"])
    if refreshed is not None:
        job = refreshed
    if disputes.has_dispute_for_job(job["job_id"]):
        return jobs.get_job(job["job_id"]) or job
    verification_status = _normalize_output_verification_status(job)
    if verification_status == "pending":
        return jobs.get_job(job["job_id"]) or job
    if verification_status == "rejected":
        return jobs.get_job(job["job_id"]) or job
    # Explicit caller acceptance should release funds immediately; only implicit acceptance
    # paths remain gated by the dispute window timeout.
    if require_dispute_window_expiry and _is_dispute_window_open(job):
        return jobs.get_job(job["job_id"]) or job
    if not job["settled_at"]:
        payments.post_call_payout(
            job["agent_wallet_id"],
            job["platform_wallet_id"],
            job["charge_tx_id"],
            job["price_cents"],
            job["agent_id"],
            platform_fee_pct=job.get("platform_fee_pct_at_create"),
            fee_bearer_policy=job.get("fee_bearer_policy"),
        )
        newly_settled = jobs.mark_settled(job["job_id"])
        if newly_settled:
            registry.update_call_stats(job["agent_id"], latency_ms=_job_latency_ms(job), success=True)
    settled = jobs.get_job(job["job_id"]) or job
    if newly_settled:
        _record_job_event(
            settled,
            "job.settled",
            actor_owner_id=actor_owner_id,
            payload={"status": settled["status"], "settled_at": settled.get("settled_at")},
        )
    return settled


def _settle_failed_job(
    job: dict,
    actor_owner_id: str,
    event_type: str = "job.failed",
    refund_fraction: float = 1.0,
) -> dict:
    newly_settled = False
    if not job["settled_at"]:
        refund_fraction = max(0.0, min(1.0, float(refund_fraction)))
        if refund_fraction >= 1.0:
            # Full refund — original fast path
            payments.post_call_refund(
                job["caller_wallet_id"],
                job["charge_tx_id"],
                int(job.get("caller_charge_cents") or job["price_cents"]),
                job["agent_id"],
            )
        else:
            # Partial settle: refund fraction to caller, keep rest for agent
            payments.post_call_partial_settle(
                caller_wallet_id=job["caller_wallet_id"],
                agent_wallet_id=job["agent_wallet_id"],
                platform_wallet_id=job["platform_wallet_id"],
                charge_tx_id=job["charge_tx_id"],
                price_cents=job["price_cents"],
                refund_fraction=refund_fraction,
                agent_id=job["agent_id"],
                platform_fee_pct=job.get("platform_fee_pct_at_create"),
                fee_bearer_policy=job.get("fee_bearer_policy"),
                caller_charge_cents=job.get("caller_charge_cents"),
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
        try:
            caller_email = _get_owner_email(settled.get("caller_owner_id", ""))
            if caller_email:
                _agent_name = (registry.get_agent(settled["agent_id"]) or {}).get("name", settled["agent_id"])
                _email.send_job_failed(caller_email, settled["job_id"], _agent_name, settled.get("error_message") or "")
        except Exception as exc:
            _LOG.warning("Failed to send job failure email for job %s: %s", settled.get("job_id"), exc)
    if (
        str(settled.get("status") or "").strip().lower() == "failed"
    ):
        _cascade_fail_active_child_jobs(settled, actor_owner_id=actor_owner_id)
    return settled


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
    if _caller_worker_authorized_for_job(caller, job):
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
        latest_dispute = disputes.get_dispute(dispute_id)
        if latest_dispute is not None:
            _apply_dispute_effects(latest_dispute, outcome)
    elif status == "tied":
        disputes.set_dispute_tied(dispute_id)

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


def _apply_reputation_decay(now_dt: datetime | None = None) -> dict[str, int]:
    current = now_dt or datetime.now(timezone.utc)
    scanned = 0
    decayed = 0
    with jobs._conn() as conn:
        rows = conn.execute(
            """
            SELECT
                a.agent_id,
                a.trust_decay_multiplier,
                a.last_decay_at,
                a.total_calls,
                MAX(j.completed_at) AS last_completed_at,
                a.created_at
            FROM agents a
            LEFT JOIN jobs j
              ON j.agent_id = a.agent_id
             AND j.status = 'complete'
             AND j.completed_at IS NOT NULL
            WHERE a.status = 'active'
            GROUP BY a.agent_id
            """
        ).fetchall()
    for row in rows:
        scanned += 1
        # Skip decay when there isn't enough signal — penalizing new agents is unfair.
        if (row["total_calls"] or 0) < 20:
            continue
        reference = _parse_iso_datetime(row["last_completed_at"]) or _parse_iso_datetime(row["created_at"])
        if reference is None:
            continue
        decay_threshold = reference + timedelta(days=_REPUTATION_DECAY_GRACE_DAYS)
        if current <= decay_threshold:
            continue
        last_decay_at = _parse_iso_datetime(row["last_decay_at"]) or decay_threshold
        start = decay_threshold if last_decay_at < decay_threshold else last_decay_at
        elapsed_days = int((current - start).total_seconds() // 86400)
        if elapsed_days <= 0:
            continue
        current_multiplier = max(0.0, min(1.0, float(row["trust_decay_multiplier"] or 1.0)))
        new_multiplier = current_multiplier * ((1.0 - _REPUTATION_DECAY_DAILY_RATE) ** elapsed_days)
        new_multiplier = max(0.0, min(1.0, new_multiplier))
        if new_multiplier >= current_multiplier:
            continue
        registry.set_agent_decay_multiplier(row["agent_id"], new_multiplier, current.isoformat())
        decayed += 1
    return {"scanned_agents": scanned, "decayed_agents": decayed}


def _should_monitor_agent_endpoint(agent: dict) -> bool:
    status = str(agent.get("status") or "").strip().lower()
    if status in {"banned", "suspended"}:
        return False
    endpoint = str(agent.get("endpoint_url") or "").strip()
    if not endpoint:
        return False
    if endpoint.startswith("internal://"):
        return False
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").strip().lower()
    if host in {"example.com"} or host.endswith(".example.com"):
        return False
    if host.endswith(".test") or host.endswith(".invalid"):
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _probe_agent_endpoint_health(endpoint_url: str, timeout_seconds: int) -> tuple[bool, str | None]:
    safe_url = _validate_outbound_url(str(endpoint_url or "").strip(), "endpoint_url")
    response = http.head(safe_url, timeout=timeout_seconds, allow_redirects=False)
    status_code = int(response.status_code)
    if status_code in {405, 501}:
        response = http.get(safe_url, timeout=timeout_seconds, allow_redirects=False)
        status_code = int(response.status_code)
    if 200 <= status_code < 500:
        return True, None
    return False, f"status_code={status_code}"


def _monitor_agent_endpoints(
    *,
    limit: int = _ENDPOINT_MONITOR_BATCH_SIZE,
    timeout_seconds: int = _ENDPOINT_MONITOR_TIMEOUT_SECONDS,
    failure_threshold: int = _ENDPOINT_MONITOR_FAILURE_THRESHOLD,
) -> dict[str, Any]:
    agents = registry.get_agents(include_internal=True, include_banned=True)
    checked = 0
    healthy = 0
    degraded = 0
    recovered = 0
    degraded_agent_ids: list[str] = []
    recovered_agent_ids: list[str] = []
    for agent in agents:
        if checked >= limit:
            break
        if not _should_monitor_agent_endpoint(agent):
            continue
        checked += 1
        agent_id = str(agent.get("agent_id") or "")
        previous_status = str(agent.get("endpoint_health_status") or "unknown").strip().lower()
        previous_failures = _to_non_negative_int(agent.get("endpoint_consecutive_failures"), default=0)
        endpoint_url = str(agent.get("endpoint_url") or "").strip()
        ok = False
        error_text: str | None = None
        try:
            ok, error_text = _probe_agent_endpoint_health(endpoint_url, timeout_seconds=timeout_seconds)
        except Exception as exc:
            ok = False
            error_text = str(exc) or "endpoint health check failed"
        if ok:
            new_failures = 0
            new_status = "healthy"
            healthy += 1
            if previous_status == "degraded":
                recovered += 1
                recovered_agent_ids.append(agent_id)
        else:
            new_failures = previous_failures + 1
            new_status = "degraded" if new_failures >= failure_threshold else "healthy"
            if new_status == "degraded":
                degraded += 1
                if previous_status != "degraded":
                    degraded_agent_ids.append(agent_id)
        registry.set_agent_endpoint_health(
            agent_id,
            endpoint_health_status=new_status,
            endpoint_consecutive_failures=new_failures,
            endpoint_last_checked_at=_utc_now_iso(),
            endpoint_last_error=None if ok else error_text,
        )
    return {
        "endpoint_checks_scanned": checked,
        "endpoint_healthy_count": healthy,
        "endpoint_degraded_count": degraded,
        "endpoint_recovered_count": recovered,
        "endpoint_degraded_agent_ids": degraded_agent_ids,
        "endpoint_recovered_agent_ids": recovered_agent_ids,
    }


def _auto_suspend_low_performing_agents(actor_owner_id: str) -> dict[str, Any]:
    suspended_agent_ids: list[str] = []
    generated_events: list[dict[str, Any]] = []
    now_iso = _utc_now_iso()
    with jobs._conn() as conn:
        rows = conn.execute(
            """
            SELECT agent_id, owner_id, successful_calls, total_calls
            FROM agents
            WHERE status = 'active' AND total_calls >= ?
            """,
            (AUTO_SUSPEND_MIN_CALLS,),
        ).fetchall()
        for row in rows:
            total_calls = int(row["total_calls"] or 0)
            successful_calls = int(row["successful_calls"] or 0)
            if total_calls <= 0:
                continue
            failure_rate = 1.0 - (float(successful_calls) / float(total_calls))
            if failure_rate <= AUTO_SUSPEND_FAILURE_RATE_THRESHOLD:
                continue
            status_update = conn.execute(
                "UPDATE agents SET status = 'suspended' WHERE agent_id = ? AND status = 'active'",
                (row["agent_id"],),
            )
            if status_update.rowcount <= 0:
                continue

            payload = {
                "reason": "failure_rate_threshold",
                "failure_rate": round(failure_rate, 4),
                "total_calls": total_calls,
            }
            cursor = conn.execute(
                """
                INSERT INTO job_events
                    (job_id, agent_id, agent_owner_id, caller_owner_id, event_type, actor_owner_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"agent:{row['agent_id']}",
                    row["agent_id"],
                    row["owner_id"] or "unknown",
                    "system:sweeper",
                    "agent_auto_suspended",
                    actor_owner_id,
                    _stable_json_text(payload),
                    now_iso,
                ),
            )
            event = {
                "event_id": int(cursor.lastrowid),
                "job_id": f"agent:{row['agent_id']}",
                "agent_id": str(row["agent_id"]),
                "agent_owner_id": str(row["owner_id"] or "unknown"),
                "caller_owner_id": "system:sweeper",
                "event_type": "agent_auto_suspended",
                "actor_owner_id": actor_owner_id,
                "payload": payload,
                "created_at": now_iso,
            }
            generated_events.append(event)
            suspended_agent_ids.append(str(row["agent_id"]))
    for event in generated_events:
        _deliver_job_event_hooks(event)
    return {
        "auto_suspended_count": len(suspended_agent_ids),
        "auto_suspended_agent_ids": suspended_agent_ids,
    }


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
    timeout_failed_job_ids: list[str] = []
    timeout_retry_job_ids: list[str] = []
    for item in expired:
        updated = jobs.mark_job_timeout(
            item["job_id"],
            retry_delay_seconds=retry_delay_seconds,
            allow_retry=True,
        )
        if updated is None:
            continue
        if updated.get("status") == "pending":
            timeout_retry_job_ids.append(updated["job_id"])
            _record_job_event(
                updated,
                "job.timeout_retry_scheduled",
                actor_owner_id=actor_owner_id,
                payload={
                    "retry_count": updated.get("retry_count"),
                    "next_retry_at": updated.get("next_retry_at"),
                },
            )
        else:
            settled = _settle_failed_job(updated, actor_owner_id=actor_owner_id, event_type="job.timeout_terminal")
            timeout_failed_job_ids.append(settled["job_id"])

    clarification_timeout_failed_job_ids: list[str] = []
    clarification_timeout_proceeded_job_ids: list[str] = []
    expired_clarification = jobs.list_jobs_with_expired_clarification_deadline(limit=limit)
    for item in expired_clarification:
        timeout_policy = str(item.get("clarification_timeout_policy") or "").strip().lower() or "fail"
        if timeout_policy == "proceed":
            resumed = jobs.update_job_status(item["job_id"], "running", completed=False)
            if resumed is None:
                continue
            clarification_timeout_proceeded_job_ids.append(resumed["job_id"])
            _record_job_event(
                resumed,
                "job.clarification_timeout_proceeded",
                actor_owner_id=actor_owner_id,
                payload={"clarification_deadline_at": item.get("clarification_deadline_at")},
            )
            continue

        failed = jobs.update_job_status(
            item["job_id"],
            "failed",
            error_message="Clarification response timeout reached.",
            completed=True,
        )
        if failed is None:
            continue
        settled = _settle_failed_job(
            failed,
            actor_owner_id=actor_owner_id,
            event_type="job.clarification_timeout_failed",
            refund_fraction=1.0,
        )
        clarification_timeout_failed_job_ids.append(settled["job_id"])

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
    retry_ready_job_ids: list[str] = []
    for item in due_retry:
        previous_next_retry_at = item.get("next_retry_at")
        advanced = jobs.mark_retry_ready(item["job_id"])
        if advanced is None:
            continue
        retry_ready_job_ids.append(advanced["job_id"])
        _record_job_event(
            advanced,
            "retry_ready",
            actor_owner_id=actor_owner_id,
            payload={"previous_next_retry_at": previous_next_retry_at},
        )
    output_verification_expired_job_ids: list[str] = []
    output_verification_auto_settled_job_ids: list[str] = []
    for item in jobs.list_jobs_with_expired_output_verification(limit=limit):
        expired = jobs.mark_output_verification_expired(item["job_id"])
        if expired is None:
            continue
        output_verification_expired_job_ids.append(expired["job_id"])
        _record_job_event(
            expired,
            "job.output_verification_expired",
            actor_owner_id=actor_owner_id,
            payload={"output_verification_deadline_at": item.get("output_verification_deadline_at")},
        )
        auto_settled = _settle_successful_job(expired, actor_owner_id=actor_owner_id)
        if auto_settled.get("settled_at"):
            output_verification_auto_settled_job_ids.append(auto_settled["job_id"])
    completed_pending_settlement = jobs.list_completed_jobs_pending_settlement(limit=limit)
    settled_successful_job_ids: list[str] = []
    for item in completed_pending_settlement:
        settled = _settle_successful_job(item, actor_owner_id=actor_owner_id)
        if settled.get("settled_at"):
            settled_successful_job_ids.append(settled["job_id"])
    endpoint_health_summary = _monitor_agent_endpoints(limit=limit)
    suspension_summary = _auto_suspend_low_performing_agents(actor_owner_id)
    decay_summary = _apply_reputation_decay()
    return {
        "expired_leases_scanned": len(expired),
        "due_retry_count": len(due_retry),
        "retry_ready_count": len(retry_ready_job_ids),
        "retry_ready_job_ids": retry_ready_job_ids,
        "timeout_retry_job_ids": timeout_retry_job_ids,
        "timeout_failed_job_ids": timeout_failed_job_ids,
        "clarification_timeout_scanned": len(expired_clarification),
        "clarification_timeout_failed_job_ids": clarification_timeout_failed_job_ids,
        "clarification_timeout_proceeded_job_ids": clarification_timeout_proceeded_job_ids,
        "sla_failed_job_ids": sla_failed_job_ids,
        "output_verification_expired_job_ids": output_verification_expired_job_ids,
        "output_verification_auto_settled_job_ids": output_verification_auto_settled_job_ids,
        "completed_pending_settlement_scanned": len(completed_pending_settlement),
        "settled_successful_count": len(settled_successful_job_ids),
        "settled_successful_job_ids": settled_successful_job_ids,
        **endpoint_health_summary,
        "auto_suspended_count": int(suspension_summary["auto_suspended_count"]),
        "auto_suspended_agent_ids": suspension_summary["auto_suspended_agent_ids"],
        "reputation_decay": decay_summary,
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
            active = {k: v for k, v in summary.items() if isinstance(v, int) and v > 0}
            if active:
                logging_utils.log_event(_LOG, logging.INFO, "sweeper.pass_completed", active)
        except Exception as exc:
            _LOG.exception("Jobs sweeper loop failed.")
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
    failed_deliveries = int(delivery_status_counts.get("failed", 0))
    if failed_deliveries > 0:
        alerts.append(f"{failed_deliveries} hook deliveries failed permanently.")
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
    sweeper_last_summary = sweeper_state.get("last_summary")
    if not isinstance(sweeper_last_summary, dict):
        sweeper_last_summary = {}
    retry_ready_last_sweep = int(sweeper_last_summary.get("retry_ready_count") or 0)
    auto_suspended_last_sweep = int(sweeper_last_summary.get("auto_suspended_count") or 0)
    with _HOOK_WORKER_STATE_LOCK:
        hook_worker_state = dict(_HOOK_WORKER_STATE)
    with _BUILTIN_WORKER_STATE_LOCK:
        builtin_worker_state = dict(_BUILTIN_WORKER_STATE)
    with _DISPUTE_JUDGE_STATE_LOCK:
        dispute_judge_state = dict(_DISPUTE_JUDGE_STATE)
    with _PAYMENTS_RECONCILIATION_STATE_LOCK:
        payments_reconciliation_state = dict(_PAYMENTS_RECONCILIATION_STATE)

    return {
        "status_counts": status_counts,
        "unsettled_jobs": int(unsettled),
        "failed_unsettled_jobs": int(failed_unsettled),
        "expired_leases": expired_leases_count,
        "due_retries": due_retry_count,
        "retry_ready_last_sweep": retry_ready_last_sweep,
        "auto_suspended_last_sweep": auto_suspended_last_sweep,
        "sla_breaches": sla_breach_count,
        "events_last_24h": int(events_24h),
        "alerts": alerts,
        "sweeper": sweeper_state,
        "hook_worker": hook_worker_state,
        "builtin_worker": builtin_worker_state,
        "dispute_judge": dispute_judge_state,
        "payments_reconciliation": payments_reconciliation_state,
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
        resp = http.get(safe_url, timeout=15, allow_redirects=False)
        if 300 <= int(resp.status_code) < 400:
            raise HTTPException(status_code=502, detail="manifest_url redirects are not allowed.")
        resp.raise_for_status()
    except http.RequestException as exc:
        _LOG.warning("Failed to fetch manifest_url %s: %s", safe_url, exc)
        raise HTTPException(status_code=502, detail="Failed to fetch manifest_url.")
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
        mode = "trust"
    else:
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
# Error normalization + exception handlers
# ---------------------------------------------------------------------------

def _default_error_code_for_request(status_code: int, path: str, message: str) -> str:
    lowered_path = str(path or "").lower()
    lowered_message = str(message or "").lower()
    if status_code == 404 and lowered_path.startswith("/jobs/"):
        return error_codes.JOB_NOT_FOUND
    if status_code == 404 and lowered_path.startswith("/registry/agents"):
        return error_codes.AGENT_NOT_FOUND
    if status_code == 410:
        return error_codes.AGENT_TIMEOUT
    if status_code == 400 and "dispute window" in lowered_message:
        return error_codes.DISPUTE_WINDOW_CLOSED
    if status_code == 503 and "suspend" in lowered_message:
        return error_codes.AGENT_SUSPENDED
    return error_codes.DEFAULT_BY_STATUS.get(status_code, error_codes.INVALID_INPUT)


def _error_code_from_message(status_code: int, path: str, message: str) -> str:
    lowered_message = str(message or "").strip().lower()

    if lowered_message.startswith("authorization header missing"):
        return "auth.missing_authorization"
    if lowered_message == "invalid api key.":
        return "auth.invalid_key"
    if lowered_message == "invalid email or password.":
        return "auth.invalid_credentials"
    if lowered_message.startswith("agent-scoped keys cannot"):
        return "auth.insufficient_scope"
    if lowered_message.startswith("this endpoint requires"):
        return "auth.insufficient_scope"
    if lowered_message.startswith("not available for master key"):
        return "auth.insufficient_scope"
    if lowered_message == "not authorized." or lowered_message.startswith("not authorized"):
        return "auth.forbidden"
    if lowered_message.startswith("tool '"):
        return "mcp.tool_not_found"
    if lowered_message.startswith("agent '"):
        return "agent.not_found"
    if lowered_message.startswith("job '"):
        return "job.not_found"
    if lowered_message.startswith("dispute '"):
        return "dispute.not_found"
    if lowered_message.startswith("wallet '"):
        return "wallet.not_found"
    if lowered_message.startswith("invalid status:"):
        return "request.invalid_status"
    if "idempotency-key is too long" in lowered_message:
        return "request.idempotency_key_too_long"
    if lowered_message.startswith("a request with this idempotency-key is still in progress"):
        return "request.idempotency_conflict"
    if lowered_message.startswith("failed to fetch manifest_url"):
        return "onboarding.manifest_fetch_failed"
    if lowered_message.startswith("manifest too large"):
        return "request.payload_too_large"
    if lowered_message.startswith("fetched manifest is empty"):
        return "onboarding.manifest_empty"
    if lowered_message.startswith("failed to create job"):
        return "job.create_failed"
    if lowered_message.startswith("job is not claimable"):
        return "job.not_claimable"
    if lowered_message.startswith("job is not currently claimed by this worker"):
        return "job.claim_missing"
    if lowered_message.startswith("invalid or missing claim_token") or lowered_message.startswith("invalid or stale claim_token"):
        return "job.invalid_claim_token"
    if lowered_message.startswith("unable to heartbeat this job claim"):
        return "job.heartbeat_failed"
    if lowered_message.startswith("unable to release this job claim"):
        return "job.release_failed"
    if lowered_message.startswith("unable to update job status"):
        return "job.transition_failed"
    if lowered_message.startswith("unable to schedule retry for this job"):
        return "job.retry_failed"
    if lowered_message.startswith("upstream agent unreachable"):
        return "agent.upstream_unreachable"
    if lowered_message.startswith("agent endpoint is misconfigured"):
        return "agent.endpoint_misconfigured"
    if lowered_message.startswith("agent execution failed"):
        return "agent.execution_failed"
    if lowered_message.startswith("all llm models rate-limited"):
        return "agent.upstream_rate_limited"
    if lowered_message.startswith("hook not found"):
        return "hook.not_found"
    if lowered_message.startswith("key not found or already revoked"):
        return "auth.key_not_found"
    if lowered_message.startswith("disputes can only be filed for completed jobs"):
        return "dispute.invalid_state"
    if lowered_message.startswith("disputes must be filed before the caller submits a rating"):
        return "dispute.rating_locked"
    if lowered_message.startswith("a dispute already exists for this job"):
        return "dispute.already_exists"
    if lowered_message.startswith("dispute window has expired for this job"):
        return "dispute.window_closed"
    if lowered_message.startswith("job completion timestamp is invalid"):
        return "job.invalid_completion_timestamp"
    if lowered_message.startswith("failed to resolve dispute"):
        return "dispute.resolve_failed"
    if lowered_message.startswith("tool_result payload.correlation_id is required"):
        return "job.invalid_tool_result"
    if lowered_message.startswith("unknown tool_result correlation_id"):
        return "job.invalid_tool_result"
    if lowered_message.startswith("unsupported job message type"):
        return "job.invalid_message_type"
    if lowered_message.startswith("agent.md spec not found"):
        return "onboarding.spec_not_found"
    if lowered_message.startswith("cursor must not be empty") or lowered_message.startswith("invalid cursor"):
        return "request.invalid_cursor"
    if lowered_message.startswith("limit must be > 0"):
        return "request.invalid_limit"
    if lowered_message.startswith("sla_seconds must be > 0"):
        return "request.invalid_sla_seconds"
    if lowered_message.startswith("max_mismatches must be > 0"):
        return "request.invalid_max_mismatches"
    if lowered_message.startswith("rank_by must be one of"):
        return "request.invalid_rank_by"
    if lowered_message.startswith("authentication service is temporarily unavailable"):
        return "auth.service_unavailable"
    if lowered_message.startswith("master key cannot"):
        return "auth.master_forbidden"
    if lowered_message.startswith("only the original caller can rate this job"):
        return "job.rating_forbidden"
    if lowered_message.startswith("only the job's agent owner can rate the caller"):
        return "job.rating_forbidden"
    if lowered_message.startswith("ratings are locked once a dispute is filed"):
        return "dispute.rating_locked"
    if lowered_message.startswith("this endpoint requires caller or worker scope"):
        return "auth.insufficient_scope"
    return _default_error_code_for_request(status_code, path, lowered_message)


def _normalize_error_payload(status_code: int, detail: Any, path: str) -> dict[str, Any]:
    if isinstance(detail, dict):
        raw_error = str(detail.get("error") or "").strip()
        if {"error", "message"}.issubset(detail.keys()):
            details = detail.get("details")
            if details is None and "data" in detail:
                details = detail.get("data")
            return error_codes.make_error(
                raw_error or _error_code_from_message(status_code, path, str(detail.get("message") or "")),
                str(detail.get("message") or "Request failed."),
                details,
            )
        message = str(detail.get("message") or detail.get("detail") or "Request failed.").strip()
        details = {
            str(k): v
            for k, v in detail.items()
            if str(k) not in {"error", "message", "detail", "details", "data"}
        }
        if "details" in detail and detail["details"] is not None:
            details = detail["details"]
        elif "data" in detail and detail["data"] is not None:
            details = detail["data"]
        return error_codes.make_error(
            raw_error or _error_code_from_message(status_code, path, message),
            message,
            details,
        )
    message = str(detail or "Request failed.")
    return error_codes.make_error(
        _error_code_from_message(status_code, path, message),
        message,
        None,
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    payload = _normalize_error_payload(exc.status_code, exc.detail, request.url.path)
    return JSONResponse(content=payload, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    def _sanitize(errors):
        clean = []
        for e in errors:
            entry = {k: v for k, v in e.items() if k != "ctx"}
            ctx = e.get("ctx")
            if ctx:
                entry["ctx"] = {k: str(v) for k, v in ctx.items()}
            clean.append(entry)
        return clean

    payload = error_codes.make_error(
        error_codes.INVALID_INPUT,
        "Request validation failed.",
        {"errors": _sanitize(exc.errors())},
    )
    return JSONResponse(content=payload, status_code=422)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_exception_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    retry_after = 60
    limit = getattr(exc, "limit", None)
    if limit is not None:
        limit_item = getattr(limit, "limit", None)
        get_expiry = getattr(limit_item, "get_expiry", None)
        if callable(get_expiry):
            try:
                retry_after = int(get_expiry())
            except Exception:
                retry_after = 60
    payload = {
        "error": "rate_limit_exceeded",
        "retry_after_seconds": max(1, retry_after),
    }
    logging_utils.log_event(
        _LOG,
        logging.WARNING,
        "http.rate_limited",
        {
            "method": request.method,
            "path": request.url.path,
            "retry_after_seconds": payload["retry_after_seconds"],
        },
    )
    return JSONResponse(
        content=payload,
        status_code=429,
        headers={"Retry-After": str(payload["retry_after_seconds"])},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logging_utils.log_event(
        _LOG,
        logging.ERROR,
        "server.unhandled_exception",
        {"method": request.method, "path": request.url.path},
    )
    _LOG.exception("unhandled_exception")
    payload = error_codes.make_error("server.internal_error", "Internal server error.")
    return JSONResponse(content=payload, status_code=500)


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
    429: {"model": core_models.RateLimitErrorResponse, "description": "Rate limit exceeded."},
    500: {"model": core_models.ErrorResponse, "description": "Internal server error."},
    502: {"model": core_models.ErrorResponse, "description": "Upstream request failed."},
    503: {"model": core_models.ErrorResponse, "description": "Upstream service unavailable."},
}


def _error_responses(*codes: int) -> dict[int, dict[str, Any]]:
    return {code: _OPENAPI_ERROR_RESPONSES[code] for code in codes if code in _OPENAPI_ERROR_RESPONSES}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def _read_version() -> str:
    try:
        version_path = os.path.join(os.path.dirname(__file__), "VERSION")
        return open(version_path).read().strip()
    except Exception:
        return "unknown"


@app.get(
    "/health",
    response_model=core_models.HealthResponse,
    responses={
        200: {"description": "All checks passed."},
        503: {"model": core_models.ErrorResponse, "description": "One or more checks failed."},
        **_error_responses(429, 500),
    },
)
def health() -> core_models.HealthResponse:
    try:
        import psutil as _psutil
    except ImportError:
        _psutil = None

    checks: dict[str, core_models.HealthCheckDetail] = {}
    all_ok = True

    # DB check
    try:
        t0 = time.monotonic()
        with jobs._conn() as conn:
            conn.execute("SELECT 1").fetchone()
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        checks["db"] = core_models.HealthCheckDetail(ok=True, latency_ms=latency_ms)
    except Exception as exc:
        all_ok = False
        checks["db"] = core_models.HealthCheckDetail(ok=False, error=str(exc))

    # Disk check
    try:
        writable = os.access(jobs.DB_PATH, os.W_OK)
        checks["disk"] = core_models.HealthCheckDetail(ok=writable, writable=writable)
        if not writable:
            all_ok = False
    except Exception as exc:
        all_ok = False
        checks["disk"] = core_models.HealthCheckDetail(ok=False, error=str(exc))

    # Memory check (optional — requires psutil)
    if _psutil is not None:
        try:
            proc = _psutil.Process()
            rss_mb = round(proc.memory_info().rss / (1024 * 1024), 2)
            checks["memory"] = core_models.HealthCheckDetail(ok=True, rss_mb=rss_mb)
        except Exception as exc:
            all_ok = False
            checks["memory"] = core_models.HealthCheckDetail(ok=False, error=str(exc))

    agent_count = len(registry.get_agents())
    status = "ok" if all_ok else "degraded"
    response = core_models.HealthResponse(
        status=status,
        checks=checks,
        agent_count=agent_count,
        version=_read_version(),
        agents=agent_count,
    )

    if not all_ok:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=response.model_dump())
    return response


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
        safe_endpoint_url = _validate_agent_endpoint_url(request, payload["endpoint_url"])
        safe_healthcheck_url = None
        if payload.get("healthcheck_url"):
            safe_healthcheck_url = _validate_outbound_url(payload["healthcheck_url"], "healthcheck_url")
        safe_verifier_url = None
        if payload.get("output_verifier_url"):
            safe_verifier_url = _validate_outbound_url(payload["output_verifier_url"], "output_verifier_url")
        agent_id = registry.register_agent(
            name=payload["name"],
            description=payload["description"],
            endpoint_url=safe_endpoint_url,
            healthcheck_url=safe_healthcheck_url,
            price_per_call_usd=payload["price_per_call_usd"],
            tags=payload["tags"],
            input_schema=payload["input_schema"],
            output_schema=payload.get("output_schema"),
            output_verifier_url=safe_verifier_url,
            owner_id=caller["owner_id"],
        )
    except onboarding.ManifestValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    agent = registry.get_agent_with_reputation(agent_id, include_unapproved=True) or registry.get_agent(
        agent_id,
        include_unapproved=True,
    )
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


def _auth_legal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "legal_acceptance_required": bool(payload.get("legal_acceptance_required", True)),
        "legal_accepted_at": payload.get("legal_accepted_at"),
        "terms_version_current": str(payload.get("terms_version_current") or _auth.LEGAL_TERMS_VERSION),
        "privacy_version_current": str(payload.get("privacy_version_current") or _auth.LEGAL_PRIVACY_VERSION),
        "terms_version_accepted": payload.get("terms_version_accepted"),
        "privacy_version_accepted": payload.get("privacy_version_accepted"),
    }


@app.post(
    "/auth/register",
    status_code=201,
    response_model=core_models.AuthRegisterResponse,
    responses=_error_responses(400, 429, 500, 503),
)
@limiter.limit(_AUTH_RATE_LIMIT, key_func=get_remote_address)
def auth_register(request: Request, body: UserRegisterRequest) -> core_models.AuthRegisterResponse:
    """Create a new user account. Returns the initial API key (shown once)."""
    try:
        _auth.init_auth_db()
        result = _auth.register_user(body.username, body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.DatabaseError:
        _LOG.exception("Auth register failed; retrying after auth schema init.")
        try:
            _auth.init_auth_db()
            result = _auth.register_user(body.username, body.email, body.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except sqlite3.DatabaseError:
            _LOG.exception("Auth register failed due to auth DB error.")
            raise HTTPException(
                status_code=503,
                detail="Authentication service is temporarily unavailable. Please try again.",
            )
    # Credit $1.00 starter balance so new users can invoke agents immediately
    try:
        _owner_id = f"user:{result['user_id']}"
        _starter_wallet = payments.get_or_create_wallet(_owner_id)
        payments.deposit(_starter_wallet["wallet_id"], 100, "Welcome credit — $1.00 to get started")
    except Exception:
        _LOG.warning("Failed to credit starter balance for new user %s", result.get("user_id"))
    _email.send_welcome(result.get("email", ""), result.get("username", "there"))
    return JSONResponse(content={**result, **_auth_legal_payload(result)}, status_code=201)


@app.post(
    "/auth/login",
    response_model=core_models.AuthLoginResponse,
    responses=_error_responses(401, 429, 500, 503),
)
@limiter.limit(_AUTH_RATE_LIMIT, key_func=get_remote_address)
def auth_login(request: Request, body: UserLoginRequest) -> core_models.AuthLoginResponse:
    """Verify credentials. Returns a fresh API key valid for this session."""
    try:
        _auth.init_auth_db()
        result = _auth.login_user(body.email, body.password)
    except sqlite3.DatabaseError:
        _LOG.exception("Auth login failed; retrying after auth schema init.")
        try:
            _auth.init_auth_db()
            result = _auth.login_user(body.email, body.password)
        except sqlite3.DatabaseError:
            _LOG.exception("Auth login failed due to auth DB error.")
            raise HTTPException(
                status_code=503,
                detail="Authentication service is temporarily unavailable. Please try again.",
            )
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return JSONResponse(content={**result, **_auth_legal_payload(result)})


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
    if caller["type"] == "agent_key":
        raise HTTPException(status_code=403, detail="Agent-scoped keys cannot access /auth/me.")
    user = caller["user"]
    return JSONResponse(content={
        "user_id": user["user_id"],
        "username": user["username"],
        "email": user["email"],
        "scopes": caller.get("scopes") or [],
        **_auth_legal_payload(user),
    })


@app.post(
    "/auth/legal/accept",
    response_model=core_models.AuthLegalAcceptResponse,
    responses=_error_responses(400, 401, 403, 429, 500),
)
@limiter.limit(_AUTH_RATE_LIMIT, key_func=get_remote_address)
def auth_accept_legal(
    request: Request,
    body: AuthLegalAcceptRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AuthLegalAcceptResponse:
    if caller["type"] != "user":
        raise HTTPException(status_code=403, detail="Not available for master or agent-scoped keys.")
    client_ip = _request_client_ip(request)
    accepted_ip = str(client_ip) if client_ip is not None else None
    try:
        result = _auth.accept_legal_terms(
            caller["user"]["user_id"],
            terms_version=body.terms_version,
            privacy_version=body.privacy_version,
            accepted_ip=accepted_ip,
            accepted_user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.LEGAL_VERSION_MISMATCH,
                str(exc),
                {
                    "terms_version_current": _auth.LEGAL_TERMS_VERSION,
                    "privacy_version_current": _auth.LEGAL_PRIVACY_VERSION,
                },
            ),
        )
    return JSONResponse(content={**result, **_auth_legal_payload(result)})


@app.get(
    "/auth/keys",
    response_model=core_models.ApiKeyListResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def auth_list_keys(request: Request, caller: core_models.CallerContext = Depends(_require_api_key)) -> core_models.ApiKeyListResponse:
    """List the caller's API keys (metadata only — raw keys never returned after creation)."""
    if caller["type"] != "user":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    keys = _auth.list_api_keys(caller["user"]["user_id"])
    return JSONResponse(content={"keys": keys})


@app.post(
    "/auth/keys",
    status_code=201,
    response_model=core_models.ApiKeyCreateResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
)
@limiter.limit("10/minute")
def auth_create_key(
    request: Request,
    body: CreateKeyRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.ApiKeyCreateResponse:
    """Create a new named API key for the authenticated user."""
    if caller["type"] != "user":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    requested_scopes = {str(scope).strip().lower() for scope in body.scopes}
    if "caller" in requested_scopes and body.per_job_cap_cents is None:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.VALIDATION_ERROR,
                "caller-scoped keys require per_job_cap_cents.",
                {"field": "per_job_cap_cents", "required_for_scope": "caller"},
            ),
        )
    try:
        result = _auth.create_api_key(
            caller["user"]["user_id"],
            body.name,
            scopes=body.scopes,
            max_spend_cents=body.max_spend_cents,
            per_job_cap_cents=body.per_job_cap_cents,
        )
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
    if caller["type"] != "user":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    try:
        result = _auth.rotate_api_key(
            key_id=key_id,
            user_id=caller["user"]["user_id"],
            name=body.name,
            scopes=body.scopes,
            max_spend_cents=body.max_spend_cents,
            per_job_cap_cents=body.per_job_cap_cents,
            max_spend_cents_provided="max_spend_cents" in body.model_fields_set,
            per_job_cap_cents_provided="per_job_cap_cents" in body.model_fields_set,
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
    if caller["type"] != "user":
        raise HTTPException(status_code=403, detail="Not available for master key.")
    ok = _auth.revoke_api_key(key_id, caller["user"]["user_id"])
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found or already revoked.")
    return JSONResponse(content={"revoked": True})


# ---------------------------------------------------------------------------
# Built-in agent handlers (invoked via registry/internal routing)
# ---------------------------------------------------------------------------


def _invoke_financial_agent(body: FinancialRequest) -> dict:
    ticker = body.ticker.strip().upper()
    if not ticker.isalpha() or len(ticker) > 5:
        raise ValueError(f"Invalid ticker symbol: '{ticker}'")
    return _run_financial(ticker)


def _invoke_code_review_agent(body: CodeReviewRequest) -> dict:
    return agent_codereview.run(body.code, body.language, body.focus, getattr(body, "context", ""))


def _invoke_text_intel_agent(body: TextIntelRequest) -> dict:
    return agent_textintel.run(body.text, body.mode)


def _invoke_wiki_agent(body: WikiRequest) -> dict:
    return agent_wiki.run(body.topic, depth=body.depth)


def _invoke_negotiation_agent(body: NegotiationRequest) -> dict:
    return agent_negotiation.run(
        objective=body.objective,
        counterparty_profile=body.counterparty_profile,
        constraints=_coerce_string_list(body.constraints),
        context=body.context,
        style=getattr(body, "style", "principled"),
    )


def _invoke_scenario_agent(body: ScenarioRequest) -> dict:
    return agent_scenario.run(
        decision=body.decision,
        assumptions=body.assumptions,
        horizon=body.horizon,
        risk_tolerance=body.risk_tolerance,
        key_variables=getattr(body, "key_variables", None),
    )


def _invoke_product_strategy_agent(body: ProductStrategyRequest) -> dict:
    return agent_product.run(
        product_idea=body.product_idea,
        target_users=body.target_users,
        market_context=body.market_context,
        horizon_quarters=body.horizon_quarters,
        stage=getattr(body, "stage", "seed"),
    )


def _invoke_portfolio_agent(body: PortfolioRequest) -> dict:
    return agent_portfolio.run(
        investment_goal=body.investment_goal,
        risk_profile=body.risk_profile,
        time_horizon_years=body.time_horizon_years,
        capital_usd=body.capital_usd,
        existing_holdings=getattr(body, "existing_holdings", ""),
        constraints=getattr(body, "constraints", ""),
    )


@app.post(
    "/analyze",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 422, 429, 500, 502, 503),
)
@limiter.limit("10/minute")
def analyze_alias(
    request: Request,
    body: FinancialRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> Response:
    return registry_call(
        request=request,
        agent_id=_FINANCIAL_AGENT_ID,
        body=core_models.RegistryCallRequest(root=body.model_dump()),
        caller=caller,
    )


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
    if caller["type"] == "agent_key":
        raise HTTPException(status_code=403, detail="Agent-scoped keys cannot register new agents.")
    try:
        safe_endpoint_url = _validate_agent_endpoint_url(request, body.endpoint_url)
        if not os.environ.get("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE"):
            _probe_register_endpoint_or_400(safe_endpoint_url)
        safe_healthcheck_url = None
        if body.healthcheck_url:
            safe_healthcheck_url = _validate_outbound_url(body.healthcheck_url, "healthcheck_url")
        safe_verifier_url = None
        if body.output_verifier_url:
            safe_verifier_url = _validate_outbound_url(body.output_verifier_url, "output_verifier_url")
        registration_payload = {
            "name": body.name,
            "description": body.description,
            "endpoint_url": safe_endpoint_url,
            "healthcheck_url": safe_healthcheck_url,
            "price_per_call_usd": body.price_per_call_usd,
            "tags": body.tags,
            "input_schema": body.input_schema,
            "output_schema": body.output_schema,
        }
        verified = False
        verifier_reason = "no verifier configured"
        if safe_verifier_url:
            verified, verifier_reason = _run_registration_verifier(
                safe_verifier_url,
                registration_payload=registration_payload,
            )
        agent_id = registry.register_agent(
            name=body.name,
            description=body.description,
            endpoint_url=safe_endpoint_url,
            healthcheck_url=safe_healthcheck_url,
            price_per_call_usd=body.price_per_call_usd,
            tags=body.tags,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            output_verifier_url=safe_verifier_url,
            output_examples=body.output_examples or None,
            verified=verified,
            owner_id=caller["owner_id"],
            model_provider=body.model_provider,
            model_id=body.model_id,
        )
        agent = registry.get_agent_with_reputation(agent_id, include_unapproved=True) or registry.get_agent(
            agent_id,
            include_unapproved=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Agent ID or name already exists.")
    message = "Agent registered successfully."
    if safe_verifier_url:
        if agent and agent.get("verified"):
            message = "Agent registered and verifier approved."
        else:
            message = f"Agent registered; verifier did not approve ({verifier_reason})."
    if (agent or {}).get("review_status") == "pending_review":
        message = "Your agent listing is pending review. You will be notified when it goes live."
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "message": message,
            "review_status": (agent or {}).get("review_status"),
            "agent": _agent_response(agent, caller) if agent else None,
        },
        status_code=201,
    )


def _mcp_tool_slug(name: str, fallback: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    return base or f"agent_{fallback}"


def _mcp_active_agents() -> list[dict[str, Any]]:
    agents = registry.get_agents(include_internal=True, include_banned=True)
    return [
        agent
        for agent in agents
        if str(agent.get("status") or "").strip().lower() == "active"
        and not bool(agent.get("internal_only"))
    ]


def _mcp_tools_and_lookup() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    tools: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}
    used_names: set[str] = set()
    for agent in _mcp_active_agents():
        agent_id = str(agent.get("agent_id") or "").strip()
        if not agent_id:
            continue
        fallback = (agent_id.replace("-", "")[:8] or "agent").lower()
        slug = _mcp_tool_slug(str(agent.get("name") or ""), fallback)
        if slug in used_names:
            slug = f"{slug}_{fallback}"
        while slug in used_names:
            slug = f"{slug}_x"
        used_names.add(slug)

        raw_input_schema = agent.get("input_schema")
        if isinstance(raw_input_schema, dict) and raw_input_schema:
            input_schema = raw_input_schema
        else:
            input_schema = {"type": "object", "properties": {}}
        raw_output_schema = agent.get("output_schema")
        output_schema = raw_output_schema if isinstance(raw_output_schema, dict) else {}
        tool = {
            "name": slug,
            "description": str(agent.get("description") or ""),
            "input_schema": input_schema,
            "output_schema": output_schema,
        }
        tools.append(tool)
        lookup[slug] = agent
    return tools, lookup


def _caller_from_raw_api_key(raw_api_key: str) -> core_models.CallerContext | None:
    raw = str(raw_api_key or "").strip()
    if not raw:
        return None
    if hmac.compare_digest(raw, _MASTER_KEY):
        return {
            "type": "master",
            "owner_id": "master",
            "scopes": ["caller", "worker", "admin"],
        }
    user = _auth.verify_api_key(raw)
    if user:
        return {
            "type": "user",
            "owner_id": f"user:{user['user_id']}",
            "user": user,
            "scopes": list(user.get("scopes") or []),
        }
    agent_key = _auth.verify_agent_api_key(raw)
    if agent_key:
        return {
            "type": "agent_key",
            "owner_id": str(agent_key["owner_id"]),
            "agent_id": str(agent_key["agent_id"]),
            "key_id": str(agent_key["key_id"]),
            "scopes": ["worker"],
        }
    return None


def _mcp_text_from_response(response: Response) -> str:
    body_bytes = bytes(getattr(response, "body", b"") or b"")
    if not body_bytes:
        return "null"
    body_text = body_bytes.decode("utf-8", errors="replace")
    try:
        return json.dumps(json.loads(body_text), ensure_ascii=False)
    except json.JSONDecodeError:
        return json.dumps(body_text, ensure_ascii=False)


def _mcp_payload_from_response(response: Response) -> Any:
    body_bytes = bytes(getattr(response, "body", b"") or b"")
    if not body_bytes:
        return None
    body_text = body_bytes.decode("utf-8", errors="replace")
    try:
        return json.loads(body_text)
    except json.JSONDecodeError:
        return body_text


def _parse_data_uri(value: str) -> tuple[str | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    match = re.match(r"^data:([^;,]+);base64,([A-Za-z0-9+/=]+)$", text, re.IGNORECASE)
    if not match:
        return None, None
    return match.group(1).strip().lower(), match.group(2).strip()


def _mcp_text_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("summary", "message", "answer", "title", "one_line_summary", "signal_reasoning"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _mcp_media_content_from_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for artifact in artifacts[:6]:
        mime = str(artifact.get("mime") or "").strip().lower()
        source = str(artifact.get("url_or_base64") or "").strip()
        if not mime or not source:
            continue
        parsed_mime, base64_payload = _parse_data_uri(source)
        effective_mime = parsed_mime or mime
        if effective_mime.startswith("image/") and base64_payload:
            rendered.append({"type": "image", "mimeType": effective_mime, "data": base64_payload})
            continue
        if source.startswith("http://") or source.startswith("https://"):
            rendered.append({"type": "resource", "resource": {"uri": source, "mimeType": effective_mime}})
            continue
        if base64_payload:
            rendered.append(
                {
                    "type": "resource",
                    "resource": {"uri": f"data:{effective_mime};base64,{base64_payload}", "mimeType": effective_mime},
                }
            )
            continue
    return rendered


def _mcp_content_from_payload(payload: Any) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": _mcp_text_from_payload(payload)}]
    if isinstance(payload, dict):
        raw_artifacts = payload.get("artifacts")
        if isinstance(raw_artifacts, list):
            artifact_rows = [item for item in raw_artifacts if isinstance(item, dict)]
            content.extend(_mcp_media_content_from_artifacts(artifact_rows))
    return content


def _a2a_agent_card(agent: dict) -> dict:
    """Build a Google A2A Agent Card for a single registered agent."""
    price_usd = float(agent.get("price_per_call_usd") or 0.0)
    return {
        "name": str(agent.get("name") or ""),
        "description": str(agent.get("description") or ""),
        "url": f"{_SERVER_BASE_URL}/registry/agents/{agent['agent_id']}/call",
        "version": "1.0.0",
        "provider": {"organization": "Aztea", "url": _SERVER_BASE_URL},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": True,
        },
        "skills": [
            {
                "id": agent["agent_id"],
                "name": str(agent.get("name") or ""),
                "description": str(agent.get("description") or ""),
                "tags": list(agent.get("tags") or []),
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
                "inputSchema": agent.get("input_schema") or {},
                "outputSchema": agent.get("output_schema") or {},
            }
        ],
        "authentication": {"schemes": ["ApiKey"]},
        "agentmarket": {
            "agent_id": agent["agent_id"],
            "price_per_call_usd": price_usd,
            "trust_score": agent.get("trust_score"),
            "total_calls": agent.get("total_calls"),
            "avg_latency_ms": agent.get("avg_latency_ms"),
            "success_rate": agent.get("success_rate"),
            "hire_endpoint": f"{_SERVER_BASE_URL}/jobs",
            "status_endpoint": f"{_SERVER_BASE_URL}/jobs/{{job_id}}",
        },
    }


@app.get(
    "/.well-known/agent.json",
    include_in_schema=True,
    tags=["A2A"],
    summary="Google A2A: platform-level agent card listing all registered agents as skills.",
)
def a2a_platform_agent_card(request: Request) -> JSONResponse:
    agents = registry.get_agents_with_reputation()
    visible = [a for a in agents if not a.get("internal_only")]
    skills = []
    for agent in visible:
        skills.append(
            {
                "id": agent["agent_id"],
                "name": str(agent.get("name") or ""),
                "description": str(agent.get("description") or ""),
                "tags": list(agent.get("tags") or []),
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
                "agentmarket": {
                    "agent_id": agent["agent_id"],
                    "price_per_call_usd": float(agent.get("price_per_call_usd") or 0.0),
                    "trust_score": agent.get("trust_score"),
                    "total_calls": agent.get("total_calls"),
                    "success_rate": agent.get("success_rate"),
                    "avg_latency_ms": agent.get("avg_latency_ms"),
                },
            }
        )
    card = {
        "name": "Aztea",
        "description": "AI agent labor marketplace. Discover, hire, and orchestrate specialist agents. Pay per invocation.",
        "url": _SERVER_BASE_URL,
        "version": "1.0.0",
        "provider": {"organization": "Aztea", "url": _SERVER_BASE_URL},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": True,
        },
        "skills": skills,
        "authentication": {"schemes": ["ApiKey"]},
        "agentmarket": {
            "hire_endpoint": f"{_SERVER_BASE_URL}/jobs",
            "search_endpoint": f"{_SERVER_BASE_URL}/registry/search",
            "list_endpoint": f"{_SERVER_BASE_URL}/registry/agents",
            "mcp_tools_endpoint": f"{_SERVER_BASE_URL}/mcp/tools",
        },
    }
    return JSONResponse(content=card, headers={"Content-Type": "application/json"})


@app.get(
    "/registry/agents/{agent_id}/agent.json",
    include_in_schema=True,
    tags=["A2A"],
    summary="Google A2A: per-agent card. Also served at /.well-known/agent.json?agent_id=...",
    responses=_error_responses(404),
)
def a2a_agent_card(agent_id: str, request: Request) -> JSONResponse:
    agent = registry.get_agent_with_reputation(agent_id)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if agent.get("internal_only"):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return JSONResponse(
        content=_a2a_agent_card(agent),
        headers={"Content-Type": "application/json"},
    )


@app.post(
    "/a2a/tasks/send",
    status_code=201,
    tags=["A2A"],
    summary="Google A2A: submit a task to an Aztea skill (agent). Returns a task/job object.",
    responses=_error_responses(400, 401, 402, 403, 404, 429, 500),
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def a2a_tasks_send(
    request: Request,
    body: core_models.A2ATaskSendRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    agent = registry.get_agent(body.skill_id, include_unapproved=True)
    if agent is None or not _caller_can_access_agent(caller, agent) or agent.get("status") in {"banned"}:
        raise HTTPException(status_code=404, detail=f"Skill (agent) '{body.skill_id}' not found.")
    if agent.get("status") == "suspended":
        raise HTTPException(status_code=503, detail=f"Skill (agent) '{body.skill_id}' is suspended.")
    if agent.get("internal_only"):
        raise HTTPException(status_code=404, detail=f"Skill (agent) '{body.skill_id}' not found.")

    price_cents = _usd_to_cents(agent["price_per_call_usd"])
    fee_bearer_policy = "caller"
    platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
    success_distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )
    caller_charge_cents = int(success_distribution["caller_charge_cents"])
    caller_owner_id = _caller_owner_id(request)
    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    charge_tx_id = _pre_call_charge_or_402(
        caller=caller,
        caller_wallet_id=caller_wallet["wallet_id"],
        charge_cents=caller_charge_cents,
        agent_id=agent["agent_id"],
    )

    try:
        job = jobs.create_job(
            agent_id=agent["agent_id"],
            caller_owner_id=caller_owner_id,
            caller_wallet_id=caller_wallet["wallet_id"],
            agent_wallet_id=agent_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            price_cents=price_cents,
            caller_charge_cents=caller_charge_cents,
            platform_fee_pct_at_create=platform_fee_pct_at_create,
            fee_bearer_policy=fee_bearer_policy,
            charge_tx_id=charge_tx_id,
            input_payload=body.input or {},
            agent_owner_id=agent.get("owner_id"),
            max_attempts=3,
            dispute_window_hours=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
            judge_agent_id=_QUALITY_JUDGE_AGENT_ID,
            callback_url=body.callback_url or None,
        )
    except Exception:
        payments.post_call_refund(caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent["agent_id"])
        raise HTTPException(status_code=500, detail="Failed to create task.")

    _record_job_event(job, "job.created", actor_owner_id=caller["owner_id"])
    return JSONResponse(content={
        "id": job["job_id"],
        "skill_id": agent["agent_id"],
        "status": "submitted",
        "job_id": job["job_id"],
        "price_cents": price_cents,
        "caller_charge_cents": caller_charge_cents,
        "created_at": job["created_at"],
        "agentmarket_job": _job_response(job, caller),
    }, status_code=201)


@app.get(
    "/a2a/tasks/{task_id}",
    tags=["A2A"],
    summary="Google A2A: get task status by task/job ID.",
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("120/minute")
def a2a_tasks_get(
    request: Request,
    task_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    job = jobs.get_job(task_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view this task.")
    a2a_status_map = {
        "pending": "submitted", "claimed": "working", "complete": "completed",
        "failed": "failed", "awaiting_clarification": "input-required",
    }
    return JSONResponse(content={
        "id": task_id,
        "skill_id": job["agent_id"],
        "status": a2a_status_map.get(job.get("status", ""), job.get("status", "")),
        "output": job.get("output_payload"),
        "error": job.get("error_message"),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "agentmarket_job": _job_response(job, caller),
    })


@app.post(
    "/a2a/tasks/{task_id}/cancel",
    tags=["A2A"],
    summary="Google A2A: cancel a pending task.",
    responses=_error_responses(401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def a2a_tasks_cancel(
    request: Request,
    task_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    job = jobs.get_job(task_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to cancel this task.")
    if job.get("status") not in {"pending"}:
        raise HTTPException(status_code=409, detail=f"Cannot cancel task in status '{job.get('status')}'.")
    cancelled = jobs.update_job_status(task_id, "failed", error_message="Cancelled by caller.", completed=True)
    if cancelled:
        _settle_failed_job(cancelled, actor_owner_id=caller["owner_id"])
    return JSONResponse(content={"id": task_id, "status": "cancelled"})


@app.get(
    "/openai/tools",
    tags=["Integrations"],
    summary="OpenAI Agents SDK: tool definitions for all registered agents in function-calling format.",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("30/minute")
def openai_tools(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    agents = registry.get_agents_with_reputation()
    visible = [a for a in agents if not a.get("internal_only")]
    tools = []
    for agent in visible:
        input_schema = agent.get("input_schema") or {}
        props = input_schema.get("properties", {})
        required = input_schema.get("required", [])
        tools.append({
            "type": "function",
            "function": {
                "name": f"hire_{agent['agent_id'].replace('-', '_')}",
                "description": (
                    f"{agent.get('description', '')} "
                    f"[Aztea: {agent['agent_id']} | "
                    f"${float(agent.get('price_per_call_usd', 0)):.4f}/call]"
                ).strip(),
                "parameters": {
                    "type": "object",
                    "properties": props if props else {"input": {"type": "string", "description": "Task input"}},
                    "required": required if required else [],
                },
                "metadata": {
                    "agentmarket_agent_id": agent["agent_id"],
                    "price_per_call_usd": float(agent.get("price_per_call_usd", 0)),
                    "trust_score": agent.get("trust_score"),
                    "success_rate": agent.get("success_rate"),
                    "hire_endpoint": f"{_SERVER_BASE_URL}/jobs",
                },
            },
        })
    return JSONResponse(content={"tools": tools, "count": len(tools), "hire_endpoint": f"{_SERVER_BASE_URL}/jobs"})


@app.get(
    "/mcp/tools",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def mcp_tools_manifest(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    tools, _ = _mcp_tools_and_lookup()
    return JSONResponse(content={"tools": tools, "count": len(tools)})


@app.get(
    "/mcp/manifest",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def mcp_manifest_payload(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "caller")
    tools, _ = _mcp_tools_and_lookup()
    return JSONResponse(
        content={
            "schema_version": "v1",
            "name": "agentmarket",
            "description": "AI agent marketplace — specialized agents as callable tools",
            "tools": tools,
        }
    )


_MCP_COMPUTE_HEAVY_AGENT_IDS = frozenset({
    _PYTHON_EXECUTOR_AGENT_ID,
    _IMAGE_GENERATOR_AGENT_ID,
})


@app.post(
    "/mcp/invoke",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 402, 404, 429, 500),
)
def mcp_invoke(
    request: Request,
    body: MCPInvokeRequest,
) -> core_models.DynamicObjectResponse:
    # 1. Auth: accept agent keys (amk_), regular user caller keys (am_), or master key.
    raw_key = str(body.api_key or "").strip()
    if not raw_key:
        raise HTTPException(
            status_code=401,
            detail=error_codes.make_error("auth.invalid_key", "API key is required."),
        )
    agent_key = _auth.verify_agent_api_key(raw_key)
    user_key = None
    if agent_key is None:
        user_key = _auth.verify_api_key(raw_key)
        if user_key is not None and "caller" not in (user_key.get("scopes") or []):
            user_key = None
    is_master = hmac.compare_digest(raw_key, _MASTER_KEY)
    if agent_key is None and user_key is None and not is_master:
        raise HTTPException(
            status_code=403,
            detail=error_codes.make_error("auth.invalid_key", "Invalid or inactive API key."),
        )
    caller_key_id = str(
        (agent_key or {}).get("key_id")
        or (user_key or {}).get("key_id")
        or "master"
    )

    # 2. Per-key sliding-window rate limit: 60 req/min.
    if not _mcp_check_rate_limit(caller_key_id):
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": "60"},
            detail=error_codes.make_error(
                error_codes.RATE_LIMITED,
                "MCP rate limit exceeded. Maximum 60 requests per minute per key.",
            ),
        )

    # 3. Tool lookup.
    _, lookup = _mcp_tools_and_lookup()
    agent = lookup.get(body.tool_name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Tool '{body.tool_name}' not found.")

    # Build caller context from the agent key.
    if agent_key is not None:
        caller: core_models.CallerContext = {
            "type": "agent_key",
            "owner_id": f"agent_key:{agent_key['agent_id']}",
            "agent_id": str(agent_key["agent_id"]),
            "key_id": caller_key_id,
            "scopes": ["worker"],
        }
    elif user_key is not None:
        caller = {
            "type": "user",
            "owner_id": f"user:{user_key['user_id']}",
            "key_id": caller_key_id,
            "scopes": user_key.get("scopes") or ["caller"],
        }
    else:
        caller = {"type": "master", "owner_id": "master", "scopes": ["caller", "worker", "admin"]}

    # 4. Dispatch. registry_call owns pre-call charge, payout, and refund-on-failure.
    agent_id = str(agent["agent_id"])
    request.state._caller = caller
    t0 = time.monotonic()
    success = False
    try:
        delegated = registry_call(
            request=request,
            agent_id=agent_id,
            body=core_models.RegistryCallRequest(root=body.input),
            caller=caller,
        )
        success = True
    except Exception:
        raise
    duration_ms = int((time.monotonic() - t0) * 1000)

    # 6. Audit log (non-blocking; failure does not abort the response).
    input_json = json.dumps(body.input, default=str) if body.input is not None else "{}"
    _mcp_log_invocation(agent_id, caller_key_id, body.tool_name, input_json, duration_ms, success)

    payload = _mcp_payload_from_response(delegated)
    response_body: dict[str, Any] = {
        "content": _mcp_content_from_payload(payload),
    }
    if isinstance(payload, dict):
        response_body["structuredContent"] = payload
    elif payload is not None:
        response_body["structuredContent"] = {"result": payload}
    return JSONResponse(content=response_body)


def _normalize_model_provider_filter(raw_value: str | None) -> str | None:
    text = str(raw_value or "").strip().lower()
    if not text:
        return None
    normalized = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-")
    return normalized or None


def _build_model_catalog(
    agents: list[dict[str, Any]],
    *,
    model_provider: str | None = None,
    model_id: str | None = None,
    include_examples: bool = True,
    example_limit: int = 5,
) -> list[dict[str, Any]]:
    normalized_provider = _normalize_model_provider_filter(model_provider)
    normalized_model_id = str(model_id or "").strip() or None
    capped_examples = min(max(0, int(example_limit)), 20)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for agent in agents:
        provider = _normalize_model_provider_filter(agent.get("model_provider"))
        model = str(agent.get("model_id") or "").strip()
        if not provider or not model:
            continue
        if normalized_provider and provider != normalized_provider:
            continue
        if normalized_model_id and model != normalized_model_id:
            continue
        key = (provider, model)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "model_provider": provider,
                "model_id": model,
                "agent_count": 0,
                "total_calls": 0,
                "avg_success_rate": 0.0,
                "agents": [],
                "work_examples": [],
            }
            grouped[key] = bucket
        bucket["agent_count"] += 1
        bucket["total_calls"] += int(agent.get("total_calls") or 0)
        bucket["agents"].append(
            {
                "agent_id": str(agent.get("agent_id") or ""),
                "name": str(agent.get("name") or ""),
                "price_per_call_usd": float(agent.get("price_per_call_usd") or 0.0),
                "success_rate": float(agent.get("success_rate") or 0.0),
            }
        )
        if include_examples and capped_examples > 0 and len(bucket["work_examples"]) < capped_examples:
            examples = agent.get("output_examples")
            if isinstance(examples, list):
                for example in examples:
                    if not isinstance(example, dict):
                        continue
                    bucket["work_examples"].append(
                        {
                            "agent_id": str(agent.get("agent_id") or ""),
                            "agent_name": str(agent.get("name") or ""),
                            "example": example,
                        }
                    )
                    if len(bucket["work_examples"]) >= capped_examples:
                        break

    models: list[dict[str, Any]] = []
    for bucket in grouped.values():
        model_agents = bucket.pop("agents")
        if model_agents:
            bucket["avg_success_rate"] = round(
                sum(float(item.get("success_rate") or 0.0) for item in model_agents) / len(model_agents),
                6,
            )
        else:
            bucket["avg_success_rate"] = 0.0
        bucket["agents"] = sorted(
            model_agents,
            key=lambda item: (item.get("success_rate") or 0.0, -(item.get("price_per_call_usd") or 0.0)),
            reverse=True,
        )
        models.append(bucket)

    return sorted(
        models,
        key=lambda item: (item.get("agent_count") or 0, item.get("total_calls") or 0),
        reverse=True,
    )


@app.get(
    "/registry/models",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def registry_models_list(
    request: Request,
    model_provider: str | None = None,
    model_id: str | None = None,
    include_examples: bool = True,
    example_limit: int = 5,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    include_unapproved = _caller_is_admin(caller)
    agents = registry.get_agents_with_reputation(
        include_unapproved=include_unapproved,
    )
    models = _build_model_catalog(
        agents,
        model_provider=model_provider,
        model_id=model_id,
        include_examples=include_examples,
        example_limit=example_limit,
    )
    return JSONResponse(content={"models": models, "count": len(models)})


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
    model_provider: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistryAgentsResponse:
    _require_any_scope(caller, "caller", "worker")
    include_unapproved = _caller_is_admin(caller)
    try:
        agents = (
            registry.get_agents_with_reputation(
                tag=tag,
                include_unapproved=include_unapproved,
                model_provider=model_provider,
            )
            if include_reputation
            else registry.get_agents(
                tag=tag,
                include_unapproved=include_unapproved,
                model_provider=model_provider,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    agents = _sorted_agents(agents, rank_by=rank_by)
    bulk_stats = _compute_bulk_agent_stats([a["agent_id"] for a in agents])
    return JSONResponse(content={"agents": [_agent_response(a, caller, bulk_stats.get(a["agent_id"])) for a in agents], "count": len(agents)})


@app.get(
    "/registry/agents/mine",
    responses=_error_responses(401, 403, 429, 500),
    tags=["Registry"],
    summary="List agents owned by the authenticated caller.",
)
@limiter.limit("60/minute")
def registry_list_mine(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    agents = registry.get_agents_by_owner(caller["owner_id"])
    bulk_stats = _compute_bulk_agent_stats([a["agent_id"] for a in agents])
    return JSONResponse(content={"agents": [_agent_response(a, caller, bulk_stats.get(a["agent_id"])) for a in agents], "count": len(agents)})


@app.post(
    "/registry/search",
    response_model=core_models.RegistrySearchResponse,
    responses=_error_responses(400, 401, 403, 422, 429, 500),
)
@limiter.limit(_SEARCH_RATE_LIMIT)
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
        include_unapproved = _caller_is_admin(caller)
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
            include_unapproved=include_unapproved,
            model_provider=body.model_provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    search_stats = _compute_bulk_agent_stats([item["agent"]["agent_id"] for item in ranked])
    results = [
        {
            "agent": _agent_response(item["agent"], caller, search_stats.get(item["agent"]["agent_id"])),
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
    _require_any_scope(caller, "caller", "worker")
    include_unapproved = _caller_is_admin(caller)
    agent = registry.get_agent_with_reputation(agent_id, include_unapproved=include_unapproved)
    if agent is None or agent.get("status") == "banned" or not _caller_can_access_agent(caller, agent):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    agent_stats = _compute_bulk_agent_stats([agent_id]).get(agent_id)
    return JSONResponse(content=_agent_response(agent, caller, agent_stats))


@app.get(
    "/registry/agents/{agent_id}/work-history",
    response_model=core_models.DynamicListResponse,
    responses=_error_responses(401, 404, 429, 500),
)
@limiter.limit("60/minute")
def registry_agent_work_history(
    request: Request,
    agent_id: str,
    limit: int = 20,
    offset: int = 0,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicListResponse:
    """Return paginated public work examples for an agent."""
    capped_limit = max(1, min(int(limit), 50))
    capped_offset = max(0, int(offset))
    agent = registry.get_agent(agent_id)
    if agent is None or agent.get("status") == "banned" or not _caller_can_access_agent(caller, agent):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    examples: list = agent.get("output_examples") or []
    page = examples[capped_offset: capped_offset + capped_limit]
    return JSONResponse(content={"items": page, "total": len(examples), "limit": capped_limit, "offset": capped_offset})


@app.get(
    "/llm/providers",
    responses=_error_responses(401, 429, 500),
)
@limiter.limit("60/minute")
def llm_providers_list(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
):
    """List all registered LLM providers and their availability."""
    from core.llm import registry as llm_registry
    providers = llm_registry.list_providers()
    return JSONResponse(content={"providers": providers})


@app.get(
    "/registry/agents/{agent_id}/keys",
    response_model=core_models.AgentKeyListResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def registry_agent_key_list(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentKeyListResponse:
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(status_code=403, detail="Agent-scoped keys cannot list keys.")
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    keys = _auth.list_agent_api_keys(agent_id)
    return JSONResponse(content={"keys": keys})


@app.post(
    "/registry/agents/{agent_id}/keys",
    status_code=201,
    response_model=core_models.AgentKeyCreateResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def registry_agent_key_create(
    request: Request,
    agent_id: str,
    body: AgentKeyCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentKeyCreateResponse:
    _require_scope(caller, "worker")
    if caller["type"] == "agent_key":
        raise HTTPException(status_code=403, detail="Agent-scoped keys cannot mint new keys.")
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None or agent.get("status") == "banned":
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_manage_agent(caller, agent):
        raise HTTPException(status_code=403, detail="Not authorized.")
    key = _auth.create_agent_api_key(agent_id, name=body.name)
    return JSONResponse(
        content={
            "key_id": key["key_id"],
            "agent_id": key["agent_id"],
            "raw_key": key["raw_key"],
            "key_prefix": key["key_prefix"],
            "created_at": key["created_at"],
        },
        status_code=201,
    )


@app.post(
    "/admin/agents/{agent_id}/suspend",
    response_model=core_models.AgentResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def admin_agent_suspend(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.AgentResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    agent = registry.set_agent_status(agent_id, "suspended")
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return JSONResponse(content=_agent_response(agent, caller))


@app.post(
    "/admin/agents/{agent_id}/ban",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("20/minute")
def admin_agent_ban(
    request: Request,
    agent_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    agent = registry.set_agent_status(agent_id, "banned")
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    summary = _fail_open_jobs_for_agent(
        agent_id,
        actor_owner_id=caller["owner_id"],
        reason="Agent was banned by an administrator.",
    )
    return JSONResponse(content={"agent": _agent_response(agent, caller), "ban_summary": summary})


@app.get(
    "/admin/agents/review-queue",
    response_model=core_models.RegistryAgentsResponse,
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def admin_agents_review_queue(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.RegistryAgentsResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    agents = registry.list_pending_review_agents()
    return JSONResponse(content={"agents": [_agent_response(agent, caller) for agent in agents], "count": len(agents)})


@app.post(
    "/admin/agents/{agent_id}/review",
    response_model=core_models.DynamicObjectResponse,
    responses=_error_responses(400, 401, 403, 404, 429, 500),
)
@limiter.limit("30/minute")
def admin_review_agent(
    request: Request,
    agent_id: str,
    body: AgentReviewDecisionRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DynamicObjectResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    reviewed = registry.set_agent_review_decision(
        agent_id,
        decision=body.decision,
        note=body.note,
        reviewed_by=caller["owner_id"],
        reviewed_at=_utc_now_iso(),
    )
    if reviewed is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")

    health_probe: dict[str, Any] | None = None
    probe_url = str(reviewed.get("healthcheck_url") or "").strip()
    if body.decision == "approve" and probe_url:
        try:
            ok, error_text = _probe_agent_endpoint_health(
                probe_url,
                timeout_seconds=_ENDPOINT_MONITOR_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            ok = False
            error_text = str(exc)
        endpoint_status = "healthy" if ok else "degraded"
        endpoint_failures = 0 if ok else max(1, int(reviewed.get("endpoint_consecutive_failures") or 0) + 1)
        reviewed = registry.set_agent_endpoint_health(
            agent_id,
            endpoint_health_status=endpoint_status,
            endpoint_consecutive_failures=endpoint_failures,
            endpoint_last_checked_at=_utc_now_iso(),
            endpoint_last_error=None if ok else error_text,
        ) or reviewed
        health_probe = {"ok": bool(ok), "error": error_text}

    return JSONResponse(content={"agent": _agent_response(reviewed, caller), "health_probe": health_probe})


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
    Invoke a registered agent with full payment lifecycle:
      1. Deduct price (402 if broke).
      2. Dispatch call (internal handler for internal:// endpoints, HTTP otherwise).
      3a. Success → payout 90% agent / 10% platform.
      3b. Failure → full refund to caller.
    """
    _require_scope(caller, "caller")
    agent = registry.get_agent(agent_id, include_unapproved=True)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    if not _caller_can_access_agent(caller, agent):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    _assert_agent_callable(agent_id, agent)
    builtin_agent_id = _resolve_builtin_agent_id(agent)
    safe_endpoint_url = ""
    if builtin_agent_id is None:
        try:
            safe_endpoint_url = _validate_agent_endpoint_url(request, str(agent.get("endpoint_url") or ""))
        except ValueError as exc:
            _LOG.warning("Blocked misconfigured endpoint for agent %s: %s", agent_id, exc)
            raise HTTPException(status_code=502, detail="Agent endpoint is misconfigured.")

    caller_owner_id = _caller_owner_id(request)
    price_cents     = _usd_to_cents(agent["price_per_call_usd"])
    fee_bearer_policy = "caller"
    platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
    success_distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )
    caller_charge_cents = int(success_distribution["caller_charge_cents"])
    caller_wallet   = payments.get_or_create_wallet(caller_owner_id)
    # Payouts settle to the canonical agent wallet keyed by agent_id.
    _agent_payout_owner = f"agent:{agent['agent_id']}"
    agent_wallet    = payments.get_or_create_wallet(_agent_payout_owner)
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    charge_tx_id = _pre_call_charge_or_402(
        caller=caller,
        caller_wallet_id=caller_wallet["wallet_id"],
        charge_cents=caller_charge_cents,
        agent_id=agent_id,
    )

    payload = dict(body.root) if body is not None else {}
    requested_output_formats: list[str] = []
    try:
        payload, requested_output_formats = _normalize_input_protocol_from_payload(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_INPUT,
                str(exc),
                {"agent_id": agent_id},
            ),
        )
    start = time.monotonic()
    if builtin_agent_id is not None:
        try:
            job = jobs.create_job(
                agent_id=agent["agent_id"],
                caller_owner_id=caller_owner_id,
                caller_wallet_id=caller_wallet["wallet_id"],
                agent_wallet_id=agent_wallet["wallet_id"],
                platform_wallet_id=platform_wallet["wallet_id"],
                price_cents=price_cents,
                caller_charge_cents=caller_charge_cents,
                platform_fee_pct_at_create=platform_fee_pct_at_create,
                fee_bearer_policy=fee_bearer_policy,
                charge_tx_id=charge_tx_id,
                input_payload=payload,
                agent_owner_id=agent.get("owner_id"),
                max_attempts=1,
                dispute_window_hours=_DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
                judge_agent_id=_extract_judge_agent_id(agent.get("input_schema")) or _QUALITY_JUDGE_AGENT_ID,
            )
        except Exception:
            payments.post_call_refund(
                caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent["agent_id"]
            )
            _LOG.exception("Failed to create sync job for built-in agent %s.", agent["agent_id"])
            raise HTTPException(status_code=500, detail="Failed to create job.")
        _record_job_event(
            job,
            "job.created",
            actor_owner_id=caller["owner_id"],
            payload={"source": "registry_call_sync", "max_attempts": 1},
        )
        try:
            output = _execute_builtin_agent(builtin_agent_id, payload)
            output = _normalize_output_protocol_for_response(
                output,
                requested_output_formats=requested_output_formats,
            )
            completed = jobs.update_job_status(
                job["job_id"],
                "complete",
                output_payload=output,
                completed=True,
            )
            if completed is None:
                raise RuntimeError("Failed to mark built-in sync job complete.")
            _record_job_event(
                completed,
                "job.completed",
                actor_owner_id=caller["owner_id"],
                payload={"status": completed["status"], "source": "registry_call_sync"},
            )
            _settle_successful_job(completed, actor_owner_id=caller["owner_id"])
            _record_public_work_example(
                agent,
                payload,
                output,
                job_id=job["job_id"],
                latency_ms=_job_latency_ms(completed),
            )
            return JSONResponse(content=output)
        except ValidationError as exc:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message="Request validation failed.",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_validation",
                )
            def _sanitize_errors(errors):
                clean = []
                for e in errors:
                    entry = {k: v for k, v in e.items() if k != "ctx"}
                    ctx = e.get("ctx")
                    if ctx:
                        entry["ctx"] = {k: str(v) for k, v in ctx.items()}
                    clean.append(entry)
                return clean

            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.INVALID_INPUT,
                    "Request validation failed.",
                    {"errors": _sanitize_errors(exc.errors())},
                ),
            )
        except ValueError as exc:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message=str(exc),
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_input",
                )
            message = str(exc)
            status = 422 if message.startswith("Invalid ticker symbol:") else 400
            raise HTTPException(status_code=status, detail=message)
        except _groq.RateLimitError as exc:
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message=f"All LLM models rate-limited. ({exc})",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_rate_limit",
                )
            raise HTTPException(status_code=503, detail=f"All LLM models rate-limited. ({exc})")
        except Exception:
            _LOG.exception("Built-in agent execution failed for %s.", builtin_agent_id)
            failed = jobs.update_job_status(
                job["job_id"],
                "failed",
                error_message="Agent execution failed.",
                completed=True,
            )
            if failed is not None:
                _settle_failed_job(
                    failed,
                    actor_owner_id=caller["owner_id"],
                    event_type="job.failed_builtin",
                )
            raise HTTPException(status_code=500, detail="Agent execution failed.")

    try:
        proxy_agent = dict(agent)
        proxy_agent["endpoint_url"] = safe_endpoint_url
        resp = http.post(
            safe_endpoint_url,
            json=payload,
            headers=_proxy_headers_for_agent(proxy_agent),
            timeout=120,
            allow_redirects=False,
        )
    except http.RequestException as e:
        latency_ms = (time.monotonic() - start) * 1000
        registry.update_call_stats(agent_id, latency_ms, False)
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
        )
        _LOG.warning("Upstream agent unreachable for %s: %s", agent_id, e)
        raise HTTPException(status_code=502, detail="Upstream agent unreachable.")

    success = 200 <= int(resp.status_code) < 300
    latency_ms = (time.monotonic() - start) * 1000
    registry.update_call_stats(agent_id, latency_ms, success)

    if success:
        payments.post_call_payout(
            agent_wallet["wallet_id"], platform_wallet["wallet_id"],
            charge_tx_id, price_cents, agent_id,
            platform_fee_pct=platform_fee_pct_at_create,
            fee_bearer_policy=fee_bearer_policy,
        )
    else:
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent_id
        )

    return _proxy_response(resp)


# ---------------------------------------------------------------------------
# Jobs routes
# ---------------------------------------------------------------------------

@app.post(
    "/jobs",
    status_code=201,
    response_model=core_models.JobResponse,
    responses=_error_responses(400, 401, 402, 403, 404, 422, 429, 500, 503),
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def jobs_create(
    request: Request,
    body: JobCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "caller")
    parent_job = _resolve_parent_job_for_creation(
        caller,
        body.parent_job_id,
        parent_cascade_policy=body.parent_cascade_policy,
    )
    parent_tree_depth = _to_non_negative_int((parent_job or {}).get("tree_depth"), default=0)
    tree_depth = parent_tree_depth + 1 if parent_job is not None else 0
    if tree_depth >= 10:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.ORCHESTRATION_DEPTH_EXCEEDED,
                "Maximum orchestration depth is 10 levels.",
                {"max_depth": 10, "attempted_depth": tree_depth},
            ),
        )
    agent = registry.get_agent(body.agent_id, include_unapproved=True)
    if agent is None or not _caller_can_access_agent(caller, agent):
        raise HTTPException(status_code=404, detail=f"Agent '{body.agent_id}' not found.")
    _assert_agent_callable(body.agent_id, agent)

    caller_owner_id = _caller_owner_id(request)
    min_caller_trust = _extract_caller_trust_min(agent.get("input_schema"))
    if min_caller_trust is not None and caller["type"] != "master":
        caller_trust = _caller_trust_score(caller_owner_id)
        if caller_trust < min_caller_trust:
            raise HTTPException(
                status_code=403,
                detail=error_codes.make_error(
                    error_codes.UNAUTHORIZED,
                    "Caller trust is below this agent's required minimum.",
                    {
                        "caller_trust": round(caller_trust, 6),
                        "required_min_caller_trust": round(min_caller_trust, 6),
                        "agent_id": agent["agent_id"],
                    },
                ),
            )

    price_cents = _usd_to_cents(agent["price_per_call_usd"])
    fee_bearer_policy = payments.normalize_fee_bearer_policy(body.fee_bearer_policy)
    platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
    success_distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=platform_fee_pct_at_create,
        fee_bearer_policy=fee_bearer_policy,
    )
    caller_charge_cents = int(success_distribution["caller_charge_cents"])
    if caller_charge_cents <= 0 and price_cents > 0:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.INVALID_CHARGE_AMOUNT,
                "Computed caller charge is non-positive.",
                {"caller_charge_cents": caller_charge_cents, "price_cents": price_cents},
            ),
        )
    if caller_charge_cents > price_cents * 2:
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.CHARGE_EXCEEDS_LISTED_PRICE,
                "Caller charge must not exceed twice the listed price.",
                {"caller_charge_cents": caller_charge_cents, "price_cents": price_cents},
            ),
        )
    if body.budget_cents is not None and price_cents > body.budget_cents:
        raise HTTPException(
            status_code=400,
            detail=error_codes.make_error(
                error_codes.BUDGET_EXCEEDED,
                f"Agent price ({price_cents}¢) exceeds your budget ({body.budget_cents}¢).",
                {"price_cents": price_cents, "budget_cents": body.budget_cents, "agent_id": agent["agent_id"]},
            ),
        )
    if price_cents > 5000 and not _agent_has_verified_contract(agent):
        raise HTTPException(
            status_code=422,
            detail=error_codes.make_error(
                error_codes.VERIFIED_CONTRACT_REQUIRED,
                "Jobs above $50 require a worker with a verified input/output contract.",
                {"agent_id": agent["agent_id"], "price_cents": price_cents},
            ),
        )
    key_per_job_cap_cents = _caller_key_per_job_cap(caller)
    if key_per_job_cap_cents is not None and price_cents > key_per_job_cap_cents:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.SPEND_LIMIT_EXCEEDED,
                "API key per-job cap exceeded.",
                {
                    "scope": "api_key_per_job",
                    "key_id": str(caller.get("key_id") or "").strip() or None,
                    "limit_cents": key_per_job_cap_cents,
                    "attempted_cents": price_cents,
                },
            ),
        )
    output_verification_window_seconds = (
        86400
        if body.output_verification_window_seconds is None
        else body.output_verification_window_seconds
    )
    try:
        input_payload = _merge_protocol_input_envelope(
            body.input_payload,
            input_artifacts=_normalize_protocol_artifact_list(
                body.input_artifacts,
                field_name="input_artifacts",
            ),
            preferred_input_formats=_normalize_format_preferences(
                body.preferred_input_formats,
                field_name="preferred_input_formats",
            ),
            preferred_output_formats=_normalize_format_preferences(
                body.preferred_output_formats,
                field_name="preferred_output_formats",
            ),
            communication_channel=_normalize_protocol_channel(
                body.communication_channel,
                field_name="communication_channel",
            ),
            protocol_metadata=_normalize_protocol_metadata(
                body.protocol_metadata,
                field_name="protocol_metadata",
            ),
            private_task=bool(body.private_task),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    _agent_payout_owner2 = f"agent:{agent['agent_id']}"
    agent_wallet = payments.get_or_create_wallet(_agent_payout_owner2)
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)

    charge_tx_id = _pre_call_charge_or_402(
        caller=caller,
        caller_wallet_id=caller_wallet["wallet_id"],
        charge_cents=caller_charge_cents,
        agent_id=agent["agent_id"],
    )

    try:
        job = jobs.create_job(
            agent_id=agent["agent_id"],
            caller_owner_id=caller_owner_id,
            caller_wallet_id=caller_wallet["wallet_id"],
            agent_wallet_id=agent_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            price_cents=price_cents,
            caller_charge_cents=caller_charge_cents,
            platform_fee_pct_at_create=platform_fee_pct_at_create,
            fee_bearer_policy=fee_bearer_policy,
            charge_tx_id=charge_tx_id,
            input_payload=input_payload,
            agent_owner_id=agent.get("owner_id"),
            max_attempts=body.max_attempts,
            parent_job_id=(parent_job or {}).get("job_id"),
            tree_depth=tree_depth,
            parent_cascade_policy=body.parent_cascade_policy,
            clarification_timeout_seconds=body.clarification_timeout_seconds,
            clarification_timeout_policy=body.clarification_timeout_policy,
            dispute_window_hours=body.dispute_window_hours or _DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
            judge_agent_id=_extract_judge_agent_id(agent.get("input_schema")) or _QUALITY_JUDGE_AGENT_ID,
            callback_url=body.callback_url or None,
            callback_secret=body.callback_secret or None,
            output_verification_window_seconds=output_verification_window_seconds,
        )
    except Exception:
        payments.post_call_refund(
            caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent["agent_id"]
        )
        _LOG.exception("Failed to create job for agent %s.", agent["agent_id"])
        raise HTTPException(status_code=500, detail="Failed to create job.")

    _record_job_event(
        job,
        "job.created",
        actor_owner_id=caller["owner_id"],
        payload={
            "max_attempts": body.max_attempts,
            "parent_job_id": (parent_job or {}).get("job_id"),
            "parent_cascade_policy": body.parent_cascade_policy,
            "tree_depth": tree_depth,
        },
    )
    return JSONResponse(content=_job_response(job, caller), status_code=201)


@app.post(
    "/jobs/batch",
    status_code=201,
    responses=_error_responses(400, 401, 402, 403, 422, 429, 500),
    tags=["Jobs"],
    summary="Create up to 50 jobs atomically. Single wallet pre-debit for total cost.",
)
@limiter.limit(_JOBS_CREATE_RATE_LIMIT)
def jobs_batch_create(
    request: Request,
    body: core_models.JobBatchCreateRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    if not body.jobs:
        raise HTTPException(status_code=400, detail="jobs array must not be empty.")
    if len(body.jobs) > 50:
        raise HTTPException(status_code=400, detail="Batch size limited to 50 jobs.")

    caller_owner_id = _caller_owner_id(request)
    batch_id = str(uuid.uuid4())

    resolved: list[dict] = []
    total_price_cents = 0
    key_per_job_cap_cents = _caller_key_per_job_cap(caller)
    for spec in body.jobs:
        parent_job = _resolve_parent_job_for_creation(
            caller,
            spec.parent_job_id,
            parent_cascade_policy=spec.parent_cascade_policy,
        )
        parent_tree_depth = _to_non_negative_int((parent_job or {}).get("tree_depth"), default=0)
        tree_depth = parent_tree_depth + 1 if parent_job is not None else 0
        if tree_depth >= 10:
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.ORCHESTRATION_DEPTH_EXCEEDED,
                    "Maximum orchestration depth is 10 levels.",
                    {"max_depth": 10, "attempted_depth": tree_depth},
                ),
            )
        agent = registry.get_agent(spec.agent_id, include_unapproved=True)
        if agent is None or not _caller_can_access_agent(caller, agent):
            raise HTTPException(status_code=404, detail=f"Agent '{spec.agent_id}' not found.")
        _assert_agent_callable(spec.agent_id, agent)
        price_cents = _usd_to_cents(agent["price_per_call_usd"])
        if price_cents > 5000 and not _agent_has_verified_contract(agent):
            raise HTTPException(
                status_code=422,
                detail=error_codes.make_error(
                    error_codes.VERIFIED_CONTRACT_REQUIRED,
                    "Jobs above $50 require a worker with a verified input/output contract.",
                    {"agent_id": agent["agent_id"], "price_cents": price_cents},
                ),
            )
        if key_per_job_cap_cents is not None and price_cents > key_per_job_cap_cents:
            raise HTTPException(
                status_code=402,
                detail=error_codes.make_error(
                    error_codes.SPEND_LIMIT_EXCEEDED,
                    "API key per-job cap exceeded.",
                    {
                        "scope": "api_key_per_job",
                        "key_id": str(caller.get("key_id") or "").strip() or None,
                        "limit_cents": key_per_job_cap_cents,
                        "attempted_cents": price_cents,
                        "agent_id": agent["agent_id"],
                    },
                ),
            )
        fee_bearer_policy = payments.normalize_fee_bearer_policy(spec.fee_bearer_policy)
        platform_fee_pct_at_create = int(payments.PLATFORM_FEE_PCT)
        success_distribution = payments.compute_success_distribution(
            price_cents,
            platform_fee_pct=platform_fee_pct_at_create,
            fee_bearer_policy=fee_bearer_policy,
        )
        caller_charge_cents = int(success_distribution["caller_charge_cents"])
        if spec.budget_cents is not None and price_cents > spec.budget_cents:
            raise HTTPException(
                status_code=400,
                detail=error_codes.make_error(
                    error_codes.BUDGET_EXCEEDED,
                    f"Agent '{spec.agent_id}' price ({price_cents}¢) exceeds budget ({spec.budget_cents}¢).",
                    {"agent_id": spec.agent_id, "price_cents": price_cents, "budget_cents": spec.budget_cents},
                ),
            )
        try:
            normalized_spec_input_payload = _merge_protocol_input_envelope(
                spec.input_payload,
                input_artifacts=_normalize_protocol_artifact_list(
                    spec.input_artifacts,
                    field_name="jobs[].input_artifacts",
                ),
                preferred_input_formats=_normalize_format_preferences(
                    spec.preferred_input_formats,
                    field_name="jobs[].preferred_input_formats",
                ),
                preferred_output_formats=_normalize_format_preferences(
                    spec.preferred_output_formats,
                    field_name="jobs[].preferred_output_formats",
                ),
                communication_channel=_normalize_protocol_channel(
                    spec.communication_channel,
                    field_name="jobs[].communication_channel",
                ),
                protocol_metadata=_normalize_protocol_metadata(
                    spec.protocol_metadata,
                    field_name="jobs[].protocol_metadata",
                ),
                private_task=bool(spec.private_task),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        total_price_cents += caller_charge_cents
        resolved.append(
            {
                "agent": agent,
                "price_cents": price_cents,
                "caller_charge_cents": caller_charge_cents,
                "platform_fee_pct_at_create": platform_fee_pct_at_create,
                "fee_bearer_policy": fee_bearer_policy,
                "spec": spec,
                "input_payload": normalized_spec_input_payload,
                "parent_job_id": (parent_job or {}).get("job_id"),
                "tree_depth": tree_depth,
            }
        )

    caller_wallet = payments.get_or_create_wallet(caller_owner_id)
    if caller_wallet["balance_cents"] < total_price_cents:
        raise HTTPException(
            status_code=402,
            detail=error_codes.make_error(
                error_codes.INSUFFICIENT_FUNDS,
                "Insufficient balance for batch.",
                {"balance_cents": caller_wallet["balance_cents"], "required_cents": total_price_cents},
            ),
        )

    created_jobs = []
    charge_tx_ids = []
    try:
        for item in resolved:
            agent = item["agent"]
            price_cents = item["price_cents"]
            caller_charge_cents = item["caller_charge_cents"]
            platform_fee_pct_at_create = item["platform_fee_pct_at_create"]
            fee_bearer_policy = item["fee_bearer_policy"]
            spec = item["spec"]
            input_payload = item["input_payload"]
            parent_job_id = item["parent_job_id"]
            tree_depth = item["tree_depth"]
            agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
            platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
            charge_tx_id = _pre_call_charge_or_402(
                caller=caller,
                caller_wallet_id=caller_wallet["wallet_id"],
                charge_cents=caller_charge_cents,
                agent_id=agent["agent_id"],
            )
            charge_tx_ids.append((caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent["agent_id"]))
            job = jobs.create_job(
                agent_id=agent["agent_id"],
                caller_owner_id=caller_owner_id,
                caller_wallet_id=caller_wallet["wallet_id"],
                agent_wallet_id=agent_wallet["wallet_id"],
                platform_wallet_id=platform_wallet["wallet_id"],
                price_cents=price_cents,
                caller_charge_cents=caller_charge_cents,
                platform_fee_pct_at_create=platform_fee_pct_at_create,
                fee_bearer_policy=fee_bearer_policy,
                charge_tx_id=charge_tx_id,
                input_payload=input_payload,
                agent_owner_id=agent.get("owner_id"),
                max_attempts=spec.max_attempts,
                parent_job_id=parent_job_id,
                tree_depth=tree_depth,
                parent_cascade_policy=spec.parent_cascade_policy,
                clarification_timeout_seconds=spec.clarification_timeout_seconds,
                clarification_timeout_policy=spec.clarification_timeout_policy,
                dispute_window_hours=spec.dispute_window_hours or _DEFAULT_JOB_DISPUTE_WINDOW_HOURS,
                judge_agent_id=_extract_judge_agent_id(agent.get("input_schema")) or _QUALITY_JUDGE_AGENT_ID,
                callback_url=spec.callback_url or None,
                callback_secret=spec.callback_secret or None,
                output_verification_window_seconds=(
                    86400
                    if spec.output_verification_window_seconds is None
                    else spec.output_verification_window_seconds
                ),
                batch_id=batch_id,
            )
            _record_job_event(job, "job.created", actor_owner_id=caller["owner_id"])
            created_jobs.append(_job_response(job, caller))
    except HTTPException:
        for wallet_id, charge_tx_id, price_cents, agent_id in charge_tx_ids:
            try:
                payments.post_call_refund(wallet_id, charge_tx_id, price_cents, agent_id)
            except Exception as exc:
                _LOG.exception(
                    "Batch refund failed after handled error (wallet=%s charge_tx_id=%s agent=%s): %s",
                    wallet_id,
                    charge_tx_id,
                    agent_id,
                    exc,
                )
        raise
    except Exception:
        for wallet_id, charge_tx_id, price_cents, agent_id in charge_tx_ids:
            try:
                payments.post_call_refund(wallet_id, charge_tx_id, price_cents, agent_id)
            except Exception as exc:
                _LOG.exception(
                    "Batch refund failed after unhandled error (wallet=%s charge_tx_id=%s agent=%s): %s",
                    wallet_id,
                    charge_tx_id,
                    agent_id,
                    exc,
                )
        raise HTTPException(status_code=500, detail="Batch creation failed; all charges refunded.")

    return JSONResponse(
        content={
            "batch_id": batch_id,
            "jobs": created_jobs,
            "count": len(created_jobs),
            "total_price_cents": total_price_cents,
        },
        status_code=201,
    )


@app.get(
    "/jobs/batch/{batch_id}",
    responses=_error_responses(401, 403, 404, 429, 500),
    tags=["Jobs"],
    summary="Get aggregate status for a batch created via POST /jobs/batch.",
)
@limiter.limit("60/minute")
def jobs_batch_status(
    request: Request,
    batch_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    owner_id = _caller_owner_id(request)
    batch_jobs = jobs.list_jobs_for_batch(batch_id, owner_id)
    if not batch_jobs:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found.")

    n_pending = 0
    n_running = 0
    n_awaiting_clarification = 0
    n_complete = 0
    n_failed = 0
    total_cost_cents = 0
    for job in batch_jobs:
        total_cost_cents += int(job.get("price_cents") or 0)
        status = str(job.get("status") or "")
        if status == "pending":
            n_pending += 1
        elif status == "running":
            n_running += 1
            n_pending += 1
        elif status == "awaiting_clarification":
            n_awaiting_clarification += 1
            n_pending += 1
        elif status == "complete":
            n_complete += 1
        elif status == "failed":
            n_failed += 1

    return JSONResponse(
        content={
            "batch_id": batch_id,
            "count": len(batch_jobs),
            "n_pending": n_pending,
            "n_running": n_running,
            "n_awaiting_clarification": n_awaiting_clarification,
            "n_complete": n_complete,
            "n_failed": n_failed,
            "total_cost_cents": total_cost_cents,
            "jobs": [_job_response(job, caller) for job in batch_jobs],
        }
    )


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
    _require_scope(caller, "caller")
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
    _require_scope(caller, "caller")
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
    agent = registry.get_agent(agent_id, include_unapproved=True)
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
    agent = registry.get_agent(str(job.get("agent_id") or ""), include_unapproved=True)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if (
        not _caller_is_admin(caller)
        and str(agent.get("review_status") or "approved").strip().lower() != "approved"
    ):
        raise HTTPException(status_code=403, detail="Agent listing is pending review and cannot accept jobs.")

    if not _caller_worker_authorized_for_job(caller, job):
        status = 403 if caller["type"] == "agent_key" else 409
        detail = "Not authorized for this agent job." if status == 403 else "Job is not claimable."
        raise HTTPException(status_code=status, detail=detail)
    worker_owner_id = caller["owner_id"]
    require_auth = caller["type"] == "user"
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
    claimed["caller_owner_id"] = job.get("caller_owner_id")
    claimed["caller_trust_score"] = _caller_trust_score(str(job.get("caller_owner_id") or ""))
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
        timed_out_response = _job_response(timed_out, caller)
        return JSONResponse(
            content=_timeout_error_payload(timed_out_response),
            status_code=410,
        )

    if caller["type"] != "master":
        _assert_worker_claim(job, caller, worker_owner_id, body.claim_token)

    heartbeat = jobs.heartbeat_job_lease(
        job_id,
        claim_owner_id=worker_owner_id,
        lease_seconds=body.lease_seconds,
        claim_token=body.claim_token,
        require_authorized_owner=(caller["type"] == "user"),
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
        timed_out_response = _job_response(timed_out, caller)
        return JSONResponse(
            content=_timeout_error_payload(timed_out_response),
            status_code=410,
        )

    if caller["type"] != "master":
        _assert_worker_claim(job, caller, worker_owner_id, body.claim_token)

    released = jobs.release_job_claim(
        job_id,
        claim_owner_id=worker_owner_id,
        claim_token=body.claim_token,
        require_authorized_owner=(caller["type"] == "user"),
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
    responses=_error_responses(401, 403, 404, 409, 410, 422, 429, 500),
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
        if not _caller_worker_authorized_for_job(caller, job):
            raise HTTPException(status_code=403, detail="Not authorized for this agent job.")
        timed_out = _timeout_stale_lease_at_touchpoint(
            job,
            actor_owner_id=actor_owner_id,
            touchpoint="complete",
        )
        if timed_out is not None:
            timed_out_response = _job_response(timed_out, caller)
            return (
                _timeout_error_payload(timed_out_response),
                410,
            )

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
        try:
            normalized_output_payload = _merge_protocol_output_envelope(
                body.output_payload,
                output_artifacts=_normalize_protocol_artifact_list(
                    body.output_artifacts,
                    field_name="output_artifacts",
                ),
                output_format=(str(body.output_format).strip().lower() if body.output_format else None),
                protocol_metadata=_normalize_protocol_metadata(
                    body.protocol_metadata,
                    field_name="protocol_metadata",
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        agent = registry.get_agent(job["agent_id"], include_unapproved=True)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Agent '{job['agent_id']}' not found.")
        output_schema = agent.get("output_schema")
        if isinstance(output_schema, dict) and output_schema:
            mismatches = _validate_json_schema_subset(body.output_payload, output_schema)
            if mismatches:
                raise HTTPException(
                    status_code=422,
                    detail=error_codes.make_error(
                        error_codes.SCHEMA_MISMATCH,
                        "output_payload does not match the declared output_schema.",
                        {"mismatches": mismatches},
                    ),
                )

        quality = _run_quality_gate(job, agent, body.output_payload)
        jobs.set_job_quality_result(
            job_id,
            judge_verdict=quality["judge_verdict"],
            quality_score=quality["quality_score"],
            judge_agent_id=quality["judge_agent_id"],
        )
        if not quality["passed"]:
            failed = jobs.update_job_status(
                job_id,
                "failed",
                error_message=f"Quality judge failed: {quality['reason']}",
                completed=True,
            )
            if failed is None:
                raise HTTPException(status_code=409, detail="Unable to update job status.")
            settled_failed = _settle_failed_job(failed, actor_owner_id=actor_owner_id, event_type="job.failed_quality")
            return _job_response(settled_failed, caller), 200

        updated = jobs.update_job_status(
            job_id, "complete", output_payload=normalized_output_payload, completed=True
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="Unable to update job status.")
        initialized = jobs.initialize_output_verification_state(job_id)
        if initialized is not None:
            updated = initialized
        _record_job_event(
            updated,
            "job.completed",
            actor_owner_id=actor_owner_id,
            payload={
                "status": updated["status"],
                "output_verification_status": updated.get("output_verification_status"),
                "output_verification_deadline_at": updated.get("output_verification_deadline_at"),
            },
        )
        settled = _settle_successful_job(updated, actor_owner_id=actor_owner_id)
        distribution = payments.compute_success_distribution(
            int(updated.get("price_cents") or 0),
            platform_fee_pct=updated.get("platform_fee_pct_at_create"),
            fee_bearer_policy=updated.get("fee_bearer_policy"),
        )
        platform_fee_cents = int(distribution["platform_fee_cents"])
        judge_fee_cents = min(_JUDGE_FEE_CENTS, platform_fee_cents)
        if judge_fee_cents > 0:
            judge_wallet = payments.get_or_create_wallet(f"agent:{quality['judge_agent_id']}")
            payments.record_judge_fee(
                updated["platform_wallet_id"],
                judge_wallet["wallet_id"],
                charge_tx_id=updated["charge_tx_id"],
                agent_id=updated["agent_id"],
                fee_cents=judge_fee_cents,
            )
            settled = jobs.get_job(job_id) or settled
        caller_email = _get_owner_email(settled.get("caller_owner_id", ""))
        if caller_email:
            _agent_row = registry.get_agent(settled.get("agent_id", ""))
            _agent_name = (_agent_row or {}).get("name", "agent")
            _email.send_job_complete(caller_email, job_id, _agent_name, int(settled.get("price_cents") or 0))
        _record_public_work_example(
            agent,
            settled.get("input_payload") or {},
            normalized_output_payload,
            job_id=job_id,
            latency_ms=_job_latency_ms(settled),
            quality_score=quality.get("quality_score"),
        )
        return _job_response(settled, caller), 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.complete:{job_id}",
        payload={
            "output_payload": body.output_payload,
            "output_artifacts": body.output_artifacts,
            "output_format": body.output_format,
            "protocol_metadata": body.protocol_metadata,
            "claim_token": body.claim_token,
        },
        operation=_operation,
    )


@app.post(
    "/jobs/{job_id}/verification",
    response_model=core_models.JobResponse,
    responses=_error_responses(400, 401, 403, 404, 409, 429, 500),
)
@limiter.limit("30/minute")
def jobs_output_verification_decide(
    request: Request,
    job_id: str,
    body: JobVerificationDecisionRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobResponse:
    _require_scope(caller, "caller")

    def _operation() -> tuple[dict, int]:
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        if caller["type"] != "master" and caller["owner_id"] != job.get("caller_owner_id"):
            raise HTTPException(status_code=403, detail="Only the job caller can decide output verification.")
        if job.get("status") != "complete" or not job.get("completed_at"):
            raise HTTPException(status_code=400, detail="Output verification is only available for completed jobs.")
        if job.get("settled_at"):
            raise HTTPException(status_code=409, detail="Job is already settled.")

        initialized = jobs.initialize_output_verification_state(job_id) or job
        verification_status = _normalize_output_verification_status(initialized)
        if verification_status == "not_required":
            raise HTTPException(
                status_code=400,
                detail="This job does not have an output verification window configured.",
            )

        if verification_status == "pending":
            deadline = _parse_iso_datetime(initialized.get("output_verification_deadline_at"))
            if deadline is not None and datetime.now(timezone.utc) > deadline:
                expired = jobs.mark_output_verification_expired(
                    job_id,
                    decision_owner_id="system:verification-expiry-api",
                )
                if expired is not None:
                    initialized = expired
                    verification_status = "expired"
                    _record_job_event(
                        expired,
                        "job.output_verification_expired",
                        actor_owner_id=caller["owner_id"],
                        payload={"output_verification_deadline_at": expired.get("output_verification_deadline_at")},
                    )

        if body.decision == "accept":
            if disputes.has_dispute_for_job(job_id):
                raise HTTPException(status_code=409, detail="Cannot accept output after a dispute is already filed.")
            if verification_status == "accepted":
                settled = _settle_successful_job(
                    initialized,
                    actor_owner_id=caller["owner_id"],
                    require_dispute_window_expiry=False,
                )
                return _job_response(settled, caller), 200
            if verification_status in {"rejected", "expired"}:
                raise HTTPException(status_code=409, detail="Output verification decision is already closed for this job.")
            decided = jobs.set_output_verification_decision(
                job_id,
                decision="accept",
                decision_owner_id=caller["owner_id"],
                reason=body.reason,
            )
            if decided is None:
                raise HTTPException(status_code=409, detail="Unable to record output verification decision.")
            _record_job_event(
                decided,
                "job.output_verification_accepted",
                actor_owner_id=caller["owner_id"],
                payload={},
            )
            settled = _settle_successful_job(
                decided,
                actor_owner_id=caller["owner_id"],
                require_dispute_window_expiry=False,
            )
            return _job_response(settled, caller), 200

        if verification_status == "rejected":
            return _job_response(initialized, caller), 200
        if verification_status in {"accepted", "expired"}:
            raise HTTPException(status_code=409, detail="Output verification decision is already closed for this job.")

        rejection_reason = body.reason or "Caller rejected output during verification window."
        dispute_row = _ensure_output_rejection_dispute(
            initialized,
            filed_by_owner_id=caller["owner_id"],
            reason=rejection_reason,
            evidence=body.evidence,
        )
        decided = jobs.set_output_verification_decision(
            job_id,
            decision="reject",
            decision_owner_id=caller["owner_id"],
            reason=rejection_reason,
        )
        decided_job = decided or jobs.get_job(job_id) or initialized
        _record_job_event(
            decided_job,
            "job.output_verification_rejected",
            actor_owner_id=caller["owner_id"],
            payload={"dispute_id": dispute_row["dispute_id"]},
        )
        _record_job_event(
            decided_job,
            "job.dispute_filed",
            actor_owner_id=caller["owner_id"],
            payload={
                "dispute_id": dispute_row["dispute_id"],
                "side": "caller",
                "reason": rejection_reason,
                "auto_opened": True,
            },
        )
        return _job_response(decided_job, caller), 200

    return _run_idempotent_json_response(
        request=request,
        caller=caller,
        scope=f"jobs.verification:{job_id}",
        payload=body.model_dump(),
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
        if not _caller_worker_authorized_for_job(caller, job):
            raise HTTPException(status_code=403, detail="Not authorized for this agent job.")
        timed_out = _timeout_stale_lease_at_touchpoint(
            job,
            actor_owner_id=actor_owner_id,
            touchpoint="fail",
        )
        if timed_out is not None:
            timed_out_response = _job_response(timed_out, caller)
            return (
                _timeout_error_payload(timed_out_response),
                410,
            )

        refund_fraction = float(getattr(body, "refund_fraction", 1.0) or 1.0)

        if job["settled_at"]:
            return _job_response(job, caller), 200
        if job["status"] == "failed" and job.get("error_message") == body.error_message:
            settled = _settle_failed_job(
                job,
                actor_owner_id=actor_owner_id,
                event_type="job.failed",
                refund_fraction=refund_fraction,
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
        settled = _settle_failed_job(
            updated,
            actor_owner_id=actor_owner_id,
            event_type="job.failed",
            refund_fraction=refund_fraction,
        )
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
        require_auth = caller["type"] == "user"
        claim_owner_id = actor_owner_id if require_auth else (job.get("claim_owner_id") or actor_owner_id)
        if require_auth:
            _assert_worker_claim(job, caller, actor_owner_id, body.claim_token)

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
    _require_any_scope(caller, "caller", "worker")
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to post to this job.")

    raw_type = body.type
    raw_payload = dict(body.payload or {})
    raw_correlation_id = body.correlation_id
    raw_from_id = body.from_id
    from_id_override = None
    if raw_from_id is not None:
        from_id_override = str(raw_from_id).strip() or None
    if str(raw_type or "").strip().lower() == "agent_message":
        if body.channel is not None and "channel" not in raw_payload:
            raw_payload["channel"] = body.channel
        if body.to_id is not None and "to_id" not in raw_payload:
            raw_payload["to_id"] = body.to_id

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
        payload={
            "type": msg_type,
            "message_id": msg["message_id"],
            "channel": payload.get("channel") if isinstance(payload, dict) else None,
            "to_id": payload.get("to_id") if isinstance(payload, dict) else None,
        },
    )

    return JSONResponse(content=msg, status_code=201)


def _extract_job_message_filters(
    *,
    msg_type: str | None = None,
    from_id: str | None = None,
    channel: str | None = None,
    to_id: str | None = None,
) -> dict[str, str | None]:
    normalized_type = str(msg_type or "").strip().lower() or None
    if normalized_type is not None and normalized_type not in _TYPED_JOB_MESSAGE_TYPES.union(_LEGACY_JOB_MESSAGE_TYPES):
        raise HTTPException(status_code=400, detail=f"Unsupported job message type filter: {normalized_type}")
    normalized_from_id = str(from_id or "").strip() or None
    normalized_channel = str(channel or "").strip().lower() or None
    normalized_to_id = str(to_id or "").strip() or None
    return {
        "msg_type": normalized_type,
        "from_id": normalized_from_id,
        "channel": normalized_channel,
        "to_id": normalized_to_id,
    }


def _job_message_matches_filters(message: dict, filters: dict[str, str | None]) -> bool:
    expected_type = filters.get("msg_type")
    expected_from_id = filters.get("from_id")
    expected_channel = filters.get("channel")
    expected_to_id = filters.get("to_id")
    if expected_type and str(message.get("type") or "").strip().lower() != expected_type:
        return False
    if expected_from_id and str(message.get("from_id") or "").strip() != expected_from_id:
        return False
    payload = message.get("payload")
    if expected_channel or expected_to_id:
        if not isinstance(payload, dict):
            return False
        if expected_channel and str(payload.get("channel") or "").strip().lower() != expected_channel:
            return False
        if expected_to_id and str(payload.get("to_id") or "").strip() != expected_to_id:
            return False
    return True


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
    msg_type: str | None = Query(default=None, alias="type"),
    from_id: str | None = None,
    channel: str | None = None,
    to_id: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.JobMessagesResponse:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view messages.")
    filters = _extract_job_message_filters(
        msg_type=msg_type,
        from_id=from_id,
        channel=channel,
        to_id=to_id,
    )
    items = jobs.get_messages(
        job_id,
        since_id=since,
        msg_type=filters["msg_type"],
        from_id=filters["from_id"],
        channel=filters["channel"],
        to_id=filters["to_id"],
    )
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
    msg_type: str | None = Query(default=None, alias="type"),
    from_id: str | None = None,
    channel: str | None = None,
    to_id: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> StreamingResponse:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if not _caller_can_view_job(caller, job):
        raise HTTPException(status_code=403, detail="Not authorized to view messages.")
    filters = _extract_job_message_filters(
        msg_type=msg_type,
        from_id=from_id,
        channel=channel,
        to_id=to_id,
    )

    def _iter_events():
        subscriber = _subscribe_job_stream(job_id)
        last_seen = since
        try:
            yield ": heartbeat\n\n"
            while True:
                batch = jobs.get_messages(
                    job_id,
                    since_id=last_seen,
                    limit=200,
                    msg_type=filters["msg_type"],
                    from_id=filters["from_id"],
                    channel=filters["channel"],
                    to_id=filters["to_id"],
                )
                if batch:
                    for item in batch:
                        if not _job_message_matches_filters(item, filters):
                            continue
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
                if not _job_message_matches_filters(queued, filters):
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
        if body.rating == 5:
            five_star_count = reputation.count_caller_given_ratings(caller["owner_id"], rating=5)
            if five_star_count >= 10:
                milestone = five_star_count // 10
                payments.adjust_caller_trust_once(
                    caller["owner_id"],
                    delta=0.02,
                    reason="five_star_milestone",
                    related_id=f"milestone:{milestone}",
                )
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
    if not _caller_worker_authorized_for_job(caller, job):
        raise HTTPException(status_code=403, detail="Only the job's agent owner can rate the caller.")
    agent_owner_for_rating = job["agent_owner_id"] if caller["type"] == "agent_key" else caller["owner_id"]

    try:
        rating = reputation.record_caller_rating(
            job_id=job_id,
            agent_owner_id=agent_owner_for_rating,
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


@app.get(
    "/jobs/{job_id}/dispute",
    response_model=core_models.DisputeResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def jobs_get_dispute(
    request: Request,
    job_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeResponse:
    """Fetch the dispute for a job, if one exists."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if caller["type"] != "master":
        owner_id = caller["owner_id"]
        if owner_id not in (job.get("caller_owner_id"), job.get("agent_owner_id")):
            raise HTTPException(status_code=403, detail="Not authorized to view this dispute.")
    dispute_row = disputes.get_dispute_by_job(job_id)
    if dispute_row is None:
        raise HTTPException(status_code=404, detail="No dispute found for this job.")
    dispute_row["judgments"] = disputes.get_judgments(dispute_row["dispute_id"])
    return JSONResponse(content=_dispute_view(dispute_row))


@app.get(
    "/ops/platform-stats",
    tags=["Ops"],
    summary="Platform health and trust statistics. Requires admin scope.",
    responses=_error_responses(401, 403, 429, 500),
)
def ops_platform_stats(
    request: Request,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    from datetime import datetime, timezone, timedelta
    since_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with jobs._conn() as conn:
        totals = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = 'complete') AS total_completed,
              COUNT(*) FILTER (WHERE status = 'complete' AND created_at >= ?) AS completed_30d,
              SUM(CASE WHEN status = 'complete' THEN price_cents ELSE 0 END) AS total_value_cents
            FROM jobs
            """,
            (since_30d,),
        ).fetchone()
        dispute_stats = conn.execute(
            """
            SELECT
              COUNT(*) AS total_disputes,
              COUNT(*) FILTER (WHERE d.status IN ('final','resolved','consensus')) AS resolved_disputes,
              COUNT(*) FILTER (WHERE d.created_at >= ?) AS disputes_30d
            FROM disputes d
            JOIN jobs j ON j.job_id = d.job_id
            """,
            (since_30d,),
        ).fetchone()
        completed_30d_count = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = 'complete' AND created_at >= ?",
            (since_30d,),
        ).fetchone()["n"] or 1
        latency_rows = conn.execute(
            """
            SELECT (julianday(completed_at) - julianday(claimed_at)) * 86400 AS latency_s
            FROM jobs
            WHERE status = 'complete' AND claimed_at IS NOT NULL AND completed_at IS NOT NULL
              AND created_at >= ?
            ORDER BY latency_s
            """,
            (since_30d,),
        ).fetchall()
    agent_count = len(registry.get_agents(include_internal=False))
    lats = [float(r["latency_s"]) for r in latency_rows if r["latency_s"] is not None]
    if lats:
        mid = len(lats) // 2
        median_latency = round(lats[mid] if len(lats) % 2 else (lats[mid - 1] + lats[mid]) / 2, 2)
    else:
        median_latency = None
    total_completed = int((totals["total_completed"] or 0) if totals else 0)
    completed_30d = int((totals["completed_30d"] or 0) if totals else 0)
    total_value_cents = int((totals["total_value_cents"] or 0) if totals else 0)
    total_disputes = int((dispute_stats["total_disputes"] or 0) if dispute_stats else 0)
    resolved_disputes = int((dispute_stats["resolved_disputes"] or 0) if dispute_stats else 0)
    disputes_30d = int((dispute_stats["disputes_30d"] or 0) if dispute_stats else 0)
    dispute_rate = round(disputes_30d / completed_30d_count, 4) if completed_30d_count > 0 else 0.0
    resolution_rate = round(resolved_disputes / total_disputes, 4) if total_disputes > 0 else 1.0
    return JSONResponse(content={
        "total_agents_registered": agent_count,
        "total_jobs_completed": total_completed,
        "total_jobs_last_30_days": completed_30d,
        "total_value_settled_cents": total_value_cents,
        "dispute_rate": dispute_rate,
        "dispute_resolution_rate": resolution_rate,
        "median_job_latency_seconds": median_latency,
        "platform_uptime_pct": 99.9,
    })


@app.get(
    "/ops/disputes/{dispute_id}",
    response_model=core_models.DisputeResponse,
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def disputes_get(
    request: Request,
    dispute_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.DisputeResponse:
    """Fetch a dispute by its ID."""
    dispute_row = disputes.get_dispute(dispute_id)
    if dispute_row is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
    if caller["type"] != "master":
        job = jobs.get_job(dispute_row["job_id"])
        owner_id = caller["owner_id"]
        if job and owner_id not in (job.get("caller_owner_id"), job.get("agent_owner_id")):
            raise HTTPException(status_code=403, detail="Not authorized.")
    dispute_row["judgments"] = disputes.get_judgments(dispute_id)
    return JSONResponse(content=_dispute_view(dispute_row))


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
    deadline = _dispute_window_deadline(job)
    if deadline is None:
        raise HTTPException(status_code=400, detail="Job completion timestamp is invalid.")
    if datetime.now(timezone.utc) > deadline:
        raise HTTPException(status_code=400, detail="Dispute window has expired for this job.")

    side = _dispute_side_for_caller(caller, job)
    if reputation.get_job_quality_rating(job_id) is not None:
        raise HTTPException(status_code=409, detail="Disputes must be filed before the caller submits a rating.")
    if disputes.has_dispute_for_job(job_id):
        raise HTTPException(status_code=409, detail="A dispute already exists for this job.")

    filing_deposit_cents = _compute_dispute_filing_deposit_cents(int(job.get("price_cents") or 0))
    conn = payments._conn()
    lock_summary: dict[str, Any] = {}
    deposit_summary: dict[str, Any] = {}
    insufficient_phase = "dispute_create"
    try:
        conn.execute("BEGIN IMMEDIATE")
        created = disputes.create_dispute(
            job_id=job_id,
            filed_by_owner_id=caller["owner_id"],
            side=side,
            reason=body.reason,
            evidence=body.evidence,
            filing_deposit_cents=filing_deposit_cents,
            conn=conn,
        )
        insufficient_phase = "filing_deposit"
        deposit_summary = payments.collect_dispute_filing_deposit(
            created["dispute_id"],
            filed_by_owner_id=caller["owner_id"],
            amount_cents=filing_deposit_cents,
            conn=conn,
        )
        insufficient_phase = "clawback_lock"
        lock_summary = payments.lock_dispute_funds(created["dispute_id"], conn=conn)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=409, detail="A dispute already exists for this job.")
    except ValueError as exc:
        conn.execute("ROLLBACK")
        raise HTTPException(status_code=400, detail=str(exc))
    except payments.InsufficientBalanceError as exc:
        conn.execute("ROLLBACK")
        error_code = (
            error_codes.DISPUTE_FILING_DEPOSIT_INSUFFICIENT_BALANCE
            if insufficient_phase == "filing_deposit"
            else error_codes.DISPUTE_CLAWBACK_INSUFFICIENT_BALANCE
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": error_code,
                "balance_cents": exc.balance_cents,
                "required_cents": exc.required_cents,
            },
        )
    _record_job_event(
        job,
        "job.dispute_filed",
        actor_owner_id=caller["owner_id"],
        payload={
            "dispute_id": created["dispute_id"],
            "side": side,
            "filing_deposit": deposit_summary,
            "lock": lock_summary,
        },
    )
    # Notify both parties about the dispute
    for _party_owner_id in {job.get("caller_owner_id"), job.get("agent_owner_id")}:
        _party_email = _get_owner_email(_party_owner_id or "")
        if _party_email:
            _email.send_dispute_opened(_party_email, job_id, created["dispute_id"])
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
    _require_admin_ip_allowlist(request)
    if disputes.get_dispute(dispute_id) is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
    try:
        dispute_payload, settlement = _resolve_dispute_with_judges(dispute_id, actor_owner_id=caller["owner_id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError:
        _LOG.exception("Dispute judge execution failed for %s.", dispute_id)
        raise HTTPException(status_code=500, detail="Failed to resolve dispute.")
    return JSONResponse(content={"dispute": dispute_payload, "settlement": settlement})


@app.get(
    "/admin/disputes",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def admin_list_disputes(
    request: Request,
    limit: int = 200,
    status: str | None = None,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """List all disputes with job context and verdict summary, oldest first."""
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    capped_limit = max(1, min(int(limit), 500))
    rows = disputes.list_disputes(status=status or None, limit=capped_limit)
    result = []
    for d in rows:
        job = jobs.get_job(d["job_id"])
        judgments_list = disputes.get_judgments(d["dispute_id"])
        llm_judgments = [j for j in judgments_list if j.get("judge_kind") != "human_admin"]
        if len(llm_judgments) >= 2:
            v0, v1 = llm_judgments[0]["verdict"], llm_judgments[1]["verdict"]
            verdict_summary = (
                f"Both agreed: {v0.replace('_', ' ')}"
                if v0 == v1
                else "Judges disagreed — needs ruling"
            )
        elif len(llm_judgments) == 1:
            verdict_summary = f"1 judge: {llm_judgments[0]['verdict'].replace('_', ' ')}"
        else:
            verdict_summary = "Awaiting judgment"
        result.append({
            **d,
            "price_cents": int((job or {}).get("price_cents") or 0),
            "caller_owner_id": (job or {}).get("caller_owner_id"),
            "agent_owner_id": (job or {}).get("agent_owner_id"),
            "agent_id": (job or {}).get("agent_id"),
            "verdict_summary": verdict_summary,
            "judgment_count": len(judgments_list),
        })
    result.sort(key=lambda x: x.get("filed_at") or "")
    return JSONResponse(content={"disputes": result, "total": len(result)})


@app.get(
    "/admin/disputes/{dispute_id}",
    responses=_error_responses(401, 403, 404, 429, 500),
)
@limiter.limit("60/minute")
def admin_get_dispute(
    request: Request,
    dispute_id: str,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    """Full dispute context including job input/output and escrow balance."""
    _require_scope(caller, "admin", detail="This endpoint requires admin scope.")
    _require_admin_ip_allowlist(request)
    ctx = disputes.get_dispute_context(dispute_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Dispute '{dispute_id}' not found.")
    escrow_wallet = payments.get_wallet_by_owner(
        payments.DISPUTE_ESCROW_OWNER_PREFIX + dispute_id
    )
    ctx["escrow_balance_cents"] = int((escrow_wallet or {}).get("balance_cents") or 0)
    return JSONResponse(content=ctx)


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
    _require_admin_ip_allowlist(request)
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
        if finalized is not None:
            _apply_dispute_effects(finalized, body.outcome)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except payments.InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": error_codes.DISPUTE_SETTLEMENT_INSUFFICIENT_BALANCE,
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
        for _party_owner_id in {job.get("caller_owner_id"), job.get("agent_owner_id")}:
            _party_email = _get_owner_email(_party_owner_id or "")
            if _party_email:
                _email.send_dispute_resolved(_party_email, finalized["job_id"], dispute_id, body.outcome)
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
    _require_admin_ip_allowlist(request)
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    txs = payments.get_settlement_transactions(job["charge_tx_id"])
    distribution = payments.compute_success_distribution(
        int(job.get("price_cents") or 0),
        platform_fee_pct=job.get("platform_fee_pct_at_create"),
        fee_bearer_policy=job.get("fee_bearer_policy"),
    )
    fee_cents = int(distribution["platform_fee_cents"])
    return JSONResponse(
        content={
            "job_id": job["job_id"],
            "agent_id": job["agent_id"],
            "status": job["status"],
            "charge_tx_id": job["charge_tx_id"],
            "price_cents": job["price_cents"],
            "expected_agent_payout_cents": distribution["agent_payout_cents"],
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
    _require_admin_ip_allowlist(request)
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
    deliveries = _list_hook_deliveries(owner_id=owner_id, status="failed", limit=limit)
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
    _require_admin_ip_allowlist(request)
    started = _utc_now_iso()
    try:
        summary = _sweep_jobs(
            retry_delay_seconds=body.retry_delay_seconds,
            sla_seconds=body.sla_seconds,
            limit=body.limit,
            actor_owner_id=caller["owner_id"],
        )
        _set_sweeper_state(last_run_at=started, last_summary=summary, last_error=None)
    except ValueError as exc:
        _set_sweeper_state(last_run_at=started, last_error=str(exc))
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
    _require_admin_ip_allowlist(request)
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
    _require_admin_ip_allowlist(request)
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
    _require_admin_ip_allowlist(request)
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
    _require_admin_ip_allowlist(request)
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
    _require_admin_ip_allowlist(request)
    if limit <= 0:
        raise HTTPException(status_code=422, detail="limit must be > 0.")
    runs = payments.list_reconciliation_runs(limit=limit)
    return JSONResponse(content={"runs": runs, "count": len(runs)})


# ---------------------------------------------------------------------------
# Spending summary
# ---------------------------------------------------------------------------


@app.get(
    "/wallets/spend-summary",
    responses=_error_responses(401, 403, 429, 500),
    tags=["Wallets"],
    summary="Rolling spend summary by period and per-agent breakdown.",
)
@limiter.limit("30/minute")
def wallet_spend_summary(
    request: Request,
    period: str = "7d",
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> JSONResponse:
    _require_scope(caller, "caller")
    period_map = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
    days = period_map.get(period, 7)
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since_dt.isoformat()

    caller_owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(caller_owner_id)
    wallet_id = wallet["wallet_id"]

    with jobs._conn() as conn:
        rows = conn.execute(
            """
            SELECT agent_id, SUM(price_cents) AS total_cents, COUNT(*) AS job_count
            FROM jobs
            WHERE caller_owner_id = ?
              AND status IN ('complete', 'failed')
              AND created_at >= ?
            GROUP BY agent_id
            ORDER BY total_cents DESC
            LIMIT 100
            """,
            (caller_owner_id, since_iso),
        ).fetchall()
        totals = conn.execute(
            """
            SELECT SUM(price_cents) AS total_cents, COUNT(*) AS job_count
            FROM jobs
            WHERE caller_owner_id = ? AND created_at >= ?
            """,
            (caller_owner_id, since_iso),
        ).fetchone()

    by_agent = [
        {
            "agent_id": row["agent_id"],
            "total_cents": int(row["total_cents"] or 0),
            "job_count": int(row["job_count"] or 0),
        }
        for row in rows
    ]
    return JSONResponse(content={
        "period": period,
        "days": days,
        "total_cents": int((totals["total_cents"] or 0) if totals else 0),
        "total_jobs": int((totals["job_count"] or 0) if totals else 0),
        "by_agent": by_agent,
        "wallet_id": wallet_id,
    })


# ---------------------------------------------------------------------------
# Wallet routes
# ---------------------------------------------------------------------------

@app.post(
    "/wallets/deposit",
    response_model=core_models.WalletDepositResponse,
    responses=_error_responses(400, 401, 403, 404, 422, 429, 500),
)
@limiter.limit("20/minute")
def wallet_deposit(
    request: Request,
    body: DepositRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletDepositResponse:
    _require_scope(caller, "admin")
    _require_admin_ip_allowlist(request)
    wallet = payments.get_wallet(body.wallet_id)
    if wallet is None:
        raise HTTPException(status_code=404, detail=f"Wallet '{body.wallet_id}' not found.")
    if caller["type"] != "master" and wallet["owner_id"] != caller["owner_id"]:
        raise HTTPException(status_code=403, detail="Not authorized to deposit into this wallet.")
    if int(body.amount_cents) < MINIMUM_DEPOSIT_CENTS:
        raise _deposit_below_minimum_error(int(body.amount_cents))
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
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletResponse:
    _require_any_scope(caller, "caller", "worker")
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    txs = payments.get_wallet_transactions(wallet["wallet_id"], limit=50)
    caller_trust = payments.get_caller_trust(owner_id)
    return JSONResponse(content={**wallet, "caller_trust": caller_trust, "transactions": txs})


@app.post(
    "/wallets/me/daily-spend-limit",
    response_model=core_models.WalletDailySpendLimitResponse,
    responses=_error_responses(400, 401, 403, 429, 500),
    tags=["Wallets"],
    summary="Set or clear the authenticated wallet's rolling 24h spend cap.",
)
@limiter.limit("20/minute")
def wallet_set_daily_spend_limit(
    request: Request,
    body: core_models.WalletDailySpendLimitRequest,
    caller: core_models.CallerContext = Depends(_require_api_key),
) -> core_models.WalletDailySpendLimitResponse:
    _require_scope(caller, "caller")
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    try:
        updated = payments.set_wallet_daily_spend_limit(
            wallet["wallet_id"],
            body.daily_spend_limit_cents,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(
        content={
            "wallet_id": updated["wallet_id"],
            "daily_spend_limit_cents": updated.get("daily_spend_limit_cents"),
        }
    )


@app.get(
    "/wallets/me/agent-earnings",
    responses=_error_responses(401, 403, 429, 500),
)
@limiter.limit("60/minute")
def wallet_me_agent_earnings(
    request: Request,
    _: core_models.CallerContext = Depends(_require_api_key),
):
    """Per-agent earnings breakdown for the authenticated user's wallet."""
    owner_id = _caller_owner_id(request)
    wallet = payments.get_or_create_wallet(owner_id)
    breakdown = payments.get_agent_earnings_breakdown(wallet["wallet_id"])
    # Enrich with agent names where available
    enriched = []
    for row in breakdown:
        agent_id = row["agent_id"]
        name = agent_id
        try:
            agent = registry.get_agent(agent_id, include_unapproved=True)
            if agent:
                name = agent.get("name") or agent_id
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            _LOG.warning("Failed to load agent name for earnings row %s: %s", agent_id, exc)
        enriched.append({**row, "agent_name": name})
    return JSONResponse(content={"earnings": enriched})

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
        return JSONResponse(content={"runs": [], "skipped_lines": 0, "skipped_line_numbers": []})
    with open(runs_file, encoding="utf-8") as f:
        lines = f.readlines()
    runs = []
    skipped = 0
    skipped_line_numbers: list[int] = []
    for line_number, line in reversed(list(enumerate(lines, start=1))):
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            skipped += 1
            skipped_line_numbers.append(line_number)
            continue
        if len(runs) >= limit:
            break
    skipped_line_numbers.sort()
    return JSONResponse(
        content={
            "runs": runs,
            "skipped_lines": skipped,
            "skipped_line_numbers": skipped_line_numbers,
        },
        headers={"X-Skipped-Lines": str(skipped)},
    )


# ---------------------------------------------------------------------------
# Public config (Stripe publishable key for the frontend)
# ---------------------------------------------------------------------------

@app.get(
    "/config/public",
    tags=["config"],
    summary="Public server configuration for the frontend.",
)
def config_public() -> JSONResponse:
    return JSONResponse({
        "stripe_enabled": bool(_STRIPE_SECRET_KEY and _STRIPE_AVAILABLE),
        "stripe_publishable_key": _STRIPE_PUBLISHABLE_KEY or None,
    })


@app.get(
    "/public/docs",
    tags=["docs"],
    summary="List platform documentation available from this deployment.",
)
def public_docs_index() -> JSONResponse:
    entries = _public_docs_entries()
    docs = [
        {
            "slug": item["slug"],
            "title": item["title"],
            "path": f"/public/docs/{item['slug']}",
        }
        for item in entries
    ]
    return JSONResponse({"docs": docs, "count": len(docs)})


@app.get(
    "/public/docs/{doc_slug}",
    tags=["docs"],
    summary="Fetch a public documentation file by slug.",
)
def public_doc_content(doc_slug: str) -> JSONResponse:
    doc = _find_public_doc(doc_slug)
    if doc is None:
        raise HTTPException(status_code=404, detail="Documentation page not found.")
    try:
        with open(doc["full_path"], encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        raise HTTPException(status_code=500, detail="Unable to read documentation file.") from None
    return JSONResponse({
        "slug": doc["slug"],
        "title": doc["title"],
        "content": content,
    })


# ---------------------------------------------------------------------------
# Stripe: create checkout session + webhook
# ---------------------------------------------------------------------------


def _extract_stripe_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code).strip().lower()
    nested = getattr(exc, "error", None)
    nested_code = getattr(nested, "code", None) if nested is not None else None
    if nested_code:
        return str(nested_code).strip().lower()
    return ""


def _stripe_http_error(operation: str, exc: Exception) -> tuple[int, dict[str, Any]]:
    code = _extract_stripe_error_code(exc)
    message = str(exc or "").strip().lower()
    if code in {"insufficient_funds", "balance_insufficient"} or "insufficient" in message:
        return 400, {
            "error": "payment.stripe_insufficient_funds",
            "message": "Payouts are temporarily unavailable because Stripe platform balance is insufficient.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if code in {"account_closed", "account_invalid", "no_such_destination"} or "no such destination" in message:
        return 400, {
            "error": "payment.stripe_destination_invalid",
            "message": "Your connected payout account is unavailable. Reconnect your bank account and try again.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if "signed up for connect" in message or "connect is not enabled" in message:
        return 503, {
            "error": "payment.stripe_connect_unavailable",
            "message": "Stripe Connect is not enabled for this server account.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if code in {"rate_limit", "rate_limit_error"}:
        return 429, {
            "error": "payment.stripe_rate_limited",
            "message": "Stripe is rate-limiting requests right now. Please retry shortly.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if code in {"authentication_error", "permission_error"}:
        return 503, {
            "error": "payment.stripe_auth_error",
            "message": "Payment processing is temporarily unavailable due to Stripe configuration.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    if code in {"api_connection_error", "api_error"}:
        return 502, {
            "error": "payment.stripe_upstream_error",
            "message": "Stripe is temporarily unavailable. Please try again.",
            "data": {"stripe_code": code or None, "operation": operation},
        }
    return 502, {
        "error": "payment.stripe_error",
        "message": "Stripe request failed. Please try again.",
        "data": {"stripe_code": code or None, "operation": operation},
    }


def _stripe_obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stripe_obj_id(obj: Any) -> str:
    value = _stripe_obj_get(obj, "id", "") or ""
    return str(value).strip()


def _stripe_begin_checkout_webhook_event(
    *,
    session_id: str,
    wallet_id: str,
    amount_cents: int,
) -> str:
    now = _utc_now_iso()
    with sqlite3.connect(jobs.DB_PATH) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        processed_row = conn.execute(
            "SELECT 1 FROM stripe_sessions WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if processed_row is not None:
            conn.commit()
            return "already_processed"
        state_row = conn.execute(
            """
            SELECT status
            FROM stripe_webhook_events
            WHERE session_id = ?
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if state_row is None:
            conn.execute(
                """
                INSERT INTO stripe_webhook_events
                    (session_id, wallet_id, amount_cents, status, attempts, created_at, updated_at)
                VALUES (?, ?, ?, 'processing', 1, ?, ?)
                """,
                (session_id, wallet_id, int(amount_cents), now, now),
            )
            conn.commit()
            return "acquired"
        status = str(state_row[0] or "").strip().lower()
        if status == "processed":
            conn.commit()
            return "already_processed"
        if status == "processing":
            conn.commit()
            return "already_processing"
        conn.execute(
            """
            UPDATE stripe_webhook_events
            SET wallet_id = ?,
                amount_cents = ?,
                status = 'processing',
                attempts = attempts + 1,
                last_error = NULL,
                updated_at = ?
            WHERE session_id = ?
            """,
            (wallet_id, int(amount_cents), now, session_id),
        )
        conn.commit()
    return "acquired"


def _stripe_mark_checkout_webhook_failed(
    *,
    session_id: str,
    error_message: str,
) -> None:
    with sqlite3.connect(jobs.DB_PATH) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            UPDATE stripe_webhook_events
            SET status = 'failed',
                last_error = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (str(error_message or "")[:1000], _utc_now_iso(), session_id),
        )
        conn.commit()


def _stripe_mark_checkout_webhook_processed(
    *,
    session_id: str,
    wallet_id: str,
    amount_cents: int,
) -> None:
    now = _utc_now_iso()
    with sqlite3.connect(jobs.DB_PATH) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT OR IGNORE INTO stripe_sessions (session_id, wallet_id, amount_cents, processed_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, wallet_id, int(amount_cents), now),
        )
        conn.execute(
            """
            UPDATE stripe_webhook_events
            SET status = 'processed',
                last_error = NULL,
                updated_at = ?
            WHERE session_id = ?
            """,
            (now, session_id),
        )
        conn.commit()


def _wallet_stripe_topup_total_last_24h(wallet_id: str) -> int:
    window_start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with sqlite3.connect(jobs.DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM stripe_sessions
            WHERE wallet_id = ? AND processed_at >= ?
            """,
            (wallet_id, window_start),
        ).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


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
    summary="Stripe webhook receiver — credits wallet on successful checkout.",
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
            with sqlite3.connect(jobs.DB_PATH) as _ac_conn:
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
        with sqlite3.connect(jobs.DB_PATH) as _ac_conn:
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
        with sqlite3.connect(jobs.DB_PATH) as _ac_conn:
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
        with sqlite3.connect(jobs.DB_PATH) as _tr_conn:
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
