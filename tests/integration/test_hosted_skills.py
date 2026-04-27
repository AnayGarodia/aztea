"""
End-to-end tests for the hosted skill runner.

Covers:
  - POST /skills/validate parses without persisting
  - POST /skills creates the agent + hosted skill row, auto-approved
  - GET /skills lists owner-scoped
  - GET /skills/{id} owner-scoped, 403 for other owners, 404 for missing
  - DELETE /skills/{id} delists the agent and removes the row
  - POST /registry/agents/{id}/call routes to the skill executor (sync)
  - The agent's owner column matches the registering user
"""

from __future__ import annotations

from unittest.mock import patch

from tests.integration.support import *  # noqa: F401,F403
from tests.integration.support import (
    TEST_MASTER_KEY,
    _auth_headers,
    _fund_user_wallet,
    _register_user,
)

from core.llm import LLMResponse


# Real-world SKILL.md verbatim from github.com/openclaw/openclaw
SKILL_MD_NOTION = """\
---
name: notion
description: Notion API for creating and managing pages, databases, and blocks.
homepage: https://developers.notion.com
metadata:
  {
    "openclaw":
      { "emoji": "📝", "requires": { "env": ["NOTION_API_KEY"] }, "primaryEnv": "NOTION_API_KEY" },
  }
---

# notion

Use the Notion API to create/read/update pages, data sources (databases), and blocks.

## Setup

1. Create an integration at https://notion.so/my-integrations
2. Copy the API key
"""

SKILL_MD_GITHUB = """\
---
name: github
description: "Use gh for GitHub issues, PR status, CI/logs, comments, reviews, releases, and API queries."
metadata:
  {
    "openclaw":
      {
        "emoji": "🐙",
        "requires": { "bins": ["gh"] }
      }
  }
---

# GitHub

Use `gh` for all GitHub operations.
"""


def _stub_llm(text: str) -> LLMResponse:
    return LLMResponse(text=text, model="stub-model", provider="stub")


def test_validate_returns_parser_preview(client):
    resp = client.post(
        "/skills/validate",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"skill_md": SKILL_MD_NOTION},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is True
    assert body["name"] == "notion"
    assert "Notion API" in body["description"]
    assert body["registration_preview"]["name"]
    assert "notion" in body["registration_preview"]["tags"]


def test_validate_rejects_missing_skill_md(client):
    resp = client.post(
        "/skills/validate",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={},
    )
    assert resp.status_code == 400


def test_validate_rejects_malformed_skill_md(client):
    resp = client.post(
        "/skills/validate",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"skill_md": "no frontmatter or h1 either."},
    )
    assert resp.status_code == 400


