"""Architecture + OSS guards for the listing-verification surface.

  1. The new core/listing_* modules must not import server/ (one-way deps, H6).
  2. The advisory council never blocks and never raises when no LLM is reachable
     (OSS / offline), including under partial member failure.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import core.listing_council as council
from core.llm.errors import LLMError

_NEW_MODULES = [
    "listing_probe_core",
    "listing_reliability",
    "listing_dedup",
    "listing_value_add",
    "listing_council",
    "listing_council_prompts",
    "listing_verification",
]

_CORE_DIR = Path(__file__).resolve().parent.parent / "core"


@pytest.mark.parametrize("module", _NEW_MODULES)
def test_module_does_not_import_server(module):
    """core/ must never import server/ — verified at the AST import level."""
    tree = ast.parse((_CORE_DIR / f"{module}.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "server"]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] == "server":
                offenders.append(node.module)
    assert offenders == [], f"{module} imports server: {offenders}"


_CANDIDATE = {
    "name": "X", "description": "d", "kind": "skill_md",
    "input_schema": {}, "output_schema": {}, "body": "b",
}


def test_council_no_llm_returns_empty_and_never_blocks(monkeypatch):
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL", "on")
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL_CHAIN", "m1,m2,m3")

    def _no_provider(req, model_chain=None):
        raise LLMError("none", "", "no provider configured")

    monkeypatch.setattr(council, "run_with_fallback", _no_provider)
    council.clear_member_cache()

    result = council.review_listing(_CANDIDATE, [])
    assert result.findings == []
    assert result.needs_human_review is False
    assert result.member_count == 0


def test_council_partial_failure_never_blocks(monkeypatch):
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL", "on")
    monkeypatch.setenv("AZTEA_LISTING_COUNCIL_CHAIN", "m1,m2")

    def runner(spec, system, user, h):
        if spec == "m1":
            raise RuntimeError("network down")
        from core.listing_council_prompts import DimensionVerdict, MemberVerdict
        dims = {
            d: DimensionVerdict("concern", 0.9, "x")
            for d in ("reliability", "originality", "value_add")
        }
        return MemberVerdict(spec, dims)

    # One present member can never reach quorum -> no flag, no block, no raise.
    result = council.review_listing(_CANDIDATE, [], member_runner=runner)
    assert result.findings == []
    assert all(f.level != "block" for f in result.findings)
