"""Reliability evidence for a candidate listing.

# OWNS: three reliability checks on a publish candidate —
#   1. ``validate_response_against_schema`` (inline, BLOCK on schema-invalid),
#   2. ``probe_repeatability`` (async, WARN on flakiness),
#   3. ``skill_dry_run`` (async, WARN when a hosted SKILL.md errors at run time).
# NOT OWNS: the probe transport (``core.listing_probe_core``), the security/leak
#   scanners (``core.listing_safety_probe``), or the registration-policy
#   "at-least-one-probe-must-succeed" rule (that stays in the server wrapper).
# INVARIANTS:
#   - Pure validators never raise on caller-supplied content; they return findings.
#   - Only schema-invalid is a BLOCK here. Flakiness and dry-run failures are WARN
#     (advisory → probation) per the 2026-06-03 enforcement decision.
# DECISIONS:
#   - ``skill_dry_run`` degrades to *no finding* when no LLM provider is configured,
#     so an offline/OSS deploy never mislabels a fine skill as flaky.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import jsonschema

from core import listing_probe_core as _probe
from core.listing_safety import (
    LEVEL_BLOCK,
    LEVEL_WARN,
    VerificationFinding,
    synthesize_input_from_schema,
)

_LOG = logging.getLogger(__name__)

# Inline schema-validation block code (split out of the old catch-all
# ``listing.unreliable`` so the remediation text can be specific — DX2).
CODE_SCHEMA_INVALID = "listing.unreliable.schema"
CODE_FLAKY = "listing.flaky"
CODE_DRY_RUN_FAILED = "listing.flaky.dry_run"

# How many times the async pass replays the same synthetic input to gauge
# flakiness, and the success-rate floor below which we WARN.
_RELIABILITY_PROBE_SAMPLES = 3
_RELIABILITY_MIN_SUCCESS_RATE = 0.67  # 2 of 3 must succeed

# Cap on how many jsonschema errors we surface — enough to be actionable
# without dumping a wall of text into the publish response.
_MAX_SCHEMA_ERRORS = 5


def validate_response_against_schema(
    body: Any, output_schema: dict[str, Any] | None,
) -> list[VerificationFinding]:
    """Pure: BLOCK finding when ``body`` violates a declared object ``output_schema``.

    Upgrades the old key-overlap WARN to a typed check. Only validates when the
    schema is a usable object schema (``type: object`` with ``properties``);
    anything looser is skipped (we can't meaningfully validate it).
    """
    if not _is_validatable_object_schema(output_schema):
        return []
    validator = jsonschema.Draft7Validator(output_schema)
    errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
    if not errors:
        return []
    details = [
        {"path": list(err.path), "message": err.message}
        for err in errors[:_MAX_SCHEMA_ERRORS]
    ]
    return [
        VerificationFinding(
            code=CODE_SCHEMA_INVALID,
            level=LEVEL_BLOCK,
            message=(
                "Endpoint response does not conform to the declared output_schema. "
                "Buyers rely on the schema to parse results; a non-conforming "
                "endpoint breaks every integration."
            ),
            detail={"errors": details, "error_count": len(errors)},
        )
    ]


def _is_validatable_object_schema(schema: Any) -> bool:
    return bool(
        isinstance(schema, dict)
        and schema
        and schema.get("type") == "object"
        and isinstance(schema.get("properties"), dict)
        and schema["properties"]
    )


def probe_repeatability(
    url: str,
    input_schema: dict[str, Any] | None,
    *,
    http_post: _probe.HttpPost,
    read_body: _probe.ReadBody | None = None,
    timeout: float = _probe.DEFAULT_PROBE_TIMEOUT,
    samples: int = _RELIABILITY_PROBE_SAMPLES,
) -> list[VerificationFinding]:
    """Advisory: replay one synthetic input ``samples`` times; WARN if flaky.

    Runs in the async pass — a flaky endpoint never blocks a publish, it just
    lowers the listing's standing in probation. Transport is injected so this
    stays pure of ``server/`` and easy to stub in tests.
    """
    read_body = read_body or _probe.read_probe_body
    payload = synthesize_input_from_schema(input_schema)
    nonce = _probe.new_probe_nonce()
    headers = _probe.build_probe_headers(nonce)
    successes = 0
    for i in range(max(1, samples)):
        resp = _probe.probe_once(
            url, payload, http_post=http_post, read_body=read_body,
            timeout=timeout, headers=headers, job_id=f"reliability-{nonce}-{i}",
        )
        if resp is not None and resp.ok:
            successes += 1
    rate = successes / max(1, samples)
    if rate >= _RELIABILITY_MIN_SUCCESS_RATE:
        return []
    return [
        VerificationFinding(
            code=CODE_FLAKY,
            level=LEVEL_WARN,
            message=(
                f"Endpoint answered only {successes}/{samples} repeat probes "
                "(below the reliability floor). Flaky agents get ranked down "
                "and price-capped in probation until they stabilise."
            ),
            detail={"successes": successes, "samples": samples, "success_rate": rate},
        )
    ]


def skill_dry_run(
    skill_row: dict[str, Any],
    input_schema: dict[str, Any] | None,
    *,
    executor: Callable[..., dict[str, Any]] | None = None,
    llm_available: bool | None = None,
) -> list[VerificationFinding]:
    """Advisory: execute a hosted SKILL.md once against a synthetic input.

    A hard execution error becomes a WARN (probation evidence, never a block).
    Degrades to *no finding* when no LLM provider is configured, so an offline
    deploy doesn't mislabel a perfectly good skill as broken.
    """
    if llm_available is None:
        llm_available = _any_llm_provider_configured()
    if not llm_available:
        return []
    if executor is None:
        from core.skill_executor import execute_hosted_skill as executor  # noqa: PLC0415
    payload = synthesize_input_from_schema(input_schema)
    try:
        executor(skill_row, payload or {"task": "ping"})
    except Exception as exc:  # noqa: BLE001 — any run failure is advisory evidence
        _LOG.info("skill dry-run failed for candidate listing: %s", exc)
        return [
            VerificationFinding(
                code=CODE_DRY_RUN_FAILED,
                level=LEVEL_WARN,
                message=(
                    "Hosted skill raised an error on a sample run during "
                    f"verification ({type(exc).__name__}). It will run in "
                    "probation until it executes cleanly."
                ),
                detail={"error_type": type(exc).__name__, "error": str(exc)[:300]},
            )
        ]
    return []


def _any_llm_provider_configured() -> bool:
    """True when at least one provider in the default chain resolves to a key.

    ``resolve(spec)`` raises ``ValueError`` when a provider has no configured
    key, so a clean return (no exception) means that spec is usable.
    """
    try:
        from core.llm.registry import DEFAULT_CHAIN, resolve  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — registry import shouldn't gate publishing
        _LOG.debug("could not import LLM registry; assuming no provider", exc_info=True)
        return False
    for spec in DEFAULT_CHAIN:
        try:
            resolve(spec)
            return True
        except Exception:  # noqa: BLE001 — one unresolved spec shouldn't gate the rest
            continue
    return False


__all__ = [
    "CODE_DRY_RUN_FAILED",
    "CODE_FLAKY",
    "CODE_SCHEMA_INVALID",
    "probe_repeatability",
    "skill_dry_run",
    "validate_response_against_schema",
]
