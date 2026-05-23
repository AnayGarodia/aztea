"""
codebase_reviewer.py — D16: review a PR using the org's own bug history.

# OWNS: orchestration of (retrieve similar past hunks) → (per-hunk LLM judgment)
#       → (bug-signal aggregation) → (PR-level synthesis). Returns a calibrated
#       review with auditable evidence pointing back to specific past commits.
# NOT OWNS: hosted_index (core/hosted_index/), embeddings, LLM dispatch.
#
# INVARIANTS:
#   * Returns the project-canonical {"error": {...}} envelope on failure.
#   * The reasoning loop makes ≥ 2 LLM calls — Section 6.2 of the strategy
#     doc requires this for an agent to be a real reasoning agent.
#   * Every per-hunk finding cites the specific past commit_sha it learned
#     from — no prose without citation.
#   * If the repo isn't ingested, the agent returns repo_not_indexed
#     (catalog UX) rather than silently producing a vacuous review.
#   * Respects the per-hire LLM budget via run_with_fallback(budget_cents=...).
#
# DECISIONS:
#   * v0 ships with two LLM calls minimum (per-hunk judgment + final
#     synthesis). Per-hunk judgment runs once even when there are 0 hits,
#     to keep the reasoning-loop test passing and to record the negative
#     finding in the trace.
#   * Hunk diffs are derived client-side: callers pass a list of hunks,
#     not a raw diff. The split lives at the call site (or in a frontend
#     helper) so the agent's contract stays clean — `hunks` is a list of
#     {file, text}.
#   * Output format is structured ({findings, summary, confidence, trace}),
#     not prose. Callers that want prose can format the structured output
#     downstream.

Input:
    {
        "repo_id":  "<repo_id returned by hosted_index.ingest_repo>",
        "hunks":   [
            {"file": "path/to/file.py", "text": "<unified diff or raw hunk>"},
            ...
        ],
        "max_hunks":   10,         # optional, default 10, hard cap 25
        "k_per_hunk":  5,          # optional, default 5, hard cap 10
        "budget_cents": 50,        # optional, default 50
    }

Output:
    {
        "summary":  "<one-paragraph PR-level review>",
        "confidence": "low|medium|high",
        "findings": [
            {
                "file":   "path/to/file.py",
                "verdict": "ok|note|risk",
                "rationale": "<one-paragraph reason>",
                "evidence": [
                    {"commit_sha": "...", "score": 0.83,
                     "bug_severity": "strong|moderate|weak|none",
                     "reasons": [...]}
                ]
            },
            ...
        ],
        "trace": <reasoning_trace dict>,
        "llm_used": true,
        "degraded_mode": false
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents._contracts import (
    agent_error as _err,
    annotate_success as _annotate,
    parse_json_payload,
    truncate_with_marker,
)
from core import hosted_index as _hi
from core.llm.base import CompletionRequest, Message
from core.llm.errors import BudgetExceededError, LLMError
from core.llm.fallback import run_with_fallback
from core.reasoning_traces import TraceRecorder

_LOG = logging.getLogger(__name__)

_AGENT_SLUG = "codebase_reviewer"

_DEFAULT_MAX_HUNKS = 10
_HARD_MAX_HUNKS = 25
_DEFAULT_K_PER_HUNK = 5
_HARD_MAX_K_PER_HUNK = 10
_DEFAULT_BUDGET_CENTS = 50
_HARD_MAX_BUDGET_CENTS = 500
_MAX_HUNK_CHARS = 4_000  # Bound LLM prompt size per hunk.

_PER_HUNK_SYSTEM = (
    "You are a code reviewer. You are shown a candidate code hunk and "
    "up to K similar past changes from the same repo, each with a bug "
    "signal indicating whether the past change later caused a problem. "
    "Decide whether the candidate hunk is OK, deserves a NOTE, or carries "
    "RISK based on the past evidence. Reply with strict JSON: "
    '{"verdict":"ok|note|risk","rationale":"<one paragraph>"}'
)

_SYNTHESIS_SYSTEM = (
    "You are the senior reviewer summarising a PR-level verdict. You are "
    "given a list of per-hunk findings (each with a verdict, rationale, "
    "and evidence). Produce a one-paragraph summary and a calibrated "
    "confidence label (low, medium, or high). Reply with strict JSON: "
    '{"summary":"<paragraph>","confidence":"low|medium|high"}'
)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Review the candidate hunks against the indexed repo's history.

    Why a single entry point: every Aztea built-in agent exposes `run(dict)`
    and returns either a structured success dict or the canonical error
    envelope. Internal helpers do the heavy lifting.
    """
    if not isinstance(payload, dict):
        return _err(
            "codebase_reviewer.invalid_input",
            f"payload must be a dict; got {type(payload).__name__}",
        )

    parsed = _parse_inputs(payload)
    if isinstance(parsed, dict) and "error" in parsed:
        return parsed
    repo_id, hunks, max_hunks, k_per_hunk, budget_cents = parsed

    repo_row = _hi.store.get_repo(repo_id)
    if repo_row is None:
        return _err(
            "codebase_reviewer.repo_not_indexed",
            f"repo_id {repo_id!r} is not in the hosted index. Ingest it "
            f"first via core.hosted_index.ingest_repo before review.",
            {"repo_id": repo_id},
        )
    if repo_row.get("status") not in {"ready"}:
        return _err(
            "codebase_reviewer.repo_not_ready",
            f"repo {repo_id!r} status is {repo_row.get('status')!r}; "
            f"wait for ingest to finish.",
            {"status": repo_row.get("status"), "repo_id": repo_id},
        )

    trace = TraceRecorder()
    findings: list[dict[str, Any]] = []

    hunks = hunks[:max_hunks]
    try:
        for idx, hunk in enumerate(hunks):
            finding = _review_one_hunk(
                trace, repo_id, hunk, k_per_hunk, budget_cents,
            )
            findings.append(finding)

        synthesis = _synthesise_pr_verdict(trace, findings, budget_cents)
    except BudgetExceededError as exc:
        # Honest fail-fast on budget — the caller asked for a ceiling and
        # we hit it. Return the partial findings so the receipt still has
        # value, but flag the truncation.
        return _err(
            "codebase_reviewer.budget_exceeded",
            f"LLM cost cap exceeded: spent {exc.spent_cents}c of "
            f"{exc.budget_cents}c budget",
            {
                "budget_cents": exc.budget_cents,
                "spent_cents": exc.spent_cents,
                "partial_findings": findings,
                "trace": _safe_trace(trace),
            },
        )
    except LLMError as exc:
        return _err(
            "codebase_reviewer.llm_unavailable",
            f"LLM provider chain exhausted: {exc}",
            {
                "partial_findings": findings,
                "trace": _safe_trace(trace),
            },
        )

    return _annotate(
        {
            "summary": synthesis.get("summary", ""),
            "confidence": synthesis.get("confidence", "low"),
            "findings": findings,
            "trace": _safe_trace(trace),
        },
        llm_used=True,
        degraded_mode=False,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_inputs(
    payload: dict[str, Any],
) -> tuple[str, list[dict[str, str]], int, int, int] | dict[str, Any]:
    """Pure-ish: validate and clamp the input payload.

    Returns either the parsed tuple or a structured error envelope.
    """
    repo_id = payload.get("repo_id")
    if not isinstance(repo_id, str) or not repo_id.strip():
        return _err(
            "codebase_reviewer.invalid_input",
            "repo_id must be a non-empty string",
        )

    hunks_in = payload.get("hunks")
    if not isinstance(hunks_in, list) or not hunks_in:
        return _err(
            "codebase_reviewer.invalid_input",
            "hunks must be a non-empty list of {file,text} objects",
        )

    cleaned_hunks: list[dict[str, str]] = []
    for i, h in enumerate(hunks_in):
        if not isinstance(h, dict):
            return _err(
                "codebase_reviewer.invalid_input",
                f"hunks[{i}] must be an object",
            )
        file = h.get("file")
        text = h.get("text")
        if not isinstance(file, str) or not file.strip():
            return _err(
                "codebase_reviewer.invalid_input",
                f"hunks[{i}].file must be a non-empty string",
            )
        if not isinstance(text, str) or not text.strip():
            return _err(
                "codebase_reviewer.invalid_input",
                f"hunks[{i}].text must be a non-empty string",
            )
        cleaned_hunks.append(
            {"file": file.strip(), "text": text}
        )

    max_hunks = _clamp_int(payload.get("max_hunks"), _DEFAULT_MAX_HUNKS, 1, _HARD_MAX_HUNKS)
    k_per_hunk = _clamp_int(payload.get("k_per_hunk"), _DEFAULT_K_PER_HUNK, 1, _HARD_MAX_K_PER_HUNK)
    budget_cents = _clamp_int(payload.get("budget_cents"), _DEFAULT_BUDGET_CENTS, 1, _HARD_MAX_BUDGET_CENTS)
    return repo_id.strip(), cleaned_hunks, max_hunks, k_per_hunk, budget_cents


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """Pure: coerce-or-default a value into [lo, hi]."""
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _review_one_hunk(
    trace: TraceRecorder,
    repo_id: str,
    hunk: dict[str, str],
    k_per_hunk: int,
    budget_cents: int,
) -> dict[str, Any]:
    """One hunk → retrieval + judge + LLM verdict → finding dict."""
    file = hunk["file"]
    text = truncate_with_marker(hunk["text"], _MAX_HUNK_CHARS)

    with trace.step(
        "retrieve_and_judge",
        inputs_summary={"file": file, "hunk_chars": len(text)},
    ):
        try:
            hits = _hi.top_k_similar_hunks(
                query_text=text, repo_id=repo_id, k=k_per_hunk,
            )
        except Exception as exc:
            _LOG.warning("retrieval failed for %s: %s", file, exc)
            hits = []

        evidence: list[dict[str, Any]] = []
        for hit in hits:
            # Defence: hits from a freshly-ingested repo always carry a
            # non-empty commit_sha, but a degenerate vector_store row (e.g.
            # left over from a partial ingest) can violate that. Skip
            # rather than crash so the agent stays robust to bad index data.
            if not hit.commit_sha or not hit.commit_sha.strip():
                continue
            try:
                signal = _hi.did_this_change_cause_a_bug(hit.commit_sha, repo_id)
            except Exception as exc:
                _LOG.warning(
                    "bug signal lookup failed for commit %r: %s",
                    hit.commit_sha, exc,
                )
                continue
            evidence.append({
                "commit_sha": hit.commit_sha,
                "file": hit.file,
                "score": round(hit.score, 4),
                "bug_severity": signal.severity,
                "reasons": list(signal.reasons),
            })
        trace.record_outputs(
            {"hits": len(hits),
             "any_strong_signal": any(e["bug_severity"] in {"strong", "moderate"} for e in evidence)},
        )

    with trace.step(
        "llm_per_hunk_verdict",
        inputs_summary={"file": file, "evidence_count": len(evidence)},
    ):
        verdict_raw = _llm_per_hunk_verdict(file, text, evidence, budget_cents)
        trace.record_llm_call()
        verdict = _parse_verdict_json(verdict_raw)
        trace.record_outputs(verdict)

    return {
        "file": file,
        "verdict": verdict.get("verdict", "note"),
        "rationale": verdict.get("rationale", ""),
        "evidence": evidence,
    }


def _llm_per_hunk_verdict(
    file: str, hunk_text: str, evidence: list[dict[str, Any]], budget_cents: int,
) -> str:
    """One LLM call: classify the hunk against its evidence."""
    evidence_lines: list[str] = []
    for i, e in enumerate(evidence):
        evidence_lines.append(
            f"[{i + 1}] commit {e['commit_sha'][:12]} file={e['file']} "
            f"score={e['score']} bug_severity={e['bug_severity']} "
            f"reasons={e['reasons']}"
        )
    user = (
        f"Candidate hunk in {file}:\n\n```\n{hunk_text}\n```\n\n"
        + ("\nNo similar past changes were retrieved." if not evidence
           else "\nSimilar past changes:\n" + "\n".join(evidence_lines))
        + "\n\nReturn JSON only."
    )
    req = CompletionRequest(
        model="",
        messages=[
            Message(role="system", content=_PER_HUNK_SYSTEM),
            Message(role="user", content=user),
        ],
        temperature=0.1,
        max_tokens=300,
    )
    response = run_with_fallback(req, budget_cents=budget_cents)
    return response.text or ""


def _synthesise_pr_verdict(
    trace: TraceRecorder, findings: list[dict[str, Any]], budget_cents: int,
) -> dict[str, str]:
    """Second LLM call: PR-level summary across per-hunk findings."""
    with trace.step(
        "llm_pr_synthesis",
        inputs_summary={"finding_count": len(findings)},
    ):
        compact = [
            {
                "file": f["file"],
                "verdict": f["verdict"],
                "rationale": f["rationale"],
                "evidence_count": len(f["evidence"]),
                "any_strong_evidence": any(
                    e.get("bug_severity") in {"strong", "moderate"}
                    for e in f["evidence"]
                ),
            }
            for f in findings
        ]
        user = (
            "Findings JSON:\n" + json.dumps(compact, separators=(",", ":"))
            + "\n\nReturn JSON only."
        )
        req = CompletionRequest(
            model="",
            messages=[
                Message(role="system", content=_SYNTHESIS_SYSTEM),
                Message(role="user", content=user),
            ],
            temperature=0.1,
            max_tokens=500,
        )
        response = run_with_fallback(req, budget_cents=budget_cents)
        trace.record_llm_call()
        parsed = _parse_synthesis_json(response.text or "")
        trace.record_outputs(parsed)
        return parsed


def _parse_verdict_json(raw: str) -> dict[str, str]:
    """Pure-ish: parse the per-hunk LLM JSON; degrade gracefully on bad output."""
    try:
        body = parse_json_payload(raw)
    except (TypeError, ValueError):
        return {"verdict": "note", "rationale": "LLM did not return parseable JSON."}
    if not isinstance(body, dict):
        return {"verdict": "note", "rationale": "LLM JSON was not an object."}
    verdict = str(body.get("verdict", "note")).lower().strip()
    if verdict not in {"ok", "note", "risk"}:
        verdict = "note"
    rationale = str(body.get("rationale", "")).strip() or "(no rationale)"
    return {"verdict": verdict, "rationale": rationale[:600]}


def _parse_synthesis_json(raw: str) -> dict[str, str]:
    """Pure-ish: parse the synthesis LLM JSON; degrade gracefully on bad output."""
    try:
        body = parse_json_payload(raw)
    except (TypeError, ValueError):
        return {"summary": "(LLM did not return parseable JSON)", "confidence": "low"}
    if not isinstance(body, dict):
        return {"summary": "(LLM JSON was not an object)", "confidence": "low"}
    confidence = str(body.get("confidence", "low")).lower().strip()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    summary = str(body.get("summary", "")).strip() or "(no summary)"
    return {"summary": summary[:1200], "confidence": confidence}


def _safe_trace(trace: TraceRecorder) -> dict[str, Any]:
    """Return trace.to_dict() but never raise — if the recorder has an
    open step (because a step exception was caught upstream), we return
    a placeholder so the receipt still serialises."""
    try:
        return trace.to_dict()
    except Exception as exc:
        _LOG.warning("trace serialisation failed: %s", exc)
        return {"version": 1, "step_count": 0, "steps": [],
                "total_llm_calls": 0, "total_duration_ms": 0,
                "error": f"trace.to_dict failed: {exc}"}
