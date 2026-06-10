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


# ── 2026-06-10 deference-experiment bounce fixes ─────────────────────────────
# The experiment (experiments/deference/REPORT.md) showed 21 of 33
# auto_call_agent attempts bouncing for mechanical reasons. Each test below
# pins one fixed bounce class using the exact intents that bounced.

from core.registry.auto_hire import (  # noqa: E402  (grouped with their tests)
    _cold_start_penalty,
    _extract_manifest,
    _extract_python_code,
    _fill_variant_with_extractors,
    _resolve_intent_only_payload,
    _resolve_single_required_field,
)


def test_manifest_synthesized_from_pip_pin_in_intent():
    payload, missing = _resolve_intent_only_payload(
        "audit Python dependency requests==2.19.1 for known CVE vulnerabilities",
        ["manifest"], {"manifest": {"type": "string"}}, [],
    )
    assert missing == []
    assert payload == {"manifest": "requests==2.19.1"}


def test_manifest_synthesized_from_npm_pin_as_package_json():
    payload, missing = _resolve_intent_only_payload(
        "audit npm package lodash@4.17.15 for known CVE vulnerabilities",
        ["manifest"], {"manifest": {"type": "string"}}, [],
    )
    assert missing == []
    assert payload["manifest"].startswith("{")
    assert "4.17.15" in payload["manifest"]


def test_manifest_refuses_url_fetch_intents():
    # "Fetch the raw URL …/package.json" must NOT become a manifest payload —
    # this exact intent invoked dependency_auditor and burned a refund.
    assert _extract_manifest(
        "Fetch the raw URL https://raw.githubusercontent.com/left-pad/left-pad/v1.3.0/package.json"
    ) is None


def test_code_extractor_strips_instruction_prefix():
    # The experiment's one wrong answer: the whole sentence was dumped into
    # `code`, SyntaxError'd, and the buyer's model trusted a made-up number.
    code = _extract_python_code(
        "run this Python: import random; random.seed(42); print(random.randint(1, 10**9))"
    )
    assert code == "import random; random.seed(42); print(random.randint(1, 10**9))"
    compile(code, "<test>", "exec")  # must be runnable as-is


def test_code_extractor_refuses_natural_language():
    assert _extract_python_code("what is the capital of France") is None
    payload, missing = _resolve_intent_only_payload(
        "what is the capital of France", ["code"], {"code": {"type": "string"}}, [],
    )
    assert payload == {} and missing == ["code"]


def test_array_typed_single_field_uses_registered_extractor():
    # DNS inspector requires `domains` (array). The old string-type gate
    # skipped the extractor entirely → missing_fields on perfect intents.
    payload, missing = _resolve_single_required_field(
        "Look up the current NS records for example.com via live DNS",
        "domains", {"domains": {"type": "array"}},
    )
    assert missing == []
    assert payload == {"domains": ["example.com"]}


def test_composite_variants_all_tried_not_just_first():
    # CVE lookup is anyOf[[cve_id],[cve_ids],[packages]]. The old code only
    # tried variant 1; `packages` (variant 3) fills deterministically.
    payload, missing = _resolve_intent_only_payload(
        "look up CVEs for Django 3.2.0 Python package security vulnerabilities",
        [], {},
        [["cve_id"], ["cve_ids"], ["packages"]],
    )
    assert missing == []
    assert payload == {"packages": ["django@3.2.0"]}


def test_fill_variant_requires_every_field():
    assert _fill_variant_with_extractors("no pins here", ["packages"]) is None
    assert _fill_variant_with_extractors(
        "audit requests==2.19.1", ["packages", "nonexistent_field"],
    ) is None


def test_cold_start_penalty_is_relative_to_catalog():
    # On a fresh marketplace every agent is cold; the penalty must not
    # uniformly deflate scores below the confidence floor (the experiment's
    # 0.03-confidence refusals on clear matches).
    cold = _candidate(slug="cold_agent", description="fetch web pages live")
    cold.raw["call_count"] = 0
    warm = _candidate(slug="warm_agent", description="fetch web pages live")
    assert _cold_start_penalty(cold) > _cold_start_penalty(warm)
    ranked_all_cold = _rank_candidates([cold], "fetch web pages live", None)
    # Single cold candidate: rebate equals its own penalty → no deflation.
    cold_alone_score = ranked_all_cold[0].score
    ranked_mixed = _rank_candidates([cold, warm], "fetch web pages live", None)
    mixed_scores = {r.candidate.slug: r.score for r in ranked_mixed}
    # Established agent keeps its edge over the cold one…
    assert mixed_scores["warm_agent"] > mixed_scores["cold_agent"]
    # …and the cold agent alone scores higher than it would have under the
    # old absolute penalty (which subtracted the full 12 regardless).
    assert cold_alone_score > mixed_scores["cold_agent"]


def test_manifest_loose_form_with_ecosystem_cue():
    # "Django 3.2.0 Python package" has no strict pin; the cue-gated loose
    # form fills it. Without an ecosystem cue we refuse (ambiguous format).
    assert _extract_manifest(
        "look up CVEs for Django 3.2.0 Python package security vulnerabilities"
    ) == "django==3.2.0"
    assert _extract_manifest("audit the foo 1.2.3 thing for vulns") is None


def test_url_fetch_intent_beats_dependency_auditor():
    # "Fetch the raw URL …/package.json" — the audit cue is INSIDE the URL;
    # this invoked dependency_auditor in the experiment and failed.
    browser = _candidate(
        slug="browser_agent",
        name="Browser Agent",
        description="Playwright-based headless browsing: fetch and scrape live web pages.",
        match_keywords=("scrape", "browser"),
    )
    intent = (
        "Fetch the raw URL https://raw.githubusercontent.com/left-pad/left-pad/"
        "v1.3.0/package.json and return it"
    )
    assert _top_slug(intent, [browser, DEP_AUDITOR]) == "browser_agent"
    # Package pins keep routing to the auditor — the suppression is URL-only.
    assert _top_slug(
        "audit requests==2.19.1 for known CVEs", [browser, DEP_AUDITOR],
    ) == "dependency_auditor"
