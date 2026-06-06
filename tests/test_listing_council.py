"""Advisory listing council: quorum, abstention, never-block, caching."""
from __future__ import annotations

import pytest

import core.listing_council as council
from core.listing_council_prompts import DimensionVerdict, MemberVerdict

_CANDIDATE = {
    "name": "X", "description": "d", "kind": "skill_md",
    "input_schema": {}, "output_schema": {}, "body": "b",
}


@pytest.fixture(autouse=True)
def _three_member_chain(monkeypatch):
    """Pin a 3-member chain and force the council on regardless of ambient env."""
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL", "on")
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL_CHAIN", "m1,m2,m3")


def _verdict(spec, *, value_add="pass", conf=0.8):
    dims = {
        d: DimensionVerdict("pass", conf, f"{d} ok")
        for d in ("reliability", "originality")
    }
    dims["value_add"] = DimensionVerdict(value_add, conf, "thin wrapper")
    return MemberVerdict(spec, dims)


def test_majority_concern_flags_dimension_and_never_blocks():
    def runner(spec, system, user, h):
        return _verdict(spec, value_add="concern")

    result = council.review_listing(_CANDIDATE, ["thin"], member_runner=runner)

    assert [f.code for f in result.findings] == ["listing.council.value_add"]
    # Advisory only — the council must never emit a BLOCK.
    assert all(f.level == "warn" for f in result.findings)
    assert result.needs_human_review is True  # unanimous
    assert result.member_count == 3


def test_single_present_member_does_not_flag():
    def runner(spec, system, user, h):
        return _verdict(spec, value_add="concern") if spec == "m1" else None

    result = council.review_listing(_CANDIDATE, [], member_runner=runner)

    assert result.findings == []
    assert result.member_count == 1


def test_errored_member_abstains_and_breaks_majority():
    # m1 concern, m2 errors (abstains), m3 pass -> 1 concern of 2 present -> no flag.
    def runner(spec, system, user, h):
        if spec == "m1":
            return _verdict(spec, value_add="concern")
        if spec == "m2":
            raise RuntimeError("provider down")
        return _verdict(spec, value_add="pass")

    result = council.review_listing(_CANDIDATE, [], member_runner=runner)

    assert result.findings == []
    assert result.member_count == 2


def test_two_member_split_one_one_does_not_flag(monkeypatch):
    """Strict majority: with 2 present and a 1-1 split, no dimension is flagged."""
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL_CHAIN", "m1,m2")

    def runner(spec, system, user, h):
        return _verdict(spec, value_add="concern" if spec == "m1" else "pass")

    result = council.review_listing(_CANDIDATE, [], member_runner=runner)
    assert result.findings == []
    assert result.member_count == 2


def test_two_members_both_concern_flags(monkeypatch):
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL_CHAIN", "m1,m2")

    def runner(spec, system, user, h):
        return _verdict(spec, value_add="concern")

    result = council.review_listing(_CANDIDATE, [], member_runner=runner)
    assert [f.code for f in result.findings] == ["listing.council.value_add"]
    assert result.needs_human_review is True


def test_below_confidence_floor_does_not_flag():
    def runner(spec, system, user, h):
        return _verdict(spec, value_add="concern", conf=0.4)

    result = council.review_listing(_CANDIDATE, [], member_runner=runner)
    assert result.findings == []


def test_disabled_env_returns_empty(monkeypatch):
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL", "off")
    called = {"n": 0}

    def runner(spec, system, user, h):
        called["n"] += 1
        return _verdict(spec)

    result = council.review_listing(_CANDIDATE, [], member_runner=runner)
    assert result.findings == []
    assert called["n"] == 0  # disabled short-circuits before dispatch


def test_member_call_is_cached_by_content(monkeypatch):
    """Identical content reuses the per-member LLM result instead of re-calling."""
    calls = {"n": 0}

    class _Resp:
        text = (
            '{"reliability": {"verdict": "pass", "confidence": 0.9, "reason": "ok"},'
            ' "originality": {"verdict": "pass", "confidence": 0.9, "reason": "ok"},'
            ' "value_add": {"verdict": "pass", "confidence": 0.9, "reason": "ok"}}'
        )

    def fake_run(req, model_chain=None):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(council, "run_with_fallback", fake_run)
    council.clear_member_cache()

    out1 = council._run_member_cached("hash1", "m1", "sys", "user")
    out2 = council._run_member_cached("hash1", "m1", "sys", "user")
    assert out1 == out2
    assert calls["n"] == 1  # second call served from cache
