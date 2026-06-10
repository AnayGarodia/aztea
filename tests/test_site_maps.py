"""Unit tests for the site-map commons core (Phase 1: data + logic layer).

Covers the pure modules (normalize, signing roundtrip+tamper, ranking,
freshness) and the DB store (supersede-on-refresh, active filtering, the
consumer_job_id royalty-idempotency anchor, counters, revoke). Money settlement
(payouts) and the live navigator/dispatch wiring land in a later slice.
"""

from __future__ import annotations

import pytest

from core import crypto
from core.site_maps import freshness, graph, normalize, ranking, signing, store


# --------------------------------------------------------------------------- graph (Phase 3)
def test_navigation_graph_groups_by_heading():
    rows = [
        {"role": "heading", "name": "Pricing"}, {"role": "link", "name": "Starter"},
        {"role": "link", "name": "Pro"}, {"role": "heading", "name": "Docs"},
        {"role": "link", "name": "API"},
    ]
    g = graph.build_navigation_graph(rows)
    assert g["schema"] == "aztea/site-map/2"
    assert [s["heading"] for s in g["sections"]] == ["Pricing", "Docs"]
    pricing = next(s for s in g["sections"] if s["heading"] == "Pricing")
    assert pricing["links"] == ["Starter", "Pro"]
    assert "API" in g["entry_points"]


def test_recommend_modality_flags_sparse_pages():
    assert graph.recommend_modality([{"role": "link", "name": "x"}]) == "screenshot"
    rich = [{"role": "link", "name": str(i)} for i in range(20)]
    assert graph.recommend_modality(rich) == "accessibility_tree"


# --------------------------------------------------------------------------- normalize
def test_normalize_site_key_is_idempotent_and_collapses_ids():
    u = "https://WWW.Example.com/item/12345?utm_source=x&id=9#frag"
    k1 = normalize.normalize_site_key(u)
    assert k1 == normalize.normalize_site_key("https://" + k1.split("?")[0] + "?id=9")
    assert "example.com" in k1 and "www." not in k1
    assert "/item/*" in k1            # numeric id collapsed
    assert "utm_source" not in k1     # tracking param dropped
    assert k1.endswith("?id")         # semantic key kept (key only, not value)


def test_dom_fingerprint_value_independent_structure_sensitive():
    a = ["link", "heading", "button"]
    b = ["link", "heading", "button"]   # same roles
    c = ["link", "heading"]             # different structure
    url = "example.com/"
    assert normalize.dom_fingerprint(url, a) == normalize.dom_fingerprint(url, b)
    assert normalize.dom_fingerprint(url, a) != normalize.dom_fingerprint(url, c)


def test_response_shape_fingerprint_ignores_values_tracks_shape():
    a = {"tiers": [{"name": "Pro", "price": 20}]}
    b = {"tiers": [{"name": "Starter", "price": 0}]}   # same shape, diff values
    c = {"tiers": [{"name": "Pro"}]}                    # shape changed (price gone)
    assert normalize.response_shape_fingerprint(a) == normalize.response_shape_fingerprint(b)
    assert normalize.response_shape_fingerprint(a) != normalize.response_shape_fingerprint(c)


# --------------------------------------------------------------------------- signing
def test_sign_and_verify_map_roundtrip_and_tamper():
    priv, pub = crypto.generate_signing_keypair()
    manifest = signing.build_map_manifest(
        site_key="example.com/", url_pattern="example.com/", map_json={"affordances": {}},
        dom_fingerprint="abc", author_did="did:web:host:agents:x", version=1,
    )
    sig = signing.sign_map(priv, manifest)
    assert signing.verify_map(pub, manifest, sig) is True
    tampered = dict(manifest, dom_fingerprint="evil")
    assert signing.verify_map(pub, tampered, sig) is False
    # A different keypair must not verify.
    _, other_pub = crypto.generate_signing_keypair()
    assert signing.verify_map(other_pub, manifest, sig) is False


def test_map_sha256_is_deterministic_and_order_independent():
    assert signing.map_sha256({"a": 1, "b": 2}) == signing.map_sha256({"b": 2, "a": 1})


