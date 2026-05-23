"""Tests for hour-scale lease support (Build 3)."""

from __future__ import annotations

import importlib
import os

import pytest

from core.jobs.db import MAX_LONG_LEASE_SECONDS, _validate_lease_seconds


# ---------------------------------------------------------------------------
# _validate_lease_seconds (pure helper)
# ---------------------------------------------------------------------------


def test_validates_positive_lease():
    assert _validate_lease_seconds(60) == 60
    assert _validate_lease_seconds(3600) == 3600  # 1 hour
    assert _validate_lease_seconds(MAX_LONG_LEASE_SECONDS) == MAX_LONG_LEASE_SECONDS


def test_rejects_zero_and_negative():
    with pytest.raises(ValueError, match="> 0"):
        _validate_lease_seconds(0)
    with pytest.raises(ValueError, match="> 0"):
        _validate_lease_seconds(-100)


def test_rejects_above_cap():
    with pytest.raises(ValueError, match=f"<= {MAX_LONG_LEASE_SECONDS}"):
        _validate_lease_seconds(MAX_LONG_LEASE_SECONDS + 1)
    with pytest.raises(ValueError, match=f"<= {MAX_LONG_LEASE_SECONDS}"):
        _validate_lease_seconds(999_999)


def test_rejects_non_int_types():
    with pytest.raises(ValueError, match="must be an int"):
        _validate_lease_seconds(3600.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be an int"):
        _validate_lease_seconds("3600")  # type: ignore[arg-type]


def test_rejects_booleans():
    """Booleans are ints in Python; we explicitly reject them so a stray
    True doesn't quietly become a 1-second lease."""
    with pytest.raises(ValueError, match="must be an int"):
        _validate_lease_seconds(True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Default cap value
# ---------------------------------------------------------------------------


def test_default_cap_is_four_hours():
    assert MAX_LONG_LEASE_SECONDS == 14_400


def test_env_override_changes_cap(monkeypatch):
    """The cap is read from the env at module import; reloading picks up
    the override. This proves operators can extend without a code change."""
    monkeypatch.setenv("AZTEA_MAX_LONG_LEASE_SECONDS", "7200")
    import core.jobs.db as db_module
    importlib.reload(db_module)
    try:
        assert db_module.MAX_LONG_LEASE_SECONDS == 7200
        # Within the new cap: passes.
        assert db_module._validate_lease_seconds(7200) == 7200
        # Above the new cap: raises.
        with pytest.raises(ValueError):
            db_module._validate_lease_seconds(7201)
    finally:
        # Reset to default for downstream tests in the same session.
        monkeypatch.delenv("AZTEA_MAX_LONG_LEASE_SECONDS", raising=False)
        importlib.reload(db_module)


# ---------------------------------------------------------------------------
# Cap is enforced through claim_job
# ---------------------------------------------------------------------------


def test_claim_job_rejects_lease_above_cap():
    """claim_job uses _validate_claim_params which delegates to
    _validate_lease_seconds; an over-cap value must surface a ValueError
    before any DB write."""
    from core.jobs import leases as leases_module

    # _validate_claim_params returns an Err result; raise_on_err() raises.
    result = leases_module._validate_claim_params(
        claim_owner_id="worker:test",
        lease_seconds=999_999,
    )
    with pytest.raises(ValueError, match=f"<= {MAX_LONG_LEASE_SECONDS}"):
        result.raise_on_err()


def test_claim_job_accepts_one_hour_lease():
    """An hour-scale lease must validate cleanly so reasoning agents can
    take long claims without surprise rejection."""
    from core.jobs import leases as leases_module

    result = leases_module._validate_claim_params(
        claim_owner_id="worker:test",
        lease_seconds=3600,
    )
    result.raise_on_err()
    owner_id, lease = result.value
    assert owner_id == "worker:test"
    assert lease == 3600


def test_claim_job_rejects_empty_owner_id():
    """Pre-existing invariant must still hold after the refactor."""
    from core.jobs import leases as leases_module

    result = leases_module._validate_claim_params(
        claim_owner_id="",
        lease_seconds=60,
    )
    with pytest.raises(ValueError, match="non-empty"):
        result.raise_on_err()
