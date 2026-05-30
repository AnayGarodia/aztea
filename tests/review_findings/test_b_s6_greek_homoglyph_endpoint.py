"""Finding B-S6 (2026-05-30 review): the endpoint-host anti-spoof fold
(_HOMOGLYPH_FOLD) carried only Cyrillic look-alikes, so a host using a Greek
look-alike (e.g. omicron 'ο' for Latin 'o') bypassed the "you registered against
aztea.ai itself" check.

After the fix, _HOMOGLYPH_FOLD includes the Greek rows (parity with the phrase
fold), so a Greek-homoglyph aztea.ai impersonation is caught.
"""

from __future__ import annotations

from core.listing_safety import LEVEL_BLOCK, scan_agent_md_endpoint


def _is_blocked_as_aztea(url: str) -> bool:
    findings = scan_agent_md_endpoint(url)
    return any(
        f.code == "manifest.endpoint_is_aztea" and f.level == LEVEL_BLOCK
        for f in findings
    )


def test_greek_alpha_aztea_host_is_blocked():
    # 'azteα.ai' — the trailing 'a' in 'aztea' is Greek alpha U+03B1, which
    # folds to Latin 'a'. ('aztea' has no o/p/v, so alpha is the live vector.)
    spoof = "https://azteα.ai/agent"
    assert _is_blocked_as_aztea(spoof), "Greek-homoglyph aztea.ai spoof not caught"


def test_greek_rho_and_alpha_fold():
    # Direct fold check: Greek ρ→p, α→a, ν→v, ο→o present in the table.
    from core.listing_safety import _HOMOGLYPH_FOLD
    for greek, latin in (("α", "a"), ("ο", "o"), ("ρ", "p"), ("ν", "v")):
        assert greek.translate(_HOMOGLYPH_FOLD) == latin


def test_plain_latin_aztea_still_blocked():
    # Control: the ordinary host must still be caught (no regression).
    assert _is_blocked_as_aztea("https://aztea.ai/x")


def test_cyrillic_homoglyph_still_blocked():
    # Control: Cyrillic 'е' (U+0435) in aztea was already covered.
    assert _is_blocked_as_aztea("https://aztеa.ai/x")


def test_unrelated_host_not_blocked():
    assert not _is_blocked_as_aztea("https://example.com/agent")
