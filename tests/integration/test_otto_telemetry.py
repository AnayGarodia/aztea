"""
Integration tests for the Otto telemetry surface:

  POST /otto/telemetry      ingest + dedup + auth + validation
  GET  /admin/otto/metrics  admin gate + section dispatch + window validation
  GET  /otto/download       counted redirect + records a download event

Each event the app sends carries a client-generated event_id, so a replayed
batch (retry / offline-queue flush) must not double-count — that's the headline
invariant exercised here.
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration.helpers import TEST_MASTER_KEY, _auth_headers

OTTO_TOKEN = "test-otto-app-token"


@pytest.fixture(autouse=True)
def _otto_env(monkeypatch):
    monkeypatch.setenv("OTTO_APP_TOKEN", OTTO_TOKEN)
    monkeypatch.setenv("OTTO_DMG_URL", "https://aztea.ai/otto/Otto-9.9.9.dmg")


def _otto_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OTTO_TOKEN}"}


def _task_event(**overrides):
    props = {
        "intent_category": "form_fill",
        "app": "Gmail",
        "summon": "typed",
        "outcome": "success",
        "failure_reason": "none",
        "step_count": 4,
        "from_recipe": False,
        "latency_ms": {"ttfa": 300, "total": 4200, "perceive": 800, "model": 2500, "act": 500, "verify": 400},
        "path": {"ax": 3, "dom": 0, "vision": 1},
        "models": [{"name": "gpt-5.5", "ms": 2500, "calls": 2}],
        "tokens": {"input": 1200, "output": 300},
        "cost_usd": 0.018,
    }
    props.update(overrides.pop("props", {}))
    event = {
        "event_id": overrides.pop("event_id", str(uuid.uuid4())),
        "event": "task",
        "schema_version": 1,
        "device_id": overrides.pop("device_id", "dev-1"),
        "session_id": "s1",
        "app_version": "0.5.4",
        "os_version": "macOS 15.5",
        "mac_model": "Mac14,2",
        "ts_client": "2026-06-29T00:00:00Z",
        "props": props,
    }
    event.update(overrides)
    return event


# ── Ingest ──────────────────────────────────────────────────────────────────


def test_ingest_requires_token(client):
    r = client.post("/otto/telemetry", json={"events": [_task_event()]})
    assert r.status_code == 401, r.text


def test_ingest_rejects_bad_token(client):
    r = client.post(
        "/otto/telemetry",
        headers={"Authorization": "Bearer nope"},
        json={"events": [_task_event()]},
    )
    assert r.status_code == 401, r.text


def test_ingest_requires_events_key(client):
    r = client.post("/otto/telemetry", headers=_otto_headers(), json={"nope": []})
    assert r.status_code == 400, r.text


def test_ingest_accepts_and_dedups(client):
    ev = _task_event()
    first = client.post("/otto/telemetry", headers=_otto_headers(), json={"events": [ev]})
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 1

    # Replay the exact same event_id — must be a duplicate, not a second row.
    again = client.post("/otto/telemetry", headers=_otto_headers(), json={"events": [ev]})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["accepted"] == 0
    assert body["duplicates"] == 1


def test_ingest_rejects_invalid_events(client):
    batch = {
        "events": [
            {"event": "task"},                       # no event_id
            {"event": "bogus", "event_id": "x"},     # bad event type
            _task_event(),                           # valid
        ]
    }
    r = client.post("/otto/telemetry", headers=_otto_headers(), json=batch)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 2


# ── Dashboard metrics ───────────────────────────────────────────────────────


def test_metrics_requires_admin(client):
    from tests.integration.helpers import _register_user

    non_admin = _register_user()
    r = client.get(
        "/admin/otto/metrics?section=overview",
        headers=_auth_headers(non_admin["raw_api_key"]),
    )
    assert r.status_code == 403, r.text


def test_admin_email_allowlist_grants_dashboard(client, monkeypatch):
    """A user whose email is in ADMIN_EMAILS reaches the dashboard with only
    default (caller,worker) key scopes — admin is derived from the email, so it
    survives session-key re-minting. And /auth/me reports admin so the UI gate
    opens."""
    import uuid as _uuid

    from core import auth

    email = f"founder-{_uuid.uuid4().hex[:8]}@example.com"
    user = auth.register_user(username=f"f{_uuid.uuid4().hex[:6]}", email=email, password="password123")
    key = user["raw_api_key"]

    # Without the allowlist: plain user → 403.
    denied = client.get("/admin/otto/metrics?section=overview", headers=_auth_headers(key))
    assert denied.status_code == 403, denied.text

    # With the allowlist: same key, now admin via email.
    monkeypatch.setenv("ADMIN_EMAILS", f"someone@else.com,{email.upper()}")
    ok = client.get("/admin/otto/metrics?section=overview", headers=_auth_headers(key))
    assert ok.status_code == 200, ok.text

    me = client.get("/auth/me", headers=_auth_headers(key))
    assert me.status_code == 200, me.text
    assert "admin" in me.json()["scopes"]


def test_metrics_overview_reflects_ingest(client):
    client.post("/otto/telemetry", headers=_otto_headers(), json={"events": [
        _task_event(device_id="dev-a", props={"outcome": "success"}),
        _task_event(device_id="dev-b", props={"outcome": "failed", "failure_reason": "stale_ref"}),
    ]})
    r = client.get(
        "/admin/otto/metrics?section=overview&window=30d",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["tasks"] >= 2
    assert data["active_devices"]["value"] >= 2
    assert 0.0 <= data["success_rate"] <= 1.0


def test_metrics_unknown_section_400(client):
    r = client.get(
        "/admin/otto/metrics?section=nonsense",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert r.status_code == 400, r.text


def test_metrics_all_sections_load(client):
    client.post("/otto/telemetry", headers=_otto_headers(), json={"events": [_task_event()]})
    r = client.get("/admin/otto/metrics", headers=_auth_headers(TEST_MASTER_KEY))
    assert r.status_code == 200, r.text
    sections = r.json()["sections"]
    for name in ("overview", "growth", "usage", "quality", "latency", "matrix", "cost", "reliability", "setup", "learning"):
        assert name in sections, name


# ── Download redirect ───────────────────────────────────────────────────────


def test_download_redirects_and_counts(client):
    r = client.get("/otto/download?platform=mac&utm_source=twitter", follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "https://aztea.ai/otto/Otto-9.9.9.dmg"

    # The click should have recorded a download event the dashboard can see.
    metrics = client.get(
        "/admin/otto/metrics?section=overview",
        headers=_auth_headers(TEST_MASTER_KEY),
    )
    assert metrics.json()["data"]["downloads"]["value"] >= 1
