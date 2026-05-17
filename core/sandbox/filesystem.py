"""Filesystem operations against the sandbox workspace.

# OWNS: read_file, write_file, delete_file, apply_patch (atomic), glob, grep,
#       sync_from_local. All operate on the workspace bind-mounted into the
#       sandbox, so the host sees identical content.
# INVARIANTS:
#   * Every path passes through ``_safe_join`` so a malicious payload cannot
#     traverse out of the workspace.
#   * apply_patch is atomic: failure on any hunk reverts the entire bundle.
"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from core.sandbox.models import SandboxInvalidInput
from core.sandbox.state import SandboxState, get

_LOG = logging.getLogger("aztea.sandbox.filesystem")
_MAX_READ_BYTES = 4 * 1024 * 1024  # 4 MB
_MAX_WRITE_BYTES = 8 * 1024 * 1024  # 8 MB
_MAX_GREP_RESULTS = 500
_MAX_GLOB_RESULTS = 2000


def read_file(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    target = _resolve_path(state, payload)
    if not target.is_file():
        raise SandboxInvalidInput(f"not a file: {payload.get('path')!r}")
    raw = target.read_bytes()[:_MAX_READ_BYTES]
    truncated = target.stat().st_size > _MAX_READ_BYTES
    binary = _looks_binary(raw)
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "path": payload.get("path"),
        "binary": binary,
        "content_b64": base64.b64encode(raw).decode("ascii") if binary else None,
        "content": None if binary else raw.decode("utf-8", "replace"),
        "size_bytes": target.stat().st_size,
        "truncated": truncated,
    }


def write_file(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    target = _resolve_path(state, payload)
    if "content_b64" in payload and payload["content_b64"] is not None:
        try:
            data = base64.b64decode(payload["content_b64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise SandboxInvalidInput("content_b64 must be valid base64") from exc
    elif "content" in payload and payload["content"] is not None:
        data = str(payload["content"]).encode("utf-8")
    else:
        raise SandboxInvalidInput(
            "write_file requires 'content' (str) or 'content_b64' (base64)"
        )
    if len(data) > _MAX_WRITE_BYTES:
        raise SandboxInvalidInput(
            f"write exceeds {_MAX_WRITE_BYTES // (1024*1024)} MB cap"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "path": payload.get("path"),
        "bytes_written": len(data),
    }


def delete_file(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    target = _resolve_path(state, payload)
    if not target.exists():
        return {"sandbox_id": state.sandbox_id, "path": payload.get("path"), "deleted": False}
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    state.touch()
    return {"sandbox_id": state.sandbox_id, "path": payload.get("path"), "deleted": True}


def apply_patch(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    patch = str(payload.get("patch") or "")
    if not patch.strip():
        raise SandboxInvalidInput("patch is required")
    workspace = Path(state.filesystem_root)
    backup = _snapshot_workspace_for_patch(workspace)
    try:
        proc = _run_patch(workspace, patch)
        if proc.returncode != 0:
            raise SandboxInvalidInput(
                "patch failed; workspace was rolled back. "
                f"git stderr: {(proc.stderr or '')[:512]}"
            )
    except Exception:
        _restore_workspace(workspace, backup)
        raise
    finally:
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "applied": True,
        "stdout": proc.stdout[:4000],
        "stderr": proc.stderr[:4000],
    }


def glob_files(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    pattern = str(payload.get("pattern") or "").strip()
    if not pattern:
        raise SandboxInvalidInput("pattern is required")
    workspace = Path(state.filesystem_root)
    matches = sorted(str(p.relative_to(workspace)) for p in workspace.glob(pattern) if p.is_file())
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "pattern": pattern,
        "files": matches[:_MAX_GLOB_RESULTS],
        "truncated": len(matches) > _MAX_GLOB_RESULTS,
    }


def grep_files(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    pattern = str(payload.get("pattern") or "")
    if not pattern:
        raise SandboxInvalidInput("pattern is required")
    try:
        compiled = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise SandboxInvalidInput(f"invalid regex: {exc}") from exc
    glob_filter = str(payload.get("glob") or "**/*")
    workspace = Path(state.filesystem_root)
    matches: list[dict[str, Any]] = []
    for candidate in workspace.glob(glob_filter):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text("utf-8", "replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                matches.append(
                    {
                        "path": str(candidate.relative_to(workspace)),
                        "line": lineno,
                        "text": line[:512],
                    }
                )
                if len(matches) >= _MAX_GREP_RESULTS:
                    state.touch()
                    return {
                        "sandbox_id": state.sandbox_id,
                        "pattern": pattern,
                        "matches": matches,
                        "truncated": True,
                    }
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "pattern": pattern,
        "matches": matches,
        "truncated": False,
    }


def sync_from_local(payload: dict[str, Any]) -> dict[str, Any]:
    """Push files from a local host directory into the sandbox workspace.

    Why: callers can edit locally with their normal tools, then push the
    delta into the sandbox cheaply. Implementation: ``rsync -a --delete``
    if available, else a recursive Python copy.
    """
    state = _require(payload)
    src = str(payload.get("local_path") or "").strip()
    if not src:
        raise SandboxInvalidInput("local_path is required for sync_from_local")
    src_path = Path(src).expanduser().resolve()
    if not src_path.is_dir():
        raise SandboxInvalidInput(f"local_path is not a directory: {src!r}")
    dst = Path(state.filesystem_root)
    if shutil.which("rsync"):
        proc = subprocess.run(  # noqa: S603
            ["rsync", "-a", "--delete", f"{src_path}/", f"{dst}/"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        used = "rsync"
        ok = proc.returncode == 0
        err = (proc.stderr or "")[:512]
    else:
        used = "python_shutil"
        ok = _python_sync(src_path, dst)
        err = "" if ok else "shutil copy failed"
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "synced": ok,
        "method": used,
        "error": err if not ok else None,
    }


def _python_sync(src: Path, dst: Path) -> bool:
    try:
        if dst.exists():
            for entry in dst.iterdir():
                if entry.name == ".git":
                    continue
                if entry.is_file() or entry.is_symlink():
                    entry.unlink()
                else:
                    shutil.rmtree(entry, ignore_errors=True)
        else:
            dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return True
    except Exception:
        _LOG.exception("python_sync failed")
        return False


def _run_patch(workspace: Path, patch: str) -> subprocess.CompletedProcess[str]:
    """Side-effect: ``git apply`` the unified diff inside the workspace.

    Why: prefer git so the patch matches the unified-diff shape Claude
    Code produces; fall back to ``patch -p1`` when git isn't available.
    """
    if shutil.which("git"):
        return subprocess.run(  # noqa: S603
            ["git", "apply", "--whitespace=nowarn", "-"],
            input=patch,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=60,
        )
    return subprocess.run(  # noqa: S603
        ["patch", "-p1", "--no-backup-if-mismatch"],
        input=patch,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _snapshot_workspace_for_patch(workspace: Path) -> Path | None:
    """Side-effect: copy the workspace to a sibling temp dir for rollback."""
    try:
        tmp = Path(tempfile.mkdtemp(prefix="aztea-patch-", dir=str(workspace.parent)))
        for entry in workspace.iterdir():
            if entry.name == ".git":
                continue
            target = tmp / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
        return tmp
    except Exception:
        _LOG.exception("could not snapshot workspace for patch rollback")
        return None


def _restore_workspace(workspace: Path, backup: Path | None) -> None:
    """Side-effect: replace workspace contents with the rollback copy."""
    if backup is None or not backup.exists():
        return
    for entry in workspace.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    for entry in backup.iterdir():
        target = workspace / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)


def _resolve_path(state: SandboxState, payload: dict[str, Any]) -> Path:
    rel = str(payload.get("path") or "").strip()
    if not rel:
        raise SandboxInvalidInput("path is required")
    root = Path(state.filesystem_root).resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SandboxInvalidInput(f"path escapes workspace: {rel!r}") from exc
    return candidate


def _looks_binary(raw: bytes) -> bool:
    """Pure: ``True`` if ``raw`` contains a NUL byte in its head."""
    return b"\x00" in raw[:4096]


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxInvalidInput(f"sandbox '{sandbox_id}' not active")
    return state
