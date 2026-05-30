"""
compliance_attestor.py — C11: sign an Ed25519 attestation that a PR satisfies
a named compliance control (SOC2 CC6.1 in v0).

# OWNS: control vocabulary + check enumeration + signing flow + evidence ledger.
# NOT OWNS: actually performing the security analysis on a PR diff (the
#           caller passes the check_results in v0; later versions can pair
#           with sast_scanner / dependency_auditor to auto-gather).
#
# INVARIANTS:
#   * Signed payloads use the schema "aztea/compliance-attestation/1".
#   * A missing required check → attestation_incomplete; no signature issued.
#   * A failed required check → attestation_failed; no signature issued.
#   * Only when every required check passes does the agent sign and return.
#   * Reasoning loop: ≥ 2 LLM calls (rationale per failure + final summary).
#
# DECISIONS:
#   * v0 ships SOC2 CC6.1 only. The control table is the single source of
#     truth for required-check enumeration; new controls are one-row PRs.
#   * Signing key lives at AZTEA_COMPLIANCE_SIGNING_KEY_PATH (defaults to
#     data/compliance_signing_key.pem). Reusing the workspace key would
#     conflate signing surfaces and make revocation harder later.
#   * Attestation includes both the pass-list AND the failure-list when
#     incomplete, so the caller's audit trail captures every probed check.

Input:
    {
        "control":     "SOC2_CC6_1",
        "pr_ref":      "<repo>#<pr-number-or-sha>",
        "check_results": [
            {"check_id": "secrets_not_committed", "passed": true,
             "evidence": "scan ran on diff at <sha>; no findings"},
            ...
        ]
    }

Output (passing):
    {
        "control": "SOC2_CC6_1",
        "pr_ref":  "...",
        "status":  "attested",
        "attestation": {
            "schema":      "aztea/compliance-attestation/1",
            "control":     "SOC2_CC6_1",
            "pr_ref":      "...",
            "issued_at":   "2026-05-22T...Z",
            "checks":      [{"check_id": "...", "passed": true, ...}, ...],
            "did":         "did:web:host:attestations:compliance",
        },
        "signature_b64": "<88-char Ed25519 sig>",
        "trace":         <reasoning_trace dict>,
        "llm_used":      true
    }

Output (failure):
    {
        "error": {
            "code":    "compliance_attestor.attestation_failed|attestation_incomplete",
            "message": "...",
            "details": {"missing": [...], "failed": [...], "trace": {...}}
        }
    }
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from agents._contracts import (
    agent_error as _err,
    annotate_success as _annotate,
)
from agents._reasoning_scaffold import clamp_int as _clamp_int
from core import crypto as _crypto
from core import identity as _identity
from core.llm.base import CompletionRequest, Message
from core.llm.errors import BudgetExceededError, LLMError
from core.llm.fallback import run_with_fallback
from core.reasoning_traces import TraceRecorder

_LOG = logging.getLogger(__name__)
_AGENT_SLUG = "compliance_attestor"

_ATTESTATION_SCHEMA = "aztea/compliance-attestation/1"
_DEFAULT_BUDGET_CENTS = 30
_HARD_MAX_BUDGET_CENTS = 200

_DEFAULT_SIGNING_KEY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "compliance_signing_key.pem",
)

# Control vocabulary. Each entry lists the required check_ids. Callers pass
# results for the listed checks; the agent validates completeness +
# pass/fail status before signing. Adding a new control is a single
# dict entry — no other code changes required.
#
# Why this table is small in v0: each control has hundreds of distinct
# real-world interpretations. v0 ships ONE deterministic interpretation per
# control so the signed attestation has a stable meaning; richer
# interpretations land in v1 with a per-control versioned schema.
_CONTROL_REQUIREMENTS: dict[str, list[str]] = {
    "SOC2_CC6_1": [
        # CC6.1 — Logical access controls.
        "auth_required_on_protected_routes",
        "secrets_not_committed_to_repo",
        "encryption_in_transit_for_external_traffic",
        "principle_of_least_privilege_in_iam_diffs",
    ],
    # Stubs for the next likely controls. Empty list = control known but
    # required checks not yet defined; the agent returns control_unknown
    # for these until they're filled in.
    "SOC2_CC7_2": [],
    "HIPAA_164_312": [],
    "PCI_6_5_1": [],
}

_RATIONALE_SYSTEM = (
    "You are a compliance reviewer. Given one failed check from a control, "
    "produce a one-sentence rationale explaining why the failure prevents "
    "the control from being satisfied. Reply with strict JSON: "
    '{"rationale":"<one sentence>"}'
)

_SUMMARY_SYSTEM = (
    "You are the senior compliance officer issuing the attestation. Given "
    "the control name and the per-check results, produce a one-paragraph "
    "summary suitable for inclusion in the signed manifest. Reply with "
    "strict JSON: {\"summary\":\"<paragraph>\"}"
)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate + sign (or refuse to sign) a compliance attestation."""
    if not isinstance(payload, dict):
        return _err(
            f"{_AGENT_SLUG}.invalid_input",
            f"payload must be a dict; got {type(payload).__name__}",
        )

    parsed = _parse_inputs(payload)
    if isinstance(parsed, dict) and "error" in parsed:
        return parsed
    control, pr_ref, results, budget_cents = parsed

    required = _CONTROL_REQUIREMENTS.get(control)
    if required is None:
        return _err(
            f"{_AGENT_SLUG}.control_unknown",
            f"control {control!r} is not in the attestor vocabulary",
            {"known_controls": sorted(_CONTROL_REQUIREMENTS.keys())},
        )
    if not required:
        return _err(
            f"{_AGENT_SLUG}.control_not_implemented",
            f"control {control!r} is recognised but has no required-check "
            f"definitions yet (v0 ships SOC2_CC6_1 only)",
            {"control": control},
        )

    trace = TraceRecorder()
    try:
        # Step 1: deterministic completeness + pass/fail tally.
        with trace.step(
            "validate_results",
            inputs_summary={"control": control, "required_count": len(required),
                            "supplied_count": len(results)},
        ):
            supplied = {r["check_id"]: r for r in results}
            missing = [c for c in required if c not in supplied]
            failed = [
                supplied[c] for c in required
                if c in supplied and not supplied[c].get("passed")
            ]
            trace.record_outputs(
                {"missing": missing, "failed_count": len(failed)},
            )

        if missing:
            # No LLM call needed — the caller supplied an incomplete set.
            # Return without signing; the receipt records what was missing.
            return _err(
                f"{_AGENT_SLUG}.attestation_incomplete",
                f"attestation requires {len(required)} checks for "
                f"{control}; {len(missing)} missing",
                {"missing": missing, "trace": _safe_trace(trace),
                 "control": control, "pr_ref": pr_ref},
            )

        # Step 2: if any check failed, ask the LLM to articulate the gap.
        per_fail_rationales: list[dict[str, Any]] = []
        if failed:
            for f in failed:
                rationale = _llm_failure_rationale(trace, control, f, budget_cents)
                per_fail_rationales.append(
                    {"check_id": f["check_id"], "rationale": rationale},
                )

        # Step 3: final synthesis — one LLM call summarises the outcome.
        # This runs whether we pass or fail so the trace is consistent.
        summary = _llm_attestation_summary(
            trace, control, pr_ref, results, failed, budget_cents,
        )

        if failed:
            return _err(
                f"{_AGENT_SLUG}.attestation_failed",
                f"{len(failed)} of {len(required)} required checks failed",
                {"failed": per_fail_rationales, "summary": summary,
                 "trace": _safe_trace(trace),
                 "control": control, "pr_ref": pr_ref},
            )

        # Step 4: every check passed. Sign and return the attestation.
        # Why explicit try around signing: the plan promises a
        # signing_failed envelope rather than a half-truth (an "attested"
        # output with a missing/null signature). Bubbling OSError /
        # crypto errors out of run() would violate the contract.
        manifest = _build_manifest(control, pr_ref, results, summary)
        try:
            signature_b64 = _sign_manifest(manifest)
        except Exception as exc:
            return _err(
                f"{_AGENT_SLUG}.signing_failed",
                f"signing failed: {type(exc).__name__}: {exc}",
                {"manifest": manifest, "trace": _safe_trace(trace),
                 "control": control, "pr_ref": pr_ref},
            )
        trace_dict = _safe_trace(trace)

    except BudgetExceededError as exc:
        return _err(
            f"{_AGENT_SLUG}.budget_exceeded",
            f"LLM cost cap exceeded: spent {exc.spent_cents}c of "
            f"{exc.budget_cents}c budget",
            {"budget_cents": exc.budget_cents, "spent_cents": exc.spent_cents,
             "trace": _safe_trace(trace)},
        )
    except LLMError as exc:
        return _err(
            f"{_AGENT_SLUG}.llm_unavailable",
            f"LLM provider chain exhausted: {exc}",
            {"trace": _safe_trace(trace)},
        )

    return _annotate(
        {
            "control": control,
            "pr_ref": pr_ref,
            "status": "attested",
            "attestation": manifest,
            "signature_b64": signature_b64,
            "trace": trace_dict,
        },
        llm_used=True,
        degraded_mode=False,
    )


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def _parse_inputs(
    payload: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]], int] | dict[str, Any]:
    """Pure-ish: validate inputs. Returns the parsed tuple or an error envelope."""
    control = payload.get("control")
    if not isinstance(control, str) or not control.strip():
        return _err(
            f"{_AGENT_SLUG}.invalid_input",
            "control must be a non-empty string",
        )
    pr_ref = payload.get("pr_ref")
    if not isinstance(pr_ref, str) or not pr_ref.strip():
        return _err(
            f"{_AGENT_SLUG}.invalid_input",
            "pr_ref must be a non-empty string",
        )
    results = payload.get("check_results")
    if not isinstance(results, list):
        return _err(
            f"{_AGENT_SLUG}.invalid_input",
            "check_results must be a list of objects",
        )
    cleaned: list[dict[str, Any]] = []
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            return _err(
                f"{_AGENT_SLUG}.invalid_input",
                f"check_results[{i}] must be an object",
            )
        cid = r.get("check_id")
        if not isinstance(cid, str) or not cid.strip():
            return _err(
                f"{_AGENT_SLUG}.invalid_input",
                f"check_results[{i}].check_id must be a non-empty string",
            )
        if "passed" not in r or not isinstance(r["passed"], bool):
            return _err(
                f"{_AGENT_SLUG}.invalid_input",
                f"check_results[{i}].passed must be a boolean",
            )
        cleaned.append({
            "check_id": cid.strip(),
            "passed": bool(r["passed"]),
            "evidence": str(r.get("evidence", "")).strip()[:1000],
        })
    budget_cents = _clamp_int(
        payload.get("budget_cents"), _DEFAULT_BUDGET_CENTS, 1, _HARD_MAX_BUDGET_CENTS,
    )
    return control.strip().upper(), pr_ref.strip(), cleaned, budget_cents


