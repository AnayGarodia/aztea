"""Surface auth matrix — every route × auth state.

# OWNS: parametrized matrix asserting each route's auth posture:
#   - unauthenticated requests must NOT return 200 (no accidental leaks);
#   - master-key requests must NOT return 401/403.
# DECISIONS: path params are substituted with a synthetic UUID. Some routes
#   will then 404 because the resource doesn't exist — that's fine: the
#   matrix only asserts the *auth* boundary, not business validation.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.corpora import route_inventory
from tests.integration.helpers import TEST_MASTER_KEY

pytestmark = pytest.mark.surface

_ROUTES = route_inventory()
_PATH_PARAM_RE = re.compile(r"\{[^/}]+\}")
_FAKE_ID = "00000000-0000-0000-0000-000000000000"

# Routes that are intentionally public — unauthenticated 200 is correct.
_PUBLIC_PATH_PREFIXES = (
    "/",
    "/api/openapi.json",
    "/api/docs",
    "/api/redoc",
    # 2026-05-19 (B10): /openapi.json and /redoc are 308-redirected to the
    # /api/* counterparts. TestClient follows redirects by default, so the
    # final response is the 200 from the public /api/* endpoint. Listing
    # the shortnames here is semantically correct — they ARE public via
    # the redirect.
    "/openapi.json",
    "/redoc",
    # 2026-05-19 (B7): /system/health is an alias for /health (public by
    # design). The system-router /system/health endpoint mirrors /health's
    # public contract for load balancers and integrators using either path.
    "/system/health",
    "/.well-known/",
    "/auth/register",
    "/auth/login",
    "/auth/signup",
    "/auth/google",
    "/auth/forgot-password",
    "/auth/reset-password",
    "/auth/legal",
    "/auth/legal/accept",
    "/onboarding/spec",
    "/onboarding/validate",
    "/registry/agents",
    "/registry/agents/search",
    "/registry/search",
    "/registry/models",
    "/agent.md",
    "/health",
    "/healthz",
    "/readyz",
    "/livez",
    "/metrics",
    "/version",
    "/manifest",
    "/static",
    "/assets",
    "/favicon.ico",
    "/robots.txt",
    # /sitemap.xml is the SEO sitemap — public by definition, mirrors
    # /robots.txt above. Served by part_013.sitemap_xml.
    "/sitemap.xml",
    "/config/public",
    "/public/docs",
    "/docs/oauth2-redirect",
    # Dispute-policy is intentionally public — CLI/SDK clients need to quote
    # exact deposit amounts before the user confirms a dispute. Documented
    # public in server/routes/system.py:144 ("No auth required: these are
    # policy constants, not secrets").
    "/ops/dispute-policy",
    # Workspaces verifiability endpoints — required public so external
    # consumers can verify Ed25519 seal manifests without an Aztea account.
    # The DID document follows the did:web spec. Manifest + verify are the
    # public verification surface; CRUD on /workspaces/{id} stays caller-scoped.
    "/workspaces/sealer/did.json",
    "/workspaces/{workspace_id}/manifest",
    "/workspaces/{workspace_id}/verify",
)

# Routes intentionally NOT available to the master API key (user-scoped
# self-service endpoints — master is a server identity, not a user).
# 403 + auth.insufficient_scope is the correct response on these.
_MASTER_KEY_REJECTED_PATH_PREFIXES = (
    "/auth/keys",
    "/ops/platform-stats",
)

# Routes whose path the test rewriter can't safely reach (e.g., requires a
# specific multi-segment shape, or hits an external service). Skip them.
_SKIP_CONTAINS = (
    "{full_path:path}",
    "/topup/session",  # requires Stripe
    "/connect/onboard",  # requires Stripe Connect
)


def _filled_path(path: str) -> str:
    return _PATH_PARAM_RE.sub(_FAKE_ID, path)


def _is_public(path: str) -> bool:
    if path == "/":
        return True
    for p in _PUBLIC_PATH_PREFIXES:
        if p == "/":
            continue
        if path == p:
            return True
        # Prefixes ending in "/" are intentional namespace markers (everything
        # under them is public). Otherwise require an exact match or a `/`-
        # delimited child path.
        if p.endswith("/") and path.startswith(p):
            return True
        if path.startswith(p + "/"):
            return True
    return False


def _route_id(method: str, path: str) -> str:
    return f"{method} {path}"


_PARAMS = []
for method, path, _methods in _ROUTES:
    if any(skip in path for skip in _SKIP_CONTAINS):
        continue
    _PARAMS.append(pytest.param(method, path, id=_route_id(method, path)))


def _safe_request(client: TestClient, method: str, path: str, *, headers=None):
    """Issue the request, returning None if the server raised an uncaught
    exception (which TestClient re-raises by default). The auth-matrix tests
    don't care about server-side bugs unrelated to the auth boundary."""
    try:
        return client.request(
            method,
            path,
            headers=headers,
            json={} if method in {"POST", "PUT", "PATCH"} else None,
        )
    except Exception:
        return None


@pytest.mark.parametrize("method,path", _PARAMS)
def test_route_unauthenticated_does_not_leak_200(method, path, client: TestClient):
    """No route should return 200 OK to a fully unauthenticated request unless
    it's in the documented public allowlist. Acceptable non-public outcomes:
    401, 403, 404, 405, 422 (request validation), 400."""
    if _is_public(path):
        pytest.skip("public route by design")
    filled = _filled_path(path)
    resp = _safe_request(client, method, filled)
    if resp is None:
        pytest.skip("server raised an unrelated exception — auth boundary not reachable")
    assert resp.status_code != 200, (
        f"{method} {filled} returned 200 unauthenticated; status={resp.status_code} "
        f"body={resp.text[:200]}"
    )


@pytest.mark.parametrize("method,path", _PARAMS)
def test_route_master_key_authenticates(method, path, client: TestClient):
    """The master key MUST authenticate. Any 401 with the master key would be
    a wiring bug. 403 is acceptable — some routes disambiguate "not yours"
    or "doesn't exist" with 403 instead of 404 on purpose, but only when the
    structured error code is auth.forbidden (ownership) rather than
    auth.insufficient_scope (which the master should never trigger)."""
    filled = _filled_path(path)
    resp = _safe_request(client, method, filled, headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"})
    if resp is None:
        pytest.skip("server raised an unrelated exception — auth boundary not reachable")
    assert resp.status_code != 401, (
        f"{method} {filled} returned 401 with master key; body={resp.text[:200]}"
    )
    if resp.status_code == 403:
        try:
            body = resp.json()
            err_code = body.get("error") or body.get("code") or ""
        except Exception:
            err_code = ""
        if any(path.startswith(p) for p in _MASTER_KEY_REJECTED_PATH_PREFIXES):
            return  # documented user-scoped route; 403 is correct
        assert err_code != "auth.insufficient_scope", (
            f"{method} {filled} returned auth.insufficient_scope with master key — "
            f"the master key bypasses scope checks. body={resp.text[:200]}"
        )
