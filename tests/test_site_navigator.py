"""Unit tests for the site_navigator agent (Phase 0 of the agent-readable-web build).

Covers the pure helpers (a11y flattening, affordance grouping, value-stripped
fingerprint), the structured error envelopes, and the graceful-degradation path
when no LLM provider is configured. The live Playwright render is exercised by
the integration suite, not here.
"""

from __future__ import annotations

from agents import site_navigator as sn


def test_missing_url_returns_structured_error():
    out = sn.run({"goal": "anything"})
    assert out["error"]["code"] == "site_navigator.missing_url"


def test_missing_goal_returns_structured_error():
    out = sn.run({"url": "https://example.com"})
    assert out["error"]["code"] == "site_navigator.missing_goal"


def test_non_http_scheme_is_blocked():
    # url_security rejects non-http(s) schemes regardless of the dev
    # ALLOW_PRIVATE_OUTBOUND_URLS override, so this is env-independent.
    out = sn.run({"url": "file:///etc/passwd", "goal": "read it"})
    assert out["error"]["code"] == "site_navigator.url_blocked"


def test_parse_aria_snapshot_extracts_roles_and_drops_prose():
    text = (
        '- heading "Example Domain" [level=1]\n'
        '- paragraph: prose with no quoted name\n'
        '- link "Learn more":\n'
        '    - /url: https://iana.org/x\n'
    )
    rows = sn._parse_aria_snapshot(text)
    pairs = {(r["role"], r["name"]) for r in rows}
    assert ("heading", "Example Domain") in pairs
    assert ("link", "Learn more") in pairs
    assert all(r["role"] != "paragraph" for r in rows)       # prose dropped (no quoted name)
    assert all(not r["role"].startswith("/") for r in rows)  # '- /url:' dropped


def test_parse_aria_snapshot_keeps_unnamed_inputs():
    rows = sn._parse_aria_snapshot('- searchbox\n- textbox "Email"')
    roles = {r["role"] for r in rows}
    assert "searchbox" in roles and "textbox" in roles  # unnamed input still actionable


def test_extract_affordances_groups_by_role():
    rows = [
        {"role": "link", "name": "Docs"},
        {"role": "button", "name": "Sign up"},
        {"role": "searchbox", "name": ""},
        {"role": "heading", "name": "Pricing"},
        {"role": "paragraph", "name": "ignored"},
    ]
    aff = sn._extract_affordances(rows)
    assert aff["links"] == ["Docs"]
    assert aff["buttons"] == ["Sign up"]
    assert aff["headings"] == ["Pricing"]
    assert aff["inputs"] and aff["inputs"][0].startswith("<")  # unnamed → placeholder


def test_dom_fingerprint_is_value_independent_but_structure_sensitive():
    a = [{"role": "link", "name": "Buy now"}, {"role": "heading", "name": "Sale"}]
    # Same structure (roles), different names/values → identical fingerprint.
    b = [{"role": "link", "name": "Different"}, {"role": "heading", "name": "Other"}]
    # Different structure (extra node) → different fingerprint.
    c = [{"role": "link", "name": "x"}]
    url = "https://example.com"
    assert sn._dom_fingerprint(url, a) == sn._dom_fingerprint(url, b)
    assert sn._dom_fingerprint(url, a) != sn._dom_fingerprint(url, c)


def test_resolve_goal_degrades_when_no_provider(monkeypatch):
    monkeypatch.setattr(sn, "llm_complete", lambda *a, **k: None)
    result, used = sn._resolve_goal("goal", "https://x", [], {})
    assert result is None and used is False


def test_resolve_goal_parses_json(monkeypatch):
    monkeypatch.setattr(sn, "llm_complete", lambda *a, **k: '{"tiers": [1, 2]}')
    result, used = sn._resolve_goal("goal", "https://x", [], {})
    assert result == {"tiers": [1, 2]} and used is True