# ---------------------------------------------------------------------------
# LLM passes
# ---------------------------------------------------------------------------


def _llm_failure_rationale(
    trace: TraceRecorder, control: str, failure: dict[str, Any], budget_cents: int,
) -> str:
    """One LLM call per failed check, recording the rationale."""
    with trace.step(
        "llm_failure_rationale",
        inputs_summary={"control": control, "check_id": failure["check_id"]},
    ):
        user = (
            f"Control: {control}\nFailed check: {failure['check_id']}\n"
            f"Caller evidence: {failure.get('evidence', '(none)')}\n\n"
            "Return JSON only."
        )
        req = CompletionRequest(
            model="",
            messages=[
                Message(role="system", content=_RATIONALE_SYSTEM),
                Message(role="user", content=user),
            ],
            temperature=0.1,
            max_tokens=200,
        )
        response = run_with_fallback(req, budget_cents=budget_cents)
        trace.record_llm_call()
        rationale = _parse_rationale_json(response.text or "")
        trace.record_outputs({"rationale_len": len(rationale)})
        return rationale


def _llm_attestation_summary(
    trace: TraceRecorder,
    control: str,
    pr_ref: str,
    results: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    budget_cents: int,
) -> str:
    """Final synthesis LLM call: paragraph for the signed manifest."""
    with trace.step(
        "llm_attestation_summary",
        inputs_summary={"control": control, "pr_ref": pr_ref,
                        "fail_count": len(failed)},
    ):
        compact = [
            {"check_id": r["check_id"], "passed": r["passed"]}
            for r in results
        ]
        user = (
            f"Control: {control}\nPR: {pr_ref}\n"
            f"Checks: {json.dumps(compact, separators=(',', ':'))}\n"
            f"Failures: {json.dumps([f['check_id'] for f in failed])}\n\n"
            "Return JSON only."
        )
        req = CompletionRequest(
            model="",
            messages=[
                Message(role="system", content=_SUMMARY_SYSTEM),
                Message(role="user", content=user),
            ],
            temperature=0.1,
            max_tokens=350,
        )
        response = run_with_fallback(req, budget_cents=budget_cents)
        trace.record_llm_call()
        summary = _parse_summary_json(response.text or "")
        trace.record_outputs({"summary_len": len(summary)})
        return summary


