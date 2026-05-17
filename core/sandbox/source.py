"""Materialise the user's project source into the sandbox workspace.

# OWNS: ``git`` shallow-clone, tarball extraction, raw_files write-out into
#       the per-sandbox workspace directory.
# NOT OWNS: SSRF validation (delegated to ``core.url_security``), Docker calls.
# INVARIANTS:
#   * Every external URL passes through ``core.url_security.validate_outbound_url``.
#   * Workspace dir is always under the per-sandbox state root; never absolute
#     paths supplied by the caller (path traversal would let user code escape).
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from core.sandbox.models import SandboxBootFailed, SandboxInvalidInput
from core.sandbox.state import sandbox_dir
from core.url_security import validate_outbound_url

_LOG = logging.getLogger("aztea.sandbox.source")
_GIT_CLONE_TIMEOUT_S = 180
_TARBALL_MAX_BYTES = 256 * 1024 * 1024  # 256 MB cap to match the disk default


def materialise_source(sandbox_id: str, source: dict[str, Any]) -> tuple[str, dict[str, float]]:
    """Resolve a ``source`` block into a populated workspace; returns ``(repo_path, timing)``.

    Why: callers can ship git / tarball / raw_files / snapshot indistinguishably;
    centralising the dispatch lets the rest of the engine treat the workspace
    as a plain directory regardless of provenance.
    """
    if not isinstance(source, dict):
        raise SandboxInvalidInput("source must be an object")
    kind = str(source.get("kind") or "").strip()
    if not kind:
        raise SandboxInvalidInput("source.kind is required")
    repo_dir = sandbox_dir(sandbox_id) / "repo"
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)
    repo_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    start = time.time()
    if kind == "git":
        _materialise_git(repo_dir, source)
    elif kind == "tarball":
        _materialise_tarball(repo_dir, source)
    elif kind == "raw_files":
        _materialise_raw_files(repo_dir, source)
    elif kind == "snapshot":
        raise SandboxBootFailed(
            "snapshot source is implemented via sandbox_fork; "
            "call sandbox_fork with the snapshot_id instead"
        )
    elif kind == "fork_sandbox":
        raise SandboxBootFailed(
            "fork_sandbox is implemented via sandbox_fork — see snapshot docs"
        )
    else:
        raise SandboxInvalidInput(f"unsupported source.kind: {kind!r}")
    elapsed = round(time.time() - start, 2)
    return str(repo_dir), {"clone": elapsed}


def _materialise_git(repo_dir: Path, source: dict[str, Any]) -> None:
    """Side-effect: shallow ``git clone`` into ``repo_dir``."""
    url = source.get("url")
    if not isinstance(url, str) or not url.strip():
        raise SandboxInvalidInput("source.url is required for git")
    validate_outbound_url(url, "sandbox.source.git")
    ref = str(source.get("ref") or "").strip()
    shallow = bool(source.get("shallow", True))
    submodules = str(source.get("submodules") or "none").strip().lower()
    argv: list[str] = ["git", "clone"]
    if shallow:
        argv.extend(["--depth", "1"])
    if submodules in ("recursive", "shallow"):
        argv.append("--recurse-submodules")
        if shallow:
            argv.append("--shallow-submodules")
    if ref:
        argv.extend(["--branch", ref])
    argv.extend([url, str(repo_dir)])
    proc = _run(argv, timeout=_GIT_CLONE_TIMEOUT_S)
    if proc.returncode != 0:
        raise SandboxBootFailed(
            f"git clone failed: {(proc.stderr or '').strip()[:512]}",
            details={"argv": argv[:6]},
        )


def _materialise_tarball(repo_dir: Path, source: dict[str, Any]) -> None:
    """Side-effect: download + safely extract a tarball into ``repo_dir``."""
    url = source.get("url")
    if not isinstance(url, str) or not url.strip():
        raise SandboxInvalidInput("source.url is required for tarball")
    validate_outbound_url(url, "sandbox.source.tarball")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as tmp:
        try:
            req = urllib.request.Request(  # noqa: S310 (validated above)
                url, headers={"User-Agent": "aztea-live-sandbox/1"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                read = 0
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    read += len(chunk)
                    if read > _TARBALL_MAX_BYTES:
                        raise SandboxBootFailed(
                            f"tarball exceeds {_TARBALL_MAX_BYTES // (1024*1024)} MB cap"
                        )
                    tmp.write(chunk)
        except SandboxBootFailed:
            raise
        except Exception as exc:
            raise SandboxBootFailed(f"tarball fetch failed: {exc}") from exc
        tmp.flush()
        _safe_extract_tar(tmp.name, repo_dir)


def _materialise_raw_files(repo_dir: Path, source: dict[str, Any]) -> None:
    """Side-effect: write inline ``files[]`` entries into ``repo_dir``."""
    files = source.get("files")
    if not isinstance(files, list) or not files:
        raise SandboxInvalidInput("source.files must be a non-empty list for raw_files")
    for entry in files:
        if not isinstance(entry, dict):
            raise SandboxInvalidInput("source.files entries must be objects")
        rel_path = str(entry.get("path") or "").strip()
        content_b64 = entry.get("content_b64") or ""
        if not rel_path:
            raise SandboxInvalidInput("source.files[].path is required")
        target = _safe_join(repo_dir, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            decoded = base64.b64decode(content_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise SandboxInvalidInput(
                f"source.files[{rel_path}].content_b64 must be valid base64"
            ) from exc
        target.write_bytes(decoded)


def _safe_extract_tar(tar_path: str, target: Path) -> None:
    """Side-effect: tar extraction that refuses any entry that escapes ``target``.

    Why: ``tarfile.extractall`` historically allowed ``../`` escape; the
    explicit per-member resolve-check is the canonical fix.
    """
    target = target.resolve()
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            member_path = (target / member.name).resolve()
            if not _is_within(member_path, target):
                raise SandboxBootFailed(
                    f"tarball entry escapes workspace: {member.name!r}"
                )
            if member.isdev():
                raise SandboxBootFailed(
                    f"tarball entry is a device node: {member.name!r}"
                )
        tar.extractall(target)


def _safe_join(root: Path, rel: str) -> Path:
    """Pure: join ``rel`` under ``root`` and raise on traversal."""
    root_r = root.resolve()
    candidate = (root_r / rel).resolve()
    if not _is_within(candidate, root_r):
        raise SandboxInvalidInput(f"path escapes workspace: {rel!r}")
    return candidate


def _is_within(child: Path, parent: Path) -> bool:
    """Pure: ``True`` iff ``child`` is ``parent`` or a descendant."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Side-effect: subprocess wrapper used for git only; never shell=True."""
    return subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
