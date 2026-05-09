"""L7 — authorization matrix for listing routes.

Confirms each publish/mutation route enforces the right caller scope and
ownership boundary. Matrix: caller type × route × expected response.
"""
from __future__ import annotations

import pytest

from core import auth

from tests.integration.support import *  # noqa: F401,F403
from tests.integration.support import (
    TEST_MASTER_KEY,
    _auth_headers,
    _register_agent_via_api,
    _register_user,
)


_CLEAN_SKILL_MD = (
    "---\nname: auth-matrix-skill\ndescription: clean skill for auth matrix\n---\n"
    "# auth-matrix-skill\nDoes nothing controversial.\n"
)


def _register_caller_only_key(user_id: str) -> str:
    """Create a user key with only the `caller` scope — no `worker`."""
    info = auth.create_api_key(user_id, name="caller-only", scopes=["caller"])
    return info["raw_key"]


# ---------------------------------------------------------------------------
# /skills
# ---------------------------------------------------------------------------


def test_skills_master_succeeds(client):
    resp = client.post(
        "/skills",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"skill_md": _CLEAN_SKILL_MD, "price_per_call_usd": 0.02},
    )
    assert resp.status_code == 201, resp.text


def test_skills_default_user_key_succeeds_with_approval(client):
    # Default user keys carry both caller + worker scopes; SKILL.md auto-
    # approves regardless of caller type.
    user = _register_user()
    resp = client.post(
        "/skills",
        headers=_auth_headers(user["raw_api_key"]),
        json={"skill_md": _CLEAN_SKILL_MD, "price_per_call_usd": 0.02},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["review_status"] == "approved"


def test_skills_caller_only_key_rejected_with_403(client):
    user = _register_user()
    caller_only = _register_caller_only_key(user["user_id"])
    resp = client.post(
        "/skills",
        headers=_auth_headers(caller_only),
        json={"skill_md": _CLEAN_SKILL_MD, "price_per_call_usd": 0.02},
    )
    assert resp.status_code == 403, resp.text


def test_skills_unauthenticated_rejected_with_401(client):
    resp = client.post(
        "/skills",
        json={"skill_md": _CLEAN_SKILL_MD, "price_per_call_usd": 0.02},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# /registry/register
# ---------------------------------------------------------------------------


def _register_payload(name: str) -> dict:
    return {
        "name": name,
        "description": "auth-matrix legitimate test agent",
        "endpoint_url": f"https://my.host.example.com/{name}",
        "price_per_call_usd": 0.05,
        "tags": ["t"],
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "title": "Task",
                    "description": "input",
                }
            },
        },
    }


def test_register_master_succeeds_with_approved_status(client):
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(TEST_MASTER_KEY),
        json=_register_payload("auth-matrix-master"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # master should not land in probation
    assert (body.get("agent") or {}).get("review_status") != "probation"


def test_register_default_user_key_lands_in_probation(client):
    user = _register_user()
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(user["raw_api_key"]),
        json=_register_payload("auth-matrix-user"),
    )
    assert resp.status_code == 201, resp.text
    assert (resp.json().get("agent") or {}).get("review_status") == "probation"


def test_register_caller_only_key_rejected_with_403(client):
    user = _register_user()
    caller_only = _register_caller_only_key(user["user_id"])
    resp = client.post(
        "/registry/register",
        headers=_auth_headers(caller_only),
        json=_register_payload("auth-matrix-caller"),
    )
    assert resp.status_code == 403, resp.text


def test_register_unauthenticated_rejected_with_401(client):
    resp = client.post(
        "/registry/register",
        json=_register_payload("auth-matrix-anon"),
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# PATCH /registry/agents/{id} — ownership boundary
# ---------------------------------------------------------------------------


def test_patch_owner_can_update_own_agent(client):
    user = _register_user()
    api_key = user["raw_api_key"]
    agent_id = _register_agent_via_api(
        client, api_key, name="auth-patch-owner-test", auto_approve=False
    )
    resp = client.patch(
        f"/registry/agents/{agent_id}",
        headers=_auth_headers(api_key),
        json={"description": "an updated and entirely benign description"},
    )
    assert resp.status_code == 200, resp.text


def test_patch_other_owner_returns_404(client):
    owner = _register_user()
    intruder = _register_user()
    agent_id = _register_agent_via_api(
        client, owner["raw_api_key"], name="auth-patch-cross-owner", auto_approve=False
    )
    resp = client.patch(
        f"/registry/agents/{agent_id}",
        headers=_auth_headers(intruder["raw_api_key"]),
        json={"description": "I am not the owner of this listing"},
    )
    # update_agent returns None when ownership doesn't match → 404
    assert resp.status_code == 404, resp.text


def test_patch_unauthenticated_rejected_with_401(client):
    user = _register_user()
    agent_id = _register_agent_via_api(
        client, user["raw_api_key"], name="auth-patch-anon-test", auto_approve=False
    )
    resp = client.patch(
        f"/registry/agents/{agent_id}",
        json={"description": "anonymous update attempt"},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# /onboarding/ingest scope check
# ---------------------------------------------------------------------------


def test_onboarding_ingest_caller_only_key_rejected_with_403(client):
    user = _register_user()
    caller_only = _register_caller_only_key(user["user_id"])
    resp = client.post(
        "/onboarding/ingest",
        headers=_auth_headers(caller_only),
        json={"manifest_content": "irrelevant; scope check should fire first"},
    )
    assert resp.status_code == 403, resp.text


def test_onboarding_ingest_unauthenticated_rejected_with_401(client):
    resp = client.post(
        "/onboarding/ingest",
        json={"manifest_content": "x"},
    )
    assert resp.status_code == 401, resp.text