def test_resolve_goal_tags_non_json_text(monkeypatch):
    monkeypatch.setattr(sn, "llm_complete", lambda *a, **k: "not json at all")
    result, used = sn._resolve_goal("goal", "https://x", [], {})
    # Non-JSON output is tagged so callers can tell it apart from a structured answer.
    assert result == {"_unstructured": True, "text": "not json at all"} and used is True


def test_is_chromium_missing_detects_both_signals():
    assert sn._is_chromium_missing(Exception("Executable doesn't exist at /x")) is True
    assert sn._is_chromium_missing(Exception("run: playwright install chromium")) is True
    assert sn._is_chromium_missing(Exception("some unrelated failure")) is False


def test_parse_aria_snapshot_bounds_work():
    # A giant aria snapshot must not blow up: bounded by the row cap.
    big = "\n".join('- link "x"' for _ in range(sn._AX_NODE_CAP + 2_000))
    rows = sn._parse_aria_snapshot(big)
    assert len(rows) <= sn._AX_NODE_CAP


_PRICE_SCHEMA = {"type": "object", "properties": {"price": {"type": "number"}}, "required": ["price"]}


def test_invalid_schema_returns_structured_error():
    out = sn.run({"url": "https://example.com", "goal": "x", "schema": "not-a-dict"})
    assert out["error"]["code"] == "site_navigator.invalid_schema"
    bad = sn.run({"url": "https://example.com", "goal": "x", "schema": {"type": "not-a-real-type"}})
    assert bad["error"]["code"] == "site_navigator.invalid_schema"


def test_resolve_schema_passes_when_conforming(monkeypatch):
    monkeypatch.setattr(sn, "llm_complete", lambda *a, **k: '{"price": 20}')
    result, used = sn._resolve_goal("g", "https://x", [], {}, _PRICE_SCHEMA)
    assert used is True and result == {"price": 20}


def test_resolve_schema_retries_once_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return '{"price": "twenty"}' if calls["n"] == 1 else '{"price": 20}'  # 1st invalid, 2nd valid

    monkeypatch.setattr(sn, "llm_complete", fake)
    result, used = sn._resolve_goal("g", "https://x", [], {}, _PRICE_SCHEMA)
    assert calls["n"] == 2 and result == {"price": 20}


def test_resolve_schema_returns_extraction_failed_marker(monkeypatch):
    # Both the first answer and the retry violate the schema -> typed marker, not silent prose.
    monkeypatch.setattr(sn, "llm_complete", lambda *a, **k: '{"price": "still a string"}')
    result, used = sn._resolve_goal("g", "https://x", [], {}, _PRICE_SCHEMA)
    assert used is True and result["_extraction_failed"] is True and "raw" in result


def test_build_result_shape_and_degraded_flag(monkeypatch):
    monkeypatch.setattr(sn, "llm_complete", lambda *a, **k: None)
    rows = [{"role": "heading", "name": "Pricing"}, {"role": "link", "name": "Docs"}]
    out = sn._build_result(
        url="https://example.com", requested_url="https://example.com",
        goal="list tiers", title="Pricing", rows=rows, elapsed_ms=1234,
    )
    assert out["result"] is None
    assert out["degraded_mode"] is True
    assert out["llm_used"] is False
    assert out["source"] == "fresh"
    assert out["reuse"]["reused"] is False
    assert out["reuse"]["source"] == "fresh"
    assert out["reuse"]["commons_map_available"] is False  # no prior_map passed
    assert out["modality_used"] == "accessibility_tree"
    assert out["site_map"]["schema"] == "aztea/site-map/2"
    assert out["site_map"]["node_count"] == 2
    assert out["site_map"]["affordances"]["headings"] == ["Pricing"]
    assert out["site_map"]["graph"]["schema"] == "aztea/site-map/2"  # Phase 3 nav graph
    assert out["modality_recommended"] in ("accessibility_tree", "screenshot")
