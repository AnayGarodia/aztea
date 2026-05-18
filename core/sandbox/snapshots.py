"""Snapshots: snapshot / restore / fork / diff.

# OWNS: docker commit for each container, filesystem tar, optional pg_dump,
#       bundled snapshot artefact stored under the sandbox state dir.
# NOT OWNS: cross-region replication (out of v0); export to user bucket
#           (stubbed in stubs.py).
# INVARIANTS:
#   * Snapshot IDs use the project ``snap_`` prefix and are immutable once written.
#   * Restore is a live operation: containers are stopped, image rolled, then
#     started — the snapshot itself is never mutated.
#   * Fork creates a new sandbox_id from a snapshot; the original sandbox
#     keeps running and the fork bills independently.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

from core.sandbox.boot import _collect_compose_services  # type: ignore[reportPrivateUsage]
from core.sandbox.database import db_snapshot
from core.sandbox.docker_cli import run_docker
from core.sandbox.models import (
    SNAPSHOT_ID_PREFIX,
    SandboxInvalidInput,
    SandboxNotFound,
    now_unix,
)
from core.sandbox.state import (
    BootInfo,
    LifetimePolicy,
    NetworkPolicyState,
    SandboxState,
    epoch_minute_offset,
    generate_sandbox_id,
    get,
    project_name_for,
    register,
    sandbox_dir,
)

_LOG = logging.getLogger("aztea.sandbox.snapshots")


def snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Materialise a complete sandbox snapshot.

    The snapshot bundle layout:
        <sandbox_dir>/snapshots/<snap_id>/
            manifest.json        — services, db_dump filename, fs_tar filename
            fs.tar               — workspace contents
            services/<name>.tag  — docker image tag the container was committed to
            db/<db_label>.pgdump — optional pg_dump output
    """
    state = _require(payload)
    snap_id = _snapshot_id()
    snap_root = sandbox_dir(state.sandbox_id) / "snapshots" / snap_id
    snap_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    services_dir = snap_root / "services"
    services_dir.mkdir(exist_ok=True, mode=0o700)
    fs_tar = snap_root / "fs.tar"
    _tar_workspace(state, fs_tar)
    service_tags = _commit_each_service(state, services_dir, snap_id)
    db_label = None
    if state.boot.detected_postgres_service:
        try:
            out = db_snapshot({"sandbox_id": state.sandbox_id, "label": f"{snap_id}-db"})
            db_label = out["label"]
        except Exception:
            _LOG.exception("db_snapshot inside sandbox_snapshot failed (continuing)")
    manifest = {
        "snapshot_id": snap_id,
        "sandbox_id": state.sandbox_id,
        "created_at": now_unix(),
        "reason": payload.get("reason"),
        "service_tags": service_tags,
        "fs_tar": "fs.tar",
        "db_dump_label": db_label,
        "boot_info": _serialise_boot_info(state.boot),
        "lifetime": {
            "max_minutes": state.lifetime.max_minutes,
            "idle_kill_minutes": state.lifetime.idle_kill_minutes,
            "auto_snapshot_every_minutes": state.lifetime.auto_snapshot_every_minutes,
            "snapshot_on_stop": state.lifetime.snapshot_on_stop,
        },
        "network": {
            "egress": state.network.egress,
            "egress_allowlist": list(state.network.egress_allowlist),
        },
        "size": state.size,
    }
    (snap_root / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
    state.snapshot_chain.append(snap_id)
    state.last_snapshot_at = now_unix()
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "snapshot_id": snap_id,
        "service_tags": service_tags,
        "db_dump_label": db_label,
        "fs_tar_size_bytes": fs_tar.stat().st_size if fs_tar.exists() else 0,
        "created_at": manifest["created_at"],
    }


