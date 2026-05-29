"""Phase 1 (B4, C1, C3, C4) tests.

B4: LLM tiebreaker for close confidence calls
C1: schema-driven whole-payload extraction (one LLM call vs N)
C3: per-caller affinity bias toward agents the caller has rated well
C4: utility-based scoring adjustment (latency penalty)
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

import pytest

from core import db as _db
from core.migrate import apply_migrations
from core.registry import auto_hire as ah
from core.registry import caller_affinity as ca
from core.registry import llm_tiebreaker as tb


# --- C4: utility (latency) adjustment ---------------------------------


def _make_candidate(*, agent_id="a-1", avg_latency_ms=0.0, **kwargs):
    """Build a CandidateAgent for the scoring tests."""
    defaults = dict(
        slug=kwargs.pop("slug", agent_id.replace("-", "_")),
        name=kwargs.pop("name", agent_id),
        description=kwargs.pop("description", "test agent"),
        tags=kwargs.pop("tags", []),
        category=kwargs.pop("category", ""),
        price_per_call_usd=kwargs.pop("price_per_call_usd", 0.05),
        trust_score=kwargs.pop("trust_score", 80.0),
        success_rate=kwargs.pop("success_rate", 0.9),
        stability_tier=kwargs.pop("stability_tier", "stable"),
        input_schema=kwargs.pop("input_schema", {"type": "object", "required": []}),
        raw={
            "call_count": 50,
            "success_rate": 0.9,
            "trust_score": 80.0,
            "review_status": "approved",
            "avg_latency_ms": avg_latency_ms,
        },
        match_keywords=[],
        block_keywords=[],
    )
    return ah.CandidateAgent(agent_id=agent_id, **defaults)


def test_utility_no_penalty_below_floor():
    c = _make_candidate(avg_latency_ms=500.0)
    delta, reasons = ah._score_utility_adjustment(c)
    assert delta == 0.0
    assert reasons == []


def test_utility_penalty_scales_linearly():
    fast = _make_candidate(agent_id="fast", avg_latency_ms=2000.0)
    mid = _make_candidate(agent_id="mid", avg_latency_ms=16000.0)
    slow = _make_candidate(agent_id="slow", avg_latency_ms=30000.0)
    d_fast, _ = ah._score_utility_adjustment(fast)
    d_mid, _ = ah._score_utility_adjustment(mid)
    d_slow, _ = ah._score_utility_adjustment(slow)
    assert d_fast == 0.0
    assert d_slow == -ah._UTILITY_LATENCY_PENALTY_CAP
    # Mid sits at roughly half the penalty.
    assert -ah._UTILITY_LATENCY_PENALTY_CAP < d_mid < 0.0


def test_utility_penalty_capped_at_high_latency():
    extreme = _make_candidate(avg_latency_ms=120000.0)  # 2 min
    delta, _ = ah._score_utility_adjustment(extreme)
    assert delta == -ah._UTILITY_LATENCY_PENALTY_CAP


# --- C3: caller affinity ----------------------------------------------


@pytest.fixture
def fresh_db_with_ratings(monkeypatch, tmp_path):
    db_path = tmp_path / f"phase1-{_uuid.uuid4().hex}.db"
    monkeypatch.setattr(_db, "DB_PATH", str(db_path))
    if hasattr(_db._local, "conns"):
        for c in list(_db._local.conns.values()):
            try:
                c.close()
            except Exception:
                pass
        _db._local.conns.clear()
    apply_migrations(str(db_path))
    ca.clear_cache()
    yield db_path
    ca.clear_cache()


def _insert_rating(*, caller_owner_id, agent_id, rating):
    now = datetime.now(timezone.utc).isoformat()
    job_id = f"job-{_uuid.uuid4().hex}"
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        # job_quality_ratings: minimal insert. The schema requires
        # job_id, agent_id, caller_owner_id, rating, created_at.
        conn.execute(
            "INSERT INTO job_quality_ratings "
            "(job_id, agent_id, caller_owner_id, rating, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (job_id, agent_id, caller_owner_id, rating, now),
        )
        conn.commit()


def test_caller_affinity_zero_when_no_ratings(fresh_db_with_ratings):
    delta, reasons = ca.score_for("caller-1", "agent-x")
    assert delta == 0.0
    assert reasons == []


def test_caller_affinity_zero_when_below_evidence_floor(fresh_db_with_ratings):
    # Only 2 ratings — below _AFFINITY_MIN_EVIDENCE (3).
    _insert_rating(caller_owner_id="caller-1", agent_id="agent-a", rating=5)
    _insert_rating(caller_owner_id="caller-1", agent_id="agent-a", rating=5)
    delta, _ = ca.score_for("caller-1", "agent-a")
    assert delta == 0.0


def test_caller_affinity_positive_for_high_ratings(fresh_db_with_ratings):
    for _ in range(5):
        _insert_rating(caller_owner_id="caller-1", agent_id="agent-a", rating=5)
    delta, reasons = ca.score_for("caller-1", "agent-a")
    assert delta > 0
    assert delta <= ca._AFFINITY_BONUS_CAP
    assert reasons  # human-readable reason emitted


def test_caller_affinity_negative_for_low_ratings(fresh_db_with_ratings):
    for _ in range(5):
        _insert_rating(caller_owner_id="caller-2", agent_id="agent-b", rating=1)
    delta, _ = ca.score_for("caller-2", "agent-b")
    assert delta < 0
    assert delta >= -ca._AFFINITY_BONUS_CAP


def test_caller_affinity_no_owner_id_is_safe(fresh_db_with_ratings):
    assert ca.score_for(None, "agent-a") == (0.0, [])
    assert ca.score_for("", "agent-a") == (0.0, [])
    assert ca.score_for("caller-x", "") == (0.0, [])


# --- B4: LLM tiebreaker -----------------------------------------------


class _StubRanked:
    def __init__(self, slug: str, name: str = "n", desc: str = "d"):
        self.candidate = type("X", (), {
            "slug": slug, "name": name, "description": desc,
        })()
        self.score = 1.0
        self.reasons = []


def test_tiebreaker_returns_none_with_single_candidate(monkeypatch):
    monkeypatch.setattr(tb, "_ENABLED", True)
    assert tb.try_tiebreak([_StubRanked("only_one")], "x") is None


def test_tiebreaker_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(tb, "_ENABLED", False)
    assert tb.try_tiebreak(
        [_StubRanked("a"), _StubRanked("b")], "x",
    ) is None


def test_tiebreaker_returns_matching_candidate(monkeypatch):
    monkeypatch.setattr(tb, "_ENABLED", True)

    class _Response:
        text = "agent_b"

    def _fake_run(_req):
        return _Response()

    # Inject our fake run_with_fallback by mocking the import path.
    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", _fake_run)
    cands = [_StubRanked("agent_a"), _StubRanked("agent_b")]
    picked = tb.try_tiebreak(cands, "do the thing")
    assert picked is cands[1]


def test_tiebreaker_rejects_hallucinated_slug(monkeypatch):
    monkeypatch.setattr(tb, "_ENABLED", True)

    class _Response:
        text = "agent_q_that_does_not_exist"

    def _fake_run(_req):
        return _Response()

    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", _fake_run)
    cands = [_StubRanked("agent_a"), _StubRanked("agent_b")]
    assert tb.try_tiebreak(cands, "x") is None


def test_tiebreaker_returns_none_on_llm_failure(monkeypatch):
    monkeypatch.setattr(tb, "_ENABLED", True)

    def _broken_run(_req):
        raise RuntimeError("llm down")

    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", _broken_run)
    cands = [_StubRanked("agent_a"), _StubRanked("agent_b")]
    assert tb.try_tiebreak(cands, "x") is None


def test_tiebreaker_returns_none_when_llm_says_NONE(monkeypatch):
    monkeypatch.setattr(tb, "_ENABLED", True)

    class _Response:
        text = "NONE"

    def _fake_run(_req):
        return _Response()

    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", _fake_run)
    cands = [_StubRanked("agent_a"), _StubRanked("agent_b")]
    assert tb.try_tiebreak(cands, "x") is None


# --- C1: whole-payload one-shot LLM extraction ------------------------


def test_whole_payload_extract_returns_none_when_llm_unavailable(monkeypatch):
    """Failed LLM import / call → None, caller falls back to per-field."""
    def _broken_run(_req):
        raise RuntimeError("llm down")
    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", _broken_run)
    out = ah._llm_extract_whole_payload(
        intent="audit my python project for cves",
        required_fields=["package", "version"],
        properties={
            "package": {"type": "string", "description": "pkg name"},
            "version": {"type": "string", "description": "pkg version"},
        },
    )
    assert out is None


def test_whole_payload_extract_returns_none_for_code_like_field():
    """If any required field is code-like, defer to per-field path."""
    out = ah._llm_extract_whole_payload(
        intent="run this snippet",
        required_fields=["code"],
        properties={"code": {"type": "string"}},
    )
    assert out is None


def test_whole_payload_extract_parses_valid_json(monkeypatch):
    class _Response:
        text = '{"package": "log4j", "version": "2.14.0"}'

    def _fake_run(_req):
        return _Response()

    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", _fake_run)
    out = ah._llm_extract_whole_payload(
        intent="check log4j 2.14.0 for cves",
        required_fields=["package", "version"],
        properties={
            "package": {"type": "string"},
            "version": {"type": "string"},
        },
    )
    assert out == {"package": "log4j", "version": "2.14.0"}


def test_whole_payload_extract_rejects_null_in_field(monkeypatch):
    """A field returned as null = not extractable; caller refuses with missing_fields."""
    class _Response:
        text = '{"package": "log4j", "version": null}'

    def _fake_run(_req):
        return _Response()

    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", _fake_run)
    out = ah._llm_extract_whole_payload(
        intent="check log4j without specifying a version",
        required_fields=["package", "version"],
        properties={
            "package": {"type": "string"},
            "version": {"type": "string"},
        },
    )
    assert out is None


# --- Belt-and-suspenders M1 layer 2: depth/length caps ---


def test_whole_payload_rejects_oversized_string(monkeypatch):
    """A 100KB string from a prompt-injected LLM is rejected."""
    long_value = "x" * (ah._WHOLE_PAYLOAD_MAX_STRING + 1)
    text = '{"manifest": ' + repr(long_value).replace("'", '"') + '}'

    class _Response:
        pass
    _Response.text = text

    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", lambda _r: _Response)
    out = ah._llm_extract_whole_payload(
        intent="audit my project for vulnerabilities",
        required_fields=["manifest"],
        properties={"manifest": {"type": "string"}},
    )
    assert out is None


def test_whole_payload_rejects_deeply_nested_object(monkeypatch):
    """A 50-deep nested dict is rejected."""
    # Build nested dict 50 deep.
    inner = "leaf"
    for _ in range(50):
        inner = {"x": inner}
    import json as _json
    text = _json.dumps({"config": inner})

    class _Response:
        pass
    _Response.text = text

    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", lambda _r: _Response)
    out = ah._llm_extract_whole_payload(
        intent="apply this config to my system in production",
        required_fields=["config"],
        properties={"config": {"type": "object"}},
    )
    assert out is None


def test_whole_payload_rejects_object_with_excessive_keys(monkeypatch):
    """A dict with >_WHOLE_PAYLOAD_MAX_DICT_KEYS is rejected."""
    import json as _json
    big = {f"k{i}": f"v{i}" for i in range(ah._WHOLE_PAYLOAD_MAX_DICT_KEYS + 5)}
    text = _json.dumps({"config": big})

    class _Response:
        pass
    _Response.text = text

    import core.llm
    monkeypatch.setattr(core.llm, "run_with_fallback", lambda _r: _Response)
    out = ah._llm_extract_whole_payload(
        intent="apply this config to my system in production",
        required_fields=["config"],
        properties={"config": {"type": "object"}},
    )
    assert out is None
