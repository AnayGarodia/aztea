"""Server-side tests for `GET /ops/dispute-policy`.

Public read-only endpoint exposing the filing-deposit formula + judge
panel shape. Used by the CLI wizard to quote the exact deposit before
the user confirms. No auth required.
"""
from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

import server.application as server


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# Basic shape + auth
# ---------------------------------------------------------------------------


def test_dispute_policy_endpoint_responds_200(client: TestClient) -> None:
    resp = client.get("/ops/dispute-policy")
    assert resp.status_code == 200


def test_dispute_policy_does_not_require_auth(client: TestClient) -> None:
    resp = client.get("/ops/dispute-policy")
    # No Authorization header was sent — endpoint must accept anonymous reads.
    assert resp.status_code == 200


def test_dispute_policy_returns_expected_keys(client: TestClient) -> None:
    body = client.get("/ops/dispute-policy").json()
    expected_keys = {
        "filing_deposit_bps",
        "filing_deposit_min_cents",
        "default_dispute_window_hours",
        "judges_required",
        "judges_total",
        "formula",
    }
    assert expected_keys <= set(body.keys()), (
        f"Missing keys: {expected_keys - set(body.keys())}"
    )


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_dispute_policy_default_values_match_constants(
    client: TestClient, monkeypatch
) -> None:
    """With no env overrides, the policy reflects the canonical defaults."""
    monkeypatch.delenv("DISPUTE_FILING_DEPOSIT_BPS", raising=False)
    monkeypatch.delenv("DISPUTE_FILING_DEPOSIT_MIN_CENTS", raising=False)
    monkeypatch.delenv("DEFAULT_JOB_DISPUTE_WINDOW_HOURS", raising=False)
    body = client.get("/ops/dispute-policy").json()
    assert body["filing_deposit_bps"] == 500
    assert body["filing_deposit_min_cents"] == 5
    assert body["default_dispute_window_hours"] == 72
    assert body["judges_required"] == 2
    assert body["judges_total"] == 3


# ---------------------------------------------------------------------------
# Env-var overrides
# ---------------------------------------------------------------------------


def test_dispute_policy_reflects_env_var_override_bps(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("DISPUTE_FILING_DEPOSIT_BPS", "750")
    body = client.get("/ops/dispute-policy").json()
    assert body["filing_deposit_bps"] == 750


def test_dispute_policy_reflects_env_var_override_min_cents(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("DISPUTE_FILING_DEPOSIT_MIN_CENTS", "10")
    body = client.get("/ops/dispute-policy").json()
    assert body["filing_deposit_min_cents"] == 10


def test_dispute_policy_reflects_env_var_override_window(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("DEFAULT_JOB_DISPUTE_WINDOW_HOURS", "48")
    body = client.get("/ops/dispute-policy").json()
    assert body["default_dispute_window_hours"] == 48


def test_dispute_policy_invalid_env_var_uses_default(
    client: TestClient, monkeypatch
) -> None:
    """Garbage env var falls back to the default rather than 500-ing."""
    monkeypatch.setenv("DISPUTE_FILING_DEPOSIT_BPS", "not-a-number")
    body = client.get("/ops/dispute-policy").json()
    assert body["filing_deposit_bps"] == 500


def test_dispute_policy_empty_env_var_uses_default(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("DISPUTE_FILING_DEPOSIT_BPS", "")
    body = client.get("/ops/dispute-policy").json()
    assert body["filing_deposit_bps"] == 500


# ---------------------------------------------------------------------------
# Formula string
# ---------------------------------------------------------------------------


def test_dispute_policy_formula_is_string(client: TestClient) -> None:
    body = client.get("/ops/dispute-policy").json()
    formula = body.get("formula")
    assert isinstance(formula, str)
    assert formula.strip(), "formula should be a non-empty string"
    assert "deposit_cents" in formula
