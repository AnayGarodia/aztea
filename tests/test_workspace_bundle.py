"""Unit tests for core/workspace_bundle.py — privacy + correctness."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import workspace_bundle as wb


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Build a realistic mini-repo fixture for bundle assertions."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_one(): assert 1\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"name":"demo","dependencies":{"react":"18.0.0"}}\n', encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="demo"\nversion="0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        "# Demo project\n\nA tiny test fixture for workspace bundling.\n", encoding="utf-8"
    )
    # Plant secrets that MUST be excluded.
    (tmp_path / ".env").write_text("SUPER_SECRET=should_never_leak\n", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\nbad\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text('{"api_key":"hunter2"}\n', encoding="utf-8")
    (tmp_path / ".gitignore").write_text("node_modules/\nignored_dir/\n", encoding="utf-8")
    (tmp_path / "ignored_dir").mkdir()
    (tmp_path / "ignored_dir" / "dont_show_me.txt").write_text("hidden\n", encoding="utf-8")
    return tmp_path


def test_build_returns_basic_fields(project: Path) -> None:
    bundle = wb.build_light_bundle(project)
    assert bundle.cwd_basename == project.name
    assert "src/" in bundle.file_tree
    assert "package.json" in bundle.manifests
    assert "pyproject.toml" in bundle.manifests
    assert "Demo project" in bundle.readme_excerpt
    assert bundle.bundle_fingerprint  # non-empty


def test_size_cap_enforced(project: Path) -> None:
    bundle = wb.build_light_bundle(project)
    serialised = json.dumps(bundle.to_payload(), separators=(",", ":"))
    assert len(serialised) <= wb.BUNDLE_SIZE_CAP_BYTES


def test_denylist_excludes_env_and_keys(project: Path) -> None:
    bundle = wb.build_light_bundle(project)
    serialised = json.dumps(bundle.to_payload())
    assert ".env" not in bundle.file_tree
    assert "id_rsa" not in bundle.file_tree
    assert "credentials.json" not in bundle.file_tree
    assert "SUPER_SECRET" not in serialised
    assert "should_never_leak" not in serialised
    assert "hunter2" not in serialised


def test_gitignore_patterns_excluded(project: Path) -> None:
    bundle = wb.build_light_bundle(project)
    assert "ignored_dir" not in bundle.file_tree
    assert "dont_show_me.txt" not in bundle.file_tree


def test_aztea_ignore_overrides(project: Path) -> None:
    (project / ".aztea_ignore").write_text("src/\n", encoding="utf-8")
    bundle = wb.build_light_bundle(project)
    tree_lines = bundle.file_tree.splitlines()
    assert "src/" not in tree_lines
    assert all(not line.endswith("main.py") or "test_main" in line for line in tree_lines)


def test_fingerprint_stable(project: Path) -> None:
    a = wb.build_light_bundle(project)
    b = wb.build_light_bundle(project)
    assert a.bundle_fingerprint == b.bundle_fingerprint


def test_fingerprint_changes_on_content_change(project: Path) -> None:
    a = wb.build_light_bundle(project)
    (project / "package.json").write_text('{"name":"changed"}\n', encoding="utf-8")
    b = wb.build_light_bundle(project)
    assert a.bundle_fingerprint != b.bundle_fingerprint


def test_truncation_flag_set_when_oversized(tmp_path: Path) -> None:
    # A 9KB README forces truncation to kick in.
    huge = "\n".join(f"chunk_of_text_{i}_padding" * 4 for i in range(400))
    (tmp_path / "README.md").write_text(huge, encoding="utf-8")
    huge_manifest = "\n".join(f'"key_{i}": "value_with_text_{i}",' for i in range(400))
    (tmp_path / "package.json").write_text("{\n" + huge_manifest + "\n}\n", encoding="utf-8")
    bundle = wb.build_light_bundle(tmp_path)
    assert bundle.truncated is True
    assert len(json.dumps(bundle.to_payload())) <= wb.BUNDLE_SIZE_CAP_BYTES


def test_summary_only_omits_content(project: Path) -> None:
    bundle = wb.build_light_bundle(project)
    summary = bundle.summary_only()
    serialised = json.dumps(summary)
    assert "Demo project" not in serialised  # README content stripped
    assert "react" not in serialised  # manifest body stripped
    assert "package.json" in summary["manifests_present"]


def test_round_trip_payload(project: Path) -> None:
    a = wb.build_light_bundle(project)
    b = wb.bundle_from_payload(a.to_payload())
    assert a.bundle_fingerprint == b.bundle_fingerprint
    assert a.file_tree == b.file_tree
    assert a.manifests == b.manifests


def test_non_directory_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "does_not_exist"
    with pytest.raises(ValueError):
        wb.build_light_bundle(bogus)


def test_readme_truncated_to_max_lines(tmp_path: Path) -> None:
    huge_readme = "\n".join(f"line {i}" for i in range(1000))
    (tmp_path / "README.md").write_text(huge_readme, encoding="utf-8")
    bundle = wb.build_light_bundle(tmp_path)
    assert len(bundle.readme_excerpt.splitlines()) <= wb.MAX_README_LINES


def test_branch_detection_via_gitlink(tmp_path: Path) -> None:
    # Real worktrees use a .git file pointing to the parent repo's gitdir.
    real_git = tmp_path / "real_git"
    real_git.mkdir()
    (real_git / "HEAD").write_text("ref: refs/heads/feature-x\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")
    bundle = wb.build_light_bundle(work)
    assert bundle.git_branch == "feature-x"
