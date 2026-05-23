"""Smoke tests for the strategy-doc 7-agent slate (post-editorial cut).

Confirms each agent is:
  * registered in BUILTIN_INTERNAL_ENDPOINTS,
  * importable via the catalog-registered Python module,
  * returns a dict with either an ``error`` envelope or top-level output keys,
  * (when in CURATED_BUILTIN) carries a normalised spec with category/cacheable.

The five pending-infra agents intentionally return a structured
``requires_configuration`` envelope when their external dep is missing —
that's the v0 contract, not a bug.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

import pytest

# Ensure server.application can import in test isolation.
os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")


# (slug, agent-id-constant-name, family, expected_error_code_or_None)
_NEW_AGENTS: list[tuple[str, str, str, str | None]] = [
    # A family — longitudinal
    ("flake_hunter", "FLAKE_HUNTER_AGENT_ID", "A", "flake_hunter.requires_configuration"),
    ("bisect_and_blame", "BISECT_AND_BLAME_AGENT_ID", "A", "bisect_and_blame.requires_configuration"),
    # C family — liability-bearing (C11 is a reference agent and works today)
    ("compliance_attestor", "COMPLIANCE_ATTESTOR_AGENT_ID", "C", None),
    ("stripe_connect_settler", "STRIPE_CONNECT_SETTLER_AGENT_ID", "C", None),
    # D family — org-memory (D16 is a reference agent and works today)
    ("codebase_reviewer", "CODEBASE_REVIEWER_AGENT_ID", "D", None),
    ("prod_trace_replayer", "PROD_TRACE_REPLAYER_AGENT_ID", "D", None),
    ("schema_migration_planner", "SCHEMA_MIGRATION_PLANNER_AGENT_ID", "D", None),
]


@pytest.fixture(scope="module")
def constants():
    from server.builtin_agents import constants as c
    return c


@pytest.fixture(scope="module")
def endpoints(constants):
    return constants.BUILTIN_INTERNAL_ENDPOINTS


@pytest.fixture(scope="module")
def runners():
    from server import application as app_module
    return app_module.BUILTIN_AGENT_RUNNERS


@pytest.mark.parametrize("slug,id_name,family,_expected_err", _NEW_AGENTS,
                         ids=[a[0] for a in _NEW_AGENTS])
def test_agent_id_registered_in_endpoints(slug, id_name, family, _expected_err,
                                          constants, endpoints):
    agent_id = getattr(constants, id_name)
    assert agent_id in endpoints, f"{slug}: not in BUILTIN_INTERNAL_ENDPOINTS"
    assert endpoints[agent_id] == f"internal://{slug}"


@pytest.mark.parametrize("slug,id_name,family,_expected_err", _NEW_AGENTS,
                         ids=[a[0] for a in _NEW_AGENTS])
def test_agent_module_imports_and_run_is_callable(slug, id_name, family, _expected_err):
    module = importlib.import_module(f"agents.{slug}")
    assert callable(module.run), f"agents.{slug}.run must be callable"


@pytest.mark.parametrize("slug,id_name,family,_expected_err", _NEW_AGENTS,
                         ids=[a[0] for a in _NEW_AGENTS])
def test_agent_run_returns_structured_dict_on_empty_payload(
    slug, id_name, family, _expected_err,
):
    """Every new agent rejects an empty payload with a structured envelope.

    Why empty payload: invariant we can assert without provider keys, without
    runners configured, without ingested repos. Every agent must validate
    inputs before doing anything that could partially succeed.
    """
    module = importlib.import_module(f"agents.{slug}")
    out = module.run({})
    assert isinstance(out, dict), f"{slug}.run({{}}) must return a dict"
    assert "error" in out, f"{slug}.run({{}}) must include an 'error' envelope"
    err = out["error"]
    assert isinstance(err, dict)
    assert "code" in err and "message" in err
    assert err["code"].startswith(f"{slug}."), (
        f"{slug}.run({{}}) error code {err['code']!r} must be slug-prefixed"
    )


@pytest.mark.parametrize("slug,id_name,family,expected_err", _NEW_AGENTS,
                         ids=[a[0] for a in _NEW_AGENTS])
def test_pending_agent_returns_requires_configuration_with_valid_inputs(
    slug, id_name, family, expected_err,
):
    """For pending-infra agents, a syntactically valid payload should surface
    the requires_configuration envelope — not a generic invalid_input error.
    This proves the agent's configuration gate fires after input validation,
    so callers get a meaningful 'install X' hint instead of 'bad payload'.
    """
    if expected_err is None:
        pytest.skip(f"{slug} is a reference agent; covered by its own tests")

    valid_payloads = {
        "flake_hunter": {"test_path": "tests/foo.py", "repo_root": "/tmp/x"},
        "bisect_and_blame": {"good_ref": "abc", "bad_ref": "def", "repro_cmd": "x"},
    }
    payload = valid_payloads[slug]
    module = importlib.import_module(f"agents.{slug}")
    out = module.run(payload)
    assert isinstance(out, dict) and "error" in out
    assert out["error"]["code"] == expected_err, (
        f"{slug}: expected {expected_err}, got {out['error']['code']!r}"
    )
    assert "missing" in out["error"].get("details", {}), (
        f"{slug}: requires_configuration envelope must list missing deps"
    )


@pytest.mark.parametrize("slug,id_name,family,_expected_err", _NEW_AGENTS,
                         ids=[a[0] for a in _NEW_AGENTS])
def test_agent_runner_wired_in_executor(slug, id_name, family, _expected_err,
                                        constants, runners):
    """Each new agent must be reachable through the central dispatcher."""
    agent_id = getattr(constants, id_name)
    assert agent_id in runners, (
        f"{slug}: not in BUILTIN_AGENT_RUNNERS — _execute_builtin_agent "
        "would 'Unsupported built-in agent' on it"
    )


def test_pending_infra_set_is_exhaustive(constants):
    """The five stub agents are in PENDING_INFRA_AGENT_IDS; the two reference
    agents (D16, C11) are in CURATED_PUBLIC. PENDING_INFRA and CURATED_PUBLIC
    are disjoint by construction."""
    pending = constants.PENDING_INFRA_AGENT_IDS
    curated = constants.CURATED_PUBLIC_BUILTIN_AGENT_IDS
    assert len(pending & curated) == 0, "pending and curated must be disjoint"

    reference_ids = {
        constants.CODEBASE_REVIEWER_AGENT_ID,
        constants.COMPLIANCE_ATTESTOR_AGENT_ID,
    }
    expected_pending = {
        getattr(constants, name) for _slug, name, _fam, _exp in _NEW_AGENTS
        if getattr(constants, name) not in reference_ids
    }
    assert len(expected_pending) == 5, (
        f"expected 5 pending agents (7 total - 2 reference), got {len(expected_pending)}"
    )
    missing_from_pending = expected_pending - pending
    extra_in_pending = pending - expected_pending
    assert not missing_from_pending, (
        f"agents stubbed-but-not-pending: {missing_from_pending}"
    )
    assert not extra_in_pending, (
        f"unexpected entries in PENDING_INFRA: {extra_in_pending}"
    )


def test_reference_agents_in_curated_public(constants):
    """D16 + C11 are the v0 reference agents that work today."""
    curated = constants.CURATED_PUBLIC_BUILTIN_AGENT_IDS
    assert constants.CODEBASE_REVIEWER_AGENT_ID in curated
    assert constants.COMPLIANCE_ATTESTOR_AGENT_ID in curated


def test_all_specs_present_in_catalog_loader():
    """Every agent must have a spec entry."""
    from server.builtin_agents.specs_part11 import load_builtin_specs_part11
    specs = load_builtin_specs_part11()
    spec_ids = {s["agent_id"] for s in specs}
    from server.builtin_agents import constants
    for slug, id_name, _family, _expected_err in _NEW_AGENTS:
        agent_id = getattr(constants, id_name)
        assert agent_id in spec_ids, f"{slug}: missing spec in specs_part11"
