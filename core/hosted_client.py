# SPDX-License-Identifier: Apache-2.0
"""
Thin client to aztea.ai's hosted API.

# OWNS: outbound HTTP to the hosted aztea.ai control-plane (judges, hosted
#       agent execution, public registry publish, federated trust, rating
#       sync). One call site per service. Soft-fails on every error so a
#       hosted outage degrades to local behavior, never a 500 on the caller.
# NOT OWNS: any local logic. The caller decides whether to use the hosted
#       result or fall back. We do not implement business rules here.
# INVARIANTS:
#   - When `is_enabled()` is False (AZTEA_HOSTED_API_URL unset), every method
#     returns None / no-op without touching the network. The OSS build MUST
#     be observably offline in this state.
#   - All outbound URLs are constructed from the configured base URL.
#     We never accept arbitrary URLs from callers. SSRF is therefore moot,
#     but we still pass the assembled URL through `core.url_security` for
#     belt-and-braces.
#   - Bearer auth via AZTEA_HOSTED_API_KEY only. No cookies, no signed
#     query params; we don't accept anything else.
# DECISIONS:
#   - We use `requests` (not httpx) to match `core/pipelines/executor.py` and
#     avoid pulling another dep into the OSS install.
#   - Per-call timeouts are conservative: 8s for read-mostly endpoints, 30s
#     for judge/agent-execution. Hosted-mode users can override via env.
#   - We do not retry. Hosted outages should fall back to local fast.

Any module needing hosted services imports `get_hosted_client()` and calls
methods on the returned client. If the client is disabled the methods
return `None` (or `False` for booleans) and the caller takes the local path.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Mapping, Optional

import requests

from core import feature_flags
from core import url_security


logger = logging.getLogger(__name__)


_DEFAULT_READ_TIMEOUT_SECONDS = 8.0
_DEFAULT_EXEC_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB cap on hosted-API responses

# Circuit-breaker defaults. Conservative — break only after 3 consecutive
# failures within the lookback window, and stay open for 30s. Caller paths
# (judge, call_agent) are then short-circuited to local fallback fast.
_DEFAULT_CB_THRESHOLD = 3
_DEFAULT_CB_COOLDOWN_SECONDS = 30.0


class _CircuitBreaker:
    """Per-path consecutive-failure tracker.

    After ``threshold`` consecutive failures, opens the circuit for
    ``cooldown_seconds``. While open, ``allow(path)`` returns False so
    the caller short-circuits to local fallback without a 30s network
    wait. After cooldown elapses, the next call is allowed as a probe —
    if it succeeds (``record_success``) the circuit closes; if it fails
    (``record_failure``) the circuit re-opens.

    Thread-safe: a single lock guards the state dict. Worst-case
    contention is one outbound HTTP call ↔ one breaker update, which is
    cheap relative to network latency.
    """

    def __init__(
        self,
        *,
        threshold: int = _DEFAULT_CB_THRESHOLD,
        cooldown_seconds: float = _DEFAULT_CB_COOLDOWN_SECONDS,
        clock: Any = None,
    ) -> None:
        self._threshold = max(1, int(threshold))
        self._cooldown = max(1.0, float(cooldown_seconds))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        # path → {"failures": int, "opened_at": float | None}
        self._state: dict[str, dict[str, Any]] = {}

    def allow(self, path: str) -> bool:
        with self._lock:
            entry = self._state.get(path)
            if not entry or entry.get("opened_at") is None:
                return True
            if self._clock() - entry["opened_at"] >= self._cooldown:
                # Cooldown elapsed — allow one probe and reset the counter.
                entry["failures"] = 0
                entry["opened_at"] = None
                return True
            return False

    def record_success(self, path: str) -> None:
        with self._lock:
            self._state[path] = {"failures": 0, "opened_at": None}

    def record_failure(self, path: str) -> None:
        with self._lock:
            entry = self._state.setdefault(
                path, {"failures": 0, "opened_at": None}
            )
            entry["failures"] = int(entry.get("failures") or 0) + 1
            if entry["failures"] >= self._threshold:
                entry["opened_at"] = self._clock()
                logger.warning(
                    "hosted_client: circuit OPEN for %s after %s consecutive failures",
                    path,
                    entry["failures"],
                )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Read-only copy of the current state. Used in tests."""
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}


