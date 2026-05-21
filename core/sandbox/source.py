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
import os
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

# The hardened sandbox containers run as a fixed non-root user (per
# core/sandbox/lifecycle.py::_isolation_hardening_argv). The host
# materialises the workspace as whatever UID the API process runs under
# (commonly 1001/circleci or 1000/ubuntu depending on host); without an
# explicit chown the container's non-root user cannot read its own
# checked-out repo. We retarget ownership post-materialisation so the
# repo is readable inside the hardened sandbox.
_CONTAINER_UID = 1000
_CONTAINER_GID = 1000


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
    _retarget_ownership_for_container(repo_dir)
    elapsed = round(time.time() - start, 2)
    return str(repo_dir), {"clone": elapsed}


def _retarget_ownership_for_container(repo_dir: Path) -> None:
    """Side-effect: chown the workspace tree to the canonical container UID.

    Without this, the hardened non-root user inside the sandbox cannot read
    files the host materialised under a different UID. Failure to chown
    (typically: running on a host without os.chown, or as a non-root API
    process without CAP_CHOWN) is logged but not fatal — the sandbox boot
    may still succeed if the host UID happens to match.
    """
    if not hasattr(os, "chown"):
        return
    try:
        for current_root, dirs, files in os.walk(repo_dir):
            os.chown(current_root, _CONTAINER_UID, _CONTAINER_GID)
            for entry in dirs + files:
                target = os.path.join(current_root, entry)
                try:
                    os.chown(target, _CONTAINER_UID, _CONTAINER_GID)
                except OSError:
                    # A single broken symlink shouldn't fail the whole
                    # workspace; continue with the rest.
                    continue
    except OSError as exc:
        _LOG.info(
            "could not chown sandbox repo to %d:%d (%s); container may "
            "lack read access if host UID differs",
            _CONTAINER_UID, _CONTAINER_GID, exc,
        )


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
    """Side-effect: write inline ``files[]`` entries into ``repo_dir``.

    Each entry MUST carry exactly one of ``content`` (UTF-8 text) or
    ``content_b64`` (base64-encoded bytes). Pre-fix the materialiser only
    read ``content_b64`` — callers that sent ``{"path": "x", "content": "hi"}``
    got a 0-byte file on disk with a successful signed receipt (silent data
    loss). The two-field shape mirrors a common pattern (e.g. K8s ConfigMap
    ``data`` vs ``binaryData``); we accept either to keep the schema
    ergonomic and fail loudly on ambiguous or missing payloads.
    """
    files = source.get("files")
    if not isinstance(files, list) or not files:
        raise SandboxInvalidInput("source.files must be a non-empty list for raw_files")
    for entry in files:
        if not isinstance(entry, dict):
            raise SandboxInvalidInput("source.files entries must be objects")
        rel_path = str(entry.get("path") or "").strip()
        if not rel_path:
            raise SandboxInvalidInput("source.files[].path is required")
        target = _safe_join(repo_dir, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_decode_raw_file_entry(rel_path, entry))


def _decode_raw_file_entry(rel_path: str, entry: dict[str, Any]) -> bytes:
    """Pure: resolve a raw_files entry to bytes, rejecting missing/ambiguous inputs."""
    has_text = "content" in entry and entry.get("content") is not None
    has_b64 = "content_b64" in entry and entry.get("content_b64") is not None
    if has_text and has_b64:
        raise SandboxInvalidInput(
            f"source.files[{rel_path}] must set exactly one of "
            "'content' (text) or 'content_b64' (base64), not both"
        )
    if has_text:
        text_value = entry["content"]
        if not isinstance(text_value, str):
            raise SandboxInvalidInput(
                f"source.files[{rel_path}].content must be a string"
            )
        return text_value.encode("utf-8")
    if has_b64:
        b64_value = entry["content_b64"]
        if not isinstance(b64_value, str):
            raise SandboxInvalidInput(
                f"source.files[{rel_path}].content_b64 must be a string"
            )
        try:
            return base64.b64decode(b64_value, validate=True)
        except (ValueError, TypeError) as exc:
            raise SandboxInvalidInput(
                f"source.files[{rel_path}].content_b64 must be valid base64"
            ) from exc
    raise SandboxInvalidInput(
        f"source.files[{rel_path}] must set 'content' (text) or 'content_b64' (base64); "
        "neither was provided"
    )


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
