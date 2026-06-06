"""End-to-end publish verification: exact-dup blocks, distinct content does not.

The advisory pass (fingerprint record, cosine dedup, council) runs as a FastAPI
background task. Under TestClient those execute before the response returns, so
by the time a publish call returns its fingerprint is recorded and a later exact
copy is refused inline.
"""
from __future__ import annotations

from tests.integration.support import *  # noqa: F403


def _publish_skill(client, body: str, price: float = 0.05, key: str | None = None):
    return client.post(
        "/skills",
        headers={"Authorization": f"Bearer {key or TEST_MASTER_KEY}"},  # noqa: F405
        json={"skill_md": body, "price_per_call_usd": price},
    )


def _fast_verify_env(monkeypatch):
    # Keep the async pass cheap + deterministic: no council, no embeddings model.
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL", "off")
    monkeypatch.setenv("AZTEA_LISTING_JUDGE", "off")
    import core.feature_flags as ff
    monkeypatch.setattr(ff, "DISABLE_EMBEDDINGS", True)


_BODY = (
    "---\nname: Sentence Summarizer\ndescription: Summarise text to one line.\n---\n\n"
    "# Summarizer\n\nReduce the input text to a single clear sentence.\n"
)


def test_cross_owner_exact_duplicate_is_blocked(client, monkeypatch):
    _fast_verify_env(monkeypatch)
    first = _publish_skill(client, _BODY)  # master owner
    assert first.status_code == 201, first.text

    # A DIFFERENT owner re-publishing the same body (only the title differs →
    # identical fingerprint) is refused as a copy.
    other = _register_user()  # noqa: F405
    renamed = _BODY.replace("Sentence Summarizer", "One Line Summary Tool")
    dup = _publish_skill(client, renamed, key=other["raw_api_key"])
    assert dup.status_code == 400, dup.text
    assert dup.json()["error"] == "listing.duplicate"


def test_same_owner_re_publish_is_not_blocked(client, monkeypatch):
    """An owner re-listing their own content is allowed (name-collision suffix),
    not treated as a copy."""
    _fast_verify_env(monkeypatch)
    first = _publish_skill(client, _BODY)
    second = _publish_skill(client, _BODY)  # same master owner, identical body
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["skill_id"] != second.json()["skill_id"]


def test_distinct_skill_is_not_blocked(client, monkeypatch):
    _fast_verify_env(monkeypatch)
    first = _publish_skill(client, _BODY)
    assert first.status_code == 201, first.text

    different = (
        "---\nname: Keyword Extractor\ndescription: Pull keywords from text.\n---\n\n"
        "# Keywords\n\nReturn the salient keywords from the input document.\n"
    )
    second = _publish_skill(client, different)
    assert second.status_code == 201, second.text


def test_clean_publish_still_succeeds(client, monkeypatch):
    _fast_verify_env(monkeypatch)
    resp = _publish_skill(client, _BODY)
    assert resp.status_code == 201, resp.text
    assert resp.json()["review_status"] == "approved"  # master key
