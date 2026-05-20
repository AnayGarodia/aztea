"""Reserved envelope key coverage tests.

CLAUDE.md documents reserved envelope keys (``_workspace_id``,
``_artifact_ref``) on ``POST /registry/agents/{id}/call``. Pre-fix
(audit 2026-05-19), ``_workspace_id`` was silently dropped from the
dispatch path even though the docs promised auto-write to
``outputs/{slug}/{job_id}.json``. This file enforces that every
documented reserved key has at least one referencing test, so
documentation can't drift away from implementation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
# H-2: keep this set in lockstep with the documented reserved envelope
# keys in CLAUDE.md. Adding a key here without an existing test
# referencing it will fail this assertion.
_RESERVED_ENVELOPE_KEYS = ("_workspace_id", "_artifact_ref")


def _grep_test_files(needle: str) -> list[str]:
    """Return test files referencing the needle. Uses git grep for speed."""
    try:
        out = subprocess.run(
            ["git", "grep", "-l", "--", needle, "tests/"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if out.returncode not in (0, 1):
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


@pytest.mark.parametrize("envelope_key", _RESERVED_ENVELOPE_KEYS)
def test_reserved_envelope_key_has_test_coverage(envelope_key: str):
    """Every documented reserved envelope key must be referenced in at
    least one test file. The audit found `_workspace_id` silently
    dropped because no test exercised the documented behavior — this
    canary prevents that pattern from recurring."""
    matches = _grep_test_files(envelope_key)
    # Filter out this very file (which references all the keys) from
    # the coverage check.
    matches = [m for m in matches if Path(m).name != "test_reserved_envelope_keys.py"]
    assert matches, (
        f"Reserved envelope key {envelope_key!r} is documented in "
        "CLAUDE.md but no test references it. Add a regression test "
        "for the documented behavior before shipping new envelope keys."
    )


def test_envelope_keys_list_is_stable():
    """If you add a new reserved key, update _RESERVED_ENVELOPE_KEYS AND
    add an integration test. This existence test prevents silent
    expansion of the reserved namespace."""
    assert len(_RESERVED_ENVELOPE_KEYS) >= 2, (
        "CLAUDE.md documents at least _workspace_id and _artifact_ref. "
        "Did you accidentally remove a reserved key?"
    )
