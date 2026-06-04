"""Advisory LLM council for listing verification (Karpathy llm-council pattern).

# OWNS: dispatch of N independent model "members" over a publish candidate, and a
#   deterministic chairman that tallies their verdicts into advisory findings.
# NOT OWNS: the prompts / parsing (``core.listing_council_prompts``) and any
#   blocking decision — this council NEVER blocks (2026-06-03 decision D1). Its
#   output adjusts a listing's probation standing and may flag it for human review.
# INVARIANTS:
#   - Emits WARN findings only. No code path here returns a BLOCK.
#   - Quorum: a dimension is flagged only when >=2 members are PRESENT and a strict
#     majority of present members raise a concern at confidence >= the floor. With
#     0-1 members present, the council emits nothing (no single-model authority, H4).
#   - A member that errors / times out ABSTAINS — it is absent from the tally,
#     never a vote either way.
#   - OSS / offline safe: no LLM configured, env-disabled, or every member failing
#     all yield an empty result and never an outbound call to aztea.ai.
# DECISIONS:
#   - Members run concurrently under one aggregate timeout; slow members abstain.
#   - Per-member results are LRU-cached on a content hash so a republish of
#     identical content doesn't re-spend tokens (mirrors listing_safety_judge).
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Callable

from core import feature_flags
from core import listing_council_prompts as _prompts
from core.listing_council_prompts import DIMENSIONS, VERDICT_CONCERN, MemberVerdict
from core.listing_safety import LEVEL_WARN, VerificationFinding
from core.llm import CompletionRequest, Message
from core.llm.errors import LLMError
from core.llm.fallback import run_with_fallback

_LOG = logging.getLogger(__name__)

_COUNCIL_ENABLED_ENV = "AZTEA_LISTING_COUNCIL"
_COUNCIL_CHAIN_ENV = "AZTEA_LISTING_COUNCIL_CHAIN"
_DEFAULT_CHAIN_ENV = "AZTEA_LLM_DEFAULT_CHAIN"

# A dimension is flagged only above this self-reported confidence (mirrors the
# judge's permissive floor) and only with a real quorum.
_CONCERN_CONFIDENCE_FLOOR = 0.6
_MIN_QUORUM = 2
_MAX_MEMBERS = 3
_COUNCIL_TEMPERATURE = 0.0
_COUNCIL_MAX_TOKENS = 500
_COUNCIL_CACHE_SIZE = 2048
_COUNCIL_TIMEOUT_S = feature_flags.flag_float(
    "AZTEA_LISTING_COUNCIL_TIMEOUT_S", default=20.0
)

# A type alias for the injectable per-member runner (tests stub this).
MemberRunner = Callable[[str, str, str, str], "MemberVerdict | None"]


@dataclass
class CouncilResult:
    findings: list[VerificationFinding] = field(default_factory=list)
    needs_human_review: bool = False
    member_count: int = 0


def _disabled() -> bool:
    raw = os.environ.get(_COUNCIL_ENABLED_ENV, "on")
    return str(raw).strip().lower() in {"0", "off", "false", "no", ""}


def _member_specs() -> list[str]:
    """Distinct model specs for the council, capped at ``_MAX_MEMBERS``."""
    raw = os.environ.get(_COUNCIL_CHAIN_ENV) or os.environ.get(_DEFAULT_CHAIN_ENV)
    if raw:
        specs = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        try:
            from core.llm.registry import DEFAULT_CHAIN  # noqa: PLC0415
            specs = list(DEFAULT_CHAIN)
        except Exception:  # noqa: BLE001 — registry import must not gate publishing
            return []
    seen: set[str] = set()
    distinct: list[str] = []
    for spec in specs:
        if spec in seen:
            continue
        seen.add(spec)
        distinct.append(spec)
        if len(distinct) >= _MAX_MEMBERS:
            break
    return distinct


# Manual cache keyed ONLY on (content_hash, spec). content_hash = sha256(user) and
# the system prompt is constant, so the candidate is fully captured by the hash —
# keeping the full prompt strings as keys (the old lru_cache) just pinned megabytes
# of attacker-supplied text. The dict holds short keys + small results instead.
_member_cache: dict[tuple[str, str], str | None] = {}
_member_cache_lock = threading.Lock()


def clear_member_cache() -> None:
    """Drop the per-member result cache (used by tests)."""
    with _member_cache_lock:
        _member_cache.clear()


def _call_member(spec: str, system: str, user: str) -> str | None:
    """Side-effect: one LLM round-trip pinned to ``spec``. None on clean failure."""
    try:
        resp = run_with_fallback(
            CompletionRequest(
                model="",
                messages=[Message("system", system), Message("user", user)],
                temperature=_COUNCIL_TEMPERATURE,
                max_tokens=_COUNCIL_MAX_TOKENS,
                json_mode=True,
            ),
            model_chain=[spec],
        )
    except LLMError as exc:
        _LOG.info("listing council member %s unavailable: %s", spec, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — provider raised something unexpected
        _LOG.warning("listing council member %s errored: %s", spec, exc)
        return None
    return resp.text or ""


def _run_member_cached(content_hash: str, spec: str, system: str, user: str) -> str | None:
    """Cache one member's reply by (content_hash, spec). Identical content reuses it."""
    key = (content_hash, spec)
    with _member_cache_lock:
        if key in _member_cache:
            return _member_cache[key]
    result = _call_member(spec, system, user)
    with _member_cache_lock:
        # Crude bound: council results are cheap to recompute, so a full clear at
        # the cap is fine and avoids tracking per-entry age.
        if len(_member_cache) >= _COUNCIL_CACHE_SIZE:
            _member_cache.clear()
        _member_cache[key] = result
    return result


