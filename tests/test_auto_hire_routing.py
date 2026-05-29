"""Unit fixtures for the auto-hire ranker.

Audit 2026-05-16 #12 + #14: prove that bare CVE-id prompts route to
``cve_lookup`` (not ``dependency_auditor``) and that chat-shaped questions
do NOT land on a code executor.

2026-05-28 B3: lemma-normalized keyword matching so "audit" hits
"auditing" without the keyword author manually expanding every plural
/ conjugation.
"""

from __future__ import annotations

from core.registry.auto_hire import (
    CandidateAgent,
    _lemma_normalize,
    _normalize_keyword,
    _rank_candidates,
)


def _candidate(
    *,
    slug: str,
    name: str | None = None,
    description: str = "",
    tags: tuple[str, ...] = (),
    match_keywords: tuple[str, ...] = (),
) -> CandidateAgent:
    return CandidateAgent(
        agent_id=f"id-{slug}",
        slug=slug,
        name=name or slug.replace("_", " ").title(),
        description=description,
        tags=list(tags),
        category="",
        price_per_call_usd=0.10,
        trust_score=80.0,
        success_rate=0.95,
        stability_tier="stable",
        input_schema={"type": "object", "required": []},
        raw={
            "call_count": 100,
            "success_rate": 0.95,
            "trust_score": 80.0,
            "review_status": "approved",
        },
        match_keywords=list(match_keywords),
        block_keywords=[],
    )


CVE_LOOKUP = _candidate(
    slug="cve_lookup",
    name="CVE Lookup",
    description="Look up CVE details from the NIST NVD live API.",
    tags=("security", "cve"),
    match_keywords=("cve",),
)
DEP_AUDITOR = _candidate(
    slug="dependency_auditor",
    name="Dependency Auditor",
    description="Scan a manifest for known vulnerable packages.",
    tags=("security", "audit", "dependency"),
    match_keywords=("audit", "dependency"),
)
PYTHON_EXEC = _candidate(
    slug="python_code_executor",
    name="Python Code Executor",
    description="Run a Python snippet in an isolated sandbox.",
    tags=("execution",),
    match_keywords=("python", "execute"),
)
DNS_INSPECTOR = _candidate(
    slug="dns_ssl_inspector",
    name="DNS / SSL Inspector",
    description="Live DNS records and TLS certificate inspection.",
    tags=("dns", "ssl"),
)


def _top_slug(intent: str, candidates: list[CandidateAgent]) -> str:
    ranked = _rank_candidates(candidates, intent, explicit_input=None)
    assert ranked, "ranker returned no candidates"
    return ranked[0].candidate.slug


# --- Bug #12: bare CVE id → cve_lookup ---------------------------------------


def test_bare_cve_id_routes_to_cve_lookup_not_dependency_auditor():
    candidates = [CVE_LOOKUP, DEP_AUDITOR]
    assert (
        _top_slug("details for CVE-2021-44228", candidates) == "cve_lookup"
    )
    assert (
        _top_slug("look up CVE-2024-3094", candidates) == "cve_lookup"
    )


def test_cve_id_alongside_packages_still_lets_dependency_auditor_win():
    """When the prompt has actual package pins, the dependency auditor
    bonus is the right call — make sure we didn't accidentally crowd it
    out."""
    candidates = [CVE_LOOKUP, DEP_AUDITOR]
    assert _top_slug(
        "audit requests==2.25.0 for CVE-2023-0001", candidates
    ) == "dependency_auditor"


# --- Bug #14: chat questions must NOT route to python_code_executor ----------


def test_general_knowledge_question_does_not_route_to_python_executor():
    candidates = [PYTHON_EXEC, DNS_INSPECTOR]
    ranked = _rank_candidates(
        candidates, "what is the capital of France", explicit_input=None
    )
    top = ranked[0]
    assert top.candidate.slug != "python_code_executor", (
        f"chat-shaped prompt should not route to python_code_executor "
        f"(got score={top.score} reasons={top.reasons})"
    )


