"""Plan B Phase 3b (2026-05-27) — continuous endpoint health sweeper.

Detects agents whose registered endpoint_url has rotted (DNS expired,
deploy went down, SSL invalid) and auto-suspends them after N
consecutive failed probes.
"""

from __future__ import annotations

import uuid

from unittest.mock import patch

from core import observability, registry


def _register_test_agent(endpoint_url: str = "https://example.invalid/run") -> str:
    """Register an agent without any of the security-probe side effects."""
    import os
    os.environ["AZTEA_SKIP_REGISTER_ENDPOINT_PROBE"] = "1"
    aid = f"test-health-{uuid.uuid4().hex[:8]}"
    registry.register_agent(
        name=f"HealthAgent_{aid}",
        description="health sweep test",
        endpoint_url=endpoint_url,
        price_per_call_usd=0.05,
        tags=["test"],
        input_schema={"type": "object"},
        owner_id=f"user:{uuid.uuid4().hex[:8]}",
        embed_listing=False,
        agent_id=aid,
    )
    return aid


def test_threshold_default_is_three():
    """Default suspends after 3 consecutive failures."""
    assert observability._health_suspend_threshold() == 3


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("AZTEA_HEALTH_SUSPEND_THRESHOLD", "5")
    assert observability._health_suspend_threshold() == 5


def test_threshold_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("AZTEA_HEALTH_SUSPEND_THRESHOLD", "not-an-int")
    assert observability._health_suspend_threshold() == 3


def test_healthy_agent_resets_failure_streak():
    """A successful probe must reset the consecutive_health_failures counter."""
    aid = _register_test_agent()
    # Seed an existing failure streak via direct UPDATE.
    from core import db as _db
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        conn.execute(
            "UPDATE agents SET consecutive_health_failures = %s WHERE agent_id = %s",
            (2, aid),
        )
        conn.commit()
    with patch.object(observability, "_probe_endpoint_health", return_value=True):
        summary = observability.run_endpoint_health_sweep()
    assert summary["healthy"] >= 1
    refetched = registry.get_agent(aid, include_unapproved=True)
    assert refetched["consecutive_health_failures"] == 0
    assert refetched["last_health_status"] == "ok"


def test_failing_agent_increments_streak_below_threshold():
    """A single failure must NOT suspend — only the streak count grows."""
    aid = _register_test_agent()
    with patch.object(observability, "_probe_endpoint_health", return_value=False):
        observability.run_endpoint_health_sweep()
    refetched = registry.get_agent(aid, include_unapproved=True)
    assert refetched["consecutive_health_failures"] == 1
    assert refetched["status"] == "active"  # not yet suspended
    assert refetched["last_health_status"] == "failed"


def test_failing_agent_suspends_at_threshold():
    """Three consecutive failures must transition to suspended."""
    aid = _register_test_agent()
    with patch.object(observability, "_probe_endpoint_health", return_value=False):
        observability.run_endpoint_health_sweep()
        observability.run_endpoint_health_sweep()
        observability.run_endpoint_health_sweep()
    refetched = registry.get_agent(aid, include_unapproved=True)
    assert refetched["status"] == "suspended"
    assert refetched["suspension_reason"] == "health_check_failed"
    assert refetched["consecutive_health_failures"] >= 3


def test_sweep_skips_internal_and_skill_endpoints():
    """Aztea-hosted agents shouldn't be probed (no outbound URL)."""
    aid = _register_test_agent(endpoint_url="internal://my-builtin")
    with patch.object(observability, "_probe_endpoint_health", return_value=False):
        summary = observability.run_endpoint_health_sweep()
    refetched = registry.get_agent(aid, include_unapproved=True)
    # Should not have been touched.
    assert refetched["consecutive_health_failures"] == 0
    assert refetched["status"] == "active"


def test_suspended_agents_not_re_probed():
    """Once suspended, the sweeper leaves the agent alone."""
    aid = _register_test_agent()
    with patch.object(observability, "_probe_endpoint_health", return_value=False):
        for _ in range(3):
            observability.run_endpoint_health_sweep()
    # Now suspended. A successful probe should NOT auto-revive — that requires
    # admin action. The sweeper's SELECT filters status='active' so it won't
    # re-probe.
    calls = []
    def fake_probe(*args, **kwargs):
        calls.append(args)
        return True
    with patch.object(observability, "_probe_endpoint_health", side_effect=fake_probe):
        observability.run_endpoint_health_sweep()
    refetched = registry.get_agent(aid, include_unapproved=True)
    assert refetched["status"] == "suspended"
