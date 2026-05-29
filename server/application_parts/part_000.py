# server.application shard 0 — imports, env/config, logging, Sentry, constants.
# Loaded first by server/application.py; see CLAUDE.md "Editing a shard" for the
# full shard ordering and rules. This shard must not register routes.
"""FastAPI HTTP application for the Aztea / aztea platform.

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import Token
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from queue import Empty, Queue
from typing import Any, Callable, Literal
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
_ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            integrations=[StarletteIntegration(), FastApiIntegration()],
            traces_sample_rate=float(
                os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")
            ),
            environment=_ENVIRONMENT,
            send_default_pii=False,
        )
    except Exception as _sentry_exc:
        logging.warning("Sentry init failed: %s", _sentry_exc)
elif _ENVIRONMENT == "production":
    # Errors in production will be silent without Sentry — set SENTRY_DSN in .env to fix this.
    logging.warning(
        "SENTRY_DSN is not set in production. Unhandled exceptions will not be reported. "
        "Set SENTRY_DSN=https://...@sentry.io/... in .env to enable error tracking."
    )

import groq as _groq
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

from agents import accessibility_auditor as agent_accessibility_auditor
from agents import broken_link_crawler as agent_broken_link_crawler
from agents import browser_agent as agent_browser_agent
from agents import cve_lookup as agent_cve_lookup
from agents import db_sandbox as agent_db_sandbox
from agents import dependency_auditor as agent_dependency_auditor
from agents import dns_inspector as agent_dns_inspector
from agents import lighthouse_auditor as agent_lighthouse_auditor
from agents import multi_language_executor as agent_multi_language_executor
from agents import pdf_document_parser as agent_pdf_document_parser
from agents import python_executor as agent_python_executor
from agents import secret_scanner as agent_secret_scanner
from agents import security_headers_grader as agent_security_headers_grader
from agents import visual_regression as agent_visual_regression
from agents import web_search as agent_web_search
from agents import docs_grounder as agent_docs_grounder
from agents import sast_scanner as agent_sast_scanner
from agents import stripe_webhook_debugger as agent_stripe_webhook_debugger
from agents import load_tester as agent_load_tester
from agents import ci_failure_reproducer as agent_ci_failure_reproducer
from agents import dockerfile_analyzer as agent_dockerfile_analyzer
from agents import openapi_validator as agent_openapi_validator
from agents import coverage_runner as agent_coverage_runner
from agents import ssl_certificate_decoder as agent_ssl_certificate_decoder
from agents import diff_analyzer as agent_diff_analyzer
from agents import k8s_manifest_validator as agent_k8s_manifest_validator
from agents import archive_inspector as agent_archive_inspector
from agents import unicode_inspector as agent_unicode_inspector
from agents import terraform_plan_analyzer as agent_terraform_plan_analyzer
from agents import live_sandbox as agent_live_sandbox
from agents import regex_tester as agent_regex_tester
from agents import jwt_validator as agent_jwt_validator
from agents import sbom_generator as agent_sbom_generator
from agents import pypi_metadata as agent_pypi_metadata
from agents import github_releases as agent_github_releases
from agents import hcl_terraform_analyzer as agent_hcl_terraform_analyzer
from agents import quant_patch_validator as agent_quant_patch_validator
# 2026-05-22 — strategy-doc 7-agent slate (post-editorial cut)
from agents import flake_hunter as agent_flake_hunter
from agents import bisect_and_blame as agent_bisect_and_blame
from agents import compliance_attestor as agent_compliance_attestor
from agents import stripe_connect_settler as agent_stripe_connect_settler
from agents import codebase_reviewer as agent_codebase_reviewer
from agents import prod_trace_replayer as agent_prod_trace_replayer
from agents import schema_migration_planner as agent_schema_migration_planner
from core import agent_generator as _agent_generator
from core import auth as _auth
from core import cache as _cache
from core import (
    compare,
    disputes,
    embeddings,
    error_codes,
    jobs,
    judges,
    logging_utils,
    mcp_manifest,
    onboarding,
    payments,
    pipelines,
    recipes,
    registry,
    reputation,
    tool_adapters,
)
from core import email as _email
from core import feature_flags as _feature_flags
from core import job_events as _job_events
from core.jobs import disputable
from core import hosted_skills as _hosted_skills
from core import listing_safety as _listing_safety
from core import models as core_models
from core import observability as _observability
from core import outbound_session as _outbound_session
from core import rate_limit as _rate_limit
from core import skill_executor as _skill_executor
from core import skill_parser as _skill_parser
from core import url_security as _url_security
from core import watchers as _watchers
from core.watchers import sweeper as _watchers_sweeper
from core.db import close_all_connections as _close_all_db_connections
from core.db import get_db_connection
from core.migrate import apply_migrations
from core.models import (
    AdminDisputeRuleRequest,
    AgentKeyCreateRequest,
    AgentRegisterRequest,
    AgentReviewDecisionRequest,
    AgentSuspendRequest,
    AuthLegalAcceptRequest,
    CreateKeyRequest,
    DepositRequest,
    GoogleAuthRequest,
    HookDeliveryProcessRequest,
    JobCancelRequest,
    JobClaimRequest,
    JobCompleteRequest,
    JobCreateRequest,
    JobDisputeRequest,
    JobEventHookCreateRequest,
    JobFailRequest,
    JobHeartbeatRequest,
    JobMessageRequest,
    JobRateCallerRequest,
    JobRatingRequest,
    JobReleaseRequest,
    JobRetryRequest,
    JobsSweepRequest,
    JobVerificationDecisionRequest,
    MCPInvokeRequest,
    OnboardingValidateRequest,
    ReconciliationRunRequest,
    RegistrySearchRequest,
    RotateKeyRequest,
    UserLoginRequest,
    UserRegisterRequest,
)
from core.openapi_responses import pick_error_responses as _error_responses
from core.registry import auto_hire as _auto_hire
from core.registry import catalog_broadcast
from core.registry import decision_audit as _decision_audit
from core.registry import origin_context as _origin_context
from server.builtin_agents import specs as _builtin_specs
from server.error_handlers import (
    map_value_error_to_envelope as _envelope_from_value_error,
    register_exception_handlers,
)
from server.routes import admin_usage as _admin_usage_routes
from server.routes import system as _system_routes

_LOG_LEVEL_NAME = (os.environ.get("LOG_LEVEL", "INFO") or "INFO").strip().upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
if not isinstance(_LOG_LEVEL, int):
    _LOG_LEVEL = logging.INFO
logging_utils.configure_json_logging(_LOG_LEVEL)
_LOG = logging.getLogger(__name__)


class _SecretRedactFilter(logging.Filter):
    """Strip API key values and sensitive env-var patterns from log records."""

    _PATTERNS = re.compile(
        r"((?:az_|azk_|sk_live_|sk_test_|Bearer\s+)[A-Za-z0-9_\-]{8,})",
        re.IGNORECASE,
    )

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._PATTERNS.sub("[REDACTED]", str(record.msg))
        record.args = tuple(
            self._PATTERNS.sub("[REDACTED]", str(a)) if isinstance(a, str) else a
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
        _LOG.warning(
            "Failed to open background worker lock file %s: %s", lock_path, exc
        )
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

_SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "http://localhost:8000").rstrip(
    "/"
)
_FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", _SERVER_BASE_URL).rstrip("/")

# Stripe
_STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
_STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
_STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()
try:
    import stripe as _stripe_lib

    _STRIPE_AVAILABLE = True
except ImportError:
    _stripe_lib = None
    _STRIPE_AVAILABLE = False

# Built-in agent constants. Sole source of truth lives in
# server/builtin_agents/constants.py — re-exported here under the legacy `_`
# prefix so the rest of the shards continue to compile unchanged.
from server.builtin_agents import constants as _builtin_constants

_CVELOOKUP_AGENT_ID = _builtin_constants.CVELOOKUP_AGENT_ID
_QUALITY_JUDGE_AGENT_ID = _builtin_constants.QUALITY_JUDGE_AGENT_ID
_PYTHON_EXECUTOR_AGENT_ID = _builtin_constants.PYTHON_EXECUTOR_AGENT_ID
_DNS_INSPECTOR_AGENT_ID = _builtin_constants.DNS_INSPECTOR_AGENT_ID
_DEPENDENCY_AUDITOR_AGENT_ID = _builtin_constants.DEPENDENCY_AUDITOR_AGENT_ID
_DB_SANDBOX_AGENT_ID = _builtin_constants.DB_SANDBOX_AGENT_ID
_VISUAL_REGRESSION_AGENT_ID = _builtin_constants.VISUAL_REGRESSION_AGENT_ID
_BROWSER_AGENT_ID = _builtin_constants.BROWSER_AGENT_ID
_MULTI_LANGUAGE_EXECUTOR_AGENT_ID = _builtin_constants.MULTI_LANGUAGE_EXECUTOR_AGENT_ID
_SECRET_SCANNER_AGENT_ID = _builtin_constants.SECRET_SCANNER_AGENT_ID
_LIGHTHOUSE_AUDITOR_AGENT_ID = _builtin_constants.LIGHTHOUSE_AUDITOR_AGENT_ID
_ACCESSIBILITY_AUDITOR_AGENT_ID = _builtin_constants.ACCESSIBILITY_AUDITOR_AGENT_ID
_SECURITY_HEADERS_GRADER_AGENT_ID = _builtin_constants.SECURITY_HEADERS_GRADER_AGENT_ID
_BROKEN_LINK_CRAWLER_AGENT_ID = _builtin_constants.BROKEN_LINK_CRAWLER_AGENT_ID
_PDF_DOCUMENT_PARSER_AGENT_ID = _builtin_constants.PDF_DOCUMENT_PARSER_AGENT_ID
_WEB_SEARCH_AGENT_ID = _builtin_constants.WEB_SEARCH_AGENT_ID
_DOCS_GROUNDER_AGENT_ID = _builtin_constants.DOCS_GROUNDER_AGENT_ID
_SAST_SCANNER_AGENT_ID = _builtin_constants.SAST_SCANNER_AGENT_ID
_STRIPE_WEBHOOK_DEBUGGER_AGENT_ID = _builtin_constants.STRIPE_WEBHOOK_DEBUGGER_AGENT_ID
_LOAD_TESTER_AGENT_ID = _builtin_constants.LOAD_TESTER_AGENT_ID
_CI_FAILURE_REPRODUCER_AGENT_ID = _builtin_constants.CI_FAILURE_REPRODUCER_AGENT_ID
_DOCKERFILE_ANALYZER_AGENT_ID = _builtin_constants.DOCKERFILE_ANALYZER_AGENT_ID
_OPENAPI_VALIDATOR_AGENT_ID = _builtin_constants.OPENAPI_VALIDATOR_AGENT_ID
_COVERAGE_RUNNER_AGENT_ID = _builtin_constants.COVERAGE_RUNNER_AGENT_ID
_SSL_CERTIFICATE_DECODER_AGENT_ID = _builtin_constants.SSL_CERTIFICATE_DECODER_AGENT_ID
_DIFF_ANALYZER_AGENT_ID = _builtin_constants.DIFF_ANALYZER_AGENT_ID
_K8S_MANIFEST_VALIDATOR_AGENT_ID = _builtin_constants.K8S_MANIFEST_VALIDATOR_AGENT_ID
_ARCHIVE_INSPECTOR_AGENT_ID = _builtin_constants.ARCHIVE_INSPECTOR_AGENT_ID
_UNICODE_INSPECTOR_AGENT_ID = _builtin_constants.UNICODE_INSPECTOR_AGENT_ID
_TERRAFORM_PLAN_ANALYZER_AGENT_ID = _builtin_constants.TERRAFORM_PLAN_ANALYZER_AGENT_ID
_LIVE_SANDBOX_AGENT_ID = _builtin_constants.LIVE_SANDBOX_AGENT_ID
_REGEX_TESTER_AGENT_ID = _builtin_constants.REGEX_TESTER_AGENT_ID
_JWT_VALIDATOR_AGENT_ID = _builtin_constants.JWT_VALIDATOR_AGENT_ID
_SBOM_GENERATOR_AGENT_ID = _builtin_constants.SBOM_GENERATOR_AGENT_ID
_PYPI_METADATA_AGENT_ID = _builtin_constants.PYPI_METADATA_AGENT_ID
_GITHUB_RELEASES_AGENT_ID = _builtin_constants.GITHUB_RELEASES_AGENT_ID
_HCL_TERRAFORM_ANALYZER_AGENT_ID = _builtin_constants.HCL_TERRAFORM_ANALYZER_AGENT_ID
_QUANT_PATCH_VALIDATOR_AGENT_ID = _builtin_constants.QUANT_PATCH_VALIDATOR_AGENT_ID
# 2026-05-22 — strategy-doc 7-agent slate (post-editorial cut)
_FLAKE_HUNTER_AGENT_ID = _builtin_constants.FLAKE_HUNTER_AGENT_ID
_BISECT_AND_BLAME_AGENT_ID = _builtin_constants.BISECT_AND_BLAME_AGENT_ID
_COMPLIANCE_ATTESTOR_AGENT_ID = _builtin_constants.COMPLIANCE_ATTESTOR_AGENT_ID
_STRIPE_CONNECT_SETTLER_AGENT_ID = _builtin_constants.STRIPE_CONNECT_SETTLER_AGENT_ID
_CODEBASE_REVIEWER_AGENT_ID = _builtin_constants.CODEBASE_REVIEWER_AGENT_ID
_PROD_TRACE_REPLAYER_AGENT_ID = _builtin_constants.PROD_TRACE_REPLAYER_AGENT_ID
_SCHEMA_MIGRATION_PLANNER_AGENT_ID = _builtin_constants.SCHEMA_MIGRATION_PLANNER_AGENT_ID

_normalize_endpoint_ref = _builtin_constants.normalize_endpoint_ref
_BUILTIN_INTERNAL_ENDPOINTS = _builtin_constants.BUILTIN_INTERNAL_ENDPOINTS
_BUILTIN_LEGACY_ROUTE_ENDPOINTS = _builtin_constants.BUILTIN_LEGACY_ROUTE_ENDPOINTS
_BUILTIN_ENDPOINT_TO_AGENT_ID = _builtin_constants.BUILTIN_ENDPOINT_TO_AGENT_ID
_BUILTIN_AGENT_IDS = _builtin_constants.BUILTIN_AGENT_IDS
_CURATED_PUBLIC_BUILTIN_AGENT_IDS = _builtin_constants.CURATED_PUBLIC_BUILTIN_AGENT_IDS
_CURATED_BUILTIN_AGENT_IDS = _builtin_constants.CURATED_BUILTIN_AGENT_IDS
_SUNSET_DEPRECATED_AGENT_IDS = _builtin_constants.SUNSET_DEPRECATED_AGENT_IDS
_BUILTIN_WORKER_OWNER_ID = _builtin_constants.BUILTIN_WORKER_OWNER_ID
_SYSTEM_USERNAME = _builtin_constants.SYSTEM_USERNAME
_SYSTEM_USER_EMAIL = _builtin_constants.SYSTEM_USER_EMAIL

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
# Raised 2026-05-18 from 5¢ to 25¢. The 5% BPS rate alone gave a 1¢ call a 0¢
# computed deposit (clamped to 5¢, so 500% of call cost — that *was* friction
# but inverted: cheap calls were OVER-friction while a free call needed only
# a 5¢ deposit to dispute. We want enough floor that frivolous disputes
# against sub-25¢ calls aren't a cheap weapon, while keeping the rate scale
# meaningful for higher-priced calls (e.g. $1 call → 5¢ rate, dwarfed by
# this floor — confirms the asymmetry trade-off). Higher-priced calls scale
# via BPS as before.
_DEFAULT_DISPUTE_FILING_DEPOSIT_MIN_CENTS = 25
_DEFAULT_DISPUTE_JUDGE_INTERVAL_SECONDS = 30  # 1.7.0: tightened from 60s
# Eval saw 155s+ on a 2-judge dispute even though hint said "~1 minute"; the
# leader-elected judge thread isn't always re-acquired immediately after a
# uvicorn worker restart. Halving the interval cuts the recovery window
# without doubling load — most disputes settle in one tick because both
# LLM judges fan out within the same sweep.
# Worker tuned for 100+ job parallel batches: the loop wakes immediately on
# submission via _BUILTIN_WORKER_WAKE_EVENT, drains up to MAX_BATCH_TOTAL per
# tick across PARALLELISM threads, and re-arms the wake event whenever a tick
# actually does work. The 1s interval is the idle ceiling; fan-out latency is
# bounded by per-job runtime, not by polling.
#
# IMPORTANT: PARALLELISM must stay safely below DB_MAX_CONNECTIONS (default 32)
# so worker threads never block on the connection pool. A 64-worker pool with
# a 32-connection DB caused exactly the load-test stall we saw on 2026-05-08:
# 84/100 jobs settled, 16 froze because every connection was held by an
# in-flight settlement waiting for a connection that never freed.
_DEFAULT_BUILTIN_JOB_WORKER_INTERVAL_SECONDS = 1
_DEFAULT_BUILTIN_JOB_WORKER_BATCH_SIZE = 400
_DEFAULT_BUILTIN_JOB_WORKER_PARALLELISM = 24
_DEFAULT_BUILTIN_JOB_WORKER_MAX_BATCH_TOTAL = 800
_DEFAULT_TOPUP_DAILY_LIMIT_CENTS = 100_000
_DEFAULT_PAYMENTS_RECONCILIATION_INTERVAL_SECONDS = 3600
_DEFAULT_PAYMENTS_RECONCILIATION_MAX_MISMATCHES = 100
_DEFAULT_ENDPOINT_MONITOR_BATCH_SIZE = 100
_DEFAULT_ENDPOINT_MONITOR_TIMEOUT_SECONDS = 3
_DEFAULT_ENDPOINT_MONITOR_FAILURE_THRESHOLD = 3
MINIMUM_DEPOSIT_CENTS = int(os.getenv("MINIMUM_DEPOSIT_CENTS", "500"))
_PROTOCOL_VERSION = "1.0"


def _read_server_version() -> str:
    """Read the deployable application version from the repo VERSION file."""
    candidate = os.path.join(_REPO_ROOT, "VERSION")
    try:
        with open(candidate, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            return value or "0.0.0"
    except OSError:
        return "0.0.0"


SERVER_VERSION = _read_server_version()
_PROTOCOL_VERSION_HEADER = "X-Aztea-Version"
_LEGACY_PROTOCOL_VERSION_HEADER = "X-AgentMarket-Version"
_CLIENT_ID_HEADER = "X-Aztea-Client"
# $0.001 cannot be represented in integer cents; keep ledger integer-safe until millicent support exists.
_DEFAULT_JUDGE_FEE_CENTS = 0
_REPUTATION_DECAY_GRACE_DAYS = 30
_REPUTATION_DECAY_DAILY_RATE = 0.005
# Surgical band-aid for stale latency averages (see migration 0056).
# Grace is shorter than reputation decay (7 d vs 30 d) because a stale slow
# latency actively misrepresents a healthy agent in the catalog UI; the
# rolling-window proper fix is tracked separately.
_LATENCY_DECAY_GRACE_DAYS = 7
_LATENCY_DECAY_DAILY_MULTIPLIER = 0.9
_LATENCY_DECAY_FLOOR_MS = 1
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
    error_code: str | None = None,
) -> None:
    """Side-effect: append a row to ``mcp_invocation_log``. Never raises.

    ``error_code`` is the structured ``error.code`` returned by the dispatch
    path (e.g. ``budget.insufficient_funds``, ``agent.timeout``). NULL on
    success. Failures without a structured code log NULL so a missing column
    value is unambiguous.
    """
    input_hash = hashlib.sha256(
        input_json.encode("utf-8", errors="replace")
    ).hexdigest()
    row_id = str(uuid.uuid4())
    now = _utc_now_iso()
    try:
        with jobs._conn() as conn:
            conn.execute(
                """
                INSERT INTO mcp_invocation_log
                    (id, agent_id, caller_key_id, tool_name, input_hash, invoked_at,
                     duration_ms, success, error_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row_id,
                    agent_id,
                    caller_key_id,
                    tool_name,
                    input_hash,
                    now,
                    duration_ms,
                    int(success),
                    error_code,
                ),
            )
    except Exception:
        _LOG.warning("Failed to write MCP invocation log for tool '%s'", tool_name)


