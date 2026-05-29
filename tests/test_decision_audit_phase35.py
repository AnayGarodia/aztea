"""Phase 3.5 (2026-05-28): forward-only feature logging tests.

Verifies that ``record_decision`` accepts and persists the new
``feature_vector``, ``shadow_chosen_agent_id``, and ``intent_class``
fields after migration 0068, AND that callers passing the legacy
signature still work (backward-compat).
"""

from __future__ import annotations

import json
import uuid as _uuid

import pytest

from core import db as _db
from core.migrate import apply_migrations
from core.registry import decision_audit


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    db_path = tmp_path / f"audit-{_uuid.uuid4().hex}.db"
    monkeypatch.setattr(_db, "DB_PATH", str(db_path))
    # Force the sync-write fallback in decision_audit.record_decision so
    # each test can read its audit row back synchronously. The deferred
    # queue's own behavior is exercised in tests/test_deferred_queue.py.
    monkeypatch.setattr(
        decision_audit._deferred, "enqueue", lambda *a, **kw: False
    )
    if hasattr(_db._local, "conns"):
        for c in list(_db._local.conns.values()):
            try:
                c.close()
            except Exception:
                pass
        _db._local.conns.clear()
    apply_migrations(str(db_path))
    yield db_path


def test_record_decision_backward_compatible_no_features(fresh_db):
    """Old call site without Phase 3.5 fields still works."""
    decision_id = decision_audit.record_decision(
        intent_text="audit my repo",
        auto_invoked=False,
        reason="no_match",
    )
    assert decision_id is not None

    with _db.get_raw_connection(_db.DB_PATH) as conn:
        row = conn.execute(
            "SELECT feature_vector_json, shadow_chosen_agent_id, intent_class "
            "FROM auto_hire_decisions WHERE decision_id = %s",
            (decision_id,),
        ).fetchone()
        assert row["feature_vector_json"] is None
        assert row["shadow_chosen_agent_id"] is None
        assert row["intent_class"] is None


def test_record_decision_persists_feature_vector(fresh_db):
    fv = {
        "string_signals": 50.0,
        "quality_signals": 8.5,
        "intent_interlocks": 45.0,
        "keyword_overrides": 36.0,
        "schema_shape": 0.0,
        "semantic_similarity": 18.2,
        "probation_penalty": 0.0,
        "anti_catchall": 0.0,
    }
    decision_id = decision_audit.record_decision(
        intent_text="audit my requirements.txt",
        auto_invoked=True,
        chosen_agent_id="agent-dep-audit",
        confidence=0.78,
        feature_vector=fv,
        intent_class="code_audit",
    )
    assert decision_id is not None

    with _db.get_raw_connection(_db.DB_PATH) as conn:
        row = conn.execute(
            "SELECT feature_vector_json, intent_class "
            "FROM auto_hire_decisions WHERE decision_id = %s",
            (decision_id,),
        ).fetchone()
        assert row["intent_class"] == "code_audit"
        loaded = json.loads(row["feature_vector_json"])
        assert loaded == fv


def test_record_decision_persists_shadow_agent_when_diverged(fresh_db):
    decision_id = decision_audit.record_decision(
        intent_text="scan this for secrets",
        auto_invoked=True,
        chosen_agent_id="agent-secret-scan",
        confidence=0.82,
        shadow_chosen_agent_id="agent-sast-scan",  # shadow ranker disagreed
    )
    with _db.get_raw_connection(_db.DB_PATH) as conn:
        row = conn.execute(
            "SELECT chosen_agent_id, shadow_chosen_agent_id "
            "FROM auto_hire_decisions WHERE decision_id = %s",
            (decision_id,),
        ).fetchone()
        assert row["chosen_agent_id"] == "agent-secret-scan"
        assert row["shadow_chosen_agent_id"] == "agent-sast-scan"


def test_record_decision_never_raises_on_internal_failure(fresh_db, monkeypatch):
    """Fire-and-forget invariant: write failure is logged silently, the
    caller still gets the decision_id back, and no exception propagates.

    Note: post-#91, record_decision returns the decision_id immediately
    regardless of whether the deferred write later succeeds. The previous
    "returns None on failure" contract was a sync-write-only behavior.
    The remaining invariant — never raise — is what callers actually
    depend on, since they treat record_decision as fire-and-forget
    observability.
    """
    # Force the connection to raise.
    def _broken_get_raw_connection(_path):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(_db, "get_raw_connection", _broken_get_raw_connection)
    result = decision_audit.record_decision(
        intent_text="anything",
        auto_invoked=False,
        reason="no_match",
    )
    assert result is not None and len(result) == 32  # decision_id (uuid hex)