# --------------------------------------------------------------------------- ranking
def _m(map_id, agent, *, fresh=0, drift=0):
    return {"map_id": map_id, "author_agent_id": agent,
            "fresh_validation_count": fresh, "drift_count": drift, "created_at": None}


def test_ranking_prefers_higher_trust_then_reliability():
    maps = [_m("a", "low"), _m("b", "high", fresh=10)]
    best = ranking.select_best_map(
        maps, trust_by_agent={"low": 10.0, "high": 90.0}, now_epoch=1_000_000.0,
    )
    assert best["map_id"] == "b"


def test_ranking_challenge_penalty_sinks_a_map():
    maps = [_m("a", "x", fresh=5), _m("b", "x", fresh=5)]
    best = ranking.select_best_map(
        maps, trust_by_agent={"x": 80.0},
        open_challenges_by_map={"a": 4}, now_epoch=1_000_000.0,
    )
    assert best["map_id"] == "b"


def test_ranking_empty_returns_none():
    assert ranking.select_best_map([], trust_by_agent={}) is None


# --------------------------------------------------------------------------- freshness
def test_freshness_within_ttl_skips_recompute():
    row = {"status": "active", "last_validated_at": "2026-06-01T00:00:00+00:00",
           "dom_fingerprint": "fp"}
    now = freshness._parse_iso("2026-06-01T01:00:00+00:00")
    called = {"n": 0}

    def _recompute():
        called["n"] += 1
        return "different"

    fresh, reason = freshness.validate_map_before_replay(
        row, recompute_fingerprint=_recompute, ttl_hours=24, now_epoch=now,
    )
    assert fresh is True and reason == "within_ttl" and called["n"] == 0


def test_freshness_past_ttl_revalidates_or_drifts():
    row = {"status": "active", "last_validated_at": "2026-06-01T00:00:00+00:00",
           "dom_fingerprint": "fp"}
    now = freshness._parse_iso("2026-06-05T00:00:00+00:00")  # >24h later
    ok, r1 = freshness.validate_map_before_replay(
        row, recompute_fingerprint=lambda: "fp", ttl_hours=24, now_epoch=now)
    assert ok is True and r1 == "revalidated"
    bad, r2 = freshness.validate_map_before_replay(
        row, recompute_fingerprint=lambda: "changed", ttl_hours=24, now_epoch=now)
    assert bad is False and r2 == "drift"


def test_freshness_inactive_never_fresh_and_ttl_clamped():
    row = {"status": "revoked", "dom_fingerprint": "fp"}
    ok, reason = freshness.validate_map_before_replay(row, recompute_fingerprint=lambda: "fp")
    assert ok is False and reason == "inactive"
    assert freshness.clamp_ttl_hours(0) == 1
    assert freshness.clamp_ttl_hours(10_000) == 168


# --------------------------------------------------------------------------- store (DB)
@pytest.fixture()
def commons_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "commons.db"))
    store.init_site_maps_db()
    return store


def _put(db, *, author_did="did:web:host:agents:x", site="example.com/"):
    priv, _pub = crypto.generate_signing_keypair()
    return db.put_map(
        site_key=site, url_pattern=site, author_did=author_did,
        author_agent_id="agent-x", author_owner_id="owner-x",
        map_json={"affordances": {}}, dom_fingerprint="fp", private_pem=priv,
    )


def test_put_map_signs_a_verifiable_manifest(commons_db):
    # put_map signs inside its transaction; the stored signature must verify
    # against a manifest rebuilt from the stored row + the author's public key.
    priv, pub = crypto.generate_signing_keypair()
    row = commons_db.put_map(
        site_key="example.com/", url_pattern="example.com/",
        author_did="did:web:host:agents:x", author_agent_id="agent-x",
        author_owner_id="owner-x", map_json={"affordances": {"links": ["Docs"]}},
        dom_fingerprint="fp", private_pem=priv,
    )
    manifest = signing.build_map_manifest(
        site_key="example.com/", url_pattern="example.com/",
        map_json={"affordances": {"links": ["Docs"]}}, dom_fingerprint="fp",
        author_did="did:web:host:agents:x", version=row["version"],
    )
    assert signing.verify_map(pub, manifest, row["signature"]) is True