def _extract_mcp_error_code(exc: BaseException) -> str | None:
    """Pure: pull a structured error code out of an HTTPException detail, if present.

    Why: ``error_codes.make_error()`` emits ``{"code": str, "message": str, ...}``.
    Anything that isn't a dict with a ``code`` key returns None so the column
    stays unambiguous.
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = detail.get("code")
        if isinstance(code, str) and code:
            return code
    return None


def _env_int(
    name: str, default: int, minimum: int | None = None, maximum: int | None = None
) -> int:
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


def _env_float(
    name: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
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
            raise RuntimeError(
                f"{name} contains invalid IP/CIDR value {candidate!r}."
            ) from exc
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
    raise RuntimeError(
        "HOOK_DELIVERY_MAX_DELAY_SECONDS must be >= HOOK_DELIVERY_BASE_DELAY_SECONDS."
    )
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
_BUILTIN_JOB_WORKER_PARALLELISM = _env_int(
    "BUILTIN_JOB_WORKER_PARALLELISM",
    _DEFAULT_BUILTIN_JOB_WORKER_PARALLELISM,
    minimum=1,
    maximum=128,
)
_BUILTIN_JOB_WORKER_MAX_BATCH_TOTAL = _env_int(
    "BUILTIN_JOB_WORKER_MAX_BATCH_TOTAL",
    _DEFAULT_BUILTIN_JOB_WORKER_MAX_BATCH_TOTAL,
    minimum=1,
    maximum=5000,
)
_BUILTIN_JOB_WORKER_ENABLED = _env_bool(
    "BUILTIN_JOB_WORKER_ENABLED",
    default=_builtin_worker_interval > 0,
)
# Event used to wake the builtin worker immediately when new pending jobs are
# submitted (e.g. from hire_batch). Avoids the 1-2s polling delay on small
# batches and keeps queued work moving when the pool has free slots.
_BUILTIN_WORKER_WAKE_EVENT: threading.Event = threading.Event()


def _wake_builtin_worker() -> None:
    """Signal the builtin worker loop to skip its next sleep window."""
    try:
        _BUILTIN_WORKER_WAKE_EVENT.set()
    except Exception:
        pass


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
_ENVIRONMENT = (
    str(os.environ.get("ENVIRONMENT", "development") or "development").strip().lower()
)
_ALLOW_PRIVATE_OUTBOUND_URLS = os.environ.get(
    "ALLOW_PRIVATE_OUTBOUND_URLS", "0"
).strip().lower() in {
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
_BUILTIN_WORKER_RESCUE_LOCK = threading.Lock()
_BUILTIN_WORKER_RESCUE_RUNNING = False
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
_JOB_TERMINAL_STATUSES = {"complete", "failed", "stopped", "cancelled"}
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
    "partial_output",
    "artifact",
    "agent_message",
    "note",
    "tool_call",
    "tool_result",
    "steer",
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
    """Pure: deposit = max(floor, 5% of call price).

    2026-05-18: the floor applies even for free calls. The pre-2026-05-18
    behaviour was "0¢ call → 0¢ deposit", which gave attackers a free
    dispute weapon against gateway-subsidized free-tier agents — every
    such call could be disputed risk-free. The fix is to charge the floor
    even on free calls (still refunded if the dispute succeeds).
    """
    normalized_price = max(0, int(price_cents))
    if _DISPUTE_FILING_DEPOSIT_MIN_CENTS <= 0:
        return 0
    computed = (normalized_price * _DISPUTE_FILING_DEPOSIT_BPS) // 10_000
    return max(_DISPUTE_FILING_DEPOSIT_MIN_CENTS, computed)
