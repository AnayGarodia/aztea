"""Outbound HTTP record/replay (VCR) for sandboxes.

# OWNS: sandbox_outbound_record + sandbox_outbound_replay. Cassettes live
#       under the per-sandbox state dir as JSON files; the recording proxy
#       captures (method, host, path, query, body_hash) → (status, headers,
#       body) tuples.
# NOT OWNS: the recording proxy server itself — for v0 this module
#           snapshots an already-running HTTP-mocking layer that the user
#           opts into via the AZTEA_VCR_* env vars baked into ``sandbox_start``.
#           Full mitmproxy/aiohttp recording proxy is the v1 follow-up.
# INVARIANTS:
#   * A cassette is identified by (sandbox_id, cassette_name) and is
#     append-only during record mode.
#   * Replay mode never reaches the network: cassette MISS returns a
#     structured error so a buggy test fails loudly instead of falling
#     back to a live call (the whole point of replay is determinism).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from core.sandbox.models import SandboxInvalidInput
from core.sandbox.state import SandboxState, get, sandbox_dir

_LOG = logging.getLogger("aztea.sandbox.vcr")
_CASSETTE_NAME_MAX = 64
_INTERACTION_MAX_BYTES = 256 * 1024
_DEFAULT_CASSETTE = "default"


def outbound_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Switch this sandbox's HTTP-recorder into ``record`` mode.

    The mode is persisted on disk so the sandbox's helper containers
    (and the in-network ``sandbox_http_request`` path) can read it on
    each call. Sandbox containers that opt into recording set their
    ``HTTPS_PROXY``/``HTTP_PROXY`` env to the proxy URL the operator
    configured; the proxy reads ``AZTEA_VCR_MODE`` from the same disk
    location to decide whether to record, replay, or pass through.
    """
    state, cassette = _require_with_cassette(payload)
    cassette_path = _cassette_path(state.sandbox_id, cassette)
    cassette_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not cassette_path.is_file():
        # Initialise an empty cassette so replay can be flipped on later
        # without having to retry a fresh record cycle.
        _write_cassette(cassette_path, [])
    _write_mode(state.sandbox_id, mode="record", cassette=cassette)
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "mode": "record",
        "cassette": cassette,
        "cassette_path": str(cassette_path),
        "interactions": _interaction_count(cassette_path),
        "proxy_url_env": _proxy_env_hint(),
        "note": (
            "Containers in this sandbox should set HTTPS_PROXY / HTTP_PROXY "
            "to the recorder proxy and ALL outbound HTTPS requests will be "
            "captured. Switch to replay mode with sandbox_outbound_replay; "
            "MISS in replay mode returns a structured error (no live "
            "fallback) so tests stay deterministic."
        ),
    }


def outbound_replay(payload: dict[str, Any]) -> dict[str, Any]:
    """Switch this sandbox's HTTP-recorder into ``replay`` mode."""
    state, cassette = _require_with_cassette(payload)
    cassette_path = _cassette_path(state.sandbox_id, cassette)
    if not cassette_path.is_file():
        raise SandboxInvalidInput(
            f"cassette '{cassette}' has no recordings; run "
            f"sandbox_outbound_record first."
        )
    _write_mode(state.sandbox_id, mode="replay", cassette=cassette)
    state.touch()
    interactions = _interaction_count(cassette_path)
    return {
        "sandbox_id": state.sandbox_id,
        "mode": "replay",
        "cassette": cassette,
        "interactions": interactions,
        "deterministic": True,
        "note": (
            f"Replay mode is now active over {interactions} recorded "
            "interactions. A miss returns a structured error to keep the "
            "deterministic guarantee."
        ),
    }


