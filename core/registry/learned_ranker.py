# OWNS: Phase 4 (A1+A2) — learned ranker + calibrated confidence
#       SCAFFOLD. Reads model weights from ranker_model_weights (migration
#       0071). Feature vectors come from the existing scoring helpers in
#       auto_hire.py via extract_features(). All inference is pure given
#       fixed weights.
# NOT OWNS: training (training/ package); scoring helpers themselves
#       (auto_hire.py); the orchestrator wiring (auto_hire.py).
# INVARIANTS:
#   - Pure given fixed weights: same (feature_vector, model_state) →
#     same (score, calibrated_confidence) byte-for-byte.
#   - get_active_model() returns None when no model is active. Callers
#     fall back to the heuristic ranker.
#   - Honest framing: ships as imitation + calibration (per /autoplan
#     E-3). NDCG parity is the ship gate; no surpass claim.
# DECISIONS:
#   - Logistic regression on the feature vector. Interpretable,
#     calibratable via Platt scaling, runs in microseconds. Upgrade to
#     LambdaMART iff logistic ceiling is hit (unlikely at current vol).
#   - Feature vector schema is defined here (FEATURE_NAMES). Training
#     output must serialize weights in this order.
#   - Weights cached at process load; refresh via SIGHUP or version
#     bump in the active table.
# KNOWN DEBT:
#   - Training pipeline is a placeholder (training/extract_features.py
#     + training/train.py). Real training requires 30+ days of
#     feature_vector_json accumulation (Phase 3.5 ships the column).
#   - No IPW / propensity correction. See /autoplan E-3 — Phase 4
#     ships as imitation, not policy improvement.
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from core import db as _db

logger = logging.getLogger(__name__)


# Feature vector schema. Order matters: training output indexes by
# position. New features can be appended (never inserted in the middle)
# so old models keep working until retrained.
FEATURE_NAMES: tuple[str, ...] = (
    "string_signals",
    "quality_signals",
    "intent_interlocks",
    "keyword_overrides",
    "schema_shape",
    "semantic_similarity",
    "intent_class_fit",
    "utility_adjustment",
    "caller_affinity",
    "probation_penalty",
    "anti_catchall",
)


@dataclass(frozen=True)
class ModelState:
    """Loaded learned-ranker weights + Platt calibration coefficients."""
    version: str
    weights: dict[str, float]
    intercept: float
    platt_a: float  # P(y=1) = 1 / (1 + exp(platt_a * z + platt_b))
    platt_b: float
    feature_names: tuple[str, ...]


_active_model: ModelState | None = None
_active_loaded_at: float | None = None


def _now_iso_path() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_active_model() -> ModelState | None:
    """Side-effect (one DB read on cache miss). Cached at process scope.

    Returns None when no model is active OR when the table is missing
    (migration 0071 not yet applied). Callers (auto_hire.py) fall back
    to the heuristic ranker.
    """
    global _active_model
    if _active_model is not None:
        return _active_model
    try:
        with _db.get_raw_connection(_db.DB_PATH) as conn:
            active_row = conn.execute(
                "SELECT active_version FROM ranker_model_active "
                "WHERE singleton_key = 1"
            ).fetchone()
            if active_row is None or not active_row["active_version"]:
                return None
            version = str(active_row["active_version"])
            weights_row = conn.execute(
                "SELECT version, weights_json, calibration_json, "
                "       feature_names_json "
                "  FROM ranker_model_weights WHERE version = %s",
                (version,),
            ).fetchone()
            if weights_row is None:
                return None
            weights_raw = json.loads(weights_row["weights_json"] or "{}")
            calibration = json.loads(weights_row["calibration_json"] or "{}")
            feature_names_raw = json.loads(
                weights_row["feature_names_json"] or "[]"
            )
            intercept = float(weights_raw.pop("__intercept__", 0.0))
            platt_a = float(calibration.get("a", -1.0))
            platt_b = float(calibration.get("b", 0.0))
            state = ModelState(
                version=version,
                weights={k: float(v) for k, v in weights_raw.items()},
                intercept=intercept,
                platt_a=platt_a,
                platt_b=platt_b,
                feature_names=tuple(str(n) for n in feature_names_raw),
            )
            _active_model = state
            return state
    except _db.OperationalError:
        return None
    except Exception:  # noqa: BLE001 — never crash scoring
        logger.exception("learned_ranker: load failed")
        return None