def _default_member_runner(
    spec: str, system: str, user: str, content_hash: str,
) -> MemberVerdict | None:
    text = _run_member_cached(content_hash, spec, system, user)
    if text is None:
        return None
    return _prompts.parse_member_verdict(spec, text)


def _gather_verdicts(
    specs: list[str], system: str, user: str, content_hash: str,
    runner: MemberRunner,
) -> list[MemberVerdict]:
    """Run members concurrently; collect whatever returns before the timeout."""
    verdicts: list[MemberVerdict] = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(specs))
    futures = {
        executor.submit(runner, spec, system, user, content_hash): spec
        for spec in specs
    }
    try:
        for fut in concurrent.futures.as_completed(futures, timeout=_COUNCIL_TIMEOUT_S):
            verdict = _safe_future_result(fut)
            if verdict is not None:
                verdicts.append(verdict)
    except concurrent.futures.TimeoutError:
        _LOG.info("listing council timed out; %d/%d members answered",
                  len(verdicts), len(specs))
        for fut in futures:
            if fut.done():
                verdict = _safe_future_result(fut)
                if verdict is not None and verdict not in verdicts:
                    verdicts.append(verdict)
    finally:
        # Don't block on slow members — they've already abstained.
        executor.shutdown(wait=False, cancel_futures=True)
    return verdicts


def _safe_future_result(fut: concurrent.futures.Future) -> MemberVerdict | None:
    try:
        return fut.result(timeout=0)
    except Exception:  # noqa: BLE001 — a member failure is an abstention
        return None


def _tally(verdicts: list[MemberVerdict]) -> tuple[list[VerificationFinding], bool]:
    """Chairman: strict-majority concern per dimension among present members."""
    present = len(verdicts)
    if present < _MIN_QUORUM:
        return [], False
    findings: list[VerificationFinding] = []
    needs_human = False
    for dim in DIMENSIONS:
        concerns = [
            v for v in verdicts
            if dim in v.dimensions
            and v.dimensions[dim].verdict == VERDICT_CONCERN
            and v.dimensions[dim].confidence >= _CONCERN_CONFIDENCE_FLOOR
        ]
        # Strict majority of present members.
        if len(concerns) * 2 <= present:
            continue
        reasons = [v.dimensions[dim].reason for v in concerns if v.dimensions[dim].reason]
        findings.append(VerificationFinding(
            code=f"listing.council.{dim}",
            level=LEVEL_WARN,
            message=(
                f"Council concern on {dim}: {len(concerns)}/{present} reviewers "
                f"flagged it. {reasons[0] if reasons else ''}".strip()
            ),
            detail={
                "dimension": dim,
                "concern_votes": len(concerns),
                "members_present": present,
                "reasons": reasons,
            },
        ))
        if len(concerns) == present:
            needs_human = True
    return findings, needs_human


def review_listing(
    candidate: dict,
    evidence: list[str],
    *,
    member_runner: MemberRunner | None = None,
) -> CouncilResult:
    """Advisory multi-model review. Never blocks; returns WARN findings + a flag.

    ``member_runner`` is injectable so tests can supply canned member verdicts
    without an LLM. The default runs the real, cached, provider-pinned members.
    """
    if _disabled():
        return CouncilResult()
    specs = _member_specs()
    if not specs:
        return CouncilResult()
    runner = member_runner or _default_member_runner
    system = _prompts.COUNCIL_SYSTEM_PROMPT
    user = _prompts.build_user_message(candidate, evidence)
    content_hash = hashlib.sha256(user.encode("utf-8")).hexdigest()
    verdicts = _gather_verdicts(specs, system, user, content_hash, runner)
    findings, needs_human = _tally(verdicts)
    return CouncilResult(
        findings=findings, needs_human_review=needs_human, member_count=len(verdicts),
    )


__all__ = ["CouncilResult", "MemberRunner", "review_listing"]