def test_find_reusable_map_selects_active_and_none_when_empty(commons_db, monkeypatch):
    from core.site_maps import authoring
    monkeypatch.setattr(authoring.store, "DB_PATH", commons_db.DB_PATH)
    assert authoring.find_reusable_map("https://nothing.example/") is None
    _put(commons_db, author_did="did:web:host:agents:a")
    best = authoring.find_reusable_map("https://example.com/")
    assert best is not None and best["status"] == "active"


def test_author_map_degrades_without_agent_key(commons_db):
    # No agents row for this id -> ensure_agent_signing_keys returns Nones ->
    # author_map returns None and never raises (commons is additive).
    from core.site_maps import authoring
    out = authoring.author_map(
        agent_id="missing-agent-id", owner_id="o", url="https://example.com/",
        map_json={"affordances": {}}, roles=["link"],
    )
    assert out is None


def test_put_map_supersedes_prior_active_version(commons_db):
    v1 = _put(commons_db)
    assert v1["version"] == 1 and v1["status"] == "active"
    v2 = _put(commons_db)  # same author + site
    assert v2["version"] == 2 and v2["status"] == "active"
    active = commons_db.get_active_maps("example.com/")
    assert len(active) == 1 and active[0]["map_id"] == v2["map_id"]
    assert commons_db.get_map(v1["map_id"])["status"] == "superseded"


def test_record_usage_is_idempotent_on_job(commons_db):
    first = commons_db.record_usage(
        map_id="m1", api_spec_id=None, site_key="example.com/", consumer_job_id="job-1",
        consumer_owner_id="c", author_owner_id="a", royalty_cents=3, validated_fresh=True)
    assert first is not None and first["royalty_cents"] == 3
    dup = commons_db.record_usage(
        map_id="m1", api_spec_id=None, site_key="example.com/", consumer_job_id="job-1",
        consumer_owner_id="c", author_owner_id="a", royalty_cents=3, validated_fresh=True)
    assert dup is None  # idempotent — no second usage row for the same job


def test_two_authors_coexist_as_active(commons_db):
    # The partial unique index is on (site_key, author_did), so two different
    # authors may each hold an active map for the same site — the invariant the
    # whole "competing maps, ranked at read time" design rests on.
    a = _put(commons_db, author_did="did:web:host:agents:a")
    b = _put(commons_db, author_did="did:web:host:agents:b")
    active_ids = {m["map_id"] for m in commons_db.get_active_maps("example.com/")}
    assert active_ids == {a["map_id"], b["map_id"]}


def test_open_challenge_counts_feeds_ranking(commons_db):
    from core import db as _db
    with _db.get_raw_connection(commons_db.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO site_map_challenges (challenge_id, map_id, site_key, "
            "challenger_owner_id, reason, status, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("smc_x", "m1", "example.com/", "c", "wrong_data", "open",
             "2026-06-01T00:00:00+00:00"),
        )
    assert commons_db.open_challenge_counts("example.com/").get("m1") == 1


def test_bump_hit_and_revoke(commons_db):
    m = _put(commons_db)
    commons_db.bump_hit(m["map_id"], fresh=True)
    commons_db.bump_hit(m["map_id"], fresh=False)
    row = commons_db.get_map(m["map_id"])
    assert row["hit_count"] == 2 and row["fresh_validation_count"] == 1 and row["drift_count"] == 1
    commons_db.revoke_map(m["map_id"], reason="poisoned")
    assert commons_db.get_map(m["map_id"])["status"] == "revoked"
    assert commons_db.get_active_maps("example.com/") == []


