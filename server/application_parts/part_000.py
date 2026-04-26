# server.application shard 0 — imports, env/config, logging, Sentry, constants.
# Loaded first by server/application.py; see CLAUDE.md "Editing a shard" for the
# full shard ordering and rules. This shard must not register routes.
"""FastAPI HTTP application for the Aztea / aztea platform.

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

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_APP_DIR, ".."))

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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ValidationError
from slowapi import Limiter
from slowapi.util import get_remote_address

import groq as _groq

from agents import codereview as agent_codereview
from agents import cve_lookup as agent_cve_lookup
from agents import image_generator as agent_image_generator
from agents import video_storyboard as agent_video_storyboard
from agents import wiki as agent_wiki
from agents import arxiv_research as agent_arxiv_research
from agents import python_executor as agent_python_executor
from agents import web_researcher as agent_web_researcher
from agents import github_fetcher as agent_github_fetcher
from agents import hn_digest as agent_hn_digest
from agents import dns_inspector as agent_dns_inspector
from agents import pr_reviewer as agent_pr_reviewer
from agents import test_generator as agent_test_generator
from agents import spec_writer as agent_spec_writer
from agents import dependency_auditor as agent_dependency_auditor
from agents import multi_file_executor as agent_multi_file_executor
from agents import changelog_agent as agent_changelog_agent
from agents import package_finder as agent_package_finder
from agents import linter_agent as agent_linter_agent
from core import auth as _auth
from core import embeddings
from core import onboarding
from core import payments
from core.db import close_all_connections as _close_all_db_connections
from core.db import get_db_connection
from core.openapi_responses import pick_error_responses as _error_responses
from server.builtin_agents import specs as _builtin_specs
from server.error_handlers import register_exception_handlers
from server.routes import system as _system_routes
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
from core import hosted_skills as _hosted_skills
from core import skill_executor as _skill_executor
from core import skill_parser as _skill_parser
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
    AdminDisputeRuleRequest,
    OnboardingValidateRequest,
    ReconciliationRunRequest,
    RegistrySearchRequest,
    RotateKeyRequest,
    AgentKeyCreateRequest,
    AgentReviewDecisionRequest,
    AuthLegalAcceptRequest,
    GoogleAuthRequest,
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
        r'((?:az_|azk_|sk_live_|sk_test_|Bearer\s+)[A-Za-z0-9_\-]{8,})',
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
_WIKI_AGENT_ID             = "9a175aa2-8ffd-52f7-aae0-5a33fc88db83"
_CVELOOKUP_AGENT_ID        = "a3e239dd-ea92-556b-9c95-0a213a3daf59"
_QUALITY_JUDGE_AGENT_ID    = "9cf0d9d0-4a10-58c9-b97a-6b5f81b1cf33"
_IMAGE_GENERATOR_AGENT_ID  = "4fb167bd-b474-5ea5-bd5c-8976dfe799ae"
_VIDEO_STORYBOARD_AGENT_ID = "c12994de-cde9-514a-9c07-a3833b25bb1f"
_ARXIV_RESEARCH_AGENT_ID   = "9e673f6e-9115-516f-b41b-5af8bcbf15bd"
_PYTHON_EXECUTOR_AGENT_ID  = "040dc3f5-afe7-5db7-b253-4936090cc7af"
_WEB_RESEARCHER_AGENT_ID   = "32cd7b5c-44d0-5259-bb02-1bbc612e92d7"
_GITHUB_FETCHER_AGENT_ID   = "5896576f-bbe6-59e4-83c1-5106002e7d10"
_HN_DIGEST_AGENT_ID        = "31cc3a99-eca6-5202-96d4-8366f426ae1d"
_DNS_INSPECTOR_AGENT_ID    = "3d677381-791c-5e83-8e66-5b77d0e43e2e"
_PR_REVIEWER_AGENT_ID      = "3e133b66-3bc6-5003-9b64-3284b28a60c6"
_TEST_GENERATOR_AGENT_ID   = "f515323c-7df2-5742-ac06-bc38b59a40cb"
_SPEC_WRITER_AGENT_ID      = "ce9504a3-74c8-51a5-913e-6ae55787abc8"
_DEPENDENCY_AUDITOR_AGENT_ID = "11fab82a-426e-513e-abf3-528d99ef2b87"
_MULTI_FILE_EXECUTOR_AGENT_ID = "ea95cdec-32c1-5a2b-a032-3e7061abf3a4"
_CHANGELOG_AGENT_ID = "48c24ce5-d9cb-5f76-9e2f-fce1878f8c4c"
_PACKAGE_FINDER_AGENT_ID = "d11ddab1-bcca-55de-8b00-c9efadc69c79"
_LINTER_AGENT_ID = "7ec4c987-9a7e-5af8-984f-7b8ad0ad0536"

def _normalize_endpoint_ref(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


_BUILTIN_INTERNAL_ENDPOINTS = {
    _FINANCIAL_AGENT_ID: "internal://financial",
    _CODEREVIEW_AGENT_ID: "internal://code-review",
    _WIKI_AGENT_ID: "internal://wiki",
    _QUALITY_JUDGE_AGENT_ID: "internal://quality-judge",
    _CVELOOKUP_AGENT_ID: "internal://cve-lookup",
    _IMAGE_GENERATOR_AGENT_ID: "internal://image-generator",
    _VIDEO_STORYBOARD_AGENT_ID: "internal://video-storyboard-generator",
    _ARXIV_RESEARCH_AGENT_ID:  "internal://arxiv-research",
    _PYTHON_EXECUTOR_AGENT_ID: "internal://python-executor",
    _WEB_RESEARCHER_AGENT_ID:  "internal://web-researcher",
    _GITHUB_FETCHER_AGENT_ID:  "internal://github_fetcher",
    _HN_DIGEST_AGENT_ID:       "internal://hn_digest",
    _DNS_INSPECTOR_AGENT_ID:   "internal://dns_inspector",
    _PR_REVIEWER_AGENT_ID:     "internal://pr_reviewer",
    _TEST_GENERATOR_AGENT_ID:  "internal://test_generator",
    _SPEC_WRITER_AGENT_ID:     "internal://spec_writer",
    _DEPENDENCY_AUDITOR_AGENT_ID: "internal://dependency_auditor",
    _MULTI_FILE_EXECUTOR_AGENT_ID: "internal://multi_file_executor",
    _CHANGELOG_AGENT_ID: "internal://changelog_agent",
    _PACKAGE_FINDER_AGENT_ID: "internal://package_finder",
    _LINTER_AGENT_ID: "internal://linter_agent",
}
_BUILTIN_LEGACY_ROUTE_ENDPOINTS = {
    _FINANCIAL_AGENT_ID: f"{_SERVER_BASE_URL}/agents/financial",
    _CODEREVIEW_AGENT_ID: f"{_SERVER_BASE_URL}/agents/code-review",
    _WIKI_AGENT_ID: f"{_SERVER_BASE_URL}/agents/wiki",
    _QUALITY_JUDGE_AGENT_ID: f"{_SERVER_BASE_URL}/agents/quality-judge",
    _CVELOOKUP_AGENT_ID: f"{_SERVER_BASE_URL}/agents/cve-lookup",
    _IMAGE_GENERATOR_AGENT_ID: f"{_SERVER_BASE_URL}/agents/image-generator",
    _VIDEO_STORYBOARD_AGENT_ID: f"{_SERVER_BASE_URL}/agents/video-storyboard-generator",
    _ARXIV_RESEARCH_AGENT_ID:  f"{_SERVER_BASE_URL}/agents/arxiv-research",
    _PYTHON_EXECUTOR_AGENT_ID: f"{_SERVER_BASE_URL}/agents/python-executor",
    _WEB_RESEARCHER_AGENT_ID:  f"{_SERVER_BASE_URL}/agents/web-researcher",
    _GITHUB_FETCHER_AGENT_ID:  f"{_SERVER_BASE_URL}/agents/github-fetcher",
    _HN_DIGEST_AGENT_ID:       f"{_SERVER_BASE_URL}/agents/hn-digest",
    _DNS_INSPECTOR_AGENT_ID:   f"{_SERVER_BASE_URL}/agents/dns-inspector",
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
        # Developer / Claude Code tools — live APIs, code execution, or structured output
        _CODEREVIEW_AGENT_ID,           # structured code review
        _PR_REVIEWER_AGENT_ID,          # GitHub PR diff review
        _TEST_GENERATOR_AGENT_ID,       # test suite generation
        _DEPENDENCY_AUDITOR_AGENT_ID,   # CVE + outdated dep audit
        _PYTHON_EXECUTOR_AGENT_ID,      # subprocess sandbox
        _MULTI_FILE_EXECUTOR_AGENT_ID,  # multi-file Python project runner
        _LINTER_AGENT_ID,               # ruff + LLM linting
        _WEB_RESEARCHER_AGENT_ID,       # HTTP fetch + parse
        _GITHUB_FETCHER_AGENT_ID,       # GitHub API live fetch
        _CVELOOKUP_AGENT_ID,            # NIST NVD API
        _ARXIV_RESEARCH_AGENT_ID,       # arXiv API
        _DNS_INSPECTOR_AGENT_ID,        # DNS/SSL live inspection
        _CHANGELOG_AGENT_ID,            # PyPI/npm changelogs
        _PACKAGE_FINDER_AGENT_ID,       # package search + stats
        # Benched (available via API/MCP but not shown in marketplace):
        # _FINANCIAL_AGENT_ID        — SEC EDGAR, narrow use case
        # _SPEC_WRITER_AGENT_ID      — pure LLM, no external data
        # _WIKI_AGENT_ID             — Claude can search Wikipedia natively
        # _IMAGE_GENERATOR_AGENT_ID  — creative tool, not a developer tool
        # _VIDEO_STORYBOARD_AGENT_ID — creative tool, not a developer tool
        # _HN_DIGEST_AGENT_ID        — nice-to-have, not Claude Code specific
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


