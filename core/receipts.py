"""Co-pilot mode signed receipts.

# OWNS: Build the canonical job transcript, sign it with the agent's
#   per-call Ed25519 key, persist the resulting JWS-compact string on
#   ``jobs.receipt_jws``, and serve it back to the receipt endpoint.
# NOT OWNS: Settlement decisions, ledger writes, dispute logic. The
#   settlement runner orchestrates *when* this is called; receipts only
#   shape and sign the transcript.
# INVARIANTS:
#   - Signing key is the agent's existing keypair (``agents.signing_*``)
#     — never a platform key.
#   - Transcript schema string is exactly ``aztea/copilot-receipt/1``.
#   - ``messages`` are ordered strictly by ``message_id ASC``.
#   - JWS-compact wire format: ``b64url(header).b64url(payload).b64url(sig)``
#     with header ``{"alg":"EdDSA","kid":"<agent did>"}``.
# DECISIONS:
#   - We re-derive the JWS signature input as ``b64url(header) + "." +
#     b64url(payload)`` per RFC 7515 §3.1, then sign those bytes
#     directly with Ed25519 (NOT ``canonical_json`` of the bytes — JWS
#     defines the signing input). The ``payload`` itself is the
#     canonical-JSON encoding of the transcript so verifiers can
#     re-derive it deterministically.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from core import crypto as _crypto
from core.db import get_db_connection
from core.identity import build_agent_did
from core.jobs.db import _decode_json

_RECEIPT_SCHEMA = "aztea/copilot-receipt/1"
_JWS_ALG = "EdDSA"


def _b64url(raw: bytes) -> str:
    """Base64url-encode without padding (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _row_get(row: Any, key: str, idx: int | None = None) -> Any:
    """Read ``key`` from a DB row regardless of dict-row vs tuple-row backend."""
    if row is None:
        return None
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        if idx is not None:
            try:
                return row[idx]
            except (TypeError, IndexError):
                return None
        return None


def _fetch_job_row(job_id: str) -> dict[str, Any]:
    """Load the job columns we need for the transcript. Raises if missing."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT job_id, agent_id, caller_owner_id, input_payload, "
            "output_payload, status, stop_reason_json, terminal_at "
            "FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"job not found: {job_id}")
    return {
        "job_id": _row_get(row, "job_id", 0),
        "agent_id": _row_get(row, "agent_id", 1),
        "caller_id": _row_get(row, "caller_owner_id", 2),
        "input": _decode_json(_row_get(row, "input_payload", 3), default={}),
        "output": _decode_json(_row_get(row, "output_payload", 4), default=None),
        "terminal_state": _row_get(row, "status", 5),
        "stop_reason": _decode_json(
            _row_get(row, "stop_reason_json", 6), default=None
        ),
        "terminal_at": _row_get(row, "terminal_at", 7),
    }


def _fetch_messages(job_id: str) -> list[dict[str, Any]]:
    """Return all job_messages for *job_id*, ordered by ``message_id ASC``.

    Each message is shaped as ``{id, type, from, payload, ts}`` with
    ``payload`` JSON-decoded. The ``from`` field is a coarse direction
    label derived from the message type rather than a per-message lookup,
    matching the spec's transcript schema.
    """
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT message_id, type, from_id, payload, created_at "
            "FROM job_messages WHERE job_id = %s ORDER BY message_id ASC",
            (job_id,),
        ).fetchall()
    return [
        {
            "id": _row_get(r, "message_id", 0),
            "type": _row_get(r, "type", 1),
            "from": _row_get(r, "from_id", 2),
            "payload": _decode_json(_row_get(r, "payload", 3), default={}),
            "ts": _row_get(r, "created_at", 4),
        }
        for r in rows
    ]


def build_transcript(job_id: str) -> dict[str, Any]:
    """Assemble the canonical receipt transcript dict for *job_id*.

    Pure-ish: only side effect is two DB SELECTs. The output dict is
    ready to be canonicalized via ``core.crypto.canonical_json`` and
    signed.
    """
    job = _fetch_job_row(job_id)
    messages = _fetch_messages(job_id)
    return {
        "schema": _RECEIPT_SCHEMA,
        "job_id": job["job_id"],
        "agent_id": job["agent_id"],
        "caller_id": job["caller_id"],
        "input": job["input"],
        "messages": messages,
        "output": job["output"],
        "terminal_state": job["terminal_state"],
        "stop_reason": job["stop_reason"],
        "terminal_at": job["terminal_at"],
    }


def _load_agent_signing_material(agent_id: str) -> tuple[str, str, str]:
    """Return ``(private_pem, public_pem, did)`` for *agent_id*.

    Reads directly from the ``agents`` row. We do not lazy-provision
    here; ``ensure_agent_signing_keys`` runs at agent registration and
    in the identity backfill, so by the time a job reaches a terminal
    state the keys must exist. Failing loud if they don't is correct
    behavior — a missing key means the agent row is corrupt.
    """
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT signing_private_key, signing_public_key, did "
            "FROM agents WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"agent not found: {agent_id}")
    private_pem = _row_get(row, "signing_private_key", 0)
    public_pem = _row_get(row, "signing_public_key", 1)
    did_value = _row_get(row, "did", 2) or build_agent_did(agent_id)
    if not private_pem or not public_pem:
        raise RuntimeError(
            f"agent {agent_id} has no signing keypair; "
            "expected ensure_agent_signing_keys to have provisioned one"
        )
    return private_pem, public_pem, did_value


def _jws_compact_sign(private_pem: str, kid: str, payload_bytes: bytes) -> str:
    """Produce a JWS-compact serialization per RFC 7515 §7.1.

    Signing input is the ASCII string
    ``b64url(header) + "." + b64url(payload)``. The signature bytes are
    Ed25519 over that input. Header is fixed: ``{"alg":"EdDSA","kid":...}``.
    """
    key = serialization.load_pem_private_key(
        private_pem.encode("utf-8"), password=None
    )
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError("agent private_pem is not Ed25519")
    header = {"alg": _JWS_ALG, "kid": kid}
    header_bytes = json.dumps(
        header, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    header_b64 = _b64url(header_bytes)
    payload_b64 = _b64url(payload_bytes)
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig_bytes = key.sign(signing_input)
    sig_b64 = _b64url(sig_bytes)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def sign_and_store_receipt(job_id: str) -> str:
    """Build, sign, and persist the receipt JWS for *job_id*.

    Returns the JWS-compact string. Idempotent at the call site only in
    the sense that re-signing produces a fresh signature over the same
    bytes; the settlement runner is responsible for not invoking this
    twice (it stamps ``pending_settlements.receipt_built_at``).
    """
    transcript = build_transcript(job_id)
    agent_id = transcript["agent_id"]
    private_pem, _public_pem, did_value = _load_agent_signing_material(agent_id)
    payload_bytes = _crypto.canonical_json(transcript)
    jws = _jws_compact_sign(private_pem, did_value, payload_bytes)
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE jobs SET receipt_jws = %s WHERE job_id = %s",
            (jws, job_id),
        )
    return jws


def read_receipt(job_id: str) -> dict[str, Any] | None:
    """Return ``{jws, transcript, public_jwk, kid}`` or ``None`` if not yet built.

    The ``GET /jobs/{id}/receipt`` route is the only intended caller.
    Returning ``None`` lets the route translate to ``425 Too Early``.
    """
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT receipt_jws, agent_id FROM jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    jws = _row_get(row, "receipt_jws", 0)
    if not jws:
        return None
    agent_id = _row_get(row, "agent_id", 1)
    _private_pem, public_pem, did_value = _load_agent_signing_material(agent_id)
    public_jwk = _crypto.public_key_to_jwk(public_pem)
    transcript = build_transcript(job_id)
    return {
        "jws": jws,
        "transcript": transcript,
        "public_jwk": public_jwk,
        "kid": did_value,
    }
