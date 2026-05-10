"""Privacy invariants for workspace_context — must NEVER leak file content
into work examples, the cache, or the audit log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core import workspace_bundle as wb
from core import workspace_bundle_cache as wb_cache
from core import workspace_helpers as wh


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "package.json").write_text(
        '{"name":"demo"}\n', encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        "# Project\nSome notes.\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("API_TOKEN=should_never_appear_anywhere\n", encoding="utf-8")
    return tmp_path


def test_strip_workspace_context_helper(project: Path) -> None:
    """Privacy backstop must remove workspace_context cleanly without mutating input."""
    bundle = wb.build_light_bundle(project)
    payload = {
        "manifest": "ignored",
        "workspace_context": bundle.to_payload(),
        "other_field": "kept",
    }
    cleaned = wh.strip_workspace_context(payload)
    assert "workspace_context" not in cleaned
    assert cleaned["other_field"] == "kept"
    assert cleaned["manifest"] == "ignored"
    # Original is untouched (privacy backstop must not mutate the live envelope).
    assert "workspace_context" in payload


def test_strip_handles_non_dict() -> None:
    assert wh.strip_workspace_context(None) is None
    assert wh.strip_workspace_context("string") == "string"
    assert wh.strip_workspace_context([1, 2]) == [1, 2]


def test_strip_passthrough_when_field_absent() -> None:
    payload = {"manifest": "ok"}
    assert wh.strip_workspace_context(payload) is payload  # no copy when no-op


def test_extract_workspace_context_returns_bundle(project: Path) -> None:
    bundle = wb.build_light_bundle(project)
    payload: dict[str, Any] = {"workspace_context": bundle.to_payload()}
    extracted = wh.extract_workspace_context(payload)
    assert extracted is not None
    assert extracted.bundle_fingerprint == bundle.bundle_fingerprint
    # Secret never made it in to begin with.
    assert "should_never_appear_anywhere" not in str(extracted.to_payload())


def test_extract_handles_missing_or_malformed() -> None:
    assert wh.extract_workspace_context(None) is None
    assert wh.extract_workspace_context({"workspace_context": None}) is None
    assert wh.extract_workspace_context({"workspace_context": "string"}) is None
    assert wh.extract_workspace_context({"workspace_context": {}}) is None


def test_render_for_prompt_respects_budget(project: Path) -> None:
    bundle = wb.build_light_bundle(project)
    short = wh.render_for_prompt(bundle, max_chars=200)
    assert len(short) <= 200
    full = wh.render_for_prompt(bundle, max_chars=10_000)
    assert len(full) > len(short)
    assert "Workspace context" in full


def test_bundle_cache_round_trip(project: Path) -> None:
    wb_cache._reset_for_tests()
    bundle = wb.build_light_bundle(project)
    payload = bundle.to_payload()
    wb_cache.cache_workspace_bundle(bundle.bundle_fingerprint, payload)
    cached = wb_cache.get_workspace_bundle(bundle.bundle_fingerprint)
    assert cached == payload


def test_bundle_cache_miss_returns_none() -> None:
    wb_cache._reset_for_tests()
    assert wb_cache.get_workspace_bundle("does-not-exist") is None
    assert wb_cache.get_workspace_bundle("") is None


def test_bundle_cache_expires(project: Path) -> None:
    wb_cache._reset_for_tests()
    bundle = wb.build_light_bundle(project)
    wb_cache.cache_workspace_bundle(bundle.bundle_fingerprint, bundle.to_payload(), ttl_seconds=1)
    import time as _time

    _time.sleep(1.1)
    assert wb_cache.get_workspace_bundle(bundle.bundle_fingerprint) is None