def restore(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    snap_id = str(payload.get("snapshot_id") or "").strip()
    if not snap_id:
        raise SandboxInvalidInput("snapshot_id is required")
    manifest_path = (
        sandbox_dir(state.sandbox_id) / "snapshots" / snap_id / "manifest.json"
    )
    if not manifest_path.is_file():
        raise SandboxNotFound(f"snapshot '{snap_id}' not found for sandbox '{state.sandbox_id}'")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    # Filesystem rollback first — quickest signal for the caller.
    fs_tar = sandbox_dir(state.sandbox_id) / "snapshots" / snap_id / "fs.tar"
    if fs_tar.is_file():
        _restore_fs(state, fs_tar)
    # Container image rollback: stop each service, retag image, restart.
    for service, tag in (manifest.get("service_tags") or {}).items():
        meta = state.boot.services.get(service)
        if not meta:
            continue
        container = meta.get("container") or service
        run_docker(["stop", container], timeout=30, check=False)
        run_docker(["rm", "-f", container], timeout=15, check=False)
        run_docker(
            [
                "run",
                "--detach",
                "--name",
                container,
                "--label",
                f"com.docker.compose.project={state.boot.project_name}",
                tag,
            ],
            timeout=60,
            check=False,
        )
    # Optional DB restore
    db_label = manifest.get("db_dump_label")
    if db_label and state.boot.detected_postgres_service:
        from core.sandbox.database import db_restore as db_restore_action

        db_restore_action({"sandbox_id": state.sandbox_id, "label": db_label})
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "snapshot_id": snap_id,
        "restored_services": list((manifest.get("service_tags") or {}).keys()),
    }


