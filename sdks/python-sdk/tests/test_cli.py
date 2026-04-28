from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from typer.testing import CliRunner

from aztea.cli import app


runner = CliRunner()


@dataclass
class _FakeAgent:
    agent_id: str
    name: str
    price_per_call_usd: float = 0.05
    trust_score: float = 90.0
    success_rate: float = 0.95


class _FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        self.auth = self

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def close(self) -> None:
        return None

    def me(self) -> dict:
        return {"username": "alice"}

    def list_agents(self):
        return [_FakeAgent(agent_id="agent-1", name="Web Researcher")]

    def get_agent(self, agent_id: str):
        return _FakeAgent(agent_id=agent_id, name="Web Researcher")

    def search_agents(self, query: str, **kwargs):
        return [_FakeAgent(agent_id="agent-1", name=f"Search:{query}")]

    def hire(self, agent_id: str, payload: dict):
        return type("Result", (), {"job_id": "job-1", "cost_cents": 11, "output": payload})()


def test_cli_login_with_api_key_saves_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("aztea.cli.AzteaClient", _FakeClient)
    result = runner.invoke(app, ["login", "--api-key", "az_test", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["saved"] is True
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["api_key"] == "az_test"


def test_cli_hire_reads_file_payload(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("aztea.cli._client", lambda **kwargs: _FakeClient())
    input_path = tmp_path / "payload.json"
    input_path.write_text('{"query":"anthropic news"}')
    result = runner.invoke(app, ["hire", "web-researcher", "--input", f"@{input_path}", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["job_id"] == "job-1"
    assert payload["output"]["query"] == "anthropic news"


def test_cli_agents_list_renders_json(monkeypatch) -> None:
    monkeypatch.setattr("aztea.cli._client", lambda **kwargs: _FakeClient())
    result = runner.invoke(app, ["agents", "list", "--search", "pdf", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload[0]["agent_id"] == "agent-1"
