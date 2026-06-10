"""Dedup DB round-trip: cosine reads agent_embeddings (C1), fingerprint block."""
from __future__ import annotations

import pytest

from core import feature_flags, hosted_skills, listing_dedup, registry
from scripts.backfill_listing_fingerprints import backfill


def _register(name, description, tags=None):
    return registry.register_agent(
        name=name,
        description=description,
        endpoint_url="https://example.test/agent",
        price_per_call_usd=0.05,
        tags=tags or [],
        owner_id="user:alice",
    )


@pytest.mark.skipif(
    feature_flags.DISABLE_EMBEDDINGS,
    reason="Needs the real cosine pass (find_near_duplicates skips it when embeddings "
    "are disabled). CI runs the integration suite with AZTEA_DISABLE_EMBEDDINGS=1 to "
    "avoid the torch/sentence-transformers segfault on the hosted runner; devs run it "
    "with embeddings on. See .github/workflows/ci.yml.",
)
def test_find_near_duplicates_reads_agent_embeddings(isolated_db):
    """C1 guard: the cosine pass must read agent_embeddings, not the empty
    vector_store. Registering an agent writes an embedding there; querying with
    the same text must find it."""
    aid = _register(
        "PDF Table Extractor",
        "Extract tables from PDF documents into clean CSV rows.",
        tags=["pdf", "tables"],
    )
    matches = listing_dedup.find_near_duplicates(
        "PDF Table Extractor",
        "Extract tables from PDF documents into clean CSV rows.",
        ["pdf", "tables"],
        {},
    )
    assert any(m.agent_id == aid for m in matches), matches
    assert matches[0].similarity >= 0.85


def test_find_near_duplicates_excludes_self(isolated_db):
    aid = _register("Solo Agent", "A unique one-of-a-kind description here.")
    matches = listing_dedup.find_near_duplicates(
        "Solo Agent", "A unique one-of-a-kind description here.", [], {},
        exclude_agent_id=aid,
    )
    assert all(m.agent_id != aid for m in matches)


def test_disable_embeddings_skips_cosine(isolated_db, monkeypatch):
    _register("Embeddings Off", "Should never be cosine-matched when disabled.")
    monkeypatch.setattr(feature_flags, "DISABLE_EMBEDDINGS", True)
    matches = listing_dedup.find_near_duplicates(
        "Embeddings Off", "Should never be cosine-matched when disabled.", [], {},
    )
    assert matches == []


def test_record_and_find_verbatim_copy(isolated_db):
    aid = _register("Dup Skill", "A duplicate-detection fixture skill.")
    body = "---\nname: Dup Skill\n---\ndo the thing\nreturn result"
    listing_dedup.record_fingerprint(aid, body, "skill_md")

    # A copy whose ONLY change is the frontmatter title hashes identically.
    renamed = "---\nname: Totally Different Title\n---\ndo the thing\nreturn result"
    fp = listing_dedup.content_fingerprint(renamed, "skill_md")
    match = listing_dedup.find_verbatim_copy(fp)
    assert match is not None and match.agent_id == aid

    # Excluding the matched agent yields nothing (used during re-verification).
    assert listing_dedup.find_verbatim_copy(fp, exclude_agent_id=aid) is None


def test_python_handler_fingerprint_keeps_leading_dashes(isolated_db):
    """For python_handler, a leading '---' is code/data, not frontmatter — so it
    is NOT stripped (unlike skill_md)."""
    body = "---\nx = 1\n---\ndef handler(p):\n    return p"
    norm_skill = listing_dedup.normalize_body_for_fingerprint(body, "skill_md")
    norm_py = listing_dedup.normalize_body_for_fingerprint(body, "python_handler")
    assert norm_py != norm_skill
    assert listing_dedup.content_fingerprint(body, "python_handler") != \
        listing_dedup.content_fingerprint(body, "skill_md")


def test_record_fingerprint_overwrites_prior(isolated_db):
    aid = _register("Overwrite Agent", "Fixture for fingerprint overwrite.")
    body_a = "def handler(payload):\n    return compute_first_version(payload)"
    body_b = "def handler(payload):\n    return compute_second_version(payload)"
    listing_dedup.record_fingerprint(aid, body_a, "python_handler")
    listing_dedup.record_fingerprint(aid, body_b, "python_handler")  # overwrite

    fp_a = listing_dedup.content_fingerprint(body_a, "python_handler")
    fp_b = listing_dedup.content_fingerprint(body_b, "python_handler")
    assert listing_dedup.find_verbatim_copy(fp_b) is not None
    assert listing_dedup.find_verbatim_copy(fp_a) is None  # old fingerprint gone


def test_backfill_fingerprints_pre_existing_hosted_skill(isolated_db):
    """A hosted skill with no fingerprint row (pre-0077) becomes a dup source
    after the backfill runs."""
    aid = _register("Backfill Skill", "A skill needing a backfilled fingerprint.")
    raw = "---\nname: Backfill Skill\n---\nsummarise the input into a single sentence."
    hosted_skills.create_hosted_skill(
        agent_id=aid, owner_id="user:alice", slug="backfill-skill",
        raw_md=raw, system_prompt="do it", parsed_metadata={},
    )
    fp = listing_dedup.content_fingerprint(raw, "skill_md")
    assert listing_dedup.find_verbatim_copy(fp) is None  # not fingerprinted yet

    candidates, written = backfill(dry_run=False)
    assert written >= 1
    assert listing_dedup.find_verbatim_copy(fp) is not None

    # Idempotent: a second run finds nothing new to do for this agent.
    _, written_again = backfill(dry_run=False)
    assert written_again == 0


def test_distinct_body_does_not_match_fingerprint(isolated_db):
    aid = _register("Original", "Original body skill.")
    listing_dedup.record_fingerprint(
        aid, "---\nname: Original\n---\nstep one\nstep two", "skill_md",
    )
    other_fp = listing_dedup.content_fingerprint(
        "---\nname: Original\n---\ncompletely different body", "skill_md",
    )
    assert listing_dedup.find_verbatim_copy(other_fp) is None
