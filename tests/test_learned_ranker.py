"""Phase 4 scaffold tests.

Honest framing per /autoplan E-3: ships as imitation + calibration,
not policy improvement. These tests cover the storage/load contract
and the pure predict/calibrate math. Real training pipeline is a
follow-up that needs 30+ days of Phase 3.5 data.
"""

from __future__ import annotations

import math
import uuid as _uuid

import pytest

from core import db as _db
from core.migrate import apply_migrations
from core.registry import learned_ranker as lr


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    db_path = tmp_path / f"ranker-{_uuid.uuid4().hex}.db"
    monkeypatch.setattr(_db, "DB_PATH", str(db_path))
    if hasattr(_db._local, "conns"):
        for c in list(_db._local.conns.values()):
            try:
                c.close()
            except Exception:
                pass
        _db._local.conns.clear()
    apply_migrations(str(db_path))
    lr.clear_cache()
    yield db_path
    lr.clear_cache()


# --- Pure math --------------------------------------------------------


def test_predict_is_linear():
    model = lr.ModelState(
        version="v0",
        weights={"a": 1.0, "b": 2.0},
        intercept=0.5,
        platt_a=-1.0, platt_b=0.0,
        feature_names=("a", "b"),
    )
    assert lr.predict({"a": 1.0, "b": 1.0}, model) == 0.5 + 1.0 + 2.0
    assert lr.predict({"a": 0.0, "b": 0.0}, model) == 0.5
    assert lr.predict({"a": -1.0, "b": -1.0}, model) == 0.5 - 1.0 - 2.0


def test_predict_missing_features_default_to_zero():
    model = lr.ModelState(
        version="v0", weights={"a": 1.0, "b": 2.0}, intercept=0.0,
        platt_a=-1.0, platt_b=0.0, feature_names=("a", "b"),
    )
    # Only `a` provided; `b` defaults to 0.
    assert lr.predict({"a": 1.0}, model) == 1.0


def test_calibrate_returns_probability_in_unit_interval():
    model = lr.ModelState(
        version="v0", weights={}, intercept=0.0,
        platt_a=-1.0, platt_b=0.0, feature_names=(),
    )
    for logit in (-100, -1, 0, 1, 100):
        p = lr.calibrate(float(logit), model)
        assert 0.0 <= p <= 1.0


def test_calibrate_monotonic_in_logit():
    model = lr.ModelState(
        version="v0", weights={}, intercept=0.0,
        platt_a=-1.0, platt_b=0.0, feature_names=(),
    )
    ps = [lr.calibrate(float(x), model) for x in (-2, -1, 0, 1, 2)]
    assert all(ps[i] <= ps[i + 1] for i in range(len(ps) - 1))


def test_calibrate_sigmoid_default_returns_half_at_zero():
    model = lr.ModelState(
        version="v0", weights={}, intercept=0.0,
        platt_a=-1.0, platt_b=0.0, feature_names=(),
    )
    assert abs(lr.calibrate(0.0, model) - 0.5) < 1e-6


# --- Storage round-trip -----------------------------------------------


def test_no_active_model_returns_none(fresh_db):
    assert lr.get_active_model() is None


def test_register_then_get_active(fresh_db):
    ok = lr.register_model(
        version="v1",
        weights={"string_signals": 0.4, "keyword_overrides": 0.3},
        intercept=-0.1,
        platt_a=-1.2,
        platt_b=0.05,
        notes="Phase 4 first imitation model",
        activate=True,
    )
    assert ok
    model = lr.get_active_model()
    assert model is not None
    assert model.version == "v1"
    assert model.weights == {
        "string_signals": 0.4, "keyword_overrides": 0.3,
    }
    assert abs(model.intercept - (-0.1)) < 1e-9
    assert abs(model.platt_a - (-1.2)) < 1e-9


def test_register_without_activate_does_not_change_active(fresh_db):
    lr.register_model(
        version="v1", weights={"a": 1.0}, intercept=0.0,
        platt_a=-1.0, platt_b=0.0, activate=True,
    )
    lr.register_model(
        version="v2", weights={"a": 2.0}, intercept=0.0,
        platt_a=-1.0, platt_b=0.0, activate=False,
    )
    model = lr.get_active_model()
    assert model is not None
    assert model.version == "v1"  # v2 registered but not active


def test_clear_cache_forces_reload(fresh_db):
    lr.register_model(
        version="v1", weights={"a": 1.0}, intercept=0.0,
        platt_a=-1.0, platt_b=0.0, activate=True,
    )
    m1 = lr.get_active_model()
    assert m1 is not None and m1.version == "v1"
    lr.register_model(
        version="v2", weights={"a": 2.0}, intercept=0.0,
        platt_a=-1.0, platt_b=0.0, activate=True,
    )
    lr.clear_cache()
    m2 = lr.get_active_model()
    assert m2 is not None and m2.version == "v2"
