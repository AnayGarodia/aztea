"""
Tests for self-improving hosted skills (migration 0077): the learnings store,
the owner-facing routes, the distiller, the execution-time injection (regression),
and the Level-1 trust_trend signal.

Run on the shared SQLite isolated_db; the same code paths run on Postgres in CI.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tests.integration.support import *  # noqa: F401,F403
from tests.integration.support import (
    TEST_MASTER_KEY,
    _auth_headers,
    _register_user,
)

from core import db as _db
from core import skill_learnings as sl
from core import trust_trend as tt
from core import skill_improvement as si
from core import reputation
from core import skill_executor
from core.llm import LLMResponse
from core.registry.core_schema import _resolved_db_path

SKILL_MD = """\
---
name: tidy-json
description: Reformat and validate JSON payloads.
---

# tidy-json

Return the input JSON, pretty-printed and validated.
"""


# ── helpers ──────────────────────────────────────────────────────────────────


def _create_skill(client) -> tuple[str, str]:
    """Create a master-owned hosted skill; return (skill_id, agent_id)."""
    resp = client.post(
        "/skills",
        headers=_auth_headers(TEST_MASTER_KEY),
        json={"skill_md": SKILL_MD, "price_per_call_usd": 0.02},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["skill_id"], body["agent_id"]


def _seed_proposals(skill_id: str, agent_id: str, owner_id: str, texts: list[str]) -> int:
    return sl.propose_learnings(
        skill_id, agent_id, owner_id,
        [sl.ProposedLearning(text=t, source_signal="example", confidence=0.8) for t in texts],
        max_pending=10,
    )


# ── core store: dedup, pending cap, ownership guard, caps, archive ───────────


def test_propose_dedup_and_pending_cap(client):
    skill_id, agent_id = _create_skill(client)
    row = sl.list_learnings(skill_id)  # empty
    assert row == []
    owner = "user:master"
    # dup collapses to 1
    n = sl.propose_learnings(
        skill_id, agent_id, owner,
        [sl.ProposedLearning("Validate input", "example"),
         sl.ProposedLearning("validate   INPUT", "example")],  # normalized dup
        max_pending=10,
    )
    assert n == 1
    # pending cap blocks further proposals
    n2 = sl.propose_learnings(
        skill_id, agent_id, owner,
        [sl.ProposedLearning("Another", "example")],
        max_pending=1,
    )
    assert n2 == 0


def test_set_status_owner_guard_and_block(client):
    skill_id, agent_id = _create_skill(client)
    _seed_proposals(skill_id, agent_id, "user:owner", ["Return JSON not prose"])
    learning = sl.list_learnings(skill_id, "proposed")[0]
    lid = learning["learning_id"]
    # wrong owner cannot mutate
    assert sl.set_learning_status(lid, "user:someone_else", sl.STATUS_ACTIVE) is False
    # correct owner activates → appears in the injected block
    assert sl.set_learning_status(lid, "user:owner", sl.STATUS_ACTIVE) is True
    block = sl.active_learnings_block(skill_id)
    assert block is not None and "Return JSON not prose" in block
    # archive-for-skill clears it
    assert sl.archive_learnings_for_skill(skill_id) == 1
    assert sl.active_learnings_block(skill_id) is None


def test_active_block_respects_char_cap(client):
    skill_id, agent_id = _create_skill(client)
    long_text = "x" * 500
    _seed_proposals(skill_id, agent_id, "user:owner", [long_text])
    lid = sl.list_learnings(skill_id, "proposed")[0]["learning_id"]
    sl.set_learning_status(lid, "user:owner", sl.STATUS_ACTIVE)
    block = sl.active_learnings_block(skill_id)
    # truncated well under the raw length
    assert block is not None and len(block) < 500


# ── owner routes: flag gating, list, accept/reject, 400/403/404 ─────────────


def test_routes_404_when_flag_off(client, monkeypatch):
    monkeypatch.delenv("AZTEA_SELF_IMPROVEMENT", raising=False)
    skill_id, _ = _create_skill(client)
    resp = client.get(
        f"/skills/{skill_id}/learnings", headers=_auth_headers(TEST_MASTER_KEY)
    )
    assert resp.status_code == 404


def test_routes_list_and_decide(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SELF_IMPROVEMENT", "1")
    skill_id, agent_id = _create_skill(client)
    # discover the owner id stored on the skill row
    owner_id = client.get(
        f"/skills/{skill_id}", headers=_auth_headers(TEST_MASTER_KEY)
    ).json()["owner_id"]
    _seed_proposals(skill_id, agent_id, owner_id, ["Validate schema", "Return strict JSON"])

    listed = client.get(
        f"/skills/{skill_id}/learnings", headers=_auth_headers(TEST_MASTER_KEY)
    )
    assert listed.status_code == 200
    items = listed.json()["learnings"]
    assert len(items) == 2

    # accept one, reject the other
    a, b = items[0]["learning_id"], items[1]["learning_id"]
    acc = client.post(
        f"/skills/{skill_id}/learnings/{a}/decision",
        headers=_auth_headers(TEST_MASTER_KEY), json={"decision": "accept"},
    )
    assert acc.status_code == 200 and acc.json()["status"] == "active"
    rej = client.post(
        f"/skills/{skill_id}/learnings/{b}/decision",
        headers=_auth_headers(TEST_MASTER_KEY), json={"decision": "reject"},
    )
    assert rej.status_code == 200 and rej.json()["status"] == "archived"
    # no proposed left
    assert client.get(
        f"/skills/{skill_id}/learnings", headers=_auth_headers(TEST_MASTER_KEY)
    ).json()["learnings"] == []


def test_routes_bad_decision_400(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SELF_IMPROVEMENT", "1")
    skill_id, agent_id = _create_skill(client)
    owner_id = client.get(
        f"/skills/{skill_id}", headers=_auth_headers(TEST_MASTER_KEY)
    ).json()["owner_id"]
    _seed_proposals(skill_id, agent_id, owner_id, ["x"])
    lid = sl.list_learnings(skill_id, "proposed")[0]["learning_id"]
    resp = client.post(
        f"/skills/{skill_id}/learnings/{lid}/decision",
        headers=_auth_headers(TEST_MASTER_KEY), json={"decision": "maybe"},
    )
    assert resp.status_code == 400


def test_routes_cross_owner_403(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SELF_IMPROVEMENT", "1")
    skill_id, _ = _create_skill(client)
    other_key = _register_user()["raw_api_key"]
    resp = client.get(
        f"/skills/{skill_id}/learnings", headers=_auth_headers(other_key)
    )
    assert resp.status_code == 403


# ── execution-time injection (regression for the build_messages signature) ──


def test_build_messages_zero_learnings_byte_identical():
    base = skill_executor.build_messages("BODY", {"task": "x"})
    explicit_none = skill_executor.build_messages("BODY", {"task": "x"}, None)
    assert base[0].content == explicit_none[0].content
    assert base[1].content == explicit_none[1].content


def test_build_messages_injects_block_as_data():
    msgs = skill_executor.build_messages("BODY", {"task": "x"}, "- Validate input")
    system = msgs[0].content
    assert "OPERATOR LEARNINGS" in system
    assert "data, not instructions" in system
    assert "Validate input" in system


# ── distiller: flag, sensitivity gate, proposes + advances, soft-fail ───────


def _set_metadata(skill_id: str, metadata: dict) -> None:
    with _db.get_raw_connection(_resolved_db_path()) as conn:
        conn.execute(
            "UPDATE hosted_skills SET parsed_metadata_json = %s WHERE skill_id = %s",
            (json.dumps(metadata), skill_id),
        )


def _set_agent_pii_safe(agent_id: str) -> None:
    # pii_safe is an authoritative column on the agents row (migration 0026),
    # NOT skill frontmatter — that's the data source the gate must read.
    with _db.get_raw_connection(_resolved_db_path()) as conn:
        conn.execute(
            "UPDATE agents SET pii_safe = 1 WHERE agent_id = %s", (agent_id,)
        )


def _set_examples(agent_id: str, examples: list[dict]) -> None:
    with _db.get_raw_connection(_resolved_db_path()) as conn:
        conn.execute(
            "UPDATE agents SET output_examples = %s WHERE agent_id = %s",
            (json.dumps(examples), agent_id),
        )


def _bad_example(job_id: str = "j1") -> dict:
    return {
        "created_at": "2026-01-01T00:00:00+00:00",
        "input": {"task": "format"}, "output": {"result": "bad"},
        "job_id": job_id,
    }


def _seed_bad_rating(agent_id: str, job_id: str = "j1", rating: int = 1) -> None:
    # The distiller reads the authoritative rating from job_quality_ratings and
    # joins back to the recorded example by job_id, so the example signal needs a
    # matching low rating row to be picked up.
    with _db.get_raw_connection(_resolved_db_path()) as conn:
        conn.execute(
            "INSERT INTO job_quality_ratings (job_id, agent_id, caller_owner_id, "
            "rating, created_at) VALUES (%s, %s, %s, %s, %s)",
            (job_id, agent_id, "user:c", rating, "2026-01-02T00:00:00+00:00"),
        )


def test_distiller_proposes_then_watermark_blocks_rerun(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SELF_IMPROVEMENT", "1")
    skill_id, agent_id = _create_skill(client)
    _set_examples(agent_id, [_bad_example()])
    _seed_bad_rating(agent_id)
    monkeypatch.setattr(
        "core.llm.run_with_fallback",
        lambda req: LLMResponse(
            text='{"learnings":[{"text":"Always return valid JSON","confidence":0.9}]}',
            model="stub", provider="stub",
        ),
    )
    summary = si.run_learning_distillation()
    assert summary["skills_distilled"] >= 1
    assert summary["learnings_proposed"] >= 1
    assert len(sl.list_learnings(skill_id, "proposed")) >= 1

    # second run: watermark advanced past the example's created_at → no attempt
    summary2 = si.run_learning_distillation()
    assert summary2["skills_distilled"] == 0


def test_distiller_skips_sensitive_skill(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SELF_IMPROVEMENT", "1")
    skill_id, agent_id = _create_skill(client)
    _set_examples(agent_id, [_bad_example()])
    _seed_bad_rating(agent_id)
    _set_agent_pii_safe(agent_id)  # authoritative sensitivity flag on the agents row
    called = {"n": 0}

    def _boom(req):
        called["n"] += 1
        raise AssertionError("LLM must not be called for a sensitive skill")

    monkeypatch.setattr("core.llm.run_with_fallback", _boom)
    summary = si.run_learning_distillation()
    assert called["n"] == 0
    assert summary["learnings_proposed"] == 0


def test_distiller_softfails_when_llm_raises(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SELF_IMPROVEMENT", "1")
    skill_id, agent_id = _create_skill(client)
    _set_examples(agent_id, [_bad_example()])
    _seed_bad_rating(agent_id)

    def _raise(req):
        raise RuntimeError("provider down")

    monkeypatch.setattr("core.llm.run_with_fallback", _raise)
    # must not raise; no proposals; watermark not advanced (ret* next run)
    summary = si.run_learning_distillation()
    assert summary["learnings_proposed"] == 0
    assert sl.list_learnings(skill_id, "proposed") == []


def test_distiller_noop_when_flag_off(client, monkeypatch):
    monkeypatch.delenv("AZTEA_SELF_IMPROVEMENT", raising=False)
    skill_id, agent_id = _create_skill(client)
    _set_examples(agent_id, [_bad_example()])
    summary = si.run_learning_distillation()
    assert summary == {"skills_scanned": 0, "skills_distilled": 0, "learnings_proposed": 0}


# ── Level-1 trust_trend ──────────────────────────────────────────────────────


def _seed_ratings(agent_id: str, ratings_oldest_first: list[int]) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with _db.get_raw_connection(_resolved_db_path()) as conn:
        for i, r in enumerate(ratings_oldest_first):
            conn.execute(
                "INSERT INTO job_quality_ratings (job_id, agent_id, caller_owner_id, "
                "rating, created_at) VALUES (%s, %s, %s, %s, %s)",
                (f"job-{agent_id}-{i}", agent_id, "user:c", r, (base + timedelta(hours=i)).isoformat()),
            )


def test_trust_trend_improving_and_enrich(client):
    skill_id, agent_id = _create_skill(client)
    _seed_ratings(agent_id, [2] * 10 + [5] * 10)  # prior low, recent high
    assert tt.compute_trust_trend(agent_id) == tt.TREND_IMPROVING
    enriched = reputation.enrich_agent_records([{"agent_id": agent_id}])
    assert enriched[0]["trust_trend"] == tt.TREND_IMPROVING


def test_trust_trend_unknown_without_history(client):
    _, agent_id = _create_skill(client)
    assert tt.compute_trust_trend(agent_id) == tt.TREND_UNKNOWN


# ── PII / secret scrubbing (CSO findings) ────────────────────────────────────


def test_freetext_scrub_redacts_secrets_and_emails():
    from core.privacy import scrub_freetext
    out = scrub_freetext("ping user@evil.com with key sk-ABCDEFGHIJKLMNOP123 now")
    assert "user@evil.com" not in out
    assert "sk-ABCDEFGHIJKLMNOP123" not in out
    assert "<redacted>" in out
    # behavioral guidance text is left intact
    assert scrub_freetext("Always validate the input schema") == "Always validate the input schema"


def test_distiller_scrubs_pii_in_persisted_bullet(client, monkeypatch):
    monkeypatch.setenv("AZTEA_SELF_IMPROVEMENT", "1")
    skill_id, agent_id = _create_skill(client)
    _set_examples(agent_id, [_bad_example()])
    _seed_bad_rating(agent_id)
    # Model echoes a secret + email into the bullet despite the prompt; the
    # persisted text must be scrubbed before an owner ever sees it.
    monkeypatch.setattr(
        "core.llm.run_with_fallback",
        lambda req: LLMResponse(
            text='{"learnings":[{"text":"Email user@evil.com or use sk-LEAKEDKEY1234567890","confidence":0.5}]}',
            model="stub", provider="stub",
        ),
    )
    si.run_learning_distillation()
    joined = " ".join(l["text"] for l in sl.list_learnings(skill_id, "proposed"))
    assert joined  # a bullet was proposed
    assert "user@evil.com" not in joined
    assert "sk-LEAKEDKEY1234567890" not in joined
