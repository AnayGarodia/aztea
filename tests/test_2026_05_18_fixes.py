"""Regression tests for the 2026-05-18 user test report surgical fixes.

Each test names the user-reported defect and asserts the fix.  Kept in
its own file (rather than appended to tests/test_bug_regressions.py
which is already over the 1000-line budget) so it can be deleted
wholesale once the defects are obsolete.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# A1 — dependency_auditor: stop fabricating CVSS, surface parse warnings,
#                          flag bad inputs.
# ---------------------------------------------------------------------------


def test_a1_cvss_bucket_fabrication_removed():
    """The label-only OSV path no longer fabricates 5.5/7.5/9.5 CVSS scores."""
    from agents import dependency_auditor as da
    vuln = {
        "id": "GHSA-fake",
        "aliases": ["CVE-2099-9999"],
        "database_specific": {"severity": "HIGH"},
        "affected": [],
    }
    record = da._osv_vuln_to_cve(vuln)
    assert record["severity"] == "high"
    assert record["cvss"] is None, (
        "CVSS must be null when only a severity label is provided — "
        "fabricating a midpoint score is a trust violation."
    )


def test_a1_dependency_auditor_surfaces_parse_warnings_and_flags():
    """Bad inputs surface as structured warnings + flagged actions."""
    from agents import dependency_auditor as da

    def fake_fetch_pypi(name: str):
        if "fake-pkg" in name:
            return None, None, True   # not_found
        if name == "requests":
            return "2.31.0", "Apache-2.0", False
        if name == "django":
            return "5.0", "BSD", False
        return None, None, False

    with patch.object(da, "_osv_fetch", lambda *a, **k: []), \
         patch.object(da, "_fetch_pypi_latest", fake_fetch_pypi):
        result = da.run({
            "manifest": (
                "fake-pkg-xyz==1.0\n"
                "-e git+https://x\n"
                "requests==2.0\n"
                "requests==2.1\n"
                "django>=99.0.0\n"
            ),
            "ecosystem": "pypi",
            "checks": ["cve", "outdated", "license"],
        })

    warnings = result["parse_warnings"]
    assert any(w.get("reason") == "unparseable" for w in warnings), (
        f"parse_warnings should flag the '-e git+...' line: {warnings}"
    )
    assert any(w.get("reason") == "duplicate_entry" for w in warnings), (
        f"parse_warnings should flag duplicate 'requests' entries: {warnings}"
    )

    actions = {p["name"]: p["action"] for p in result["packages"]}
    assert actions["fake-pkg-xyz"] == "not_found"
    assert actions["django"] == "version_unreachable"


def test_a1_license_classifiers_fallback():
    """When info[license] is empty, fall back to PyPI classifiers."""
    from agents import dependency_auditor as da
    cls = ["License :: OSI Approved :: MIT License"]
    assert da._license_from_classifiers(cls) == "MIT License"


# ---------------------------------------------------------------------------
# A2 — browser_agent: image dimensions, script return values, wait_ms cap.
# ---------------------------------------------------------------------------


def test_a2_png_dimensions_extracted_from_header():
    """A real PNG's width/height are extractable without decoding."""
    import struct
    from agents.browser_agent import _png_dimensions, _bytes_to_artifact
    # Minimal valid PNG: signature + IHDR length(13) + "IHDR" + w + h + ...
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", 1280, 720) + b"\x08\x06\x00\x00\x00"
    png_bytes = sig + b"\x00\x00\x00\x0dIHDR" + ihdr_data + b"\x00" * 4
    w, h = _png_dimensions(png_bytes)
    assert w == 1280 and h == 720

    artifact = _bytes_to_artifact("test.png", "image/png", png_bytes)
    assert artifact["width"] == 1280
    assert artifact["height"] == 720


def test_a2_max_wait_ms_aligned_with_sync_budget():
    """wait_ms cap should not exceed the 8s sync budget."""
    from agents import browser_agent
    assert browser_agent._MAX_WAIT_MS <= 6_000, (
        f"_MAX_WAIT_MS={browser_agent._MAX_WAIT_MS} risks 504s under "
        "the 8s sync budget"
    )


def test_a2_script_result_serializer_handles_unserializable():
    """Script return values round-trip safely; unserializable values become repr."""
    from agents.browser_agent import _serialize_script_result
    assert _serialize_script_result(None) is None
    assert _serialize_script_result("Example Domain") == "Example Domain"
    assert _serialize_script_result({"x": 1}) == {"x": 1}
    # Non-JSON-serializable type goes through default=str.
    class Weird:
        def __str__(self) -> str:
            return "WEIRD_OBJ"
    out = _serialize_script_result(Weird())
    assert isinstance(out, str) and "WEIRD" in out


# ---------------------------------------------------------------------------
# A3 — pdf_document_parser: page-1 title fallback.
# ---------------------------------------------------------------------------


