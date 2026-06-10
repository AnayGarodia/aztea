"""Proof-of-observation receipts (Phase 2 of the agent-readable-web build).

# OWNS: the observation_receipts table, the canonical signing sigil, issuing a
#        signed receipt, and verifying one (signature + extraction-hash match).
# NOT OWNS: signing primitives (core.crypto), key management (the agent's PEM
#           comes from the agents table via registry.ensure_agent_signing_keys),
#           dispute/reputation consequences.
# INVARIANTS:
#   * The claim is PROVENANCE, not truth: the receipt attests what was observed
#     and by whom, NOT that the page content is correct. verify() says so.
#   * observed_at is server-stamped (int epoch) — never read from caller input.
#   * Hashes are computed here over the bytes/extraction we received; signing
#     always goes through core.crypto (never reimplemented).
#   * Insert-only — a correction is a new receipt, never an UPDATE.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from core import crypto
from core import db as _db

_LOG = logging.getLogger(__name__)

DB_PATH = _db.DB_PATH
_local = _db._local

OBSERVATION_RECEIPT_SCHEMA = "aztea/observation-receipt/1"
_SIG_ALG = "Ed25519"
_CLAIM = "provenance_only"
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _new_receipt_id() -> str:
    n = int.from_bytes(secrets.token_bytes(16), "big")
    chars: list[str] = []
    while n:
        n, rem = divmod(n, 62)
        chars.append(_BASE62[rem])
    return "obs_" + "".join(reversed(chars)).rjust(22, "0")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(DB_PATH)


def init_observation_receipts_db() -> None:
    """Ensure the table exists by applying migrations (single schema source)."""
    from core.migrate import apply_migrations

    apply_migrations(DB_PATH)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_signing_payload(
    *, receipt_id: str, job_id: str, agent_id: str, signer_kind: str,
    observed_at: int, observation: dict[str, Any],
) -> dict[str, Any]:
    """Pure: the canonical sigil the signature covers.

    Binds (receipt_id, job_id, agent_id, observed_at) plus the observation
    metadata + content hashes, so a signature can't be replayed onto a
    different observation (the OUTPUT_SIG_SCHEME_V2 anti-replay lesson).
    """
    return {
        "v": OBSERVATION_RECEIPT_SCHEMA,
        "receipt_id": receipt_id,
        "job_id": job_id,
        "agent_id": agent_id,
        "signer_kind": signer_kind,
        "observed_at": int(observed_at),
        "request_url": observation["request_url"],
        "final_url": observation["final_url"],
        "http_status": observation.get("http_status"),
        "content_type": observation.get("content_type"),
        "snapshot_kind": observation["snapshot_kind"],
        "dom_sha256": observation["dom_sha256"],
        "dom_bytes": int(observation["dom_bytes"]),
        "extraction_sha256": observation["extraction_sha256"],
    }


def issue_observation_receipt(
    *, agent_id: str, private_pem: str, signer_did: str, request_url: str,
    final_url: str, dom_snapshot: bytes, extraction: Any,
    http_status: int | None = None, content_type: str | None = None,
    snapshot_kind: str = "accessibility_tree", job_id: str = "",
    signer_kind: str = "agent",
) -> dict[str, Any] | None:
    """Compute hashes server-side, sign the sigil, persist, return the receipt.

    Best-effort: returns None (never raises) if signing/persistence fails, so a
    receipt failure can't break the underlying navigation.
    """
    try:
        receipt_id = _new_receipt_id()
        observed_at = int(time.time())  # server-stamped, never agent-supplied
        dom_sha256 = _sha256_hex(dom_snapshot)
        extraction_sha256 = _sha256_hex(crypto.canonical_json(extraction))
        observation = {
            "request_url": request_url, "final_url": final_url,
            "http_status": http_status, "content_type": content_type,
            "snapshot_kind": snapshot_kind, "dom_sha256": dom_sha256,
            "dom_bytes": len(dom_snapshot), "extraction_sha256": extraction_sha256,
        }
        sigil = build_signing_payload(
            receipt_id=receipt_id, job_id=job_id, agent_id=agent_id,
            signer_kind=signer_kind, observed_at=observed_at, observation=observation,
        )
        signature = crypto.sign_payload(private_pem, sigil)
        _persist(receipt_id, job_id, agent_id, signer_kind, signer_did,
                 observed_at, observation, extraction, signature)
        return {
            "schema": OBSERVATION_RECEIPT_SCHEMA, "receipt_id": receipt_id,
            "job_id": job_id, "agent_id": agent_id, "observed_at": observed_at,
            "observation": observation, "extraction": extraction,
            "claim": _CLAIM, "signer_kind": signer_kind, "signer_did": signer_did,
            "signature": signature, "signature_alg": _SIG_ALG,
        }
    except Exception:  # noqa: BLE001 — attestation is additive; never break the call
        _LOG.warning("observation receipt issue failed for %s", final_url, exc_info=True)
        return None


def _persist(
    receipt_id: str, job_id: str, agent_id: str, signer_kind: str, signer_did: str,
    observed_at: int, observation: dict[str, Any], extraction: Any, signature: str,
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO observation_receipts (receipt_id, job_id, agent_id, signer_kind,
                signer_did, observed_at, request_url, final_url, http_status, content_type,
                snapshot_kind, dom_sha256, dom_bytes, extraction_sha256, extraction_json,
                signature, signature_alg, schema_version, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (receipt_id, job_id, agent_id, signer_kind, signer_did, observed_at,
             observation["request_url"], observation["final_url"], observation.get("http_status"),
             observation.get("content_type"), observation["snapshot_kind"],
             observation["dom_sha256"], int(observation["dom_bytes"]),
             observation["extraction_sha256"],
             json.dumps(extraction, default=str, ensure_ascii=False),
             signature, _SIG_ALG, OBSERVATION_RECEIPT_SCHEMA, _now_iso()),
        )


def get_observation_receipt(receipt_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM observation_receipts WHERE receipt_id = %s", (receipt_id,)
        ).fetchone()


def verify_receipt_object(
    receipt: dict[str, Any], public_pem: str, *, expected_did: str | None = None,
) -> dict[str, Any]:
    """Pure: verify a receipt object against a public key.

    valid == signature_ok AND extraction_hash_ok AND did_ok. (Raw DOM bytes are not
    retained, so dom-byte substitution is only detectable where bytes are kept; the
    dom_sha256 is still covered by the signature.) Claim is provenance only.

    did_ok closes the provenance-spoof hole: the key is resolved from the (signed)
    agent_id, so a caller can sign with their OWN key but still claim another agent's
    ``signer_did`` in the echoed verdict. When ``expected_did`` (the did the agent_id
    actually resolves to) is supplied, we require the receipt's signer_did to match it.
    expected_did=None means the caller is checking the signature only.
    """
    observation = receipt.get("observation") or {}
    sigil = build_signing_payload(
        receipt_id=str(receipt.get("receipt_id") or ""),
        job_id=str(receipt.get("job_id") or ""),
        agent_id=str(receipt.get("agent_id") or ""),
        signer_kind=str(receipt.get("signer_kind") or "agent"),
        observed_at=int(receipt.get("observed_at") or 0),
        observation=observation,
    )
    signature_ok = crypto.verify_signature(public_pem, sigil, str(receipt.get("signature") or ""))
    extraction_hash_ok = (
        _sha256_hex(crypto.canonical_json(receipt.get("extraction")))
        == str(observation.get("extraction_sha256") or "")
    )
    did_ok = expected_did is None or str(receipt.get("signer_did") or "") == str(expected_did)
    return {
        "valid": bool(signature_ok and extraction_hash_ok and did_ok),
        "checks": {
            "signature_ok": signature_ok, "extraction_hash_ok": extraction_hash_ok,
            "did_ok": did_ok,
        },
        "signer_did": receipt.get("signer_did"),
        "observed_at": receipt.get("observed_at"),
        "claim": _CLAIM,
        "note": ("valid means signed by the named did:web AND the extraction matches "
                 "what was committed; it does NOT assert the page content is true."),
    }


def verify_observation_receipt(receipt_id: str) -> dict[str, Any]:
    """Load a stored receipt and verify it against the signer's public key + real did."""
    row = get_observation_receipt(receipt_id)
    if row is None:
        return {"valid": False, "error": "receipt_not_found", "claim": _CLAIM}
    receipt = _row_to_receipt(row)
    agent_id = str(row.get("agent_id") or "")
    public_pem = _resolve_public_pem(agent_id)
    if not public_pem:
        return {"valid": False, "error": "signer_key_unavailable", "claim": _CLAIM}
    return verify_receipt_object(receipt, public_pem, expected_did=_resolve_signer_did(agent_id))


