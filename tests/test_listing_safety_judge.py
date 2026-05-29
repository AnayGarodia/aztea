"""Tests for the LLM-judge layer on top of core/listing_safety.

Mocks ``run_with_fallback`` so we never make a real LLM call — the
unit under test is the parsing + thresholding logic, not the provider
chain (the provider chain is tested separately in core/llm/).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _enable_judge(monkeypatch):
    """Force the judge ON for the duration of each test. The module
    reads the env at call time, so monkeypatching the variable works
    even though the import happens at fixture setup."""
    monkeypatch.setenv("AZTEA_LISTING_JUDGE", "on")
    # The judge uses an LRU cache. Clear it before each test so
    # mocked responses from a prior test don't leak across.
    import core.listing_safety_judge as judge
    judge._run_judge_cached.cache_clear()
    yield
    judge._run_judge_cached.cache_clear()


def _patch_llm(monkeypatch, *, response: str | None, raise_exc: Exception | None = None):
    """Install a fake ``run_with_fallback`` that returns the supplied JSON
    string verbatim. ``response=None`` simulates empty content;
    ``raise_exc`` simulates a provider failure."""
    import core.listing_safety_judge as judge

    def _fake(req):
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(text=response or "", provider="fake", model="fake-1")

    monkeypatch.setattr(judge, "run_with_fallback", _fake)


# ---------------------------------------------------------------------------
# Env gating
# ---------------------------------------------------------------------------


def test_judge_disabled_via_env_returns_empty(monkeypatch):
    monkeypatch.setenv("AZTEA_LISTING_JUDGE", "off")
    from core.listing_safety_judge import judge_python_handler
    assert judge_python_handler("def handler(p): return p") == []


def test_judge_disabled_via_unset_env_returns_empty(monkeypatch):
    # Empty string is treated as disabled by the gate.
    monkeypatch.setenv("AZTEA_LISTING_JUDGE", "")
    from core.listing_safety_judge import judge_python_handler
    assert judge_python_handler("def handler(p): return p") == []


# ---------------------------------------------------------------------------
# Verdict thresholding
# ---------------------------------------------------------------------------


def test_judge_allow_verdict_returns_empty(monkeypatch):
    _patch_llm(monkeypatch, response=json.dumps({
        "verdict": "allow",
        "reasoning": "pure computation, no side effects",
        "confidence": 0.9,
    }))
    from core.listing_safety_judge import judge_python_handler
    assert judge_python_handler("def handler(p): return p['x'] + 1") == []


def test_judge_block_high_confidence_returns_block_finding(monkeypatch):
    _patch_llm(monkeypatch, response=json.dumps({
        "verdict": "block",
        "reasoning": "handler exfiltrates the payload to an attacker host",
        "confidence": 0.92,
    }))
    from core.listing_safety_judge import judge_python_handler
    findings = judge_python_handler("def handler(p): import x; x.send(p)")
    assert len(findings) == 1
    f = findings[0]
    # LEVEL_BLOCK is a sentinel string; assert by attribute we expect
    assert getattr(f, "level", None) == "block"
    assert f.code == "python.judge.block"
    assert "exfiltrate" in f.message.lower()
    assert f.detail["confidence"] == 0.92


def test_judge_block_low_confidence_demotes_to_warn(monkeypatch):
    _patch_llm(monkeypatch, response=json.dumps({
        "verdict": "block",
        "reasoning": "maybe suspicious",
        "confidence": 0.3,  # below the 0.6 floor
    }))
    from core.listing_safety_judge import judge_python_handler
    findings = judge_python_handler("def handler(p): return p")
    assert len(findings) == 1
    assert getattr(findings[0], "level", None) == "warn"


def test_judge_warn_verdict_returns_warn(monkeypatch):
    _patch_llm(monkeypatch, response=json.dumps({
        "verdict": "warn",
        "reasoning": "unusual but plausibly legitimate",
        "confidence": 0.7,
    }))
    from core.listing_safety_judge import judge_python_handler
    findings = judge_python_handler("def handler(p): return p")
    assert len(findings) == 1
    assert getattr(findings[0], "level", None) == "warn"


# ---------------------------------------------------------------------------
# Failure paths — judge must never block on its own malfunction
# ---------------------------------------------------------------------------


def test_judge_llm_unavailable_returns_empty(monkeypatch):
    from core.llm.errors import LLMError
    _patch_llm(
        monkeypatch,
        response=None,
        raise_exc=LLMError(provider="fake", model="fake-1", message="no provider"),
    )
    from core.listing_safety_judge import judge_python_handler
    # Must NOT block — fallback to "judge unavailable, allow through" is
    # the documented safety floor (static scanner is the real gate).
    assert judge_python_handler("def handler(p): return p") == []


def test_judge_unexpected_exception_returns_empty(monkeypatch):
    _patch_llm(monkeypatch, response=None, raise_exc=RuntimeError("oops"))
    from core.listing_safety_judge import judge_python_handler
    assert judge_python_handler("def handler(p): return p") == []


def test_judge_non_json_response_returns_empty(monkeypatch):
    _patch_llm(monkeypatch, response="not even close to JSON")
    from core.listing_safety_judge import judge_python_handler
    assert judge_python_handler("def handler(p): return p") == []


def test_judge_unknown_verdict_returns_empty(monkeypatch):
    _patch_llm(monkeypatch, response=json.dumps({
        "verdict": "maybe",  # not in {allow, warn, block}
        "reasoning": "hmm",
        "confidence": 0.9,
    }))
    from core.listing_safety_judge import judge_python_handler
    assert judge_python_handler("def handler(p): return p") == []


def test_judge_strips_code_fence_around_json(monkeypatch):
    """Some providers wrap JSON in ```json …``` despite json_mode. The
    parser must tolerate that."""
    _patch_llm(monkeypatch, response='```json\n{"verdict": "allow", "reasoning": "ok", "confidence": 0.9}\n```')
    from core.listing_safety_judge import judge_python_handler
    assert judge_python_handler("def handler(p): return p") == []


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_judge_cache_returns_same_result_for_identical_source(monkeypatch):
    """Two judge calls on the same source must reuse the cache — verified
    by counting how many times the fake LLM was invoked."""
    call_count = {"n": 0}

    def _fake(req):
        call_count["n"] += 1
        return SimpleNamespace(
            text=json.dumps({"verdict": "allow", "reasoning": "ok", "confidence": 0.9}),
            provider="fake",
            model="fake-1",
        )

    import core.listing_safety_judge as judge
    monkeypatch.setattr(judge, "run_with_fallback", _fake)

    src = "def handler(p): return p['x']"
    judge.judge_python_handler(src)
    judge.judge_python_handler(src)
    assert call_count["n"] == 1, f"Cache miss on identical source: {call_count['n']} calls"


# ---------------------------------------------------------------------------
# Scanner is judge-free — judge is called explicitly by the publish path
# ---------------------------------------------------------------------------
#
# 2026-05-27 (/cso fix): the LLM judge was previously layered inside
# scan_python_handler / scan_skill_md, which meant every anonymous
# /api/playground/test invocation burnt an LLM call. The judge is now
# invoked explicitly by /skills POST (and a future /api/playground/publish).
# Anonymous transient probes pay zero LLM cost. The tests below pin both
# halves of that contract.


def test_scan_python_handler_does_not_invoke_judge(monkeypatch):
    """Regression: the scanner MUST NOT call the LLM judge. Anonymous
    /api/playground/test relies on this — it calls scan_python_handler
    on every request and would otherwise burn LLM credits per probe."""
    call_count = {"n": 0}

    def _fake(req):
        call_count["n"] += 1
        return SimpleNamespace(
            text=json.dumps({"verdict": "block", "reasoning": "x", "confidence": 0.9}),
            provider="fake", model="fake-1",
        )

    import core.listing_safety_judge as judge
    monkeypatch.setattr(judge, "run_with_fallback", _fake)
    from core.listing_safety import scan_python_handler
    scan_python_handler("def handler(p):\n    return {'echo': p}\n")
    assert call_count["n"] == 0, (
        "scan_python_handler called the LLM judge — this regresses the "
        "cost-amplification fix. The judge must be invoked by the publish "
        "path (part_012.py /skills), not by the scanner."
    )


def test_scan_skill_md_does_not_invoke_judge(monkeypatch):
    """Same contract as above, for the SKILL.md surface."""
    call_count = {"n": 0}

    def _fake(req):
        call_count["n"] += 1
        return SimpleNamespace(
            text=json.dumps({"verdict": "block", "reasoning": "x", "confidence": 0.9}),
            provider="fake", model="fake-1",
        )

    import core.listing_safety_judge as judge
    monkeypatch.setattr(judge, "run_with_fallback", _fake)
    from core.listing_safety import scan_skill_md
    scan_skill_md("# A helpful tool\n\nSummarises text into bullets.\n")
    assert call_count["n"] == 0


# Integration tests (publish path triggers judge) live in
# tests/integration/test_skills_publish_judge.py — they need the `client`
# fixture which is only available under tests/integration/.
