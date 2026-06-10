# OWNS: Phase 5 (C5) — detect compound user intents like "audit my repo
#       and post findings to Slack" and route them to a manage_workflow
#       recipe instead of forcing a single specialist pick.
# NOT OWNS: actually executing pipelines (core/pipelines/executor.py);
#       recipe registration (core/recipes.py); the single-intent
#       scoring path (core/registry/auto_hire.py).
# INVARIANTS:
#   - Pure: detect_compound() takes a string, returns the decomposed
#     step list (or None). No DB reads, no LLM calls.
#   - Conservative: false negatives are fine (fall back to normal
#     ranking); false positives misroute. Tune detection tight.
#   - Never auto-executes pipelines from do_specialist_task; multi-step
#     charges need explicit caller consent via manage_workflow.
# DECISIONS:
#   - Rule-based (not LLM) detection for v1. Cheap, deterministic,
#     fits the existing pure-decide contract. LLM-based richer parsing
#     is a follow-up if rule recall is too low in production.
#   - Recipe matching is keyword-overlap on the step verbs against
#     recipe.name + recipe.description. Coarse but works for the four
#     built-in recipes; refine when there's more catalog to match against.
# KNOWN DEBT:
#   - "audit my package.json" naive parsing as compound ("audit" + "my
#     package") would be a false positive; the gate `_min_step_chars`
#     prevents this. Could be tighter with a real conjunction parser.
from __future__ import annotations

import re
from dataclasses import dataclass

# Compound markers. Order matters — most-specific first.
# Use word-boundary anchors so we don't match inside ordinary words.
_COMPOUND_SPLITTERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\s+and\s+then\s+", re.IGNORECASE),
    re.compile(r"\s*,\s*then\s+", re.IGNORECASE),
    re.compile(r"\s+then\s+also\s+", re.IGNORECASE),
    re.compile(r"\s+then\s+", re.IGNORECASE),
    re.compile(r"\s*;\s*and\s+", re.IGNORECASE),
    re.compile(r"\s*;\s*then\s+", re.IGNORECASE),
)

# Imperative verbs that mark the start of a step. If a string contains
# two or more imperative starts joined by " and ", that's compound too.
_IMPERATIVE_VERBS: frozenset[str] = frozenset({
    "audit", "scan", "lint", "check", "validate", "review",
    "extract", "screenshot", "post", "send", "notify",
    "summarise", "summarize", "report", "publish", "ship",
    "fetch", "download", "upload", "deploy", "create",
    "generate", "build", "test", "run", "execute",
    "analyze", "analyse", "inspect", "diagnose", "trace",
    "decode", "verify", "sign", "encrypt", "decrypt",
})

# Below this and we don't treat a fragment as a real step — guards
# against splitting on noise like "X and Y" where Y is a short tail.
_MIN_STEP_CHARS = 6
_MAX_STEPS = 6

# Output-shaping verbs: "fetch X and report it" is ONE hire whose answer is
# delivered a certain way, not two specialist steps. 2026-06-10: the
# deference experiment showed "Fetch …rfc2606.txt and report it" refusing
# with compound_intent, forcing a retry round-trip. A non-first segment that
# starts with one of these and is short (or refers back to the prior step's
# result) folds into the preceding step instead of counting as a step.
_REPORTING_VERBS: frozenset[str] = frozenset({
    "report", "return", "tell", "show", "give", "print",
    "output", "say", "list", "display", "describe",
})
_BACKREF_TOKENS = ("it", "its", "them", "me", "the result", "the results",
                   "the content", "the contents", "the output", "the text")
_REPORTING_TAIL_MAX_CHARS = 60


def _is_reporting_tail(step: str) -> bool:
    """Pure: True when a step only shapes the previous step's output."""
    text = (step or "").strip().rstrip(".").lower()
    first = (text.split() or [""])[0].rstrip(",.")
    if first not in _REPORTING_VERBS:
        return False
    if len(text) <= _REPORTING_TAIL_MAX_CHARS:
        return True
    return any(ref in text for ref in _BACKREF_TOKENS)


def _fold_reporting_tails(parts: tuple[str, ...]) -> tuple[str, ...]:
    """Pure: drop non-first segments that are output shaping, not steps."""
    if len(parts) < 2:
        return parts
    return (parts[0],) + tuple(p for p in parts[1:] if not _is_reporting_tail(p))


