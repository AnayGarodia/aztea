"""Integration tests for the publish-path judge invocation.

2026-05-27: the LLM judge was moved out of ``scan_skill_md`` and into the
``/skills`` POST handler so anonymous probe traffic on
``/api/playground/test`` doesn't burn LLM credits. These tests pin the
new contract:

  1. POST /skills runs the judge after the static scan.
  2. A BLOCK verdict from the judge refuses the upload with the
     structured ``listing.safety_block`` envelope.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from tests.integration.support import *  # noqa: F403


_SKILL_MD_OK = (
    "---\nname: judge-test\ndescription: An OK helpful tool.\n---\n\n"
    "# Judge test\n\nSummarises text into a single sentence.\n"
)


def _patch_judge_llm(monkeypatch, *, verdict: str, confidence: float = 0.9):
    """Replace the LLM provider so we can control the judge verdict.
    Returns a callable that returns the in-process call counter."""
    import core.listing_safety_judge as judge
    judge._run_judge_cached.cache_clear()
    counter = {"n": 0}

    def _fake(req):
        counter["n"] += 1
        return SimpleNamespace(
            text=json.dumps({
                "verdict": verdict,
                "reasoning": f"{verdict} verdict for test",
                "confidence": confidence,
            }),
            provider="fake",
            model="fake-1",
        )

    monkeypatch.setattr(judge, "run_with_fallback", _fake)
    return counter


def test_skills_publish_invokes_judge_after_static_scan(client, monkeypatch):
    """A SKILL.md body that passes the static scanner must trigger
    exactly one judge call from the POST /skills handler."""
    monkeypatch.setenv("AZTEA_LISTING_JUDGE", "on")
    counter = _patch_judge_llm(monkeypatch, verdict="allow")
    resp = client.post(
        "/skills",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
        json={"skill_md": _SKILL_MD_OK, "price_per_call_usd": 0.05},
    )
    assert resp.status_code == 201, resp.text
    assert counter["n"] == 1, (
        f"Expected exactly 1 judge call on /skills publish, got {counter['n']}"
    )


def test_skills_publish_judge_block_refuses_upload(client, monkeypatch):
    """A judge BLOCK verdict at the publish path refuses the upload with
    the structured listing.safety_block envelope, even though the static
    scanner found nothing wrong."""
    monkeypatch.setenv("AZTEA_LISTING_JUDGE", "on")
    _patch_judge_llm(monkeypatch, verdict="block", confidence=0.92)
    resp = client.post(
        "/skills",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
        json={"skill_md": _SKILL_MD_OK, "price_per_call_usd": 0.05},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body.get("error") == "listing.safety_block", body


def test_skills_publish_skips_judge_when_static_blocks(client, monkeypatch):
    """When the static scanner already produced a BLOCK, the publish path
    MUST NOT call the LLM judge. Saves tokens on payloads we've already
    refused for free."""
    monkeypatch.setenv("AZTEA_LISTING_JUDGE", "on")
    counter = _patch_judge_llm(monkeypatch, verdict="allow")
    # Embedded API key triggers the static scanner immediately.
    malicious = (
        "---\nname: leak\ndescription: leaky tool.\n---\n\n"
        "My API key is sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.\n"
    )
    resp = client.post(
        "/skills",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
        json={"skill_md": malicious, "price_per_call_usd": 0.05},
    )
    assert resp.status_code == 400, resp.text
    assert counter["n"] == 0, (
        "Judge was called even though the static scanner already produced "
        "a BLOCK — this regresses the cost-amplification fix."
    )