# --------------------------------------------------------------------------- API specs (signing)
def test_sign_and_verify_api_spec_roundtrip_and_tamper():
    # Tampering the endpoint host (the SSRF-critical field) must break verification:
    # this is the property the whole "signed, non-templatable authority" design rests on.
    priv, pub = crypto.generate_signing_keypair()
    manifest = signing.build_api_spec_manifest(
        site_key="example.com/data", author_did="did:web:host:agents:x", method="GET",
        endpoint_scheme="https", endpoint_host="api.example.com", endpoint_port=None,
        path_template="/v1/items", query_template="page=1", response_fingerprint="rf",
        field_map={"items": "$.items"}, param_schema={},
    )
    sig = signing.sign_api_spec(priv, manifest)
    assert signing.verify_api_spec(pub, manifest, sig) is True
    tampered = dict(manifest, endpoint_host="attacker.com")
    assert signing.verify_api_spec(pub, tampered, sig) is False
    _, other_pub = crypto.generate_signing_keypair()
    assert signing.verify_api_spec(other_pub, manifest, sig) is False


# --------------------------------------------------------------------------- API specs (store)
def _put_spec(db, *, site="example.com/data", host="api.example.com",
              path="/v1/items", author_did="did:web:host:agents:x"):
    priv, _pub = crypto.generate_signing_keypair()
    return db.put_api_spec(
        site_key=site, map_id=None, author_did=author_did, author_agent_id="agent-x",
        author_owner_id="owner-x", method="GET", endpoint_scheme="https",
        endpoint_host=host, endpoint_port=None, path_template=path, query_template="",
        param_schema={}, response_fingerprint="rf", field_map={"items": "$.items"},
        private_pem=priv,
    )


def test_put_api_spec_signs_verifiable_manifest(commons_db):
    priv, pub = crypto.generate_signing_keypair()
    row = commons_db.put_api_spec(
        site_key="example.com/data", map_id=None, author_did="did:web:host:agents:x",
        author_agent_id="agent-x", author_owner_id="owner-x", method="GET",
        endpoint_scheme="https", endpoint_host="api.example.com", endpoint_port=None,
        path_template="/v1/items", query_template="", param_schema={},
        response_fingerprint="rf", field_map={"items": "$.items"}, private_pem=priv,
    )
    manifest = signing.build_api_spec_manifest(
        site_key="example.com/data", author_did="did:web:host:agents:x", method="GET",
        endpoint_scheme="https", endpoint_host="api.example.com", endpoint_port=None,
        path_template="/v1/items", query_template="", response_fingerprint="rf",
        field_map={"items": "$.items"}, param_schema={},
    )
    assert signing.verify_api_spec(pub, manifest, row["signature"]) is True
    assert row["signature_alg"] == signing.API_SPEC_SIG_SCHEME


def test_put_api_spec_supersedes_prior_active(commons_db):
    s1 = _put_spec(commons_db)
    s2 = _put_spec(commons_db)  # same (site_key, method, host, path) -> supersedes
    active = commons_db.get_active_api_specs("example.com/data")
    assert len(active) == 1 and active[0]["api_spec_id"] == s2["api_spec_id"]
    assert commons_db.get_api_spec(s1["api_spec_id"])["status"] == "superseded"


def test_get_active_api_specs_filters_by_method(commons_db):
    _put_spec(commons_db)
    assert len(commons_db.get_active_api_specs("example.com/data", method="GET")) == 1
    assert commons_db.get_active_api_specs("example.com/data", method="POST") == []


def test_bump_api_spec_hit_and_revoke(commons_db):
    s = _put_spec(commons_db)
    commons_db.bump_api_spec_hit(s["api_spec_id"], fresh=True)
    commons_db.bump_api_spec_hit(s["api_spec_id"], fresh=False)
    row = commons_db.get_api_spec(s["api_spec_id"])
    assert row["hit_count"] == 2 and row["drift_count"] == 1
    commons_db.revoke_api_spec(s["api_spec_id"], reason="drift")
    assert commons_db.get_api_spec(s["api_spec_id"])["status"] == "revoked"
    assert commons_db.get_active_api_specs("example.com/data") == []


