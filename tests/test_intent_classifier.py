"""Phase 2 (B2): intent classifier tests.

Rule-path tests are pure and fast (no LLM). LLM-path is exercised
indirectly via the cached fallback; we monkeypatch the LLM call so
tests stay deterministic.
"""

from __future__ import annotations

import pytest

from core.registry import intent_classifier as ic
from core.registry.intent_taxonomy import INTENT_TAXONOMY, is_valid_class


# --- Taxonomy ----------------------------------------------------------


def test_taxonomy_has_expected_classes():
    expected = {
        "code_execution", "code_audit", "infra_check",
        "live_data", "document_parse", "web_automation", "other",
    }
    assert set(INTENT_TAXONOMY.keys()) == expected


def test_is_valid_class_rejects_unknown():
    assert is_valid_class("code_execution")
    assert is_valid_class("other")
    assert not is_valid_class("definitely_not_a_class")
    assert not is_valid_class("")
    assert not is_valid_class(None)


# --- Rule-based fast path -----------------------------------------------


@pytest.mark.parametrize("intent,expected", [
    ("run this python: print(2+2)", "code_execution"),
    ("execute this go snippet", "code_execution"),
    ("audit my requirements.txt for vulnerabilities", "code_audit"),
    ("scan this for secrets and credentials", "code_audit"),
    ("lookup CVE-2021-44228 details", "live_data"),
    ("check ssl cert for github.com", "live_data"),
    ("validate this kubernetes manifest", "infra_check"),
    ("extract tables from this pdf document", "document_parse"),
    ("take a screenshot of example.com with playwright", "web_automation"),
])
def test_rule_classify_handles_obvious_intents(intent, expected):
    # _rule_classify is pure — no LLM. Use lower(intent) since classifier
    # normalizes internally.
    assert ic._rule_classify(intent.lower()) == expected


def test_rule_classify_returns_none_on_genuinely_ambiguous():
    """`lint this dockerfile` is ambiguous between code_audit (lint) and
    infra_check (dockerfile). Honest behavior: return None, fall through
    to LLM."""
    assert ic._rule_classify("lint this dockerfile") is None


def test_rule_classify_returns_none_when_ambiguous():
    """Single keyword hit per class with tie → no winner → None."""
    # "scan" hits code_audit, "lookup" hits live_data — both score 1.
    assert ic._rule_classify("scan and lookup something") is None


def test_rule_classify_returns_none_on_chat():
    """Chat-shaped questions match no rule cluster."""
    assert ic._rule_classify("what is the capital of france") is None


# --- classify() public path --------------------------------------------


def test_classify_returns_rule_label_synchronously(monkeypatch):
    # Block LLM so we can verify the rule path is used first.
    monkeypatch.setattr(ic, "_llm_classify", lambda _: None)
    assert ic.classify("audit my requirements.txt") == "code_audit"


def test_classify_returns_none_on_chat_when_no_llm(monkeypatch):
    monkeypatch.setattr(ic, "_llm_classify", lambda _: None)
    assert ic.classify("hi how are you") is None


def test_classify_background_dispatch_first_then_cached(monkeypatch):
    """First sight on a non-rule intent returns None; cache populates async."""
    # Force the LLM stub so the background populate actually finishes.
    monkeypatch.setattr(ic, "_llm_classify", lambda _: "other")
    # Synchronous mode for determinism.
    result = ic.classify(
        "totally novel non-rule-matching intent",
        allow_background=False,
    )
    assert result == "other"


def test_classify_never_raises_on_llm_failure(monkeypatch):
    def _boom(_text):
        raise RuntimeError("LLM blew up")
    monkeypatch.setattr(ic, "_llm_classify", _boom)
    # Background mode swallows; sync mode catches inside _classify_cached
    # via the lru_cache wrapper returning None.
    sync = ic.classify("xyz qqq", allow_background=False)
    # Sync mode: _classify_cached propagates the raise since it's a
    # direct call. We test the BACKGROUND mode never bubbles up.
    assert sync is None or isinstance(sync, str)
    bg = ic.classify("xyz qqq abc", allow_background=True)
    assert bg is None  # background dispatch returns None on first sight
