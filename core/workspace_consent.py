"""Per-directory workspace-context consent state, persisted to ~/.aztea/.

# OWNS: ~/.aztea/workspace_consent.json — the user's approval/denial decisions
#       for sharing workspace context, keyed by absolute filesystem path.
# NOT OWNS: Bundle construction (core/workspace_bundle.py), MCP wiring
#           (aztea.mcp.server), or any backend state.
# INVARIANTS:
#   - The consent file is created with mode 0o600 (owner read/write only).
#   - All writes are atomic (write-then-rename) — concurrent invocations
#     never observe a half-written file.
#   - Path keys are normalised via os.path.realpath so symlinks and
#     trailing-slash variants resolve to a single canonical entry.
# DECISIONS:
#   - JSON file (not a DB) — this is per-user, low-volume, and the user
#     should be able to inspect/edit it with a text editor.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

ConsentState = Literal["approved", "denied", "unknown"]

CONSENT_FILE_VERSION = 1
CONSENT_FILE_MODE = 0o600
CONSENT_DIR_NAME = ".aztea"
CONSENT_FILENAME = "workspace_consent.json"


def _consent_dir() -> Path:
    """Return the path to ~/.aztea, honouring AZTEA_HOME for tests."""
    override = os.environ.get("AZTEA_HOME")
    if override:
        return Path(override)
    return Path.home() / CONSENT_DIR_NAME


def _consent_path() -> Path:
    return _consent_dir() / CONSENT_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise(path: str | Path) -> str:
    """Canonical filesystem path: absolute, real (symlinks resolved), no trailing slash."""
    return os.path.realpath(str(path))


def _empty_state() -> dict:
    return {"version": CONSENT_FILE_VERSION, "paths": {}}


def _load() -> dict:
    """Read the consent file. Returns an empty state if absent or corrupt.

    Corruption is treated as a recoverable condition: rather than crashing
    the MCP server on a torn file, we behave as if no decisions had been
    recorded yet. The next write fixes it.
    """
    path = _consent_path()
    if not path.is_file():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        return _empty_state()
    data.setdefault("version", CONSENT_FILE_VERSION)
    return data


def _atomic_write_json(data: dict) -> None:
    """Write `data` to the consent file atomically with mode 0o600."""
    target = _consent_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".workspace_consent.", suffix=".json", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp_path, CONSENT_FILE_MODE)
        os.replace(tmp_path, target)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_state(cwd: str | Path) -> ConsentState:
    """Return the consent decision recorded for `cwd`, or 'unknown'."""
    canonical = _normalise(cwd)
    data = _load()
    entry = data["paths"].get(canonical)
    if not isinstance(entry, dict):
        return "unknown"
    state = str(entry.get("state") or "").lower()
    if state in ("approved", "denied"):
        return state  # type: ignore[return-value]
    return "unknown"


def approve(cwd: str | Path) -> None:
    """Record `cwd` as approved for workspace-context sharing."""
    _set_state(cwd, "approved", "approved_at")


def deny(cwd: str | Path) -> None:
    """Record `cwd` as denied for workspace-context sharing."""
    _set_state(cwd, "denied", "denied_at")


def forget(cwd: str | Path) -> bool:
    """Remove the entry for `cwd`. Returns True if an entry existed."""
    canonical = _normalise(cwd)
    data = _load()
    if canonical not in data["paths"]:
        return False
    data["paths"].pop(canonical, None)
    _atomic_write_json(data)
    return True


def list_all() -> list[dict]:
    """Return all recorded decisions, sorted by path. Each entry exposes path,
    state, and the relevant timestamp.
    """
    data = _load()
    rows: list[dict] = []
    for path in sorted(data["paths"].keys()):
        entry = data["paths"][path]
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "path": path,
                "state": entry.get("state"),
                "approved_at": entry.get("approved_at"),
                "denied_at": entry.get("denied_at"),
            }
        )
    return rows


def _set_state(
    cwd: str | Path,
    state: ConsentState,
    timestamp_field: str,
) -> None:
    canonical = _normalise(cwd)
    data = _load()
    data["paths"][canonical] = {
        "state": state,
        timestamp_field: _utc_now_iso(),
    }
    _atomic_write_json(data)
