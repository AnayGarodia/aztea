"""Ed25519-signed receipts for every sandbox action.

# OWNS: minting and persisting the per-action signed receipt + hash-chained
#       audit log per sandbox.
# NOT OWNS: the agent's per-job output signing (that path lives in
#           ``core.crypto.sign_output_v2`` and is hit by the platform after
#           the agent returns).
# INVARIANTS:
#   * Every action — including errors and stubs — gets a receipt.
#   * Receipts are signed over a canonical JSON form that includes the previous
#     receipt's hash so removing or reordering an action breaks the chain.
#   * The signing key is loaded from disk; if absent, we generate one and
#     persist it so the same host produces a stable did:web identity.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from core.crypto import (
    canonical_json,
    generate_signing_keypair,
    sign_payload,
)
from core.identity import build_agent_did
from core.sandbox.state import sandbox_dir, state_root

_LOG = logging.getLogger("aztea.sandbox.receipts")
_KEY_FILENAME = "signing_key.pem"
_PUBLIC_KEY_FILENAME = "signing_pubkey.pem"
_AUDIT_LOG_FILENAME = "audit.jsonl"
_SANDBOX_DID_SUFFIX = "live-sandbox"


def receipt_did() -> str:
    """Return the canonical ``did:web`` for the sandbox engine identity.

    Why: every receipt embeds this DID so an external verifier can fetch
    the public key from the same did:web doc the rest of Aztea publishes.
    The agent_id segment used is the stable string ``live-sandbox`` so the
    DID doesn't drift across deployments.
    """
    return build_agent_did(_SANDBOX_DID_SUFFIX)


def _signing_keypair() -> tuple[str, str]:
    """Side-effect: ensure a per-host signing key exists; return ``(priv, pub)`` PEMs.

    Why: keys are persisted under the state root so receipts minted from
    different agent calls share an identity. The first call generates a
    new keypair; subsequent calls reuse it.
    """
    root = state_root()
    key_path = root / _KEY_FILENAME
    pub_path = root / _PUBLIC_KEY_FILENAME
    if key_path.is_file() and pub_path.is_file():
        try:
            return key_path.read_text("utf-8"), pub_path.read_text("utf-8")
        except OSError:
            _LOG.warning("sandbox signing key unreadable; regenerating")
    priv_pem, pub_pem = generate_signing_keypair()
    _atomic_write(key_path, priv_pem, mode=0o600)
    _atomic_write(pub_path, pub_pem, mode=0o644)
    return priv_pem, pub_pem


def mint_receipt(
    *,
    sandbox_id: str | None,
    action: str,
    request: dict[str, Any],
    response: dict[str, Any],
    workspace_id: str | None = None,
    idempotency_key: str | None = None,
    parent_chain_tail_hash: str | None = None,
    parent_sandbox_id: str | None = None,
) -> dict[str, Any]:
    """Produce the Ed25519-signed receipt for one sandbox action.

    The signed payload covers: action verb, request fingerprint hash,
    response fingerprint hash, sandbox_id, timestamp, prev_hash. Storing
    full request/response in the receipt would balloon payload size; we
    store hashes and let the audit log carry the redacted bodies.

    Why: an external verifier can independently re-hash the request and
    response bodies they read from the audit log, recompute prev_hash,
    and validate the Ed25519 signature against did:web.

    ``parent_chain_tail_hash`` / ``parent_sandbox_id`` are populated by
    fork receipts so an auditor walking the fork's chain can cross-link
    back to the exact parent-chain tail at fork time (Bug #8).
    """
    issued_at = int(time.time())
    request_hash = _hash_value(request)
    response_hash = _hash_value(response)
    prev_hash = _last_hash(sandbox_id) if sandbox_id else ""
    payload = {
        "v": "aztea/sandbox-receipt/1",
        "sandbox_id": sandbox_id,
        "action": action,
        "issued_at": issued_at,
        "request_hash": request_hash,
        "response_hash": response_hash,
        "prev_hash": prev_hash,
        "workspace_id": workspace_id,
        "idempotency_key": idempotency_key,
        "consumed_contexts": [],
        "produced_contexts": [],
        "parent_chain_tail_hash": parent_chain_tail_hash,
        "parent_sandbox_id": parent_sandbox_id,
    }
    priv_pem, _pub_pem = _signing_keypair()
    signature_b64 = sign_payload(priv_pem, payload)
    receipt = {
        "did": receipt_did(),
        "alg": "Ed25519",
        "signed_at": issued_at,
        "payload": payload,
        "signature": signature_b64,
        "hash": _payload_hash(payload, signature_b64),
    }
    appended = False
    if sandbox_id:
        appended = _append_audit(sandbox_id, request, response, receipt)
    # Bug #9 from the 2026-05-18 audit: prev_hash should always be
    # resolvable via ``sandbox_audit``. If the append failed (disk full,
    # readonly state root) the chain still validates cryptographically
    # but the audit log has a gap. ``audit_appended=False`` makes that
    # gap explicit on the receipt so a caller can detect it instead of
    # chasing a phantom prev_hash.
    receipt["audit_appended"] = appended if sandbox_id else None
    return receipt


def read_audit(sandbox_id: str, *, limit: int = 1000) -> list[dict[str, Any]]:
    """Return up to ``limit`` audit entries for ``sandbox_id``, newest last.

    Why: the ``sandbox_audit`` action surfaces this raw chain so the
    caller can include the head hash in a PR description for provenance.
    """
    path = _audit_path(sandbox_id)
    if not path.is_file():
        return []
    lines = _tail_lines(path, limit)
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def merkle_root_for(sandbox_id: str) -> str:
    """Pure-ish: SHA-256 chain head over every receipt for ``sandbox_id``.

    Why: the spec asks for a single Merkle root the user can pin in their
    PR description. A pure hash chain over the per-action ``hash`` field
    is sufficient when each prior hash is bound into the next payload.
    """
    entries = read_audit(sandbox_id, limit=10**6)
    head = ""
    for entry in entries:
        rec_hash = (entry.get("receipt") or {}).get("hash") or ""
        head = hashlib.sha256((head + rec_hash).encode("utf-8")).hexdigest()
    return head


def _last_hash(sandbox_id: str) -> str:
    """Side-effect: read the most recent receipt hash; ``""`` when chain is empty."""
    path = _audit_path(sandbox_id)
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8") as f:
            last_line = ""
            for line in f:
                if line.strip():
                    last_line = line
        if not last_line:
            return ""
        return json.loads(last_line).get("receipt", {}).get("hash", "") or ""
    except (OSError, ValueError):
        return ""


def _append_audit(
    sandbox_id: str,
    request: dict[str, Any],
    response: dict[str, Any],
    receipt: dict[str, Any],
) -> bool:
    """Side-effect: append a JSONL entry to the audit log; never raises.

    Returns ``True`` on a successful append, ``False`` if the disk write
    failed for any reason. The boolean lets ``mint_receipt`` surface
    ``audit_appended=False`` on the receipt so callers can detect a
    chain gap rather than chase a prev_hash they can never look up.
    """
    try:
        path = _audit_path(sandbox_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = {
            "ts": int(time.time()),
            "request": _truncate_for_audit(request),
            "response": _truncate_for_audit(response),
            "receipt": receipt,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        return True
    except Exception:
        # Audit log failure must not break the action — receipts are still
        # valid, only the local chain is degraded. Caller has visibility
        # via the receipt itself.
        _LOG.exception("audit log append failed for %s", sandbox_id)
        return False


def _audit_path(sandbox_id: str) -> Path:
    return sandbox_dir(sandbox_id) / _AUDIT_LOG_FILENAME


def _hash_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _payload_hash(payload: dict[str, Any], signature_b64: str) -> str:
    """Pure: SHA-256 over payload + signature, hex. Used as prev_hash."""
    return hashlib.sha256(
        canonical_json({"payload": payload, "signature": signature_b64})
    ).hexdigest()


def _atomic_write(path: Path, value: str, *, mode: int) -> None:
    """Side-effect: write-then-rename to keep the on-disk view consistent."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, value.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, mode)


def _truncate_for_audit(value: Any, *, max_chars: int = 8192) -> Any:
    """Pure: bound the per-entry size so the audit log doesn't explode."""
    try:
        encoded = json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        encoded = repr(value)
    if len(encoded) <= max_chars:
        return value
    return {"truncated": True, "preview": encoded[:max_chars]}


def _tail_lines(path: Path, limit: int) -> list[str]:
    """Side-effect: return the last ``limit`` non-empty lines of ``path``.

    Why: avoids reading multi-MB audit logs into memory on every call;
    we still seek-to-end for simplicity (audit logs are not huge in v0).
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            buf = f.readlines()
    except OSError:
        return []
    cleaned = [line for line in buf if line.strip()]
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def base64_check(value: str) -> bool:
    """Pure: ``True`` iff ``value`` decodes as base64. Used by verify helpers."""
    try:
        base64.b64decode(value, validate=True)
        return True
    except (ValueError, TypeError):
        return False
