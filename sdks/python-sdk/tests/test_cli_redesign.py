"""Tests for the CLI redesign: ``aztea try``, categorized ``agents list``,
and the ``aztea jobs hire`` / ``aztea jobs dispute`` deprecation shims.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from typer.testing import CliRunner

from aztea.cli import app


runner = CliRunner()


@dataclass
class _Agent:
    agent_id: str
    name: str
    price_per_call_usd: float = 0.05
    trust_score: float = 90.0
    success_rate: float = 0.95
    category: str = "Security"
    description: str = ""
    endpoint_url: str = ""
    tags: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    status: str = "active"


_CATALOG = [
    _Agent(agent_id="a-cve", name="CVE Lookup", price_per_call_usd=0.0, category="Security"),
    _Agent(agent_id="a-secret", name="Secret Scanner", price_per_call_usd=0.0, category="Security"),
    _Agent(agent_id="a-dockerfile", name="Dockerfile Analyzer", price_per_call_usd=0.0, category="Security"),
    _Agent(agent_id="a-py", name="Python Executor", price_per_call_usd=0.02, category="Code Execution"),
    _Agent(agent_id="a-multilang", name="Multi Language Executor", price_per_call_usd=0.03, category="Code Execution"),
    _Agent(agent_id="a-codereview", name="Code Reviewer", price_per_call_usd=0.05, category="Quality"),
    _Agent(agent_id="a-browser", name="Browser Agent", price_per_call_usd=0.10, category="Web"),
    _Agent(agent_id="a-unknown", name="Mystery Bucket", price_per_call_usd=0.04, category="WeirdCategory"),
    _Agent(agent_id="a-blank", name="Uncategorized Agent", price_per_call_usd=0.01, category=""),
]


class _Hire:
    def __init__(self, output: dict[str, Any]) -> None:
        self.job_id = "job-demo-1"
        self.cost_cents = 0
        self.output = output


class _Client:
    """Test double that satisfies the slice of AzteaClient the CLI calls."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.auth = self
        self.base_url = "http://test"

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def me(self) -> dict:
        return {"username": "alice"}

    def list_agents(self):
        return list(_CATALOG)

    def search_agents(self, query: str, **kwargs):
        return [a for a in _CATALOG if query.lower() in a.name.lower()]

    def get_agent(self, agent_id: str):
        for agent in _CATALOG:
            if agent.agent_id == agent_id:
                return agent
        raise KeyError(agent_id)

    def hire(self, agent_id: str, payload: dict):
        # Echo the payload back so tests can assert what was sent.
        return _Hire(output={"agent_id": agent_id, "payload_echo": payload})


@pytest.fixture
def patched_client(monkeypatch):
    monkeypatch.setattr("aztea.cli._client", lambda **kwargs: _Client())
    monkeypatch.setattr("aztea.cli.AzteaClient", _Client)
    return _Client


# ── aztea agents list — categorization ────────────────────────────────────

def test_agents_list_groups_by_category_by_default(patched_client) -> None:
    result = runner.invoke(app, ["agents", "list"])
    assert result.exit_code == 0, result.stdout
    # Bucket headers should appear in canonical order.
    out = result.stdout
    assert "Security" in out
    assert "Code Execution" in out
    assert "Quality" in out
    assert "Web" in out
    # Unknown category falls into "Other" (preserved, not dropped).
    assert "Other" in out
    # Blank-category agent falls into "Uncategorized".
    assert "Uncategorized" in out


def test_agents_list_category_filter_isolates_one_bucket(patched_client) -> None:
    result = runner.invoke(app, ["agents", "list", "--category", "Security", "--json"])
    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert {r["agent_id"] for r in rows} == {"a-cve", "a-secret", "a-dockerfile"}


def test_agents_list_free_flag_drops_priced_agents(patched_client) -> None:
    result = runner.invoke(app, ["agents", "list", "--free", "--json"])
    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)
    assert {r["agent_id"] for r in rows} == {"a-cve", "a-secret", "a-dockerfile"}


def test_agents_list_flat_renders_single_table(patched_client) -> None:
    result = runner.invoke(app, ["agents", "list", "--flat"])
    assert result.exit_code == 0, result.stdout
    # Flat output shouldn't have category section headers.
    out = result.stdout
    assert "Security" not in out or "Code Execution" not in out


# ── deprecation shims ──────────────────────────────────────────────────────

def test_jobs_hire_alias_still_works_and_warns(patched_client) -> None:
    result = runner.invoke(
        app,
        ["jobs", "hire", "cve-lookup", "--input", '{"cve_id":"CVE-2021-44228"}', "--json"],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["job_id"] == "job-demo-1"


def test_jobs_dispute_alias_help_marked_deprecated() -> None:
    result = runner.invoke(app, ["jobs", "--help"])
    assert result.exit_code == 0, result.stdout
    assert "DEPRECATED" in result.stdout
    # Both deprecated commands surface as DEPRECATED in the sub-app help.
    assert result.stdout.count("DEPRECATED") >= 2


# ── help epilogs ───────────────────────────────────────────────────────────
#
# CliRunner uses Click's default terminal width which can be narrow enough
# that Rich truncates multi-line --help output. Setting COLUMNS=120 gives
# Rich room to render the full help including the epilog.

_WIDE = {"COLUMNS": "120"}


def test_hire_help_includes_see_also_pointer() -> None:
    result = runner.invoke(app, ["hire", "--help"], env=_WIDE)
    assert result.exit_code == 0
    assert "See also" in result.stdout


# ── splash content ─────────────────────────────────────────────────────────

def test_root_no_repl_shows_banner_with_slash_commands(monkeypatch, tmp_path) -> None:
    """`aztea --no-repl` should render the V3 banner (V1 visual + slash cmds).

    In CliRunner the process is non-TTY, so the root callback hits the
    `_repl_disabled` branch and prints the banner instead of opening the
    REPL. We assert the slash-command vocabulary is what's surfaced — the
    V1 tagline and the V2 try-rows must both be gone.
    """
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(tmp_path))
    result = runner.invoke(app, ["--no-repl"])
    assert result.exit_code == 0
    out = result.stdout
    # Quickstart panel lists slash commands now, not bash commands.
    assert "/login" in out
    assert "/claude-code" in out
    assert "/help" in out
    # /agents was removed from the unauth panel — server rejects
    # /registry/agents without an API key, so showing it as the
    # second-most-discoverable command sets a bad first-run expectation.
    assert "/agents" not in out
    # V1 tagline must not regress back.
    assert "hire specialist agents from your terminal" not in out
    # V2 try-rows must not regress back.
    assert "aztea try cve-lookup" not in out
    # V3 state line was dropped — auth state lives on the REPL toolbar.
    assert "not signed in" not in out
