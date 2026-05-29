# OWNS: Phase 1 (B4) — LLM-based tiebreaker for close ranking decisions.
#       When the top candidate's confidence sits in the [floor, floor+
#       0.15] band, ask a small LLM call to pick the best of the top 3
#       specs. Bounded cost; auto-circuit-breaks on low win-rate.
# NOT OWNS: scoring (auto_hire.py); confidence math (auto_hire.py);
#       LLM provider selection (core/llm/).
# INVARIANTS:
#   - try_tiebreak() returns one of:
#       (a) a candidate from the input list when the LLM picked it
#       (b) None when the LLM failed / returned garbage / refused
#     Never returns an agent not in the input list (safety against
#     hallucinated slugs).
#   - Single LLM call per invocation. No retries.
#   - No-op when AZTEA_AUTO_HIRE_TIEBREAKER=0.
# DECISIONS:
#   - Use top 3 candidates only; more options dilute the LLM's signal.
#   - Output format: bare slug. Easier to validate than JSON.
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("AZTEA_AUTO_HIRE_TIEBREAKER", "1").lower() != "0"
_MAX_CANDIDATES = 3
_MAX_TOKENS = 24
_TEMPERATURE = 0.0


def try_tiebreak(
    candidates: list[Any], intent: str,
    *, caller_owner_id: str | None = None,
    request_budget: Any | None = None,
) -> Any | None:
    """Pure-ish: ask the LLM to pick the best fit from top candidates.

    Returns one of the input ``candidates`` (must be a Ranked-like with
    ``.candidate.slug`` and ``.candidate.name`` attrs), or None when the
    LLM was unavailable / disagreed with all options / produced garbage.
    """
    if not _ENABLED or not candidates:
        return None
    top = list(candidates[:_MAX_CANDIDATES])
    if len(top) < 2:
        # Nothing to tiebreak — caller should not have invoked us.
        return None
    # /review M3 + /cso H1 (2026-05-28) + belt-and-suspenders
    # (2026-05-29): three layers — per-request cap, per-caller bucket,
    # global bucket. Any layer can refuse independently.
    from core.registry import _llm_budget
    if not _llm_budget.try_consume(
        "tiebreaker",
        caller_owner_id=caller_owner_id,
        request_budget=request_budget,
    ):
        logger.debug("llm_tiebreaker: budget exhausted, skipping LLM call")
        return None
    try:
        from core.llm import CompletionRequest, Message, run_with_fallback
    except Exception:  # noqa: BLE001
        return None

    slug_to_ranked: dict[str, Any] = {}
    options_block_lines: list[str] = []
    for r in top:
        try:
            slug = r.candidate.slug
            name = r.candidate.name
            desc = r.candidate.description
        except AttributeError:
            continue
        if not slug:
            continue
        slug_to_ranked[slug] = r
        options_block_lines.append(
            f"  - slug={slug}, name={name}: {(desc or '')[:160]}"
        )
    if len(slug_to_ranked) < 2:
        return None

    system = (
        "You pick the best specialist for a user task. Respond with "
        "EXACTLY the slug of the best fit and nothing else (no "
        "punctuation, no explanation, no code fences). If none clearly "
        "fits, respond with the literal word NONE."
    )
    user = (
        f"User task:\n{intent.strip()[:500]}\n\n"
        f"Candidates:\n" + "\n".join(options_block_lines)
    )
    try:
        response = run_with_fallback(
            CompletionRequest(
                model="",  # fallback chain picks the model
                messages=[
                    Message(role="system", content=system),
                    Message(role="user", content=user),
                ],
                temperature=_TEMPERATURE,
                max_tokens=_MAX_TOKENS,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("llm_tiebreaker: LLM call failed: %s", exc)
        return None
    text = (response.text or "").strip()
    if not text:
        return None
    candidate_slug = re.sub(r"[^a-z0-9_\-]", "", text.lower())
    if not candidate_slug or candidate_slug == "none":
        return None
    # Hallucination safety: only return a candidate that was actually
    # in the input list. Try snake-case and dash-case forms.
    if candidate_slug in slug_to_ranked:
        return slug_to_ranked[candidate_slug]
    alt = candidate_slug.replace("-", "_")
    if alt in slug_to_ranked:
        return slug_to_ranked[alt]
    alt = candidate_slug.replace("_", "-")
    if alt in slug_to_ranked:
        return slug_to_ranked[alt]
    logger.debug(
        "llm_tiebreaker: LLM returned unknown slug %r — refusing",
        candidate_slug,
    )
    return None


__all__ = ["try_tiebreak"]
