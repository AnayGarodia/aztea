"""Generation orchestrator for vibe-an-agent.

# OWNS: the iter loop (generate → parse → safety scan → self-test → near-clone),
#       minting the probation listing, persisting seed work-examples, settling
#       the ledger.
# NOT OWNS: HTTP transport, the LLM client, the embeddings backend, or the
#       payments primitives (each of those lives behind a thin wrapper).
# INVARIANTS:
#   - Generated listings land at review_status='probation' — never 'approved'.
#   - Every terminal failure path refunds the unused budget.
#   - Idempotency is the caller's responsibility (persistence layer enforces).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core import hosted_skills as _hosted_skills
from core import listing_safety as _listing_safety
from core import skill_parser as _skill_parser
from core.llm import CompletionRequest, Message, run_with_fallback
from core.registry import agents_ops as _registry

from core.agent_generator import ledger as _ledger
from core.agent_generator import persistence as _persist
from core.agent_generator import prompts as _prompts
from core.agent_generator import qa as _qa

_LOG = logging.getLogger(__name__)

# Per-iteration LLM budget knobs. The generation cap is the caller-provided
# max_total_cost_cents; we estimate per-iter cost using approximate token
# counts and abort if the running tally exceeds the cap.
_PER_ITER_MAX_TOKENS = 4000
# Approximate cost in cents per 1k completion tokens used as a coarse upper
# bound for budget tracking — the real ledger entries come from the wallet.
_APPROX_CENTS_PER_KTOK = 2

# Reserved owner-handle prefixes — generators must not register agents under
# these names regardless of the requesting owner.  See the agent_generation
# pydantic model for the public copy.
_RESERVED_PREFIXES = (
    "aztea", "system", "admin", "built-in", "platform", "official",
)


class _Terminal(Exception):
    """Internal control-flow signal: a terminal failure with a code + msg."""

    def __init__(self, code: str, message: str, *, hint: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint or {}


def _approved_listings() -> list[dict[str, Any]]:
    """Catalog of approved listings for the prompt + near-clone check."""
    rows = _registry.get_agents(include_internal=False, include_banned=False)
    return [r for r in rows if r.get("review_status") == "approved"]


def _resolve_unique_name(display_name: str) -> str:
    """Append numeric suffix until the agents.name unique constraint clears."""
    candidate = display_name
    for attempt in range(1, 8):
        from core.db import get_db_connection
        with get_db_connection() as _raw_conn, _raw_conn as conn:
            row = conn.execute(
                "SELECT 1 FROM agents WHERE name = %s LIMIT 1", (candidate,)
            ).fetchone()
        if row is None:
            return candidate
        candidate = f"{display_name}-{attempt + 1}"
    raise _Terminal(
        "name_collision",
        f"Could not allocate a unique name after 8 attempts (last: {candidate}).",
    )


def _check_reserved_handle(handle_slug: str) -> None:
    """422-equivalent guard — the pydantic regex catches shape, not reservedness."""
    lowered = handle_slug.strip().lower()
    if any(lowered.startswith(p) for p in _RESERVED_PREFIXES):
        raise _Terminal(
            "reserved_handle",
            f"Handle '{handle_slug}' uses a reserved prefix.",
            hint={"reserved": list(_RESERVED_PREFIXES)},
        )


def _generate_one(
    *,
    request: dict[str, Any],
    catalog_rendered: str,
    prior_failures: list[str],
) -> tuple[str, int]:
    """One LLM call.  Returns (skill_md, approx_cost_cents).

    The cost estimate is intentionally coarse — the real settlement uses the
    per-call wallet ledger, this number is only for the in-iter budget guard.
    """
    system_prompt = _prompts.build_system_prompt(
        catalog_rendered=catalog_rendered,
        allow_composition=bool(request.get("allow_composition", True)),
    )
    user_prompt = _prompts.build_user_prompt(
        description=request["description"],
        example_inputs=request["example_inputs"],
        ideal_outputs=request["ideal_outputs"],
        handle_slug=request["handle_slug"],
        prior_failures=prior_failures,
    )
    req = CompletionRequest(
        model="",
        messages=[
            Message("system", system_prompt),
            Message("user", user_prompt),
        ],
        temperature=0.3,
        max_tokens=_PER_ITER_MAX_TOKENS,
        timeout_seconds=120.0,
    )
    resp = run_with_fallback(req)
    text = (resp.text or "").strip()
    skill_md = _strip_outer_fence(text)
    # Coarse cost estimate from the response Usage if available.
    usage = getattr(resp, "usage", None)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    cost_cents = max(1, (completion_tokens * _APPROX_CENTS_PER_KTOK) // 1000)
    return skill_md, cost_cents


_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*([\s\S]*?)\s*```\s*$", re.MULTILINE)


def _strip_outer_fence(text: str) -> str:
    """LLMs often wrap SKILL.md in a fence; strip exactly one outer fence."""
    if not text:
        return text
    match = _FENCE_RE.match(text.strip())
    if match:
        return match.group(1).strip()
    return text


def _safety_block(skill_md: str) -> _listing_safety.VerificationFinding | None:
    """Return the first BLOCK-level finding, or None when clean."""
    findings = _listing_safety.scan_skill_md(skill_md)
    if not _listing_safety.has_block(findings):
        return None
    return next(
        (f for f in findings if f.level == _listing_safety.LEVEL_BLOCK), None
    )


def _mint_probation_agent(
    *,
    parsed: Any,
    raw_md: str,
    owner_id: str,
    handle_slug: str,
) -> tuple[str, str]:
    """Insert agents row (probation) + hosted_skills row.  Returns (agent_id, handle).

    Mirrors the existing /skills path but writes ``review_status='probation'``
    instead of 'approved'. Roll back the agents row if hosted_skills insert fails.
    """
    base = parsed.to_aztea_registration()
    display_name = str(base.get("name") or parsed.name).strip() or handle_slug
    description = str(base.get("description") or parsed.description).strip()
    if not description:
        raise _Terminal(
            "empty_description", "Generated SKILL.md has no description after parsing."
        )
    candidate_name = _resolve_unique_name(display_name)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    agent_id = _registry.register_agent(
        name=candidate_name,
        description=description,
        endpoint_url="skill://placeholder",
        price_per_call_usd=0.05,
        tags=list(base.get("tags") or []),
        input_schema=base.get("input_schema"),
        output_schema=base.get("output_schema"),
        owner_id=owner_id,
        review_status="probation",
        review_note="Vibe-generated; on probation until track record graduates it.",
        reviewed_at=now_iso,
        reviewed_by="system:vibe-generation",
        kind="community_skill",
    )
    try:
        skill_row = _hosted_skills.create_hosted_skill(
            agent_id=agent_id,
            owner_id=owner_id,
            slug=parsed.name,
            raw_md=raw_md,
            system_prompt=parsed.body,
            parsed_metadata={"warnings": parsed.warnings},
            model_chain=None,
            temperature=0.2,
            max_output_tokens=1500,
        )
    except Exception:
        _LOG.exception(
            "vibe.mint.skill_insert_failed agent_id=%s — rolling back", agent_id
        )
        try:
            _registry.delist_agent(agent_id, owner_id)
        except Exception:
            _LOG.exception(
                "vibe.mint.rollback_failed agent_id=%s", agent_id
            )
        raise _Terminal(
            "internal_error", "Failed to persist hosted_skills row.",
        )
    final_endpoint = _hosted_skills.make_skill_endpoint_url(skill_row["skill_id"])
    from core.db import get_db_connection
    with get_db_connection() as _raw_conn, _raw_conn as conn:
        conn.execute(
            "UPDATE agents SET endpoint_url = %s WHERE agent_id = %s AND owner_id = %s",
            (final_endpoint, agent_id, owner_id),
        )
    handle = f"@{owner_id}/{handle_slug}"
    return agent_id, handle


def generate_agent(
    *,
    generation_job_id: str,
    request: dict[str, Any],
    owner_id: str,
    caller_wallet_id: str,
    charge_tx_id: str,
    max_total_cost_cents: int,
    charged_by_key_id: str | None = None,
) -> dict[str, Any]:
    """Run the generation pipeline. Returns a result dict and persists status.

    The route handler has already pre-charged the caller for ``max_total_cost_cents``
    and inserted the queued row.  This function flips it to running, runs the
    iter loop, and updates to succeeded or failed. Refund-on-failure paths
    return the full pre-charge; success paths refund the unused remainder.
    """
    _persist.update_status(generation_job_id, status="running")
    try:
        result = _run_pipeline(
            generation_job_id=generation_job_id,
            request=request,
            owner_id=owner_id,
            caller_wallet_id=caller_wallet_id,
            charge_tx_id=charge_tx_id,
            max_total_cost_cents=max_total_cost_cents,
        )
    except _Terminal as exc:
        _ledger.refund_full(
            caller_wallet_id=caller_wallet_id,
            charge_tx_id=charge_tx_id,
            max_cents=max_total_cost_cents,
        )
        _persist.update_status(
            generation_job_id,
            status="failed",
            error_code=exc.code,
            error_message=exc.message,
            cost_cents=0,
            result_payload={"error": {"code": exc.code, "message": exc.message,
                                      "hint": exc.hint}},
        )
        return {"status": "failed", "error": {"code": exc.code,
                                              "message": exc.message,
                                              "hint": exc.hint}}
    except Exception as exc:
        _LOG.exception("vibe.loop.unexpected job=%s", generation_job_id)
        _ledger.refund_full(
            caller_wallet_id=caller_wallet_id,
            charge_tx_id=charge_tx_id,
            max_cents=max_total_cost_cents,
        )
        _persist.update_status(
            generation_job_id,
            status="failed",
            error_code="internal_error",
            error_message=str(exc)[:400],
            cost_cents=0,
        )
        return {"status": "failed",
                "error": {"code": "internal_error", "message": str(exc)[:400]}}
    return result


def _run_pipeline(
    *,
    generation_job_id: str,
    request: dict[str, Any],
    owner_id: str,
    caller_wallet_id: str,
    charge_tx_id: str,
    max_total_cost_cents: int,
) -> dict[str, Any]:
    """Inner pipeline; raises _Terminal on terminal failure paths."""
    _check_reserved_handle(request["handle_slug"])

    catalog = _approved_listings()
    catalog_rendered = _prompts.format_catalog_for_prompt(catalog)
    cost_used = 0
    iters_used = 0
    prior_failures: list[str] = []
    parsed = None
    raw_md = ""
    max_iters = int(request.get("max_self_test_iters", 3))
    for attempt in range(1, max_iters + 1):
        if cost_used >= max_total_cost_cents:
            raise _Terminal(
                "budget_exceeded",
                f"Generation budget of {max_total_cost_cents} cents exhausted at iter {attempt}.",
            )
        try:
            raw_md, iter_cents = _generate_one(
                request=request,
                catalog_rendered=catalog_rendered,
                prior_failures=prior_failures,
            )
        except Exception as exc:
            raise _Terminal("llm_failure", f"LLM generation failed: {exc}")
        cost_used += iter_cents
        iters_used = attempt

        block_finding = _safety_block(raw_md)
        if block_finding is not None:
            raise _Terminal(
                "safety_block",
                f"Safety scan blocked the generated SKILL.md: {block_finding.message}",
                hint={"code": block_finding.code, "detail": block_finding.detail},
            )
        try:
            parsed = _skill_parser.parse_skill_md(raw_md, source="agent_generator")
        except _skill_parser.SkillParseError as exc:
            prior_failures.append(f"Parser rejected output: {exc}")
            continue
        passed, _, failure_notes = _qa.self_test(
            parsed_skill_body=parsed.body,
            example_inputs=request["example_inputs"],
            ideal_outputs=request["ideal_outputs"],
        )
        if passed:
            break
        prior_failures.extend(failure_notes)
    else:
        raise _Terminal(
            "self_test_exhausted",
            f"Self-test failed after {max_iters} attempts.",
            hint={"failures": prior_failures[-3:]},
        )

    if parsed is None:
        # Defensive: parser failed every iteration.
        raise _Terminal("self_test_exhausted", "No iteration produced a parseable SKILL.md.")

    clone_id, clone_score = _qa.detect_near_clone(
        candidate_name=parsed.name,
        candidate_description=parsed.description,
        existing_listings=catalog,
    )
    if clone_id:
        raise _Terminal(
            "near_clone",
            f"Generated agent is a near-clone of {clone_id} (cosine={clone_score:.2f}).",
            hint={"clone_of": clone_id, "cosine": round(clone_score, 3)},
        )

    agent_id, handle = _mint_probation_agent(
        parsed=parsed, raw_md=raw_md, owner_id=owner_id,
        handle_slug=request["handle_slug"],
    )

    refunded = _ledger.refund_unused(
        caller_wallet_id=caller_wallet_id,
        charge_tx_id=charge_tx_id,
        max_cents=max_total_cost_cents,
        actual_cents=min(cost_used, max_total_cost_cents),
    )
    actual_charged = max_total_cost_cents - refunded
    result_payload = {
        "agent_id": agent_id,
        "handle": handle,
        "skill_md": raw_md,
        "iterations": iters_used,
        "cost_cents_charged": actual_charged,
    }
    _persist.update_status(
        generation_job_id,
        status="succeeded",
        agent_id=agent_id,
        iterations=iters_used,
        cost_cents=actual_charged,
        result_payload=result_payload,
    )
    return {"status": "succeeded", **result_payload}
