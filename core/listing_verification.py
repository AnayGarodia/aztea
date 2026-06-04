"""Single entry point for publish-time listing verification.

# OWNS: orchestration only. Two phases:
#   - ``verify_listing_inline`` runs the cheap deterministic gates that CAN block a
#     publish (static content scan + security judge + exact-copy fingerprint dup).
#   - ``verify_listing_async`` runs the advisory pass that NEVER blocks (embedding
#     near-dup, thin-wrapper signal, repeat-probe/dry-run reliability, the council)
#     and refines the listing's probation standing via ``annotate_listing``.
# NOT OWNS: the individual checks (each lives in its own ``core.listing_*`` module)
#   or the HTTP endpoint wiring (``server.application_parts``).
# INVARIANTS:
#   - Only ``verify_listing_inline`` can produce a BLOCK; the async pass is advisory.
#   - Every collector is wrapped so a failure in one never blocks a publish or
#     crashes the background job (publishing must not depend on the council/LLM).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core import db as _db
from core import feature_flags
from core import listing_council, listing_dedup, listing_reliability, listing_value_add
from core.listing_safety import VerificationFinding, has_block, scan_python_handler, scan_skill_md
from core.listing_safety_judge import judge_python_handler, judge_skill_md
from core.registry.core_schema import _resolved_db_path

_LOG = logging.getLogger(__name__)

# Listing kinds the orchestrator understands.
KIND_SKILL_MD = "skill_md"
KIND_PYTHON_HANDLER = "python_handler"
KIND_EXTERNAL = "external"  # author-hosted endpoint with no body we can inspect

_BODY_KINDS = (KIND_SKILL_MD, KIND_PYTHON_HANDLER)

# Marker prepended to a probation note when the council reached full agreement on
# a concern — operators grep for it. The listing stays live in probation.
_NEEDS_REVIEW_MARKER = "[needs-human-review]"

# Below this many chars of normalised body, content is essentially empty
# (frontmatter stripped, just a heading) — too little to call one listing a copy
# of another, so we never hard-block on it. Cosine/council still flag it advisory.
# Kept low so a genuine one-line skill still blocks; only near-empty bodies skip.
_MIN_FINGERPRINT_BODY_CHARS = 32

# Cap a single review-note annotation so a long findings summary can't bloat the
# hot agents row. The async pass annotates once per published agent_id.
_MAX_NOTE_ADDITION_CHARS = 2000

# Bound how many advisory passes run concurrently per process. The pass makes
# blocking LLM/embed/probe calls on a Starlette threadpool thread; under a
# publish burst we skip rather than pile threads onto the pool that real request
# handlers also need (see the prod worker-exhaustion defect).
_MAX_CONCURRENT_ASYNC_VERIFICATIONS = feature_flags.flag_int(
    "AZTEA_LISTING_VERIFY_MAX_CONCURRENCY", default=4
)
_async_verify_slots = threading.Semaphore(_MAX_CONCURRENT_ASYNC_VERIFICATIONS)


@dataclass
class InlineResult:
    findings: list[VerificationFinding] = field(default_factory=list)

    def first_block(self) -> VerificationFinding | None:
        return next((f for f in self.findings if f.level == "block"), None)


@dataclass
class AsyncResult:
    findings: list[VerificationFinding] = field(default_factory=list)
    needs_human_review: bool = False


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(_resolved_db_path())


# ---------------------------------------------------------------------------
# Inline phase — deterministic, can block
# ---------------------------------------------------------------------------


def verify_listing_inline(
    kind: str,
    *,
    raw: str | None = None,
    name: str = "",
    description: str = "",
    owner_id: str | None = None,
) -> InlineResult:
    """Cheap deterministic gates that run in the publish request and may block.

    Reproduces the existing static-scan + security-judge behaviour (so callers can
    delegate to one entry point) and adds the exact-copy fingerprint dup block.
    Endpoint reachability + schema validation live in the probe path, not here.

    ``owner_id`` is excluded from the dup check: an owner may re-list their own
    content; only copies of *other* owners' listings are refused.
    """
    findings: list[VerificationFinding] = []
    if kind == KIND_SKILL_MD and raw:
        findings.extend(scan_skill_md(raw))
        if not has_block(findings):
            findings.extend(judge_skill_md(raw))
    elif kind == KIND_PYTHON_HANDLER and raw:
        findings.extend(scan_python_handler(raw))
        if not has_block(findings):
            findings.extend(judge_python_handler(raw))

    if raw and kind in _BODY_KINDS and not has_block(findings):
        findings.extend(_fingerprint_block(raw, kind, owner_id))
    return InlineResult(findings=findings)


def _fingerprint_block(
    raw: str, kind: str, owner_id: str | None,
) -> list[VerificationFinding]:
    """Exact-copy dup check. A lookup failure must never block a publish."""
    try:
        # Don't hard-block on near-empty / boilerplate-only bodies: two distinct
        # listings whose substance lives in their (stripped) frontmatter could
        # otherwise collide and wrongfully refuse a legitimate publish.
        if len(listing_dedup.normalize_body_for_fingerprint(raw, kind)) < _MIN_FINGERPRINT_BODY_CHARS:
            return []
        fingerprint = listing_dedup.content_fingerprint(raw, kind)
        match = listing_dedup.find_verbatim_copy(
            fingerprint, exclude_owner_id=owner_id,
        )
    except Exception:  # noqa: BLE001 — dup lookup is best-effort, not a gate
        _LOG.warning("fingerprint dup lookup failed; allowing publish", exc_info=True)
        return []
    if match is None:
        return []
    # Operator breadcrumb: the matched listing's identity is logged server-side
    # (it is deliberately kept out of the caller-facing block — see verbatim_finding).
    _LOG.info(
        "duplicate publish refused: candidate is byte-identical to agent %s (%s)",
        match.agent_id, match.name,
    )
    return [listing_dedup.verbatim_finding(match)]


# ---------------------------------------------------------------------------
# Async phase — advisory only, never blocks
# ---------------------------------------------------------------------------


def verify_listing_async(
    agent_id: str,
    kind: str,
    *,
    raw: str | None = None,
    name: str = "",
    description: str = "",
    tags: list[str] | None = None,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    endpoint_url: str | None = None,
    skill_row: dict | None = None,
    http_post=None,
    council_runner=None,
) -> AsyncResult:
    """Advisory verification run after a successful publish. Never blocks.

    Each collector is isolated: one failing (e.g. the LLM council with no provider)
    must not lose the others' findings or crash the background job.
    """
    findings: list[VerificationFinding] = []

    if raw and kind in _BODY_KINDS:
        _safe(lambda: listing_dedup.record_fingerprint(agent_id, raw, kind),
              "record_fingerprint")

    findings += _safe(
        lambda: listing_dedup.near_duplicate_findings(
            listing_dedup.find_near_duplicates(
                name, description, tags or [], input_schema, exclude_agent_id=agent_id,
            )
        ),
        "near_duplicates", default=[],
    )

    if raw and kind == KIND_PYTHON_HANDLER:
        findings += _safe(lambda: listing_value_add.assess_thin_wrapper(raw),
                          "thin_wrapper", default=[])

    if endpoint_url and http_post is not None:
        findings += _safe(
            lambda: listing_reliability.probe_repeatability(
                endpoint_url, input_schema, http_post=http_post,
            ),
            "probe_repeatability", default=[],
        )
    if kind == KIND_SKILL_MD and skill_row is not None:
        findings += _safe(
            lambda: listing_reliability.skill_dry_run(skill_row, input_schema),
            "skill_dry_run", default=[],
        )

    council = _safe(
        lambda: listing_council.review_listing(
            {
                "name": name, "description": description, "kind": kind,
                "input_schema": input_schema or {}, "output_schema": output_schema or {},
                "body": raw or "",
            },
            [f.message for f in findings],
            member_runner=council_runner,
        ),
        "council", default=listing_council.CouncilResult(),
    )
    findings += council.findings
    return AsyncResult(findings=findings, needs_human_review=council.needs_human_review)


def summarize_findings(findings: list[VerificationFinding]) -> str:
    """Pure: compact one-line-per-finding summary for a review note."""
    if not findings:
        return "no advisory findings"
    return "; ".join(f"{f.code}: {f.message}" for f in findings)


def annotate_listing(agent_id: str, result: AsyncResult) -> None:
    """Side-effect: append the advisory result to the listing's review note.

    Keeps the agent live in probation — the note is for operators and graduation
    logic, not a visibility change. The ``_NEEDS_REVIEW_MARKER`` lets ops grep for
    listings where the council fully agreed on a concern.
    """
    if not result.findings and not result.needs_human_review:
        return
    summary = summarize_findings(result.findings)
    stamp = datetime.now(timezone.utc).isoformat()
    prefix = f"{_NEEDS_REVIEW_MARKER} " if result.needs_human_review else ""
    addition = f"\n[verify {stamp}] {prefix}{summary}"[:_MAX_NOTE_ADDITION_CHARS]
    try:
        # Single atomic append: avoids a read-modify-write race that could clobber
        # a concurrent writer (operator note, another verify run). `||` is the
        # standard concat operator on both SQLite and Postgres.
        with _conn() as conn:
            conn.execute(
                "UPDATE agents SET review_note = COALESCE(review_note, '') || %s "
                "WHERE agent_id = %s",
                (addition, agent_id),
            )
    except Exception:  # noqa: BLE001 — annotation is best-effort telemetry
        _LOG.warning("failed to annotate listing %s with verify result", agent_id,
                     exc_info=True)


def run_and_annotate(agent_id: str, kind: str, **kwargs) -> AsyncResult:
    """Convenience for background tasks: run the advisory pass and persist the note.

    Designed to be handed to ``BackgroundTasks.add_task`` so it executes after the
    publish response is sent — keeping the HTTP worker free (C2). Never raises.

    Bounded by a non-blocking semaphore: under a publish burst we skip the advisory
    pass rather than queue threadpool threads that block on LLM/embed/probe I/O.
    Skipping is safe — the pass is advisory and the inline gate already ran.
    """
    if not _async_verify_slots.acquire(blocking=False):
        _LOG.info("listing verification skipped under load for %s", agent_id)
        return AsyncResult()
    try:
        result = verify_listing_async(agent_id, kind, **kwargs)
        annotate_listing(agent_id, result)
        return result
    except Exception:  # noqa: BLE001 — background work must never surface an error
        _LOG.warning("async listing verification failed for %s", agent_id, exc_info=True)
        return AsyncResult()
    finally:
        _async_verify_slots.release()


def _safe(fn, label: str, default=None):
    """Run a collector, logging and swallowing failures (publishing must not break)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — one collector's failure can't sink the rest
        _LOG.warning("listing verification step %r failed", label, exc_info=True)
        return default


__all__ = [
    "AsyncResult",
    "InlineResult",
    "KIND_EXTERNAL",
    "KIND_PYTHON_HANDLER",
    "KIND_SKILL_MD",
    "annotate_listing",
    "run_and_annotate",
    "summarize_findings",
    "verify_listing_async",
    "verify_listing_inline",
]