def _resolve_public_pem(agent_id: str) -> str | None:
    """Side-effect: fetch the agent's Ed25519 public PEM for verification."""
    try:
        from core.registry.identity_backfill import ensure_agent_signing_keys

        _priv, public_pem, _did = ensure_agent_signing_keys(agent_id)
        return public_pem
    except Exception:  # noqa: BLE001 — missing key -> cannot verify, reported by caller
        return None


def _resolve_signer_did(agent_id: str) -> str | None:
    """Side-effect: the agent's REAL did:web. verify_* compares this to the receipt's
    claimed signer_did so a caller can't sign with their own key yet claim another
    agent's identity (the provenance-spoof fix). None -> signature-only verification."""
    try:
        from core.registry.identity_backfill import ensure_agent_signing_keys

        _priv, _pub, did = ensure_agent_signing_keys(agent_id)
        return did
    except Exception:  # noqa: BLE001 — unresolved did -> caller verifies signature only
        return None


def _row_to_receipt(row: dict[str, Any]) -> dict[str, Any]:
    """Pure: reshape a stored row back into the receipt object verify expects."""
    extraction = None
    if row.get("extraction_json"):
        try:
            extraction = json.loads(row["extraction_json"])
        except (json.JSONDecodeError, TypeError):
            extraction = None
    return {
        "receipt_id": row.get("receipt_id"), "job_id": row.get("job_id") or "",
        "agent_id": row.get("agent_id"), "signer_kind": row.get("signer_kind") or "agent",
        "signer_did": row.get("signer_did"), "observed_at": row.get("observed_at"),
        "observation": {
            "request_url": row.get("request_url"), "final_url": row.get("final_url"),
            "http_status": row.get("http_status"), "content_type": row.get("content_type"),
            "snapshot_kind": row.get("snapshot_kind"), "dom_sha256": row.get("dom_sha256"),
            "dom_bytes": row.get("dom_bytes"), "extraction_sha256": row.get("extraction_sha256"),
        },
        "extraction": extraction, "signature": row.get("signature"),
    }
