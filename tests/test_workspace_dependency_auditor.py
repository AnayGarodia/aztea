"""Verify dependency_auditor falls back to workspace_context when no manifest is supplied."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents import dependency_auditor as da
from core import workspace_bundle as wb


def _bundle_with_pyproject(tmp_path: Path) -> dict:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="demo"\ndependencies = ["requests==2.0.0"]\n', encoding="utf-8"
    )
    bundle = wb.build_light_bundle(tmp_path)
    return bundle.to_payload()


def _bundle_with_package_json(tmp_path: Path) -> dict:
    (tmp_path / "package.json").write_text(
        '{"name":"demo","dependencies":{"react":"18.0.0"}}\n', encoding="utf-8"
    )
    bundle = wb.build_light_bundle(tmp_path)
    return bundle.to_payload()


def test_falls_back_to_workspace_pypi(tmp_path: Path) -> None:
    bundle_payload = _bundle_with_pyproject(tmp_path)
    payload = {"workspace_context": bundle_payload}
    manifest, ecosystem, _ = da._normalize_run_inputs(payload)
    assert "demo" in manifest
    assert ecosystem == "pypi"


def test_falls_back_to_workspace_npm(tmp_path: Path) -> None:
    bundle_payload = _bundle_with_package_json(tmp_path)
    payload = {"workspace_context": bundle_payload}
    manifest, ecosystem, _ = da._normalize_run_inputs(payload)
    assert "react" in manifest
    assert ecosystem == "npm"


def test_explicit_manifest_wins_over_workspace(tmp_path: Path) -> None:
    bundle_payload = _bundle_with_package_json(tmp_path)
    payload = {
        "manifest": "requests==1.0.0",
        "ecosystem": "pypi",
        "workspace_context": bundle_payload,
    }
    manifest, ecosystem, _ = da._normalize_run_inputs(payload)
    assert manifest == "requests==1.0.0"
    assert ecosystem == "pypi"


def test_missing_everywhere_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        da._normalize_run_inputs({})


def test_workspace_without_manifests_raises(tmp_path: Path) -> None:
    bundle = wb.build_light_bundle(tmp_path)  # empty dir, no manifests
    with pytest.raises(ValueError):
        da._normalize_run_inputs({"workspace_context": bundle.to_payload()})