class HostedClient:
    """Stateless client. Cheap to construct; no connection pooling state."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        read_timeout: float | None = None,
        exec_timeout: float | None = None,
        breaker: _CircuitBreaker | None = None,
    ) -> None:
        self._base_url = (base_url or feature_flags.hosted_api_url()).rstrip("/")
        self._api_key = api_key or feature_flags.hosted_api_key()
        self._read_timeout = (
            read_timeout if read_timeout is not None
            else _env_float("AZTEA_HOSTED_READ_TIMEOUT", _DEFAULT_READ_TIMEOUT_SECONDS)
        )
        self._exec_timeout = (
            exec_timeout if exec_timeout is not None
            else _env_float("AZTEA_HOSTED_EXEC_TIMEOUT", _DEFAULT_EXEC_TIMEOUT_SECONDS)
        )
        self._breaker = breaker or _CircuitBreaker(
            threshold=int(_env_float(
                "AZTEA_HOSTED_CB_THRESHOLD", float(_DEFAULT_CB_THRESHOLD)
            )),
            cooldown_seconds=_env_float(
                "AZTEA_HOSTED_CB_COOLDOWN_SECONDS", _DEFAULT_CB_COOLDOWN_SECONDS
            ),
        )

    def is_enabled(self) -> bool:
        """True iff this instance is configured to call the hosted API.

        Disabled clients short-circuit every method to None / no-op. This
        is the canonical OSS-mode check — read it before calling anything
        else if you want to skip work entirely on the local path.
        """
        return bool(self._base_url) and bool(self._api_key)

    # ---- Service methods --------------------------------------------------

    def judge_dispute(self, context: Mapping[str, Any]) -> Optional[dict]:
        """Run a dispute through the hosted LLM judge.

        Returns the verdict dict on success, None on any error (caller falls
        back to local judge). Hosted-side cost is metered against the
        instance's hosted account.
        """
        return self._post_json(
            "/v1/judges/judge",
            payload={"context": dict(context)},
            timeout=self._exec_timeout,
        )

    def call_agent(self, slug: str, payload: Mapping[str, Any]) -> Optional[dict]:
        """Invoke a hosted built-in agent. Caller pays via the hosted ledger."""
        if not slug or not isinstance(slug, str):
            return None
        return self._post_json(
            f"/v1/agents/{slug}/call",
            payload={"payload": dict(payload)},
            timeout=self._exec_timeout,
        )

    def publish_listing(self, spec: Mapping[str, Any]) -> Optional[dict]:
        """Publish an agent spec to aztea.ai's public registry.

        Hosted side enforces the listing fee or 10% commission on traffic
        through the public listing. This client just hands over the spec.
        """
        return self._post_json(
            "/v1/registry/publish",
            payload={"spec": dict(spec)},
            timeout=self._read_timeout,
        )

    def push_rating(self, rating: Mapping[str, Any]) -> bool:
        """Fire-and-forget rating push. Returns True on any 2xx (including
        a 204 No Content), False otherwise.

        Caller should not block on the result. Failures are logged at debug
        level only — the local rating row is the source of truth.
        """
        return self._post_acknowledge(
            "/v1/reputation/ratings",
            payload=dict(rating),
            timeout=self._read_timeout,
        )

    def fetch_trust(self, agent_did: str) -> Optional[dict]:
        """Look up a federated trust score for an agent DID."""
        if not agent_did:
            return None
        return self._get_json(
            f"/v1/trust/{agent_did}",
            timeout=self._read_timeout,
        )

    # ---- Internals --------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "aztea-oss/1 hosted-client",
        }

    def _validate_target(self, path: str) -> str | None:
        """Compose + SSRF-check the full URL. Returns None on rejection."""
        if not self.is_enabled():
            return None
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self._base_url}{path}"
        try:
            return url_security.validate_outbound_url(url, "hosted_api_url")
        except Exception as exc:  # noqa: BLE001 — url_security raises broadly
            logger.warning("hosted_client: rejected URL %s: %s", url, exc)
            return None

    def _post_json(
        self,
        path: str,
        *,
        payload: Mapping[str, Any],
        timeout: float,
    ) -> Optional[dict]:
        if not self._breaker.allow(path):
            logger.info("hosted_client: short-circuit (circuit open) for %s", path)
            return None
        url = self._validate_target(path)
        if not url:
            return None
        try:
            with requests.post(
                url,
                json=dict(payload),
                headers=self._headers(),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            ) as response:
                result = _read_capped_json(response)
        except requests.RequestException as exc:
            logger.info("hosted_client: POST %s failed: %s", path, exc)
            self._breaker.record_failure(path)
            return None
        if result is None:
            self._breaker.record_failure(path)
        else:
            self._breaker.record_success(path)
        return result

    def _post_acknowledge(
        self,
        path: str,
        *,
        payload: Mapping[str, Any],
        timeout: float,
    ) -> bool:
        """Fire-and-forget POST. Returns True on any 2xx response (including
        an empty 204), False otherwise. Body content is ignored.
        """
        if not self._breaker.allow(path):
            logger.info("hosted_client: short-circuit (circuit open) for %s", path)
            return False
        url = self._validate_target(path)
        if not url:
            return False
        try:
            response = requests.post(
                url,
                json=dict(payload),
                headers=self._headers(),
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.info("hosted_client: POST %s failed: %s", path, exc)
            self._breaker.record_failure(path)
            return False
        ok = bool(getattr(response, "ok", False))
        try:
            response.close()
        except Exception:  # noqa: BLE001 — close() in best-effort cleanup
            pass
        if ok:
            self._breaker.record_success(path)
        else:
            logger.info(
                "hosted_client: HTTP %s on %s",
                getattr(response, "status_code", "?"),
                path,
            )
            self._breaker.record_failure(path)
        return ok

    def _get_json(self, path: str, *, timeout: float) -> Optional[dict]:
        if not self._breaker.allow(path):
            logger.info("hosted_client: short-circuit (circuit open) for %s", path)
            return None
        url = self._validate_target(path)
        if not url:
            return None
        try:
            with requests.get(
                url,
                headers=self._headers(),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            ) as response:
                result = _read_capped_json(response)
        except requests.RequestException as exc:
            logger.info("hosted_client: GET %s failed: %s", path, exc)
            self._breaker.record_failure(path)
            return None
        if result is None:
            self._breaker.record_failure(path)
        else:
            self._breaker.record_success(path)
        return result


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _read_capped_json(response: requests.Response) -> Optional[dict]:
    """Read at most _MAX_RESPONSE_BYTES from response and parse as JSON."""
    if not response.ok:
        logger.info(
            "hosted_client: HTTP %s on %s",
            response.status_code,
            response.url,
        )
        return None
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > _MAX_RESPONSE_BYTES:
        logger.warning("hosted_client: declared response too large (%s bytes)", declared)
        return None
    buf = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > _MAX_RESPONSE_BYTES:
            logger.warning("hosted_client: streamed response exceeded cap")
            return None
    try:
        import json as _json

        parsed = _json.loads(buf.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("hosted_client: invalid JSON in response: %s", exc)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


_GLOBAL_CLIENT: HostedClient | None = None


def get_hosted_client() -> HostedClient:
    """Return a process-wide HostedClient. Safe to call repeatedly.

    The client is reconstructed if env vars change between calls (the env
    read happens inside HostedClient.__init__, so each new instance picks
    up the current values). We cache one instance to avoid the (small) cost
    of repeated constructor work in hot paths.
    """
    global _GLOBAL_CLIENT
    cur_url = feature_flags.hosted_api_url()
    cur_key = feature_flags.hosted_api_key()
    if (
        _GLOBAL_CLIENT is None
        or _GLOBAL_CLIENT._base_url != cur_url
        or _GLOBAL_CLIENT._api_key != cur_key
    ):
        _GLOBAL_CLIENT = HostedClient(base_url=cur_url, api_key=cur_key)
    return _GLOBAL_CLIENT


def reset_hosted_client_for_tests() -> None:
    """Drop the cached client so the next call re-reads env. Tests only."""
    global _GLOBAL_CLIENT
    _GLOBAL_CLIENT = None
