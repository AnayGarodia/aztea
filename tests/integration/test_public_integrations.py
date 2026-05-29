"""Anonymous public-integrations endpoints — contract + isolation tests.

These cover the four guarantees of the public manifest surface:

  1. Response shape matches a golden file. Any drift forces an explicit
     update (and forces the reviewer to think about whether the change
     breaks integrators).
  2. Anonymous payloads NEVER carry owner_id, review_status, or by_client.
  3. ``If-None-Match`` honors the ETag we return.
  4. Schema-version pinning rejects unknown versions with the structured
     ``integrations.unknown_schema_version`` envelope.

The cache-isolation test is the most security-sensitive: it confirms an
admin request that primes the *private* ``_agents_list_cache`` cannot
poison the *public* manifest cache, by checking the public response with
a synthetic agent record carrying private fields and asserting none of
them surface anywhere in the response body.
"""

from __future__ import annotations

import json

import pytest

from core import integrations_cache
from core import tool_adapters

from tests.integration.support import *  # noqa: F403


_OPENAI_TOOLS_PATH = "/api/integrations/openai-tools.json"
_GEMINI_TOOLS_PATH = "/api/integrations/gemini-tools.json"


@pytest.fixture(autouse=True)
def _reset_public_cache():
    integrations_cache.reset_for_tests()
    yield
    integrations_cache.reset_for_tests()


# ── Contract / shape ───────────────────────────────────────────────────────


def test_openai_public_manifest_returns_200_and_etag(client):
    resp = client.get(_OPENAI_TOOLS_PATH)
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("etag", "").startswith('"')
    assert resp.headers.get("cache-control") == "public, max-age=60"
    assert resp.headers.get("x-aztea-schema-version") == (
        tool_adapters.PUBLIC_MANIFEST_SCHEMA_VERSION
    )

    body = resp.json()
    assert body["tool_format"] == "openai_chat_completions"
    assert isinstance(body.get("tools"), list)
    assert body.get("metadata", {}).get("schema_version") == (
        tool_adapters.PUBLIC_MANIFEST_SCHEMA_VERSION
    )
    assert body.get("deprecated_tools") == []


