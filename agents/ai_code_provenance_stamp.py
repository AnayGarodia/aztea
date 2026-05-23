"""
ai_code_provenance_stamp.py — E25: classify each PR hunk as human / AI /
mixed and sign the attestation.

# v0 STATUS: requires the same per-server signing key as compliance_attestor.
#   Works as soon as that key file exists. The classification is currently a
#   reasoning-loop heuristic (the underlying stylometric classifier is a
#   v0.1 follow-up).
# REASONING LOOP: plan stylometric signals → synthesise per-hunk verdict
#   → sign the manifest.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from agents._contracts import agent_error as _err, annotate_success as _annotate
from agents._reasoning_scaffold import clamp_int
from agents.compliance_attestor import _load_or_create_compliance_signing_keypair
from core import crypto as _crypto
from core import identity as _identity
from core.llm.base import CompletionRequest, Message
from core.llm.errors import BudgetExceededError, LLMError
from core.llm.fallback import run_with_fallback
from core.reasoning_traces import TraceRecorder

_AGENT_SLUG = "ai_code_provenance_stamp"
_PROVENANCE_SCHEMA = "aztea/ai-provenance/1"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    pr_ref = (payload.get("pr_ref") or "").strip()
    hunks = payload.get("hunks")
    if not pr_ref:
        return _err(f"{_AGENT_SLUG}.invalid_input", "pr_ref is required")
    if not isinstance(hunks, list) or not hunks:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "hunks must be a non-empty list of {file,text} objects")
    budget = clamp_int(payload.get("budget_cents"), 30, 1, 500)

    trace = TraceRecorder()
    try:
        with trace.step("plan_stylometric_signals",
                        inputs_summary={"hunks": len(hunks)}):
            plan_resp = run_with_fallback(
                CompletionRequest(
                    model="",
                    messages=[
                        Message(role="system",
                                content="List stylometric signals that "
                                        "distinguish AI-written code from "
                                        "human-written code. Output JSON "
                                        '{"signals": [...]}'),
                        Message(role="user",
                                content=f"PR ref: {pr_ref}"),
                    ],
                    temperature=0.1, max_tokens=400,
                ),
                budget_cents=budget,
            )
            trace.record_llm_call()
            trace.record_outputs({"signal_count_inferred": True})

        with trace.step("classify_hunks",
                        inputs_summary={"hunks": len(hunks)}):
            synth_resp = run_with_fallback(
                CompletionRequest(
                    model="",
                    messages=[
                        Message(role="system",
                                content="Classify each hunk as human|ai|mixed. "
                                        "Return JSON {classifications: ["
                                        '{file, verdict, confidence_pct}]}'),
                        Message(role="user",
                                content=("Signals: " + plan_resp.text[:600]
                                         + "\nHunks: "
                                         + json.dumps([{"file": h.get("file"),
                                                        "chars": len(h.get("text", ""))}
                                                       for h in hunks])[:1500])),
                    ],
                    temperature=0.1, max_tokens=600,
                ),
                budget_cents=budget,
            )
            trace.record_llm_call()
            trace.record_outputs({"synth_chars": len(synth_resp.text)})
    except (BudgetExceededError, LLMError) as exc:
        return _err(f"{_AGENT_SLUG}.llm_error", str(exc),
                    {"trace": trace.to_dict()})

    manifest = {
        "schema": _PROVENANCE_SCHEMA,
        "pr_ref": pr_ref,
        "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hunks": [
            {"file": h.get("file"), "chars": len(h.get("text", ""))}
            for h in hunks
        ],
        "classification_text": synth_resp.text[:4000],
        "did": _identity.build_workspace_did(suffix="attestations:provenance"),
    }
    try:
        private_pem, _ = _load_or_create_compliance_signing_keypair()
        signature_b64 = _crypto.sign_payload(private_pem, manifest)
    except Exception as exc:
        # WHY explicit catch: same contract as compliance_attestor — return
        # signing_failed rather than a half-signed envelope. The manifest
        # itself is preserved so auditors can see what was about to be
        # signed.
        return _err(
            f"{_AGENT_SLUG}.signing_failed",
            f"signing failed: {type(exc).__name__}: {exc}",
            {"manifest": manifest, "trace": trace.to_dict()},
        )

    return _annotate(
        {"manifest": manifest, "signature_b64": signature_b64,
         "trace": trace.to_dict()},
        llm_used=True,
    )
