"""Phase 5 (C5): compound intent detection tests.

Positive: detected as compound, decomposed correctly.
Negative: not detected (single intent, preserved).
Integration: decide() refuses compound intents with reason='compound_intent'.
"""

from __future__ import annotations

import pytest

from core.registry import auto_hire as ah
from core.registry.compound_intent import (
    CompoundIntent,
    RecipeMatch,
    detect_compound,
    match_recipes,
)
from tests.test_auto_hire_routing import DEP_AUDITOR, PYTHON_EXEC


# --- Positive cases (compound) ------------------------------------------


@pytest.mark.parametrize("intent,expected_steps", [
    (
        "audit my requirements.txt and then post a summary to slack",
        2,
    ),
    (
        "scan this for AWS keys, then audit the dependencies",
        2,
    ),
    (
        "lint this Dockerfile and validate the kubernetes manifest",
        2,
    ),
    (
        "screenshot the homepage, then run the accessibility audit",
        2,
    ),
    (
        "audit my requirements.txt then run the lint then post to slack",
        3,
    ),
    (
        "decode this JWT, then verify the signature, then notify oncall",
        3,
    ),
])
def test_detects_compound_intent(intent: str, expected_steps: int):
    compound = detect_compound(intent)
    assert compound is not None, f"should detect compound: {intent!r}"
    assert len(compound.steps) == expected_steps


# --- Negative cases (single intent) -------------------------------------


@pytest.mark.parametrize("intent", [
    "audit my package.json for vulnerabilities",
    "look up CVE-2021-44228",
    "run this Python: print(1+1)",
    "what's the DNS for github.com",
    "lint this Dockerfile",
    "scan this file for AWS keys",
    "and",        # nonsense — too short
    "audit",      # single verb, no clauses
    "audit my package.json and CVE-2021-44228",  # AND between noun+noun, no second imperative
    "is the cert for google.com expiring soon",
])
def test_rejects_single_intent(intent: str):
    assert detect_compound(intent) is None, (
        f"should NOT detect compound: {intent!r}"
    )


# --- Recipe matching ----------------------------------------------------


def test_match_recipes_scores_overlap():
    compound = CompoundIntent(
        steps=("scan the source for secrets", "audit the dependency manifest"),
        method="splitter",
    )
    recipes = [
        {
            "recipe_id": "secret-scan-and-audit",
            "name": "secret-scan-and-audit",
            "description": "Scan source for leaked credentials, then audit dependencies.",
        },
        {
            "recipe_id": "audit-deps",
            "name": "audit-deps",
            "description": "Audit a dependency manifest for known CVEs.",
        },
        {
            "recipe_id": "domain-health",
            "name": "domain-health",
            "description": "DNS and SSL cert inspection.",
        },
    ]
    matches = match_recipes(compound, recipes)
    assert len(matches) == 2
    # secret-scan-and-audit should outscore audit-deps (covers both steps).
    assert matches[0].recipe_id == "secret-scan-and-audit"
    assert matches[0].score >= matches[1].score


def test_match_recipes_returns_empty_when_nothing_overlaps():
    compound = CompoundIntent(
        steps=("xyz qqq", "zzz mmm"), method="splitter",
    )
    recipes = [{"recipe_id": "audit-deps", "name": "a", "description": "b"}]
    assert match_recipes(compound, recipes) == []


# --- Integration with decide() ------------------------------------------


def test_decide_refuses_compound_intent():
    decision = ah.decide(
        intent="audit my requirements.txt and then post to slack",
        explicit_input=None,
        max_cost_usd=0.10,
        candidates=[DEP_AUDITOR, PYTHON_EXEC],
    )
    assert decision.auto_invoked is False
    assert decision.reason == "compound_intent"
    # Best-effort: at least one recipe candidate or a build-pipeline hint.
    assert decision.next_step is not None


def test_decide_skips_compound_check_when_explicit_input_provided():
    """If caller passed structured input, they own the decomposition."""
    decision = ah.decide(
        intent="audit my requirements.txt and then post to slack",
        explicit_input={"manifest": "requests==2.28.0"},
        max_cost_usd=0.10,
        candidates=[DEP_AUDITOR],
    )
    # Should attempt to route to dep_auditor (not refuse as compound).
    assert decision.reason != "compound_intent"


def test_decide_single_intent_still_routes_normally():
    """Negative regression: don't refuse single intents as compound."""
    decision = ah.decide(
        intent="audit my requirements.txt for CVEs",
        explicit_input=None,
        max_cost_usd=0.10,
        candidates=[DEP_AUDITOR],
    )
    assert decision.reason != "compound_intent"