def fork(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a new sandbox_id from a snapshot of an existing sandbox.

    The fork takes the same lifetime + network policy as the source by
    default; caller can override via top-level payload keys.
    """
    source_id = str(payload.get("source_sandbox_id") or payload.get("sandbox_id") or "").strip()
    snap_id = str(payload.get("snapshot_id") or "").strip()
    if not source_id or not snap_id:
        raise SandboxInvalidInput("fork requires source_sandbox_id and snapshot_id")
    source_state = get(source_id)
    if source_state is None:
        raise SandboxNotFound(f"source sandbox '{source_id}' not active")
    manifest_path = sandbox_dir(source_id) / "snapshots" / snap_id / "manifest.json"
    if not manifest_path.is_file():
        raise SandboxNotFound(f"snapshot '{snap_id}' not found")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    new_id = generate_sandbox_id()
    project = project_name_for(new_id)
    # Materialise workspace from the snapshot fs.tar.
    new_repo = sandbox_dir(new_id) / "repo"
    new_repo.mkdir(parents=True, exist_ok=True)
    fs_tar = sandbox_dir(source_id) / "snapshots" / snap_id / "fs.tar"
    if fs_tar.is_file():
        with tarfile.open(fs_tar) as tar:
            tar.extractall(new_repo)
    # Spin up each committed image under the new project label.
    services_out: dict[str, dict[str, Any]] = {}
    for service, tag in (manifest.get("service_tags") or {}).items():
        container = f"{project}-{service}"
        run_docker(
            [
                "run",
                "--detach",
                "--name",
                container,
                "--label",
                f"com.docker.compose.project={project}",
                tag,
            ],
            timeout=60,
            check=False,
        )
        services_out[service] = {"container": container, "image": tag}
    boot_info = BootInfo(
        strategy=manifest.get("boot_info", {}).get("strategy", "snapshot"),
        project_name=project,
        services=services_out or _collect_compose_services(project),
        boot_timing={"fork_from_snapshot": 0.0},
    )
    boot_info.detected_postgres_service = manifest.get("boot_info", {}).get(
        "detected_postgres_service"
    )
    boot_info.detected_postgres_db = manifest.get("boot_info", {}).get(
        "detected_postgres_db"
    )
    boot_info.detected_postgres_user = manifest.get("boot_info", {}).get(
        "detected_postgres_user"
    )
    lifetime = LifetimePolicy(**(manifest.get("lifetime") or {}))
    new_state = SandboxState(
        sandbox_id=new_id,
        status="ready",
        created_at=now_unix(),
        expires_at=epoch_minute_offset(lifetime.max_minutes),
        last_activity_at=now_unix(),
        last_snapshot_at=0,
        workspace_id=source_state.workspace_id,
        owner_hint=source_state.owner_hint,
        region=source_state.region,
        size=dict(source_state.size),
        lifetime=lifetime,
        network=NetworkPolicyState(
            egress=manifest.get("network", {}).get("egress", "isolated"),
            egress_allowlist=list(manifest.get("network", {}).get("egress_allowlist", [])),
        ),
        boot=boot_info,
        filesystem_root=str(new_repo),
    )
    register(new_state)
    return {
        "source_sandbox_id": source_id,
        "source_snapshot_id": snap_id,
        "sandbox_id": new_id,
        "status": new_state.status,
        "services": services_out,
        "filesystem_root": str(new_repo),
    }


def diff_snapshots(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    a = str(payload.get("snapshot_a") or "").strip()
    b = str(payload.get("snapshot_b") or "").strip()
    if not a or not b:
        raise SandboxInvalidInput("snapshot_a and snapshot_b are required")
    root = sandbox_dir(state.sandbox_id) / "snapshots"
    manifest_a = _load_manifest(root / a)
    manifest_b = _load_manifest(root / b)
    fs_changes = _diff_tarballs(root / a / "fs.tar", root / b / "fs.tar")
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "snapshot_a": a,
        "snapshot_b": b,
        "files_changed": fs_changes,
        "service_tags_a": manifest_a.get("service_tags", {}),
        "service_tags_b": manifest_b.get("service_tags", {}),
        "db_dump_a": manifest_a.get("db_dump_label"),
        "db_dump_b": manifest_b.get("db_dump_label"),
    }


def _tar_workspace(state: SandboxState, target: Path) -> None:
    """Side-effect: tar the workspace dir (excluding ``.git``) to ``target``.

    Audit 2026-05-17 gap #4: when the host filesystem supports reflinks
    (btrfs / xfs with reflink=1 / zfs with copy-on-write enabled),
    ``cp --reflink=auto`` produces an O(1) clone instead of a byte-by-byte
    copy. We still write the tar — it stays the portable artifact for
    snapshot_export — but we ALSO write a reflink-backed mirror that
    sandbox_restore / sandbox_fork can use directly. Skipped on systems
    that don't support reflink; the tar path stays the universal fallback.
    """
    import shutil as _shutil
    import subprocess as _subprocess
    import time as _time

    workspace = Path(state.filesystem_root)
    tar_start = _time.time()
    with tarfile.open(target, "w") as tar:
        for entry in workspace.iterdir():
            if entry.name == ".git":
                continue
            tar.add(entry, arcname=entry.name)
    state.boot.boot_timing["snapshot_tar_seconds"] = round(
        _time.time() - tar_start, 3,
    )
    # COW mirror: cp --reflink=auto copies metadata + reflinks data
    # extents on supported filesystems. Failure here is silent — the
    # tar above is the universal source of truth.
    reflink_target = target.parent / "fs.reflink"
    if reflink_target.exists():
        try:
            _shutil.rmtree(reflink_target)
        except OSError:
            pass
    cp_bin = _shutil.which("cp")
    if cp_bin is None:
        return
    cow_start = _time.time()
    try:
        # GNU cp + macOS BSD cp both honour --reflink=auto, but BSD cp
        # silently no-ops when the FS doesn't support it. Pass each
        # workspace child as a separate arg so we don't have to deal
        # with the .git skip via a complex find pipeline.
        children = [
            str(entry) for entry in workspace.iterdir() if entry.name != ".git"
        ]
        if not children:
            return
        reflink_target.mkdir(parents=True, exist_ok=True)
        proc = _subprocess.run(  # noqa: S603
            [cp_bin, "-a", "--reflink=auto", *children, str(reflink_target) + "/"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            _LOG.debug("reflink mirror failed (this is fine): %s", proc.stderr[:200])
            try:
                _shutil.rmtree(reflink_target)
            except OSError:
                pass
            return
    except (_subprocess.TimeoutExpired, OSError):
        return
    state.boot.boot_timing["snapshot_reflink_seconds"] = round(
        _time.time() - cow_start, 3,
    )
    state.boot.boot_timing["snapshot_used_reflink"] = True  # type: ignore[assignment]


def _commit_each_service(
    state: SandboxState, services_dir: Path, snap_id: str
) -> dict[str, str]:
    """Side-effect: ``docker commit`` each compose service into a snapshot tag."""
    tags: dict[str, str] = {}
    for name, meta in state.boot.services.items():
        container = meta.get("container") or name
        tag = f"aztea-snap/{state.sandbox_id}/{name}:{snap_id}"
        try:
            run_docker(["commit", container, tag], timeout=60)
            tags[name] = tag
            (services_dir / f"{name}.tag").write_text(tag, "utf-8")
        except Exception:
            _LOG.exception("docker commit failed for %s", container)
    return tags


def _restore_fs(state: SandboxState, fs_tar: Path) -> None:
    """Side-effect: replace workspace contents with the snapshot's filesystem.

    Audit 2026-05-17 gap #4: when the snapshot has a reflink mirror
    sitting next to fs.tar (created by _tar_workspace above on COW-
    capable filesystems), we use it for an O(1) restore via cp
    --reflink=auto. Falls back to tar extract on any error.
    """
    import subprocess as _subprocess
    import time as _time

    workspace = Path(state.filesystem_root)
    for entry in workspace.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    reflink_src = fs_tar.parent / "fs.reflink"
    cp_bin = shutil.which("cp")
    if reflink_src.is_dir() and cp_bin is not None:
        t0 = _time.time()
        try:
            children = [str(c) for c in reflink_src.iterdir()]
            if children:
                _subprocess.run(  # noqa: S603
                    [cp_bin, "-a", "--reflink=auto", *children, str(workspace) + "/"],
                    capture_output=True, text=True, timeout=60, check=True,
                )
                state.boot.boot_timing["restore_via_reflink_seconds"] = round(
                    _time.time() - t0, 3,
                )
                return
        except (_subprocess.CalledProcessError, _subprocess.TimeoutExpired, OSError):
            _LOG.debug("reflink restore failed; falling back to tar extract")
    with tarfile.open(fs_tar) as tar:
        tar.extractall(workspace)


def _diff_tarballs(a: Path, b: Path) -> dict[str, list[str]]:
    """Pure-ish: list files added / removed / changed between two tarballs."""
    members_a = _tar_member_map(a)
    members_b = _tar_member_map(b)
    only_a = sorted(set(members_a) - set(members_b))
    only_b = sorted(set(members_b) - set(members_a))
    both = set(members_a) & set(members_b)
    changed = sorted(name for name in both if members_a[name] != members_b[name])
    return {"only_in_a": only_a, "only_in_b": only_b, "changed": changed}


def _tar_member_map(path: Path) -> dict[str, tuple[int, int]]:
    if not path.is_file():
        return {}
    out: dict[str, tuple[int, int]] = {}
    with tarfile.open(path) as tar:
        for m in tar.getmembers():
            out[m.name] = (m.size, m.mtime)
    return out


def _load_manifest(snap_dir: Path) -> dict[str, Any]:
    path = snap_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _snapshot_id() -> str:
    return f"{SNAPSHOT_ID_PREFIX}{secrets.token_hex(8)}{int(time.time()) % 100000:05d}"


def _serialise_boot_info(boot: BootInfo) -> dict[str, Any]:
    return {
        "strategy": boot.strategy,
        "project_name": boot.project_name,
        "services": boot.services,
        "detected_postgres_service": boot.detected_postgres_service,
        "detected_postgres_db": boot.detected_postgres_db,
        "detected_postgres_user": boot.detected_postgres_user,
    }


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxNotFound(f"sandbox '{sandbox_id}' not active")
    return state