def vcr_append(
    sandbox_id: str,
    *,
    method: str,
    url: str,
    request_headers: dict[str, str] | None,
    request_body: str | bytes | None,
    status: int,
    response_headers: dict[str, str],
    response_body: str | bytes,
    cassette: str = _DEFAULT_CASSETTE,
) -> dict[str, Any]:
    """Append a recorded interaction to a cassette.

    Why: called by the recorder proxy each time it sees a request in
    record mode. Kept as a stable engine entry point so the proxy
    implementation can change without breaking the on-disk layout.
    """
    cassette_name = _validate_cassette_name(cassette)
    path = _cassette_path(sandbox_id, cassette_name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = _read_cassette(path)
    interaction = _build_interaction(
        method=method, url=url,
        request_headers=request_headers,
        request_body=request_body,
        status=status,
        response_headers=response_headers,
        response_body=response_body,
    )
    existing.append(interaction)
    _write_cassette(path, existing)
    return interaction


def vcr_replay_lookup(
    sandbox_id: str,
    *,
    method: str,
    url: str,
    request_body: str | bytes | None,
    cassette: str = _DEFAULT_CASSETTE,
) -> dict[str, Any] | None:
    """Return the recorded response for a request, or ``None`` on miss.

    Matching key: ``(method.upper(), url, sha256(body))``. Two replays
    of the same request return the same response in the order they were
    recorded — a tiny per-cassette cursor lives on disk to preserve
    ordering across process restarts.
    """
    cassette_name = _validate_cassette_name(cassette)
    path = _cassette_path(sandbox_id, cassette_name)
    if not path.is_file():
        return None
    interactions = _read_cassette(path)
    cursor = _read_cursor(sandbox_id, cassette_name)
    body_hash = _hash_body(request_body)
    key = (method.upper(), url, body_hash)
    for idx in range(cursor, len(interactions)):
        entry = interactions[idx]
        if (
            entry.get("method", "").upper(),
            entry.get("url", ""),
            entry.get("request_body_sha256", ""),
        ) == key:
            _write_cursor(sandbox_id, cassette_name, idx + 1)
            return entry
    # Wrap to beginning so a deterministic test can replay the cassette
    # multiple times if it issues the same request twice.
    for idx in range(cursor):
        entry = interactions[idx]
        if (
            entry.get("method", "").upper(),
            entry.get("url", ""),
            entry.get("request_body_sha256", ""),
        ) == key:
            _write_cursor(sandbox_id, cassette_name, idx + 1)
            return entry
    return None


def vcr_mode(sandbox_id: str) -> dict[str, Any]:
    """Pure-ish: current ``(mode, cassette)`` for a sandbox; defaults to ``off``."""
    mode_path = _mode_path(sandbox_id)
    if not mode_path.is_file():
        return {"mode": "off", "cassette": None}
    try:
        return json.loads(mode_path.read_text("utf-8"))
    except (OSError, ValueError):
        return {"mode": "off", "cassette": None}


def _require_with_cassette(payload: dict[str, Any]) -> tuple[SandboxState, str]:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxInvalidInput(f"sandbox '{sandbox_id}' not active")
    cassette = _validate_cassette_name(payload.get("cassette") or _DEFAULT_CASSETTE)
    return state, cassette


def _validate_cassette_name(name: str | None) -> str:
    """Pure: reject path-traversal-friendly cassette names."""
    if not isinstance(name, str):
        raise SandboxInvalidInput("cassette must be a string")
    cleaned = name.strip()
    if not cleaned:
        raise SandboxInvalidInput("cassette must be non-empty")
    if len(cleaned) > _CASSETTE_NAME_MAX:
        raise SandboxInvalidInput(
            f"cassette name longer than {_CASSETTE_NAME_MAX} chars"
        )
    if any(ch in cleaned for ch in ("/", "\\", "..", "\x00")):
        raise SandboxInvalidInput("cassette name must not contain path separators")
    return cleaned


def _cassette_path(sandbox_id: str, cassette: str) -> Path:
    return sandbox_dir(sandbox_id) / "vcr" / f"{cassette}.jsonl"


def _mode_path(sandbox_id: str) -> Path:
    return sandbox_dir(sandbox_id) / "vcr" / "mode.json"


def _cursor_path(sandbox_id: str, cassette: str) -> Path:
    return sandbox_dir(sandbox_id) / "vcr" / f"{cassette}.cursor"


def _read_cassette(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def _write_cassette(path: Path, interactions: list[dict[str, Any]]) -> None:
    """Side-effect: rewrite the cassette atomically as JSONL."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for entry in interactions:
            f.write(json.dumps(entry, sort_keys=True))
            f.write("\n")
    tmp.replace(path)


def _read_cursor(sandbox_id: str, cassette: str) -> int:
    path = _cursor_path(sandbox_id, cassette)
    if not path.is_file():
        return 0
    try:
        return int(path.read_text("utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_cursor(sandbox_id: str, cassette: str, cursor: int) -> None:
    path = _cursor_path(sandbox_id, cassette)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(str(int(cursor)), encoding="utf-8")


def _write_mode(sandbox_id: str, *, mode: str, cassette: str) -> None:
    path = _mode_path(sandbox_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = {
        "mode": mode,
        "cassette": cassette,
        "updated_at": int(time.time()),
    }
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")


def _interaction_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _hash_body(body: str | bytes | None) -> str:
    if body is None:
        return hashlib.sha256(b"").hexdigest()
    if isinstance(body, str):
        body = body.encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _build_interaction(
    *,
    method: str,
    url: str,
    request_headers: dict[str, str] | None,
    request_body: str | bytes | None,
    status: int,
    response_headers: dict[str, str],
    response_body: str | bytes,
) -> dict[str, Any]:
    """Pure: shape one cassette entry; truncates oversized bodies."""
    def _bound(value: Any) -> str:
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")
        if len(text) > _INTERACTION_MAX_BYTES:
            return text[:_INTERACTION_MAX_BYTES] + f"…[{len(text) - _INTERACTION_MAX_BYTES} bytes truncated]"
        return text

    return {
        "recorded_at": int(time.time()),
        "method": method.upper(),
        "url": url,
        "request_headers": {k: str(v) for k, v in (request_headers or {}).items()},
        "request_body": _bound(request_body),
        "request_body_sha256": _hash_body(request_body),
        "status": int(status),
        "response_headers": {k: str(v) for k, v in (response_headers or {}).items()},
        "response_body": _bound(response_body),
    }


def _proxy_env_hint() -> str:
    """Pure: hint for operators on which env to expose to the proxy."""
    return "AZTEA_VCR_PROXY_URL"