def _parse_rationale_json(raw: str) -> str:
    """Pure-ish: extract rationale text from LLM JSON; degrade gracefully."""
    try:
        body = json.loads(raw.strip().lstrip("`").lstrip("json").strip("`").strip())
    except (TypeError, ValueError):
        return "(LLM did not return parseable JSON)"
    if not isinstance(body, dict):
        return "(LLM JSON was not an object)"
    return str(body.get("rationale", "")).strip()[:400] or "(empty rationale)"


def _parse_summary_json(raw: str) -> str:
    """Pure-ish: extract summary text from LLM JSON; degrade gracefully."""
    try:
        body = json.loads(raw.strip().lstrip("`").lstrip("json").strip("`").strip())
    except (TypeError, ValueError):
        return "(LLM did not return parseable JSON)"
    if not isinstance(body, dict):
        return "(LLM JSON was not an object)"
    return str(body.get("summary", "")).strip()[:1500] or "(empty summary)"


# ---------------------------------------------------------------------------
# Manifest construction + signing
# ---------------------------------------------------------------------------


def _build_manifest(
    control: str,
    pr_ref: str,
    results: list[dict[str, Any]],
    summary: str,
) -> dict[str, Any]:
    """Pure: assemble the manifest dict that will be signed.

    Why a pure helper: the same dict shape is fed BOTH into the signing
    function AND into the public output, so any divergence between the
    signed bytes and the returned bytes would silently invalidate the
    attestation. Centralising the shape here prevents that.
    """
    return {
        "schema": _ATTESTATION_SCHEMA,
        "control": control,
        "pr_ref": pr_ref,
        "issued_at": _now_iso(),
        "checks": [
            {
                "check_id": r["check_id"],
                "passed": r["passed"],
                "evidence": r.get("evidence", ""),
            }
            for r in results
        ],
        "summary": summary,
        "did": _identity.build_workspace_did(suffix="attestations:compliance"),
    }


