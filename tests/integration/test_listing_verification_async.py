"""The advisory async pass annotates the listing's review_note (not block)."""
from __future__ import annotations

from core import feature_flags, listing_verification, registry
from core.listing_council_prompts import DimensionVerdict, MemberVerdict

_BODY = "---\nname: AsyncNote\n---\nsummarise the input text into one clear sentence"


def _register(name="AsyncNote"):
    return registry.register_agent(
        name=name, description="d", endpoint_url="skill://x",
        price_per_call_usd=0.01, tags=[], owner_id="user:alice",
    )


def _unanimous_concern_runner(spec, system, user, h):
    dims = {
        d: DimensionVerdict("concern", 0.9, f"{d} concern")
        for d in ("reliability", "originality", "value_add")
    }
    return MemberVerdict(spec, dims)


def test_async_pass_annotates_review_note_with_marker(isolated_db, monkeypatch):
    monkeypatch.setattr(feature_flags, "DISABLE_EMBEDDINGS", True)
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL", "on")
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL_CHAIN", "m1,m2,m3")
    aid = _register()

    result = listing_verification.run_and_annotate(
        aid, listing_verification.KIND_SKILL_MD,
        raw=_BODY, name="AsyncNote", description="d", input_schema={},
        council_runner=_unanimous_concern_runner,
    )
    assert result.needs_human_review is True

    agent = registry.get_agent(aid, include_unapproved=True)
    note = agent.get("review_note") or ""
    assert "[needs-human-review]" in note
    assert "listing.council." in note


def test_annotate_listing_noops_when_no_findings(isolated_db):
    aid = _register("QuietAgent")
    before = (registry.get_agent(aid, include_unapproved=True) or {}).get("review_note")
    listing_verification.annotate_listing(aid, listing_verification.AsyncResult())
    after = (registry.get_agent(aid, include_unapproved=True) or {}).get("review_note")
    assert before == after


def test_one_failing_collector_does_not_lose_others(isolated_db, monkeypatch):
    """If the council raises, the other collectors' findings still come through."""
    monkeypatch.setattr(feature_flags, "DISABLE_EMBEDDINGS", True)
    aid = _register("ResilientAgent")

    def boom_runner(spec, system, user, h):
        raise RuntimeError("council provider exploded")

    # Thin-wrapper signal (a python handler) should still be produced even though
    # the council errors — _safe isolates each collector.
    thin = "import requests\ndef handler(p):\n    return requests.get(p['u']).json()"
    result = listing_verification.verify_listing_async(
        aid, listing_verification.KIND_PYTHON_HANDLER,
        raw=thin, name="ResilientAgent", description="d", input_schema={},
        council_runner=boom_runner,
    )
    assert any(f.code == "listing.thin_wrapper" for f in result.findings)
