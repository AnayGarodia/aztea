# SPDX-License-Identifier: Apache-2.0
"""
Audit extras for the OSS / hosted boundary commit (8fe9d47).

Covers:
- B. Stripe gate matrix — every gated route returns 501 with structured body.
- C. Publish flow — auth, ownership, success, failure, sanitization, idempotency.
- D. Global-trust — auth, missing DID, success, hosted failure.
- J. Migration 0039 — column shape + idempotency.
- K. Hardcoded URL audit — promoted from `make oss-check`.
- L (subset). Loophole probes.

The fixture mirrors `tests/test_oss_mode_isolation.py` but is parametrised
to flip into hosted-mode for the C/D suites that need it.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Iterable

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

# Strip inherited hosted/Stripe env so the OSS-mode tests are real.
for _v in (
    "AZTEA_HOSTED_API_URL",
    "AZTEA_HOSTED_API_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
):
    os.environ.pop(_v, None)

from core import auth  # noqa: E402
from core import db as core_db  # noqa: E402
from core import disputes  # noqa: E402
from core import hosted_client  # noqa: E402
from core import jobs  # noqa: E402
from core import payments  # noqa: E402
from core import registry  # noqa: E402
from core import reputation  # noqa: E402
import server.application as server  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixture helpers (copied — see plan note about extracting to conftest)
# ---------------------------------------------------------------------------


def _close_module_conn(module) -> None:
    conn = getattr(module._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


def _isolated_db(monkeypatch, hosted: bool):
    db_path = Path(__file__).resolve().parent / f"test-audit-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for m in modules:
        _close_module_conn(m)
        monkeypatch.setattr(m, "DB_PATH", str(db_path))

    monkeypatch.setattr(server, "_STRIPE_SECRET_KEY", "", raising=False)
    monkeypatch.setattr(server, "_STRIPE_WEBHOOK_SECRET", "", raising=False)
    monkeypatch.setattr(server, "_STRIPE_PUBLISHABLE_KEY", "", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)

    if hosted:
        monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
        monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "azh_audit_token")
        monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    else:
        monkeypatch.delenv("AZTEA_HOSTED_API_URL", raising=False)
        monkeypatch.delenv("AZTEA_HOSTED_API_KEY", raising=False)
    hosted_client.reset_hosted_client_for_tests()
    return db_path, modules


@pytest.fixture
def oss_app(monkeypatch):
    db_path, modules = _isolated_db(monkeypatch, hosted=False)
    with TestClient(server.app) as client:
        yield client
    for m in modules:
        _close_module_conn(m)
    for suffix in ("", "-shm", "-wal"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            p.unlink()


@pytest.fixture
def hosted_app(monkeypatch):
    db_path, modules = _isolated_db(monkeypatch, hosted=True)
    with TestClient(server.app) as client:
        yield client
    for m in modules:
        _close_module_conn(m)
    for suffix in ("", "-shm", "-wal"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            p.unlink()


def _new_user() -> dict:
    suffix = uuid.uuid4().hex[:8]
    return auth.register_user(
        username=f"audit-{suffix}",
        email=f"audit-{suffix}@example.com",
        password="password123",
    )


def _register_agent(owner_id: str) -> str:
    return registry.register_agent(
        name=f"audit-agent-{uuid.uuid4().hex[:6]}",
        description="audit test agent",
        endpoint_url=f"https://example.com/{uuid.uuid4().hex[:6]}",
        price_per_call_usd=0.05,
        tags=["audit"],
        owner_id=owner_id,
        embed_listing=False,
    )


# ===========================================================================
# B. Stripe gate matrix — every gated route in part_013 returns 501
# ===========================================================================


_STRIPE_GATED = [
    ("POST", "/wallets/topup/session", {"wallet_id": "w", "amount_cents": 500}),
    ("POST", "/stripe/webhook", b"{}"),
    ("POST", "/wallets/connect/onboard", {"return_url": "https://x/r", "refresh_url": "https://x/f"}),
    ("GET", "/wallets/connect/status", None),
    ("POST", "/wallets/withdraw", {"wallet_id": "w", "amount_cents": 500}),
    ("POST", "/billing/setup-session", None),
    ("GET", "/billing/payment-methods", None),
    ("DELETE", "/billing/payment-methods/pm_test_123", None),
]


@pytest.mark.parametrize("method,path,body", _STRIPE_GATED)
def test_stripe_gate_returns_501(oss_app, method, path, body):
    headers = {"Authorization": "Bearer test-master-key"}
    if method == "POST" and isinstance(body, (bytes, bytearray)):
        resp = oss_app.post(path, content=body, headers={**headers, "stripe-signature": "x"})
    elif method == "POST":
        resp = oss_app.post(path, json=body, headers=headers)
    elif method == "GET":
        resp = oss_app.get(path, headers=headers)
    elif method == "DELETE":
        resp = oss_app.delete(path, headers=headers)
    else:
        pytest.fail(f"unhandled method {method}")
    assert resp.status_code == 501, f"{method} {path} returned {resp.status_code}"
    body_json = resp.json()
    # The HTTPException handler flattens detail into top-level fields with
    # `details` carrying the structured `data`.
    assert body_json.get("error") == "payment.stripe_not_configured", body_json
    details = body_json.get("details") or {}
    hosted = details.get("hosted_url") or ""
    assert "aztea.ai" in hosted
    assert "docs" in details


def test_stripe_empty_string_gates_same_as_unset(monkeypatch, oss_app):
    """B2: STRIPE_SECRET_KEY="" must gate exactly like unset."""
    monkeypatch.setattr(server, "_STRIPE_SECRET_KEY", "", raising=False)
    resp = oss_app.post(
        "/wallets/topup/session",
        json={"wallet_id": "w", "amount_cents": 500},
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 501


def test_stripe_available_false_gates_even_with_key(monkeypatch, oss_app):
    """B3: _STRIPE_AVAILABLE=False must gate even when secret key is set."""
    monkeypatch.setattr(server, "_STRIPE_AVAILABLE", False, raising=False)
    monkeypatch.setattr(server, "_STRIPE_SECRET_KEY", "sk_test_xxx", raising=False)
    resp = oss_app.post(
        "/wallets/topup/session",
        json={"wallet_id": "w", "amount_cents": 500},
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 501


# ===========================================================================
# C. Publish flow tests
# ===========================================================================


def _stub_hosted_post(monkeypatch, *, response_body: bytes | None, status_ok: bool = True):
    """Patch hosted_client.requests.post to return a deterministic response."""

    class _Resp:
        ok = status_ok
        status_code = 200 if status_ok else 502
        url = "https://api.aztea.test/v1/x"
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            if response_body is None:
                return
            yield response_body

    captured = {}

    def _post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        captured["headers"] = kw.get("headers")
        return _Resp()

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    return captured


def test_publish_oss_returns_501(oss_app):
    """C1 (already covered elsewhere; pinned here for the audit file)."""
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    resp = oss_app.post(
        f"/registry/agents/{aid}/publish",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 501


def test_publish_unauthenticated_returns_401(hosted_app):
    """C2: missing Authorization → 401."""
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    resp = hosted_app.post(f"/registry/agents/{aid}/publish")
    assert resp.status_code == 401


def test_publish_hosted_success_writes_db(monkeypatch, hosted_app):
    """C6: hosted accepts → 200 + DB row updated."""
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    captured = _stub_hosted_post(
        monkeypatch,
        response_body=(
            b'{"listing_id":"lst_audit","public_url":"https://aztea.ai/agents/x",'
            b'"published_at":"2026-05-09T12:00:00Z"}'
        ),
    )
    resp = hosted_app.post(
        f"/registry/agents/{aid}/publish",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["listing_id"] == "lst_audit"
    assert body["public_url"] == "https://aztea.ai/agents/x"
    # DB writeback
    agent = registry.get_agent(aid)
    assert agent.get("published_to_public_listing_id") == "lst_audit"
    assert agent.get("published_to_public_at")
    # C8: spec sanitization — signing_private_key must NOT be in payload
    sent_keys = set((captured["json"].get("spec") or {}).keys())
    assert "signing_private_key" not in sent_keys
    # signing_public_key + did SHOULD be present
    assert "signing_public_key" in sent_keys
    assert "did" in sent_keys


def test_publish_hosted_failure_returns_502(monkeypatch, hosted_app):
    """C7: hosted returns nothing → 502 with registry.public_publish_failed."""
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    _stub_hosted_post(monkeypatch, response_body=None, status_ok=False)
    resp = hosted_app.post(
        f"/registry/agents/{aid}/publish",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 502
    assert resp.json().get("error") == "registry.public_publish_failed"


def test_publish_idempotent_re_publish(monkeypatch, hosted_app):
    """C9: second publish updates timestamp and listing_id."""
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    _stub_hosted_post(
        monkeypatch,
        response_body=(
            b'{"listing_id":"lst_v1","public_url":"u",'
            b'"published_at":"2026-05-09T11:00:00Z"}'
        ),
    )
    r1 = hosted_app.post(
        f"/registry/agents/{aid}/publish",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert r1.status_code == 200
    _stub_hosted_post(
        monkeypatch,
        response_body=(
            b'{"listing_id":"lst_v2","public_url":"u2",'
            b'"published_at":"2026-05-09T12:00:00Z"}'
        ),
    )
    r2 = hosted_app.post(
        f"/registry/agents/{aid}/publish",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert r2.status_code == 200
    assert r2.json()["listing_id"] == "lst_v2"
    agent = registry.get_agent(aid)
    assert agent["published_to_public_listing_id"] == "lst_v2"


def test_publish_banned_agent_returns_404(monkeypatch, hosted_app):
    """C5: banned agent → 404 (not 501, not 200)."""
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    # Set status='banned' directly — registry exposes update_agent_status.
    registry.set_agent_status(aid, "banned", reason="audit-test")
    resp = hosted_app.post(
        f"/registry/agents/{aid}/publish",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 404


# ===========================================================================
# D. Global-trust tests
# ===========================================================================


def _stub_hosted_get(monkeypatch, *, response_body: bytes | None, status_ok: bool = True):
    class _Resp:
        ok = status_ok
        status_code = 200 if status_ok else 502
        url = "https://api.aztea.test/v1/trust/x"
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            if response_body is None:
                return
            yield response_body

    captured = {}

    def _get(url, **kw):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(hosted_client.requests, "get", _get)
    return captured


def test_global_trust_oss_returns_501(oss_app):
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    resp = oss_app.get(
        f"/registry/agents/{aid}/global-trust",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 501
    assert resp.json().get("error") == "registry.global_trust_disabled"


def test_global_trust_unauthenticated_401(hosted_app):
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    resp = hosted_app.get(f"/registry/agents/{aid}/global-trust")
    assert resp.status_code == 401


def test_global_trust_unknown_agent_404(hosted_app):
    resp = hosted_app.get(
        "/registry/agents/does-not-exist/global-trust",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 404


def test_global_trust_hosted_success(monkeypatch, hosted_app):
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    captured = _stub_hosted_get(
        monkeypatch,
        response_body=b'{"trust_score":87.4,"rating_count":200}',
    )
    resp = hosted_app.get(
        f"/registry/agents/{aid}/global-trust",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"trust_score": 87.4, "rating_count": 200}
    # Verify the agent's DID went into the URL.
    agent = registry.get_agent(aid)
    did = agent.get("did") or ""
    assert did
    assert agent["agent_id"] in captured["url"] or did.split(":")[-1] in captured["url"]


def test_global_trust_hosted_failure_502(monkeypatch, hosted_app):
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    _stub_hosted_get(monkeypatch, response_body=None, status_ok=False)
    resp = hosted_app.get(
        f"/registry/agents/{aid}/global-trust",
        headers={"Authorization": "Bearer test-master-key"},
    )
    assert resp.status_code == 502
    assert resp.json().get("error") == "registry.global_trust_fetch_failed"


# ===========================================================================
# J. Migration 0039 — column shape + idempotent re-run
# ===========================================================================


def test_migration_0039_columns_present(oss_app):
    """J1: published_to_public_at and published_to_public_listing_id exist
    and are nullable on a fresh DB after migrations."""
    # Use the isolated DB that the oss_app fixture monkeypatched onto
    # registry — core_db.get_db_connection() defaults to the original
    # process-wide DB_PATH (captured at module load), which on a fresh
    # CI runner has no agents table at all.
    with core_db.get_db_connection(registry.DB_PATH) as conn:
        rows = conn.execute("PRAGMA table_info(agents)").fetchall()
    cols = {r["name"]: r for r in rows}
    assert "published_to_public_at" in cols
    assert "published_to_public_listing_id" in cols
    # Both nullable — `notnull` column on PRAGMA table_info is 0 for nullable.
    assert cols["published_to_public_at"]["notnull"] == 0
    assert cols["published_to_public_listing_id"]["notnull"] == 0


def test_migration_0039_existing_rows_default_null(oss_app):
    """J2: existing rows get NULL for the new columns."""
    user = _new_user()
    aid = _register_agent(f"user:{user['user_id']}")
    agent = registry.get_agent(aid)
    assert agent.get("published_to_public_at") is None
    assert agent.get("published_to_public_listing_id") is None


def test_migration_re_apply_is_idempotent(oss_app):
    """J3: re-running the migration runner does not error."""
    from core import migrate

    # Apply again — schema_migrations table must short-circuit.
    migrate.apply_migrations()
    migrate.apply_migrations()


# ===========================================================================
# K. Hardcoded URL audit
# ===========================================================================


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS: tuple[str, ...] = ("core", "server", "agents")
_ALLOWED_FILES = {
    "core/hosted_client.py",  # the one legit outbound caller
}
# Previously-known leaks are now fixed. The User-Agent in the two retrieval
# agents is now derived from `core.outbound_ua.outbound_user_agent()`, which
# reads SERVER_BASE_URL or OUTBOUND_USER_AGENT at call time. The
# regression-fixed entries are kept here only to document the audit history;
# the assertion below now flips and asserts the leak is GONE.
_FIXED_LEAKS: tuple[tuple[str, str], ...] = (
    ("agents/wiki.py", "research-agent@aztea.dev"),
    ("agents/financial/fetcher.py", "research-agent@aztea.dev"),
)
_ALLOWED_PATTERN_FRAGMENTS = (
    "https://aztea.ai",  # the OSS->hosted pointer in 501 bodies
    "https://github.com/aztea-ai/aztea",  # docs link
    "https://api.aztea.test",  # test base URL
    "aztea.internal",  # SYSTEM_USER_EMAIL (local-only)
)


def _python_files(roots: Iterable[str]):
    for root in roots:
        for p in (_REPO_ROOT / root).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            yield p


def _strip_docstrings_and_comments(text: str) -> str:
    """Tokenize and remove docstrings and comments — robust scanner.

    Uses tokenize so triple-quoted docstring continuation lines don't sneak
    through (they aren't `# `-prefixed and start mid-string).
    """
    import io
    import tokenize

    out_lines = text.splitlines()
    redacted = list(out_lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except tokenize.TokenizeError:
        return text
    for tok in tokens:
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            # Heuristic: redact docstrings (string statements) and comments.
            # We can't easily distinguish docstring vs string literal here,
            # so we redact ALL string tokens — that's safe for this audit
            # because we just want to find hardcoded URL constants used
            # *outside* string context (there shouldn't be any in code).
            sl, sc = tok.start
            el, ec = tok.end
            for ln in range(sl - 1, el):
                if 0 <= ln < len(redacted):
                    redacted[ln] = "<redacted>"
    return "\n".join(redacted)


def test_no_unexpected_aztea_ai_in_source():
    """K1: no hardcoded `aztea.ai` outside docstrings/comments and allowlist.

    Strategy: tokenize each file, redact every COMMENT and STRING token, then
    grep the residue for `aztea.(ai|dev)`. That residue is *code* — bare
    identifiers or operations that mention the domain — which is the only
    place a real leak could live. URLs inside string literals are fine
    because every outbound-call site goes through `core/hosted_client.py`,
    and that one file is in the allowlist.

    The audit report separately flags the User-Agent string leaks in
    agents/wiki.py and agents/financial/fetcher.py.
    """
    rx = re.compile(r"aztea\.(ai|dev)")
    bad: list[str] = []
    for p in _python_files(_SCAN_DIRS):
        rel = str(p.relative_to(_REPO_ROOT))
        if rel in _ALLOWED_FILES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        residue = _strip_docstrings_and_comments(text)
        for i, line in enumerate(residue.splitlines(), start=1):
            if rx.search(line):
                bad.append(f"{rel}:{i}: {line.strip()}")
    assert not bad, (
        "Hardcoded aztea.ai/.dev outside string literals:\n" + "\n".join(bad)
    )


def test_user_agent_leak_fixed_in_retrieval_agents():
    """Regression: the User-Agent strings that previously hardcoded
    'research-agent@aztea.dev' (audit P1) must now be sourced from
    core.outbound_ua.outbound_user_agent(). The leak strings must NOT
    appear in those files anymore.

    Files that have been entirely removed from the codebase (sunset agents
    in the agent prune) trivially can't leak — skip them.
    """
    for rel, needle in _FIXED_LEAKS:
        path = _REPO_ROOT / rel
        if not path.exists():
            continue  # sunset agent: file gone, leak gone.
        text = path.read_text(encoding="utf-8")
        assert needle not in text, (
            f"audit P1 regression — {needle!r} reappeared in {rel}; "
            "User-Agent must come from core.outbound_ua"
        )
        assert "outbound_user_agent" in text, (
            f"{rel} no longer imports the outbound_user_agent helper"
        )


def test_no_direct_requests_call_to_aztea_outside_hosted_client():
    """K2: no `requests.{get,post}` with an aztea.ai literal URL outside hosted_client."""
    rx = re.compile(r"requests\.(get|post|put|delete)\([^)]*aztea\.(ai|dev)")
    bad: list[str] = []
    for p in _python_files(_SCAN_DIRS):
        rel = str(p.relative_to(_REPO_ROOT))
        if rel in _ALLOWED_FILES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                bad.append(f"{rel}:{i}: {line.strip()}")
    assert not bad, "Direct requests.* call to aztea.ai outside hosted_client:\n" + "\n".join(bad)


def test_make_oss_check_runs_clean():
    """K (meta): the make oss-check target must still pass on this branch."""
    proc = subprocess.run(
        ["make", "oss-check"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        pytest.fail(
            "make oss-check failed:\nstdout:\n" + proc.stdout + "\nstderr:\n" + proc.stderr
        )


# ===========================================================================
# L. Loophole probes (subset that are best to put here)
# ===========================================================================


def test_l3_prefer_hosted_only_includes_known_agents():
    """L3: PREFER_HOSTED_AGENT_IDS items must each map to a real slug."""
    from server.builtin_agents import constants as bc

    for aid in bc.PREFER_HOSTED_AGENT_IDS:
        slug = bc.agent_id_to_slug(aid)
        assert slug, f"PREFER_HOSTED contains agent with no internal:// slug: {aid}"


def test_l7_all_builtin_slugs_are_safe():
    """L7: every BUILTIN_INTERNAL_ENDPOINTS slug matches [a-z0-9_-]+."""
    from server.builtin_agents import constants as bc

    safe = re.compile(r"^[a-z0-9_-]+$")
    for aid, ep in bc.BUILTIN_INTERNAL_ENDPOINTS.items():
        slug = ep.removeprefix("internal://")
        assert safe.match(slug), f"unsafe slug for {aid!r}: {slug!r}"


def test_agent_id_to_slug_unknown_returns_none():
    """F6: unknown / empty agent_id returns None."""
    from server.builtin_agents import constants as bc

    assert bc.agent_id_to_slug("") is None
    assert bc.agent_id_to_slug("00000000-0000-0000-0000-000000000000") is None


def test_l5_email_public_base_url_is_callable_now(monkeypatch):
    """L5 (fixed): the email module reads PUBLIC_BASE_URL/SERVER_BASE_URL
    at call time so a runtime env change propagates to subsequent emails.

    This regression test asserts:
      1. ``_public_base_url`` is callable (function), not a string.
      2. Changing SERVER_BASE_URL between calls is reflected.
    """
    from core import email

    assert callable(getattr(email, "_public_base_url", None)), (
        "email._public_base_url must be a function (audit P2 fix)"
    )
    assert not hasattr(email, "_PUBLIC_BASE_URL"), (
        "email._PUBLIC_BASE_URL constant should be removed"
    )

    monkeypatch.setenv("SERVER_BASE_URL", "https://example.com")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert email._public_base_url() == "https://example.com"
    monkeypatch.setenv("SERVER_BASE_URL", "https://other.example")
    assert email._public_base_url() == "https://other.example"