def test_a3_first_nonempty_line_within_bounds():
    """Plain-text page-1 fallback picks the first reasonable line."""
    from agents.pdf_document_parser import _first_nonempty_line
    text = "\n\n   \nAttention Is All You Need\n\nAshish Vaswani et al."
    assert _first_nonempty_line(text) == "Attention Is All You Need"


def test_a3_attach_title_source_marks_embedded_when_present():
    """Embedded title is preserved and labeled 'embedded'."""
    from agents.pdf_document_parser import _attach_title_source
    meta: dict = {"title": "Pre-existing Title", "title_source": None}
    _attach_title_source(meta, doc=object())
    assert meta["title"] == "Pre-existing Title"
    assert meta["title_source"] == "embedded"


# ---------------------------------------------------------------------------
# A5 — avg_latency_ms decay constants present + 0.9/day.
# ---------------------------------------------------------------------------


def test_a5_latency_decay_constants_defined():
    """The surgical decay constants live alongside reputation decay."""
    from server.application_parts import part_000
    assert part_000._LATENCY_DECAY_DAILY_MULTIPLIER == 0.9
    assert part_000._LATENCY_DECAY_GRACE_DAYS == 7


# ---------------------------------------------------------------------------
# A6 — dispute deposit floor raised to 25¢.
# ---------------------------------------------------------------------------


def test_a6_dispute_deposit_floor_is_25_cents():
    from server.application_parts import part_000
    # On a free or sub-25¢ call, the 5% bps yields 0¢ and gets clamped to
    # the new 25¢ floor (was 5¢).
    assert part_000._compute_dispute_filing_deposit_cents(0) == 25
    assert part_000._compute_dispute_filing_deposit_cents(1) == 25
    # On a $1 call (10000¢), the 5% bps yields 50¢ and exceeds the floor.
    assert part_000._compute_dispute_filing_deposit_cents(10_000) == 500


# ---------------------------------------------------------------------------
# B2 — picker rebalance constants.
# ---------------------------------------------------------------------------


def test_b2_semantic_outweighs_keyword_cap():
    from core.registry.auto_hire import (
        _KEYWORD_MATCH_CAP, _SEMANTIC_BONUS_MAX,
    )
    assert _SEMANTIC_BONUS_MAX > _KEYWORD_MATCH_CAP


def test_b2_anti_catchall_constant_exists():
    from core.registry import auto_hire
    assert auto_hire._CATCHALL_PENALTY > 0
    assert 0 < auto_hire._CATCHALL_RATE_THRESHOLD < 1


# ---------------------------------------------------------------------------
# B4 — new agents register and dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,sample_input,expected_keys", [
    (
        "regex_tester",
        {"pattern": r"\d+", "test_string": "abc 123 def 456"},
        ["pattern", "results"],
    ),
    (
        "sbom_generator",
        {"manifest_content": "requests==2.28.0", "manifest_type": "requirements.txt"},
        ["bom_format", "spec_version", "components", "component_count"],
    ),
    (
        # C-1 (audit 2026-05-19): tokens with header alg=none must be
        # refused before decoding completes — they were previously
        # returning ``signature_valid: null, errors: []`` which downstream
        # code may treat as "no problem". Use an HS256-header token here
        # to smoke-test the happy path; alg=none refusal is exercised
        # separately in tests/test_jwt_alg_none_refusal.py.
        "jwt_validator",
        {"token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
        ["header", "payload", "verified_with"],
    ),
])
def test_b4_new_agents_return_expected_keys(slug, sample_input, expected_keys):
    """Each new agent's run() returns the documented shape on a minimal call."""
    import importlib
    mod = importlib.import_module(f"agents.{slug}")
    result = mod.run(sample_input)
    for key in expected_keys:
        assert key in result, f"{slug}: missing {key!r} in {result.keys()}"


def test_b4_curated_set_includes_new_agents():
    """The curated catalog now lists the six new specialists."""
    from server.builtin_agents.constants import (
        CURATED_PUBLIC_BUILTIN_AGENT_IDS,
        REGEX_TESTER_AGENT_ID, JWT_VALIDATOR_AGENT_ID, SBOM_GENERATOR_AGENT_ID,
        PYPI_METADATA_AGENT_ID, GITHUB_RELEASES_AGENT_ID,
        HCL_TERRAFORM_ANALYZER_AGENT_ID,
    )
    for agent_id in (
        REGEX_TESTER_AGENT_ID, JWT_VALIDATOR_AGENT_ID, SBOM_GENERATOR_AGENT_ID,
        PYPI_METADATA_AGENT_ID, GITHUB_RELEASES_AGENT_ID,
        HCL_TERRAFORM_ANALYZER_AGENT_ID,
    ):
        assert agent_id in CURATED_PUBLIC_BUILTIN_AGENT_IDS