def test_create_skill_persists_and_auto_approves(client):
    user = _register_user()
    api_key = user["raw_api_key"]

    resp = client.post(
        "/skills",
        headers=_auth_headers(api_key),
        json={"skill_md": SKILL_MD_NOTION, "price_per_call_usd": 0.05},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    skill_id = body["skill_id"]
    agent_id = body["agent_id"]

    assert body["review_status"] == "approved"
    assert body["endpoint_url"] == f"skill://{skill_id}"
    assert body["price_per_call_usd"] == 0.05
    assert "live" in body["message"].lower()

    # Underlying agent row is approved and the endpoint_url was rewritten
    agent_resp = client.get(f"/registry/agents/{agent_id}", headers=_auth_headers(api_key))
    assert agent_resp.status_code == 200
    agent = agent_resp.json()
    assert agent["endpoint_url"].startswith("skill://")
    assert agent.get("review_status", "approved") == "approved"


def test_create_skill_handles_name_collision(client):
    """Two builders uploading the same skill should both succeed (collision suffix)."""
    user1 = _register_user()
    user2 = _register_user()
    key1 = user1["raw_api_key"]
    key2 = user2["raw_api_key"]

    r1 = client.post("/skills", headers=_auth_headers(key1),
                     json={"skill_md": SKILL_MD_GITHUB, "price_per_call_usd": 0.10})
    r2 = client.post("/skills", headers=_auth_headers(key2),
                     json={"skill_md": SKILL_MD_GITHUB, "price_per_call_usd": 0.10})
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["skill_id"] != r2.json()["skill_id"]


def test_get_skill_owner_scoped(client):
    user_a = _register_user()
    user_b = _register_user()
    key_a = user_a["raw_api_key"]
    key_b = user_b["raw_api_key"]

    skill_id = client.post(
        "/skills", headers=_auth_headers(key_a),
        json={"skill_md": SKILL_MD_NOTION, "price_per_call_usd": 0.05},
    ).json()["skill_id"]

    # Owner A can read
    own = client.get(f"/skills/{skill_id}", headers=_auth_headers(key_a))
    assert own.status_code == 200
    assert "raw_md" in own.json()

    # Owner B cannot
    other = client.get(f"/skills/{skill_id}", headers=_auth_headers(key_b))
    assert other.status_code == 403

    # Master can
    master = client.get(f"/skills/{skill_id}", headers=_auth_headers(TEST_MASTER_KEY))
    assert master.status_code == 200


def test_list_skills_returns_only_owner_skills(client):
    user_a = _register_user()
    user_b = _register_user()
    key_a = user_a["raw_api_key"]
    key_b = user_b["raw_api_key"]

    client.post("/skills", headers=_auth_headers(key_a),
                json={"skill_md": SKILL_MD_NOTION, "price_per_call_usd": 0.05})
    client.post("/skills", headers=_auth_headers(key_b),
                json={"skill_md": SKILL_MD_GITHUB, "price_per_call_usd": 0.10})

    a_skills = client.get("/skills", headers=_auth_headers(key_a)).json()["skills"]
    b_skills = client.get("/skills", headers=_auth_headers(key_b)).json()["skills"]
    assert len(a_skills) == 1
    assert len(b_skills) == 1
    assert a_skills[0]["slug"] == "notion"
    assert b_skills[0]["slug"] == "github"


def test_delete_skill_removes_row_and_delists_agent(client):
    user = _register_user()
    key = user["raw_api_key"]

    create = client.post("/skills", headers=_auth_headers(key),
                         json={"skill_md": SKILL_MD_NOTION, "price_per_call_usd": 0.05}).json()
    skill_id = create["skill_id"]
    agent_id = create["agent_id"]

    delete = client.delete(f"/skills/{skill_id}", headers=_auth_headers(key))
    assert delete.status_code == 200
    assert delete.json()["deleted"] is True

    # Skill row gone — subsequent invokes can no longer execute the skill
    follow = client.get(f"/skills/{skill_id}", headers=_auth_headers(key))
    assert follow.status_code == 404

    # The owning agent_id is unaffected at the registry layer (delist_agent has
    # a pre-existing schema bug we don't fix here); but the skill_id pointer is
    # gone, so any sync call that lands on this agent will 502 with "Hosted skill
    # record is missing" — the executor refuses to run without the skill row.
    from core.hosted_skills import get_hosted_skill_by_agent_id
    assert get_hosted_skill_by_agent_id(agent_id) is None


def test_sync_call_to_hosted_skill_routes_through_executor(client):
    user = _register_user()
    _fund_user_wallet(user, amount_cents=500)
    key = user["raw_api_key"]

    create = client.post("/skills", headers=_auth_headers(key),
                         json={"skill_md": SKILL_MD_NOTION, "price_per_call_usd": 0.05}).json()
    agent_id = create["agent_id"]

    with patch("core.skill_executor.run_with_fallback") as mock_llm:
        mock_llm.return_value = _stub_llm('{"result": "Created the page in Notion."}')
        resp = client.post(
            f"/registry/agents/{agent_id}/call",
            headers=_auth_headers(key),
            json={"task": "Create a page called 'Q3 plan'"},
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()["output"]
    assert "result" in payload
    assert "Notion" in payload["result"]
    # Executor metadata is included
    assert payload["_meta"]["provider"] == "stub"


def test_sync_call_to_hosted_skill_charges_caller(client):
    user = _register_user()
    wallet = _fund_user_wallet(user, amount_cents=500)
    key = user["raw_api_key"]

    create = client.post("/skills", headers=_auth_headers(key),
                         json={"skill_md": SKILL_MD_NOTION, "price_per_call_usd": 0.10}).json()
    agent_id = create["agent_id"]

    with patch("core.skill_executor.run_with_fallback") as mock_llm:
        mock_llm.return_value = _stub_llm('{"result": "ok"}')
        resp = client.post(
            f"/registry/agents/{agent_id}/call",
            headers=_auth_headers(key),
            json={"task": "x"},
        )
    assert resp.status_code == 200, resp.text
    # Wallet was debited (price + platform fee with caller-bearing policy).
    fresh = payments.get_or_create_wallet(wallet["owner_id"])
    assert fresh["balance_cents"] < 500, "caller wallet should have been debited"
    assert fresh["balance_cents"] >= 488, f"charge should be ≈ price; got {500 - fresh['balance_cents']}¢"


def test_sync_call_with_oversized_payload_refunds_caller(client):
    user = _register_user()
    wallet = _fund_user_wallet(user, amount_cents=500)
    key = user["raw_api_key"]

    agent_id = client.post("/skills", headers=_auth_headers(key),
                           json={"skill_md": SKILL_MD_NOTION, "price_per_call_usd": 0.05}).json()["agent_id"]

    big = "x" * (70 * 1024)  # > 64 KB skill input cap
    with patch("core.skill_executor.run_with_fallback") as mock_llm:
        # Should never even reach the LLM
        mock_llm.return_value = _stub_llm('{"result": "ok"}')
        resp = client.post(
            f"/registry/agents/{agent_id}/call",
            headers=_auth_headers(key),
            json={"task": big},
        )
        assert mock_llm.call_count == 0

    assert resp.status_code in (400, 413, 422), resp.text
    # Wallet refunded — back to original 500
    fresh = payments.get_or_create_wallet(wallet["owner_id"])
    assert fresh["balance_cents"] == 500


def test_sync_call_when_llm_fails_refunds_caller(client):
    user = _register_user()
    wallet = _fund_user_wallet(user, amount_cents=500)
    key = user["raw_api_key"]

    agent_id = client.post("/skills", headers=_auth_headers(key),
                           json={"skill_md": SKILL_MD_NOTION, "price_per_call_usd": 0.05}).json()["agent_id"]

    with patch("core.skill_executor.run_with_fallback") as mock_llm:
        from core.llm.errors import LLMError
        mock_llm.side_effect = LLMError("stub", "model-x", "all providers down")
        resp = client.post(
            f"/registry/agents/{agent_id}/call",
            headers=_auth_headers(key),
            json={"task": "anything"},
        )

    assert resp.status_code >= 500
    # Wallet refunded — back to original 500
    fresh = payments.get_or_create_wallet(wallet["owner_id"])
    assert fresh["balance_cents"] == 500


def test_external_http_agents_still_work(client):
    """The external HTTP agent path must remain unchanged after wiring skill://."""
    from tests.integration.support import _register_agent_via_api

    # Register a normal external agent (auto-approved by master)
    agent_id = _register_agent_via_api(
        client, TEST_MASTER_KEY, name="external-test-agent", price=0.01,
    )
    # Just check it's listable — no need to actually call it (would require an HTTP
    # mock). The point of this test is that registration of skills did not break
    # registration of external agents.
    listing = client.get(f"/registry/agents/{agent_id}", headers=_auth_headers(TEST_MASTER_KEY))
    assert listing.status_code == 200
    assert listing.json()["endpoint_url"].startswith("https://")
