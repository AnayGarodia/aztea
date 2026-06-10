"""Unit tests for agents/_manifest_parsing.py — pure parsers, no network.

The parsers' contract: every non-empty, non-comment manifest line either
yields a package or a CLASSIFIED warning. The 2026-05 audit found extras,
env markers, and VCS lines silently vanishing, and the npm digit-stripper
corrupting prerelease versions (^1.2.3-beta.1 -> 1.2.3.1).
"""

from __future__ import annotations

from agents._manifest_parsing import (
    detect_ecosystem,
    parse_npm_manifest,
    parse_pypi_manifest,
)


def test_detect_ecosystem():
    assert detect_ecosystem('{"dependencies": {}}') == "npm"
    assert detect_ecosystem("requests==2.0") == "pypi"


def test_pypi_extras_are_parsed_not_mangled():
    pkgs, warns = parse_pypi_manifest("requests[socks,security]>=2.0,<3\n")
    assert pkgs == [("requests", "2.0")]
    assert warns == []


def test_pypi_environment_markers_are_handled():
    pkgs, warns = parse_pypi_manifest('django==4.2; python_version >= "3.10"\n')
    assert pkgs == [("django", "4.2")]
    assert warns == []


def test_pypi_exact_pin_preferred_over_lower_bound():
    pkgs, _ = parse_pypi_manifest("flask>=1.0,==2.3.2\n")
    assert pkgs == [("flask", "2.3.2")]


def test_pypi_unpinned_package_yields_empty_version():
    pkgs, _ = parse_pypi_manifest("flask\n")
    assert pkgs == [("flask", "")]


def test_pypi_editable_install_warns_classified():
    _, warns = parse_pypi_manifest("-e git+https://github.com/x/y.git#egg=y\n")
    assert warns == [
        {"line": "-e git+https://github.com/x/y.git#egg=y", "reason": "editable_not_audited"}
    ]


def test_pypi_vcs_url_warns_classified():
    _, warns = parse_pypi_manifest("git+https://github.com/x/y.git@v1#egg=y\n")
    assert [w["reason"] for w in warns] == ["vcs_url_not_audited"]


def test_pypi_nested_requirements_warn_classified():
    _, warns = parse_pypi_manifest("-r requirements-dev.txt\n-c constraints.txt\n")
    assert [w["reason"] for w in warns] == [
        "nested_requirements_not_followed",
        "nested_requirements_not_followed",
    ]


def test_pypi_option_with_url_is_option_not_vcs():
    """--index-url carries a URL but is a pip option, not a VCS requirement."""
    _, warns = parse_pypi_manifest("--index-url https://pypi.org/simple\n")
    assert [w["reason"] for w in warns] == ["pip_option_ignored"]


def test_pypi_garbage_line_warns_unparseable():
    pkgs, warns = parse_pypi_manifest("this is !! not a requirement\n")
    assert pkgs == []
    assert [w["reason"] for w in warns] == ["unparseable"]


def test_pypi_comments_and_blanks_are_silent():
    pkgs, warns = parse_pypi_manifest("# a comment\n\nrequests==2.0  # inline\n")
    assert pkgs == [("requests", "2.0")]
    assert warns == []


def test_npm_prerelease_version_survives_intact():
    """Regression: the old digit-stripper turned ^1.2.3-beta.1 into 1.2.3.1."""
    pkgs, _ = parse_npm_manifest('{"dependencies": {"express": "^1.2.3-beta.1"}}')
    assert pkgs == [("express", "1.2.3-beta.1")]


def test_npm_range_takes_lower_bound():
    pkgs, _ = parse_npm_manifest('{"dependencies": {"a": ">=1.2.0 <2.0.0"}}')
    assert pkgs == [("a", "1.2.0")]


def test_npm_short_and_bare_versions():
    pkgs, _ = parse_npm_manifest('{"dependencies": {"a": "~4.17", "b": "18"}}')
    assert pkgs == [("a", "4.17"), ("b", "18")]


def test_npm_git_spec_warns_and_audits_unpinned():
    pkgs, warns = parse_npm_manifest('{"dependencies": {"w": "github:user/repo"}}')
    assert pkgs == [("w", "")]
    assert [w["reason"] for w in warns] == ["vcs_url_not_audited"]


def test_npm_bare_deps_dict_still_parses():
    pkgs, _ = parse_npm_manifest('{"lodash": "4.17.21"}')
    assert pkgs == [("lodash", "4.17.21")]


def test_npm_tag_spec_is_unpinned_without_warning():
    pkgs, warns = parse_npm_manifest('{"dependencies": {"x": "latest"}}')
    assert pkgs == [("x", "")]
    assert warns == []