def _sign_manifest(manifest: dict[str, Any]) -> str:
    """Sign the canonical JSON of manifest with the compliance signing key."""
    private_pem, _ = _load_or_create_compliance_signing_keypair()
    return _crypto.sign_payload(private_pem, manifest)


def _load_or_create_compliance_signing_keypair() -> tuple[str, str]:
    """Load (or create-and-persist) the per-server compliance signing key.

    Mirrors core.workspaces._load_or_create_signing_keypair but stores at
    data/compliance_signing_key.pem so the workspace and attestation keys
    can be rotated independently.
    """
    path = os.environ.get(
        "AZTEA_COMPLIANCE_SIGNING_KEY_PATH", _DEFAULT_SIGNING_KEY_PATH,
    )
    marker = "\n---PUBLIC---\n"
    if os.path.exists(path):
        with open(path, "r", encoding="ascii") as f:
            content = f.read()
        if marker in content:
            private_pem, public_pem = content.split(marker, 1)
            return private_pem, public_pem

    private_pem, public_pem = _crypto.generate_signing_keypair()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # Atomic O_CREAT|O_EXCL|O_WRONLY with 0o600 — same pattern as
    # core.workspaces uses, so the same hardening (HARDEN-1 audit
    # 2026-05-20) protects this key.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(private_pem)
            f.write(marker)
            f.write(public_pem)
    except FileExistsError:
        # Concurrent creation won the race — re-read.
        with open(path, "r", encoding="ascii") as f:
            content = f.read()
        if marker in content:
            return content.split(marker, 1)
        raise
    return private_pem, public_pem


def _now_iso() -> str:
    """Pure: UTC ISO-8601 'Z'-suffixed."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_trace(trace: TraceRecorder) -> dict[str, Any]:
    """Serialise the trace; never raise so it can land inside an error envelope."""
    try:
        return trace.to_dict()
    except Exception as exc:
        _LOG.warning("trace serialisation failed: %s", exc)
        return {"version": 1, "step_count": 0, "steps": [],
                "total_llm_calls": 0, "total_duration_ms": 0,
                "error": f"trace.to_dict failed: {exc}"}