@dataclass(frozen=True)
class CompoundIntent:
    """The parsed shape of a multi-step intent."""
    steps: tuple[str, ...]
    method: str  # "splitter" | "imperative_chain"


def detect_compound(intent: str) -> CompoundIntent | None:
    """Pure: split an intent into ordered steps, or return None.

    Returns None when the intent reads as a single ask, even if it
    contains conjunctions. Returns CompoundIntent when at least 2
    real-looking steps are found.
    """
    text = (intent or "").strip()
    if len(text) < _MIN_STEP_CHARS * 2:
        return None

    # Splitter path: matches phrases like ", then" / " and then" / " then ".
    for pattern in _COMPOUND_SPLITTERS:
        parts = pattern.split(text)
        if len(parts) < 2:
            continue
        cleaned = _fold_reporting_tails(
            tuple(p.strip().rstrip(".") for p in parts if p.strip())
        )
        if len(cleaned) < 2 or len(cleaned) > _MAX_STEPS:
            continue
        if any(len(p) < _MIN_STEP_CHARS for p in cleaned):
            continue
        return CompoundIntent(steps=cleaned, method="splitter")

    # Imperative-chain path: " and " joining two imperatives.
    # Conservative: only fires when each side starts with an imperative
    # verb. Avoids "audit my package.json and CVE-2021-44228" → wrongly
    # split (which would parse as ["audit my package.json", "CVE-...]").
    and_parts = re.split(r"\s+and\s+", text, flags=re.IGNORECASE)
    if len(and_parts) >= 2:
        starts_with_imperative = []
        for p in and_parts:
            first = (p.strip().split() or [""])[0].lower().rstrip(",.")
            starts_with_imperative.append(first in _IMPERATIVE_VERBS)
        if sum(starts_with_imperative) >= 2:
            cleaned = _fold_reporting_tails(
                tuple(p.strip().rstrip(".") for p in and_parts if p.strip())
            )
            if 2 <= len(cleaned) <= _MAX_STEPS and all(
                len(p) >= _MIN_STEP_CHARS for p in cleaned
            ):
                return CompoundIntent(steps=cleaned, method="imperative_chain")
    return None


# --- Recipe matching ----------------------------------------------------


@dataclass(frozen=True)
class RecipeMatch:
    """A recipe that plausibly fits a compound intent."""
    recipe_id: str
    name: str
    description: str
    score: int  # composite: 10*steps_covered + total_overlap_tokens
    steps_covered: int  # how many of the compound's steps the recipe hits


_STOPWORDS = frozenset({
    "the", "a", "an", "my", "this", "that", "these", "those",
    "and", "or", "for", "with", "of", "in", "on", "at", "to",
    "is", "are", "was", "were", "be", "been",
})


def _step_keywords(step: str) -> set[str]:
    """Pure: lowercased significant tokens from one step."""
    return {
        tok for tok in re.findall(r"[a-z0-9]+", step.lower())
        if tok not in _STOPWORDS and len(tok) > 2
    }


def match_recipes(
    compound: CompoundIntent, recipes: list[dict],
) -> list[RecipeMatch]:
    """Pure: rank recipes by step coverage, then total keyword overlap.

    Composite score: ``10 * steps_covered + total_overlap_tokens``.
    Step coverage dominates so a recipe touching BOTH steps of a 2-step
    intent always outranks a recipe that only matches one step heavily.
    """
    if not compound.steps or not recipes:
        return []
    per_step_words: list[set[str]] = [
        _step_keywords(step) for step in compound.steps
    ]
    all_step_words: set[str] = set().union(*per_step_words) if per_step_words else set()
    if not all_step_words:
        return []
    matches: list[RecipeMatch] = []
    for recipe in recipes:
        rid = str(recipe.get("recipe_id") or "")
        if not rid:
            continue
        name = str(recipe.get("name") or "")
        desc = str(recipe.get("description") or "")
        recipe_words = _step_keywords(f"{name} {desc}")
        overlap = all_step_words & recipe_words
        if not overlap:
            continue
        steps_covered = sum(
            1 for step_words in per_step_words
            if step_words & recipe_words
        )
        composite = 10 * steps_covered + len(overlap)
        matches.append(RecipeMatch(
            recipe_id=rid,
            name=name,
            description=desc,
            score=composite,
            steps_covered=steps_covered,
        ))
    matches.sort(key=lambda m: (-m.score, m.recipe_id))
    return matches


__all__ = [
    "CompoundIntent",
    "RecipeMatch",
    "detect_compound",
    "match_recipes",
]