def clear_cache() -> None:
    """Test hook + SIGHUP path. Operators do not call this in production."""
    global _active_model
    _active_model = None


def predict(features: dict[str, float], model: ModelState) -> float:
    """Pure: linear score under the model.

    Score = intercept + sum(weights[name] * features[name]).
    Returns the raw logit; use calibrate() to convert to P(5-star).
    """
    score = model.intercept
    for name in model.feature_names:
        w = model.weights.get(name, 0.0)
        v = float(features.get(name, 0.0))
        score += w * v
    return score


def calibrate(logit: float, model: ModelState) -> float:
    """Pure: Platt-scale the logit to a calibrated probability in [0, 1].

    Sigmoid form: 1 / (1 + exp(a * logit + b)). When a=-1, b=0 this
    reduces to a standard sigmoid; trained Platt coefficients shift
    and slope the curve so the output is a well-calibrated P(y=1).
    """
    try:
        z = model.platt_a * logit + model.platt_b
        if z > 50:
            return 0.0
        if z < -50:
            return 1.0
        return 1.0 / (1.0 + math.exp(z))
    except (OverflowError, ValueError):
        return 0.5


def register_model(
    *, version: str, weights: dict[str, float], intercept: float,
    platt_a: float, platt_b: float,
    feature_names: list[str] | None = None,
    training_window_days: int | None = None,
    n_training_rows: int | None = None,
    notes: str | None = None,
    activate: bool = False,
) -> bool:
    """Side-effect: persist a new model version to the weights table.

    Returns True on success. ``activate=True`` also flips the active
    pointer atomically (within the same DB transaction).
    """
    weights_json = json.dumps({**weights, "__intercept__": intercept})
    calibration_json = json.dumps({"a": platt_a, "b": platt_b})
    names_json = json.dumps(feature_names or list(FEATURE_NAMES))
    # Cross-backend upsert: ON CONFLICT works on Postgres and on SQLite
    # 3.24+ (June 2018; well within Aztea's runtime baseline). The prior
    # INSERT OR REPLACE was SQLite-only and would crash on Postgres
    # prod (/review L1).
    try:
        with _db.get_raw_connection(_db.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO ranker_model_weights "
                "(version, weights_json, calibration_json, "
                " feature_names_json, trained_at, training_window_days, "
                " n_training_rows, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT(version) DO UPDATE SET "
                "  weights_json = excluded.weights_json, "
                "  calibration_json = excluded.calibration_json, "
                "  feature_names_json = excluded.feature_names_json, "
                "  trained_at = excluded.trained_at, "
                "  training_window_days = excluded.training_window_days, "
                "  n_training_rows = excluded.n_training_rows, "
                "  notes = excluded.notes",
                (
                    version, weights_json, calibration_json, names_json,
                    _now_iso_path(), training_window_days, n_training_rows,
                    notes,
                ),
            )
            if activate:
                conn.execute(
                    "INSERT INTO ranker_model_active "
                    "(singleton_key, active_version, activated_at, activated_by) "
                    "VALUES (1, %s, %s, %s) "
                    "ON CONFLICT(singleton_key) DO UPDATE SET "
                    "  active_version = excluded.active_version, "
                    "  activated_at = excluded.activated_at, "
                    "  activated_by = excluded.activated_by",
                    (version, _now_iso_path(), "system"),
                )
            conn.commit()
        clear_cache()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("learned_ranker: register_model failed")
        return False


__all__ = [
    "FEATURE_NAMES",
    "ModelState",
    "calibrate",
    "clear_cache",
    "get_active_model",
    "predict",
    "register_model",
]
