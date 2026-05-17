"""File-backed secret store + redaction for ``user-secret://`` references.

# OWNS: the on-disk ``secrets/`` directory per sandbox; secret resolution from
#       ``secret_refs``; the redaction helper used by exec/log/snapshot paths.
# NOT OWNS: actual encryption-at-rest (out of scope for v0; file perms 0o600
#           are the v0 guarantee — graduating to Vault is a follow-up).
# INVARIANTS:
#   * Secret values are NEVER returned in any response from the engine — only
#     resolved into the env of subprocesses inside the sandbox.
#   * Every action that touches stdout/stderr/log lines runs them through
#     :func:`redact` before returning.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from core.sandbox.models import SandboxInvalidInput
from core.sandbox.state import sandbox_dir, state_root

_SECRET_SCHEME = "user-secret://"
_GLOBAL_STORE_ENV = "AZTEA_SANDBOX_GLOBAL_SECRETS"
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_./-]{1,128}$")


def put_secret(sandbox_id: str, name: str, value: str) -> None:
    """Side-effect: write a secret value to the per-sandbox secret dir, 0o600.

    Why: this is the v0 implementation of secret_refs. The path is
    ``<sandbox_dir>/secrets/<name>`` so it survives auto-snapshot but is
    explicitly stripped from exported snapshots (see snapshots.py).
    """
    _validate_secret_name(name)
    target = _secret_path(sandbox_id, name)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # umask-independent perms: open + write to a temp file, fsync, rename.
    tmp = target.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    os.chmod(target, 0o600)


def get_secret(sandbox_id: str, name: str) -> str | None:
    """Read a per-sandbox secret; falls back to the global store.

    Why: callers configure shared secrets (e.g. a sanitised Stripe test key)
    once on the host and reuse them across many ephemeral sandboxes.
    """
    _validate_secret_name(name)
    per = _secret_path(sandbox_id, name)
    if per.is_file():
        return _read_value(per)
    global_path = _global_secret_path(name)
    if global_path is not None and global_path.is_file():
        return _read_value(global_path)
    return None


def resolve_secret_refs(
    sandbox_id: str, secret_refs: dict[str, str] | None
) -> tuple[dict[str, str], list[str]]:
    """Resolve a ``{ENV_VAR: 'user-secret://name'}`` map.

    Returns ``({ENV_VAR: value}, unresolved_names)``. Unresolved names
    surface back to the caller so they can be created on the host; we
    intentionally do NOT raise so sandbox boot remains usable when a few
    optional secrets are absent.
    """
    if not secret_refs:
        return {}, []
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for env_key, ref in secret_refs.items():
        if not isinstance(env_key, str) or not isinstance(ref, str):
            raise SandboxInvalidInput(
                f"secret_refs entry must be (str, str); got {env_key!r}={ref!r}"
            )
        if not ref.startswith(_SECRET_SCHEME):
            raise SandboxInvalidInput(
                f"secret_refs[{env_key}] must start with {_SECRET_SCHEME}; "
                f"got {ref!r}"
            )
        name = ref[len(_SECRET_SCHEME):]
        value = get_secret(sandbox_id, name)
        if value is None:
            unresolved.append(name)
            continue
        resolved[env_key] = value
    return resolved, unresolved


def all_secret_values(sandbox_id: str) -> list[str]:
    """Return every secret value the sandbox knows about (used by :func:`redact`).

    Why: keeps redaction stateless from the action call site — the engine
    handles the "never echo a secret" rule centrally.
    """
    out: list[str] = []
    sb_dir = sandbox_dir(sandbox_id) / "secrets"
    if sb_dir.is_dir():
        out.extend(_collect_secret_values(sb_dir))
    global_root = _global_secrets_root()
    if global_root is not None and global_root.is_dir():
        out.extend(_collect_secret_values(global_root))
    return [v for v in out if v]


def redact(text: str, secret_values: list[str]) -> str:
    """Pure: replace each secret value in ``text`` with ``[REDACTED]``.

    Why: subprocess stdout/stderr may echo env values via debug prints;
    redaction at the boundary is cheap and the only defence against an
    accidental leak in agent output.
    """
    if not text or not secret_values:
        return text or ""
    out = text
    # Sort by length descending so a long secret that contains a shorter
    # one as a substring is redacted first.
    for value in sorted(set(secret_values), key=len, reverse=True):
        if value and len(value) >= 4:
            out = out.replace(value, "[REDACTED]")
    return out


def _secret_path(sandbox_id: str, name: str) -> Path:
    """Pure: ``<sandbox_dir>/secrets/<name>``; raises on traversal."""
    sb_root = sandbox_dir(sandbox_id)
    secrets_dir = sb_root / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return secrets_dir / name


def _global_secret_path(name: str) -> Path | None:
    """Pure: ``<global_secrets_root>/<name>`` or ``None`` if unset."""
    root = _global_secrets_root()
    if root is None:
        return None
    return root / name


def _global_secrets_root() -> Path | None:
    """Pure: optional global secret store; configured via env."""
    raw = os.environ.get(_GLOBAL_STORE_ENV)
    if not raw:
        default = state_root() / "_global-secrets"
        if default.is_dir():
            return default
        return None
    return Path(raw).expanduser()


def _validate_secret_name(name: str) -> None:
    """Pure: refuse path-traversal-friendly names."""
    if not isinstance(name, str) or not _SECRET_NAME_RE.match(name):
        raise SandboxInvalidInput(f"invalid secret name: {name!r}")


def _collect_secret_values(root: Path) -> list[str]:
    """Side-effect: read every file under ``root`` non-recursively as a secret value."""
    out: list[str] = []
    try:
        for entry in root.iterdir():
            if entry.is_file():
                value = _read_value(entry)
                if value:
                    out.append(value)
    except OSError:
        return out
    return out


def _read_value(path: Path) -> str:
    """Side-effect: read a secret value from disk; trims one trailing newline."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if raw.endswith("\n"):
        raw = raw[:-1]
    return raw
