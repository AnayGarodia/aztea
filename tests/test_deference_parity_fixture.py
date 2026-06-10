"""Parity guard (Python half of TD1): the Python deference classifier must match
the shared cross-harness fixture that every plugin port (OpenClaw TS, Hermes)
also satisfies. If this and the TS-side test both pass against the same
`integrations/deference/classification-fixtures.json`, the ported classifier
cannot drift and silently under-detect wedge tasks (which would cut Aztea call
volume). The fixture is the source of truth; edit it, not the assertions.
"""
from __future__ import annotations

import json
from pathlib import Path

from aztea.cli import deference_core

_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "integrations" / "deference" / "classification-fixtures.json"
)


def _load_cases() -> list[dict]:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["cases"], "fixture has no cases"
    return data["cases"]


def test_python_classifier_matches_shared_fixture() -> None:
    for case in _load_cases():
        event, mode, expect = case["event"], case["mode"], case["expect"]
        decision = deference_core.classify_pretool_event_for_mode(event, mode)
        neutral = json.loads(
            deference_core.pretool_decision_json(json.dumps(event), mode=mode)
        )
        # Neutral contract decision matches the fixture for every case.
        assert neutral["decision"] == expect["decision"], case
        if expect["decision"] == "allow":
            assert decision is None, case
        else:
            assert decision is not None, case
            assert decision.action == expect["decision"], case
            assert decision.category == expect["category"], case