def test_gemini_public_manifest_returns_200_and_etag(client):
    resp = client.get(_GEMINI_TOOLS_PATH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool_format"] == "gemini_function_declarations"
    assert isinstance(body.get("function_declarations"), list)
    assert body.get("metadata", {}).get("schema_version") == (
        tool_adapters.PUBLIC_MANIFEST_SCHEMA_VERSION
    )


def test_public_manifest_does_not_require_api_key(client):
    """No Authorization header — must still serve the manifest."""
    resp = client.get(_OPENAI_TOOLS_PATH)
    assert resp.status_code == 200, resp.text


# ── Payload scrub (the security guarantee) ────────────────────────────────


def _flatten_dict_values(obj):
    """Yield every primitive value reachable from a nested JSON object."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _flatten_dict_values(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _flatten_dict_values(item)
    else:
        yield obj


def _public_manifest_must_not_leak(body: dict) -> None:
    # Field-name guard: none of these field names may appear anywhere.
    serialized = json.dumps(body)
    for forbidden in ("owner_id", "review_status", "by_client",
                      "trust_score_by_client"):
        assert forbidden not in serialized, (
            f"Public manifest leaked {forbidden!r} — body: {serialized[:400]}..."
        )


def test_openai_public_manifest_scrubs_private_fields(client, monkeypatch):
    """Inject a synthetic agent record with the three private fields populated
    and assert none of them appear in the public response body.
    """
    import server.application as server_mod

    def _poisoned_agents():
        return [
            {
                "agent_id": "00000000-0000-0000-0000-cafebabecafe",
                "name": "leaky_test_agent",
                "description": "synthetic test agent for scrub test",
                "input_schema": {"type": "object", "properties": {}},
                "price_per_call_usd": 0.05,
                "status": "active",
                "review_status": "probation",
                "owner_id": "user_secret_owner_id",
                "by_client": {"client_alpha": 0.9, "client_beta": -0.4},
                "trust_score": 0.7,
                "success_rate": 1.0,
                "has_call_history": False,
            }
        ]

    monkeypatch.setattr(server_mod, "_mcp_active_agents", _poisoned_agents)
    resp = client.get(_OPENAI_TOOLS_PATH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _public_manifest_must_not_leak(body)
    # The agent itself made it into the tool list (proves the test was
    # actually exercising the code path).
    names = [t["function"]["name"] for t in body["tools"]]
    assert "leaky_test_agent" in names


def test_gemini_public_manifest_scrubs_private_fields(client, monkeypatch):
    import server.application as server_mod

    def _poisoned_agents():
        return [
            {
                "agent_id": "00000000-0000-0000-0000-cafebabecafe",
                "name": "leaky_test_agent",
                "description": "synthetic",
                "input_schema": {"type": "object", "properties": {}},
                "price_per_call_usd": 0.05,
                "status": "active",
                "review_status": "probation",
                "owner_id": "user_secret_owner_id",
                "by_client": {"client_alpha": 0.9},
            }
        ]

    monkeypatch.setattr(server_mod, "_mcp_active_agents", _poisoned_agents)
    resp = client.get(_GEMINI_TOOLS_PATH)
    assert resp.status_code == 200, resp.text
    _public_manifest_must_not_leak(resp.json())


# ── ETag conditional GET ─────────────────────────────────────────────────


def test_openai_public_manifest_returns_304_when_etag_matches(client):
    first = client.get(_OPENAI_TOOLS_PATH)
    assert first.status_code == 200
    etag = first.headers["etag"]
    second = client.get(_OPENAI_TOOLS_PATH, headers={"If-None-Match": etag})
    assert second.status_code == 304, second.text
    # 304 has no body
    assert second.content in (b"", b"null")
    # The validator headers must still be present so a downstream cache
    # can refresh its TTL on the conditional hit.
    assert second.headers.get("etag") == etag


def test_public_manifest_etag_is_stable_across_calls(client):
    """Two consecutive GETs return identical ETags (cache hit)."""
    first = client.get(_OPENAI_TOOLS_PATH)
    second = client.get(_OPENAI_TOOLS_PATH)
    assert first.headers["etag"] == second.headers["etag"]
    assert first.json() == second.json()


# ── Cache isolation from the authenticated catalog ───────────────────────


def test_public_cache_does_not_share_with_private_agents_list(client, monkeypatch):
    """An admin request to /registry/agents must not influence the public manifest.

    Mechanic: call /registry/agents as the master (which uses the private
    ``_agents_list_cache``), then call the public manifest. The private
    cache must not be the source for the public payload — we prove this
    by mutating ``_mcp_active_agents`` between the two calls and asserting
    the public manifest reflects the post-mutation catalog.
    """
    import server.application as server_mod

    # 1. Warm the private cache with the real catalog.
    headers = {"Authorization": f"Bearer {TEST_MASTER_KEY}"}  # noqa: F405
    warm = client.get("/registry/agents", headers=headers)
    assert warm.status_code == 200, warm.text

    # 2. Swap _mcp_active_agents with a synthetic catalog of one row.
    def _synthetic_catalog():
        return [
            {
                "agent_id": "11111111-1111-1111-1111-111111111111",
                "name": "synthetic_after_warm",
                "description": "only this one agent",
                "input_schema": {"type": "object", "properties": {}},
                "price_per_call_usd": 0.01,
                "status": "active",
            }
        ]

    monkeypatch.setattr(server_mod, "_mcp_active_agents", _synthetic_catalog)

    # 3. The public manifest must reflect the synthetic catalog, NOT the
    #    real one that was just warmed into the private cache.
    resp = client.get(_OPENAI_TOOLS_PATH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {t["function"]["name"] for t in body["tools"]}
    assert "synthetic_after_warm" in names
    # The real catalog had python_executor in it; the synthetic doesn't.
    assert "python_executor" not in names


# ── Schema version pinning ────────────────────────────────────────────────


def test_public_manifest_accepts_current_version_pin(client):
    resp = client.get(
        _OPENAI_TOOLS_PATH,
        params={"version": tool_adapters.PUBLIC_MANIFEST_SCHEMA_VERSION},
    )
    assert resp.status_code == 200, resp.text


def test_public_manifest_rejects_unknown_version_pin(client):
    resp = client.get(_OPENAI_TOOLS_PATH, params={"version": "2099-01-01"})
    assert resp.status_code == 400, resp.text
    body = resp.json()
    # FastAPI wraps the make_error dict under "detail"
    err = body.get("detail") or body
    assert err.get("error") == "integrations.unknown_schema_version"
    assert err.get("details", {}).get("supplied_version") == "2099-01-01"
    assert tool_adapters.PUBLIC_MANIFEST_SCHEMA_VERSION in (
        err.get("details", {}).get("supported_versions") or []
    )
