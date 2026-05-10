"""Unit tests for core/workspace_consent.py — state machine + file safety."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from core import workspace_consent as wc


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ~/.aztea to a per-test tmpdir so consent state never leaks."""
    monkeypatch.setenv("AZTEA_HOME", str(tmp_path / ".aztea"))
    return tmp_path


def test_unknown_state_for_fresh_path(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    assert wc.get_state(target) == "unknown"


def test_approve_then_status(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    wc.approve(target)
    assert wc.get_state(target) == "approved"


def test_deny_then_status(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    wc.deny(target)
    assert wc.get_state(target) == "denied"


def test_forget_returns_to_unknown(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    wc.approve(target)
    assert wc.forget(target) is True
    assert wc.get_state(target) == "unknown"
    assert wc.forget(target) is False  # idempotent


def test_overwrite_approve_to_deny(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    wc.approve(target)
    wc.deny(target)
    assert wc.get_state(target) == "denied"


def test_path_normalisation_resolves_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    wc.approve(link)
    assert wc.get_state(real) == "approved"


def test_consent_file_mode_is_0600(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    wc.approve(target)
    consent_path = Path(os.environ["AZTEA_HOME"]) / "workspace_consent.json"
    mode = stat.S_IMODE(consent_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600 got {oct(mode)}"


def test_corrupt_file_recovers_to_empty_state(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    wc.approve(target)
    consent_path = Path(os.environ["AZTEA_HOME"]) / "workspace_consent.json"
    consent_path.write_text("{ this is not valid json", encoding="utf-8")
    # Corrupt file should not crash; reads behave as if no decisions exist.
    assert wc.get_state(target) == "unknown"
    # Recovery: a write should succeed and replace the bad file.
    wc.approve(target)
    assert wc.get_state(target) == "approved"
    parsed = json.loads(consent_path.read_text(encoding="utf-8"))
    assert "paths" in parsed


def test_list_all_returns_sorted(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    wc.approve(b)
    wc.deny(a)
    rows = wc.list_all()
    assert [row["path"] for row in rows] == sorted([str(a.resolve()), str(b.resolve())])