# --------------------------------------------------------------------------- API specs (authoring glue)
def test_author_api_spec_refuses_cross_origin(commons_db):
    # Endpoint host (attacker.com) doesn't share the page's registrable domain ->
    # refused before any key/DB work. The cross-origin cache-poisoning gate (fix #2).
    from core.site_maps import authoring
    out = authoring.author_api_spec(
        agent_id="agent-x", owner_id="o", page_url="https://example.com/p",
        capture={"url": "https://attacker.com/api/data", "method": "GET", "json": {"a": 1}},
    )
    assert out is None


def test_author_and_find_reusable_api_spec_roundtrip(commons_db, monkeypatch):
    from core.registry import identity_backfill
    from core.site_maps import authoring
    priv, pub = crypto.generate_signing_keypair()
    monkeypatch.setattr(
        identity_backfill, "ensure_agent_signing_keys",
        lambda aid, **k: (priv, pub, f"did:web:host:agents:{aid}"),
    )
    row = authoring.author_api_spec(
        agent_id="agent-x", owner_id="o", page_url="https://example.com/pricing",
        capture={"url": "https://api.example.com/v2/pricing", "method": "GET",
                 "json": {"tiers": [{"name": "Pro"}]}},
    )
    assert row is not None and row["endpoint_host"] == "api.example.com"
    # Subdomain of the same registrable domain may reuse the spec; signature verifies.
    best = authoring.find_reusable_api_spec("https://www.example.com/pricing")
    assert best is not None and best["api_spec_id"] == row["api_spec_id"]


def test_find_reusable_api_spec_rejects_unverifiable_signature(commons_db, monkeypatch):
    from core.registry import identity_backfill
    from core.site_maps import authoring
    priv, pub = crypto.generate_signing_keypair()
    monkeypatch.setattr(
        identity_backfill, "ensure_agent_signing_keys",
        lambda aid, **k: (priv, pub, f"did:web:host:agents:{aid}"),
    )
    authoring.author_api_spec(
        agent_id="agent-x", owner_id="o", page_url="https://example.com/x",
        capture={"url": "https://api.example.com/x", "method": "GET", "json": {"a": 1}},
    )
    # Verification now resolves a DIFFERENT key -> the stored signature fails -> the
    # spec is not returned for reuse (a tampered/forged spec can't be replayed).
    other_priv, other_pub = crypto.generate_signing_keypair()
    monkeypatch.setattr(
        identity_backfill, "ensure_agent_signing_keys",
        lambda aid, **k: (other_priv, other_pub, "did"),
    )
    assert authoring.find_reusable_api_spec("https://example.com/x") is None


# --------------------------------------------------------------------------- royalties (Phase F)
def test_record_map_royalty_obligation_records_and_moves_no_money(monkeypatch):
    from core.site_maps import payouts
    seen: dict = {}
    monkeypatch.setattr(payouts.store, "record_usage", lambda **k: (seen.update(k) or {"usage_id": "u1"}))
    out = payouts.record_map_royalty_obligation(
        consumer_job_id="job1", site_key="s", royalty_cents=2,
        author_owner_id="author", consumer_owner_id="consumer", map_id="m1",
    )
    assert out == "u1" and seen["royalty_cents"] == 2
    # The module imports no payments primitive — it records the payable, never moves money.
    assert not hasattr(payouts, "payments")


def test_record_map_royalty_obligation_idempotent(monkeypatch):
    from core.site_maps import payouts
    monkeypatch.setattr(payouts.store, "record_usage", lambda **k: None)  # already recorded
    assert payouts.record_map_royalty_obligation(
        consumer_job_id="job1", site_key="s", royalty_cents=2,
        author_owner_id="a", consumer_owner_id="c",
    ) is None


def test_record_map_royalty_obligation_skips_nonpositive(monkeypatch):
    from core.site_maps import payouts
    monkeypatch.setattr(payouts.store, "record_usage", lambda **k: pytest.fail("must not record for 0 cents"))
    assert payouts.record_map_royalty_obligation(
        consumer_job_id="j", site_key="s", royalty_cents=0, author_owner_id="a", consumer_owner_id="c",
    ) is None
