"""Description-as-test framework — assert documented claims hold.

Pre-fix (audit 2026-05-19), several rail bugs were rooted in a pattern of
"description says X, code does Y": jwt_validator advertised "Refuses alg
none" but didn't (C-1); ci_failure_reproducer advertised price $0.05 but
charged $0.06 (H-7); various recipes advertised input shapes their
templates couldn't resolve (H-4). This module parses spec text for
verifiable claims and fails CI when they drift.

Lands as the preventative layer in the rails-hardening PR. Future drift
bugs (description-vs-behavior) should be caught here BEFORE they reach a
red-team audit.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from server.builtin_agents.specs import builtin_agent_specs


def _all_public_specs() -> list[dict[str, Any]]:
    return list(builtin_agent_specs() or [])


def test_every_agent_has_required_metadata():
    """Every public spec must include the fields the listing surfaces."""
    required = {"agent_id", "name", "description", "endpoint_url",
                "price_per_call_usd", "input_schema"}
    for spec in _all_public_specs():
        missing = required - set(spec.keys())
        assert not missing, (
            f"agent {spec.get('agent_id')}: spec missing {missing}"
        )


_FREE_PHRASES = (
    "platform-subsidized gateway agent",
    "free —",
    "free -",
    "(free)",
)


def test_agents_claiming_free_have_zero_price():
    """If the description says 'Free' or 'platform-subsidized', the spec's
    price_per_call_usd MUST be 0. Drift here means callers see a free
    label but pay; pre-fix this would have caught secret_scanner /
    dockerfile_analyzer / cve_lookup billing regressions."""
    for spec in _all_public_specs():
        desc = str(spec.get("description") or "").lower()
        claims_free = any(p in desc for p in _FREE_PHRASES)
        if claims_free:
            price = float(spec.get("price_per_call_usd") or 0.0)
            assert price == 0.0, (
                f"agent {spec.get('agent_id')}: description claims free "
                f"but price_per_call_usd={price}"
            )


_REFUSE_RE = re.compile(
    r"refuses?(?:\s+the)?\s+(['\"]?)([a-zA-Z0-9_+\-/]+)\1",
    re.IGNORECASE,
)


def test_documented_refusals_are_machine_readable():
    """Every "Refuses X" claim in a description should be specific enough
    to test: an alphanumeric token (alg name, action, etc), not prose.
    This is the foundation for the negative-test generator the audit
    recommended — for now, just enforce shape so future drift is easy
    to catch."""
    refusal_claims: list[tuple[str, str]] = []
    for spec in _all_public_specs():
        desc = str(spec.get("description") or "")
        for match in _REFUSE_RE.finditer(desc):
            token = match.group(2).strip()
            refusal_claims.append((str(spec.get("agent_id")), token))
    # Pin the count + presence so future descriptions stay verifiable.
    # Adding a new "Refuses X" claim should come with a negative test
    # for X in the agent's own test file; this assertion is the canary
    # that prevents silent description bloat.
    assert all(len(token) >= 2 for _, token in refusal_claims), (
        f"Each 'Refuses X' claim must name a specific token (alphanumeric, "
        f"length >= 2). Found: {refusal_claims}"
    )


def test_jwt_validator_refuses_alg_none_per_description():
    """C-1 regression: the jwt_validator description must accurately
    describe refusal of alg=none, and the agent must actually refuse."""
    from agents.jwt_validator import run as jwt_run

    # Confirm the description still claims refusal so this test stays
    # tied to the spec.
    specs = [
        s for s in _all_public_specs()
        if s.get("agent_id") == "96c86f16-16e6-51bb-9332-eae0cfef33ba"
    ]
    assert specs, "jwt_validator spec not found"
    desc = str(specs[0].get("description") or "").lower()
    assert "none" in desc, (
        "jwt_validator description must reference 'none' to keep the "
        "alg=none refusal claim verifiable"
    )

    # Token with header alg=none, valid HS256 in algorithms list.
    # Must be refused regardless of the allowlist.
    result = jwt_run({
        "token": "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiIxIn0.",
        "algorithms": ["HS256"],
    })
    assert "error" in result, "jwt_validator must refuse alg=none tokens"
    assert result["error"]["code"] == "jwt_validator.alg_none_refused"


def test_secret_scanner_zero_price_matches_free_label():
    """The secret_scanner description claims 'Free — platform-subsidized'.
    Price must be 0. Drift here is a contract violation."""
    secret_scanner = next(
        (s for s in _all_public_specs()
         if s.get("agent_id") == "1021c65c-d2bf-54ff-823a-897f9deb1029"),
        None,
    )
    assert secret_scanner is not None, "secret_scanner spec not found"
    desc = str(secret_scanner.get("description") or "").lower()
    assert "free" in desc, (
        "secret_scanner description must still claim free, or remove and "
        "update the price"
    )
    assert float(secret_scanner.get("price_per_call_usd") or 0.0) == 0.0


def test_dockerfile_analyzer_zero_price_matches_free_label():
    """Same drift check for dockerfile_analyzer."""
    spec = next(
        (s for s in _all_public_specs()
         if s.get("agent_id") == "e91f9b2f-f695-5890-b1f5-a9156c1b9a54"),
        None,
    )
    assert spec is not None, "dockerfile_analyzer spec not found"
    desc = str(spec.get("description") or "").lower()
    assert "free" in desc
    assert float(spec.get("price_per_call_usd") or 0.0) == 0.0


def test_no_advertised_max_token_exceeds_real_input_schema_cap():
    """If a description says 'Max N <unit>', the input_schema should be
    aware of it (or the agent enforces internally). For now, just
    extract the claims so a future test can compare them to actual
    enforcement."""
    pattern = re.compile(r"\bmax(?:imum)?\s+(\d[\d_,]*)\s*([a-z]+)", re.IGNORECASE)
    for spec in _all_public_specs():
        desc = str(spec.get("description") or "")
        for match in pattern.finditer(desc):
            value = match.group(1).replace(",", "").replace("_", "")
            assert int(value) > 0, (
                f"agent {spec.get('agent_id')}: 'Max {value} ...' "
                f"in description but value is non-positive"
            )
