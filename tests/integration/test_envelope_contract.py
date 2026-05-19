"""Phase 5 (2026-05-19): cross-cutting envelope contract test.

Walks every public HTTP route and fires a controlled set of fuzz requests
against it. For each response that isn't 2xx the test asserts:

1. The body is a JSON object with at least ``error`` (str) and
   ``message`` (str) keys — the canonical envelope produced by
   ``core.error_codes.make_error``.
2. The response body does NOT contain any sensitive field name from the
   12-entry block list, neither as a key nor as a value substring.
3. The ``message`` field does NOT look like raw exception text (Phase 1
   sanitiser must have kicked in if it did).

This is the canonical "envelope correctness" CI guarantee. A future
route handler that bypasses the envelope OR leaks a sensitive substring
should fail this test immediately, not in the next red-team session.

The test runs against an isolated TestClient so it doesn't depend on
network state. It's parametrised per (route, fuzz_kind) so failures
point at the exact offending pair.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount

from tests.integration.support import *  # noqa: F403


# 12-entry block list — fields that must never appear in any response body.
# Substring match against the JSON-serialised body, so a stored field
# name "callback_secret" or a value containing "shhhh-do-not-leak" both
# trigger the failure (the F2 + F3 + Phase 4 fixes all guard against this).
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "callback_secret",
    "raw_api_key",
    "join_token",
    "signed_payload_b64",
    "signature_priv",
    "stripe_webhook_secret",
    "password_hash",
    "email_verification_token",
    "session_cookie",
    "private_key",
    "key_hash",
    "share_id",
)

# Patterns that signal "this looks like a raw library exception leaked
# into the message" — Phase 1's sanitiser is supposed to catch these but
# this test is the second line of defence.
_LEAK_MESSAGE_RE = re.compile(
    r"(traceback|psycopg2\.errors|sqlalchemy|pydantic\.|valueerror:|typeerror:|"
    r"keyerror:|attributeerror:|filenotfounderror:|cannot contain nul|"
    r"disallowed cors origin|starlette\.exceptions|internalservererror|<class ')",
    re.IGNORECASE,
)


# Paths that don't return a JSON envelope and are exempt from the contract:
# SPA fallback, openapi spec, raw HTML docs, RSS feeds.
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/static",
    "/metrics",  # Prometheus text format
    "/elixir/",  # Phoenix sidecar pass-through
    "/_internal",
)


def _route_is_exempt(path: str) -> bool:
    if not path:
        return True
    lowered = path.lower()
    for prefix in _EXEMPT_PREFIXES:
        if lowered.startswith(prefix):
            return True
    return False


def _route_inventory(app) -> list[tuple[str, str]]:
    """Pure: collect (method, path) pairs from the FastAPI route table.

    Filters out HEAD/OPTIONS auto-methods (the framework handles those),
    websocket routes, and mounted sub-apps. Returns a sorted, deduped
    list so test parametrisation is stable across runs.
    """
    seen: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, Mount):
            continue
        if not isinstance(route, APIRoute):
            continue
        path = getattr(route, "path", "") or ""
        if _route_is_exempt(path):
            continue
        for method in route.methods or set():
            m = method.upper()
            if m in {"HEAD", "OPTIONS"}:
                continue
            seen.add((m, path))
    return sorted(seen)


def _substitute_path_params(path: str, value: str = "abc-fuzz-123") -> str:
    """Replace ``{param}`` segments with a stable fuzz value.

    The substitute value is intentionally short, ascii-only, and unlikely
    to collide with any real ID — the route handler should treat it as
    "not found" and surface a structured 404 envelope.
    """
    return re.sub(r"\{[^}]+\}", value, path)


def _assert_envelope_shape(body: Any, path: str, status: int) -> None:
    assert isinstance(body, dict), (
        f"{path} returned non-dict body at status {status}: {type(body).__name__}"
    )
    error_field = body.get("error")
    message_field = body.get("message")
    # The envelope contract: error + message must both be non-empty strings.
    assert isinstance(error_field, str) and error_field, (
        f"{path} (status {status}) is missing structured `error` field: {body!r}"
    )
    assert isinstance(message_field, str) and message_field, (
        f"{path} (status {status}) is missing structured `message` field: {body!r}"
    )
    # `error` is dot-namespaced like "auth.invalid_key" — no raw English
    # codes ("INVALID_INPUT") and no empty strings.
    assert "." in error_field or "_" in error_field, (
        f"{path} (status {status}) `error` is not a recognised code shape: {error_field!r}"
    )
    # Phase 1's sanitiser should keep raw exception text out of the
    # user-facing message. Any leak is a CI-fail.
    assert not _LEAK_MESSAGE_RE.search(message_field), (
        f"{path} (status {status}) `message` looks like a raw exception leak: "
        f"{message_field!r}"
    )


def _assert_no_sensitive_substring(text: str, path: str, status: int) -> None:
    for token in _FORBIDDEN_SUBSTRINGS:
        if token not in text:
            continue
        # Allowlist: ``callback_secret`` as a redaction MARKER (i.e., the
        # block list helper itself produces the literal ``"callback_secret":
        # "<redacted>"``). That's exactly the protected state — only fail
        # if the field appears WITHOUT the <redacted> sentinel nearby.
        idx = text.find(token)
        window = text[max(0, idx - 32): idx + 64]
        if "<redacted>" in window:
            continue
        pytest.fail(
            f"{path} (status {status}) response contains forbidden substring "
            f"{token!r}: ...{window!r}..."
        )


@pytest.fixture(scope="module")
def envelope_client():
    """Module-scoped TestClient so the ~200-route walk doesn't pay
    fixture setup per request. The DB is shared across all parametrised
    tests in this file."""
    import os
    from pathlib import Path
    from fastapi.testclient import TestClient
    from tests.integration.helpers import _close_module_conn

    db_path = Path(__file__).resolve().parent / f"envelope-contract-{uuid.uuid4().hex}.db"
    os.environ.setdefault("API_KEY", "test-master-key")

    from core import (
        auth, payments, jobs, registry, reputation, disputes,
        compare, pipelines, workspaces,
    )
    from core import cache as result_cache
    import server.application as server

    modules = (registry, payments, auth, jobs, reputation, disputes,
               result_cache, compare, pipelines, workspaces)
    for module in modules:
        _close_module_conn(module)
        module.DB_PATH = str(db_path)

    from core.migrate import apply_migrations
    apply_migrations(str(db_path))
    server._MASTER_KEY = "test-master-key"

    with TestClient(server.app) as client:
        yield client

    for module in modules:
        _close_module_conn(module)
    for suffix in ("", "-shm", "-wal"):
        f = Path(f"{db_path}{suffix}")
        if f.exists():
            f.unlink()


def _all_routes() -> list[tuple[str, str]]:
    """Importing the app at module-collection time so pytest parametrises
    over the real route table. The TestClient fixture above re-imports
    it lazily for the actual requests."""
    import os
    os.environ.setdefault("API_KEY", "test-master-key")
    import server.application as server
    return _route_inventory(server.app)


_ROUTES = _all_routes()


@pytest.mark.parametrize(
    "method,path",
    _ROUTES,
    ids=[f"{m}_{p}" for m, p in _ROUTES],
)
def test_envelope_contract_no_auth(envelope_client, method, path) -> None:
    """Without auth, every protected route must return a structured envelope.

    Public routes (those that don't require auth) may return 2xx — the
    contract only triggers for 4xx/5xx responses.
    """
    target = _substitute_path_params(path)
    # Minimal fuzz body for POST/PUT/PATCH; GET/DELETE use the URL only.
    body = {} if method in {"POST", "PUT", "PATCH"} else None
    try:
        resp = envelope_client.request(method, target, json=body)
    except Exception as exc:  # noqa: BLE001 — the test framework should never crash
        pytest.fail(f"{method} {target} raised before responding: {exc!r}")
        return
    if 200 <= resp.status_code < 300:
        # Successful unauthenticated response is acceptable for public
        # routes (/health, /system/health, manifest endpoints, etc.).
        # Still walk the body for sensitive-substring leakage.
        _assert_no_sensitive_substring(resp.text, target, resp.status_code)
        return
    if resp.status_code in (307, 308):
        # Redirects (e.g. /openapi.json → /api/openapi.json) — no envelope.
        return
    try:
        parsed = resp.json()
    except (ValueError, json.JSONDecodeError):
        pytest.fail(
            f"{method} {target} (status {resp.status_code}) returned non-JSON: "
            f"{resp.text[:200]!r}"
        )
        return
    _assert_envelope_shape(parsed, target, resp.status_code)
    _assert_no_sensitive_substring(resp.text, target, resp.status_code)


@pytest.mark.parametrize(
    "method,path",
    [(m, p) for m, p in _ROUTES if m in {"POST", "PUT", "PATCH"}],
    ids=[f"{m}_{p}" for m, p in _ROUTES if m in {"POST", "PUT", "PATCH"}],
)
def test_envelope_contract_nul_body(envelope_client, method, path) -> None:
    """Mutating routes must reject a body with embedded NUL bytes via
    a structured envelope, never via raw psycopg2 / pydantic text."""
    target = _substitute_path_params(path)
    body = {"reason": "x\x00y", "name": "abc\x00def", "task": "z\x00"}
    headers = {"Authorization": "Bearer test-master-key"}
    try:
        resp = envelope_client.request(method, target, json=body, headers=headers)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{method} {target} raised on NUL body: {exc!r}")
        return
    if 200 <= resp.status_code < 300:
        _assert_no_sensitive_substring(resp.text, target, resp.status_code)
        return
    if resp.status_code in (307, 308):
        return
    try:
        parsed = resp.json()
    except (ValueError, json.JSONDecodeError):
        pytest.fail(
            f"{method} {target} (NUL body, status {resp.status_code}) "
            f"returned non-JSON: {resp.text[:200]!r}"
        )
        return
    _assert_envelope_shape(parsed, target, resp.status_code)
    _assert_no_sensitive_substring(resp.text, target, resp.status_code)


def test_envelope_contract_route_count_sanity() -> None:
    """The route walk must actually find a substantial number of routes.

    If this drops below 50, something is wrong with the inventory (the
    contract test would otherwise pass vacuously).
    """
    assert len(_ROUTES) >= 50, (
        f"Route inventory dropped to {len(_ROUTES)} — investigate "
        "(maybe a Mount got introduced and dodged the walker?)"
    )