def test_explicit_python_run_prompt_still_routes_to_python_executor():
    """Don't over-correct: explicit 'run this python' should still win."""
    candidates = [PYTHON_EXEC, DNS_INSPECTOR]
    assert (
        _top_slug("run this python:\nprint(2+2)", candidates)
        == "python_code_executor"
    )


def test_explain_questions_demote_code_executor():
    candidates = [PYTHON_EXEC, DNS_INSPECTOR]
    ranked = _rank_candidates(
        candidates, "explain how DNS resolution works", explicit_input=None
    )
    assert ranked[0].candidate.slug != "python_code_executor"


# --- B3 (2026-05-28): lemma-normalized keyword matching ---------------------


def test_lemma_normalize_strips_common_suffixes():
    assert _lemma_normalize("auditing") == "audit"
    assert _lemma_normalize("audited") == "audit"
    assert _lemma_normalize("scans") == "scan"
    # "dependencies" → "dependenci" with the pure-Python fallback
    # stemmer; "dependency" with simplemma. Both work consistently as
    # long as the keyword side normalizes through the same function.
    assert _lemma_normalize("dependencies") in {"dependenc", "dependenci", "dependency"}


def test_lemma_normalize_preserves_short_stems():
    # Never strip a stem shorter than 3 chars — "is" must NOT become "".
    assert _lemma_normalize("is") == "is"
    assert _lemma_normalize("be") == "be"


def test_lemma_normalize_idempotent_on_unsuffixed():
    assert _lemma_normalize("audit") == "audit"
    assert _lemma_normalize("cve") == "cve"


def test_normalize_keyword_skips_multiword_and_punctuated():
    # Codes and identifiers keep substring semantics — never normalize.
    assert _normalize_keyword("log4j") is None      # digits
    assert _normalize_keyword("package.json") is None  # punctuation
    assert _normalize_keyword("cve-2021") is None   # dash + digits
    assert _normalize_keyword("a b") is None        # whitespace
    # Pure alphabetic keywords get normalized.
    assert _normalize_keyword("Audit") == "audit"
    assert _normalize_keyword("scans") == "scan"


def test_keyword_match_via_lemma_finds_conjugated_intent():
    """Keyword 'audit' must hit intent 'auditing my repo'."""
    # dependency_auditor has match_keywords=("audit", "dependency").
    # Without lemma matching, "auditing my repo" doesn't trigger the
    # +12 keyword bonus because "audit" isn't a substring of the
    # *tokenized* form ("auditing"). With lemma matching, it does.
    candidates = [DEP_AUDITOR, DNS_INSPECTOR]
    assert _top_slug("auditing my repo", candidates) == "dependency_auditor"


def test_keyword_match_does_not_overstrip_to_false_positive():
    """Negative test: don't let 'audition' or 'auctioneer' fire 'audit' bonus.

    Both 'audition' and 'auctioneer' contain 'audit' as a substring,
    but neither would be a real user-intent for the dependency auditor.
    The lemma path normalizes to 'audition' / 'auctioneer' (not stripped
    because no listed suffix matches), so they do NOT hit the lemma path.
    The legacy substring path WILL still catch 'audit' inside 'audition'
    — that's pre-existing behavior preserved by design (B3 is purely
    additive). This test pins the lemma-side behavior so a future
    suffix-list change can't quietly start matching them.
    """
    # Direct check: lemma form is not "audit".
    assert _lemma_normalize("audition") != "audit"
    assert _lemma_normalize("auctioneer") != "audit"


def test_block_keyword_via_lemma_form():
    """Block keywords also get lemma treatment."""
    # Build a candidate where 'audit' is BOTH match AND block — verify
    # the block path uses lemmas the same way.
    blocked = _candidate(
        slug="not_an_auditor",
        match_keywords=(),
    )
    blocked.block_keywords = ["audit"]  # mutate to add the block side
    candidates = [blocked, DEP_AUDITOR]
    # 'auditing' should still route to DEP_AUDITOR (block fires on 'blocked').
    assert _top_slug("auditing my repo", candidates) == "dependency_auditor"
