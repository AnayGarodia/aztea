"""Action mandates for the escrowed write-web (Phase 4). Fail-closed.

# OWNS: the typed mandate lifecycle (issued -> authorized -> consumed | revoked |
#        expired), creation with a single-use confirmation nonce + signed sigil,
#        and the pure transition validator that makes illegal states unrepresentable.
# NOT OWNS: escrow/settlement math (core.payments.action_escrow), the browser
#           action (agents.web_actor), or feature gating (core.feature_flags).
# INVARIANTS:
#   * status / action_kind / reversibility are closed enums — no free-form
#     strings, no boolean flags.
#   * consumed/revoked/expired are terminal; consume only from 'authorized'.
#   * max_spend_cents is the integer-cent ceiling on real cost PLUS agent fee,
#     clamped to the platform ceiling at creation.
#   * confirmation_nonce is single-use and cleared on consume.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from core import crypto
from core import db as _db
from core import feature_flags

DB_PATH = _db.DB_PATH
_local = _db._local

MANDATE_SIG_SCHEME = "Ed25519+aztea-action-mandate/1"
_DEFAULT_TTL_SECONDS = 900   # 15 min: long enough to confirm, short enough to bound exposure
_MIN_TTL_SECONDS = 60
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class MandateStatus(str, Enum):
    ISSUED = "issued"
    AUTHORIZED = "authorized"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ActionKind(str, Enum):
    PURCHASE = "purchase"
    BOOK = "book"
    SUBMIT_FORM = "submit_form"
    CANCEL = "cancel"


class Reversibility(str, Enum):
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


# The only legal moves. consumed/revoked/expired are terminal (empty targets).
_ALLOWED_TRANSITIONS: dict[MandateStatus, set[MandateStatus]] = {
    MandateStatus.ISSUED: {MandateStatus.AUTHORIZED, MandateStatus.REVOKED, MandateStatus.EXPIRED},
    MandateStatus.AUTHORIZED: {MandateStatus.CONSUMED, MandateStatus.REVOKED, MandateStatus.EXPIRED},
    MandateStatus.CONSUMED: set(),
    MandateStatus.REVOKED: set(),
    MandateStatus.EXPIRED: set(),
}


def can_transition(current: str, target: str) -> bool:
    """Pure: is ``current`` -> ``target`` a legal mandate transition?"""
    try:
        return MandateStatus(target) in _ALLOWED_TRANSITIONS[MandateStatus(current)]
    except (ValueError, KeyError):
        return False


def _new_id() -> str:
    n = int.from_bytes(secrets.token_bytes(16), "big")
    chars: list[str] = []
    while n:
        n, rem = divmod(n, 62)
        chars.append(_BASE62[rem])
    return "amd_" + "".join(reversed(chars)).rjust(22, "0")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(DB_PATH)


def init_action_mandates_db() -> None:
    """Ensure mandate/action tables exist via migrations (single schema source)."""
    from core.migrate import apply_migrations

    apply_migrations(DB_PATH)


def build_mandate_sigil(m: dict[str, Any]) -> dict[str, Any]:
    """Pure: the canonical dict a mandate signature covers.

    Binds caller, agent, kind, cap, allowed domains, the descriptor hash, the
    nonce hash, and expiry, so a judge can later confirm the agent acted within
    exactly what the caller authorized — and a signature can't be reused.
    """
    desc_hash = hashlib.sha256(crypto.canonical_json(m.get("action_descriptor"))).hexdigest()
    nonce_hash = hashlib.sha256((m.get("confirmation_nonce") or "").encode("utf-8")).hexdigest()
    return {
        "v": MANDATE_SIG_SCHEME,
        "mandate_id": m["mandate_id"],
        "caller_owner_id": m["caller_owner_id"],
        "agent_id": m["agent_id"],
        "action_kind": m["action_kind"],
        "max_spend_cents": int(m["max_spend_cents"]),
        "currency": m["currency"],
        "allowed_domains": sorted(m.get("allowed_domains") or []),
        "action_descriptor_hash": desc_hash,
        "expires_at": m["expires_at"],
        "confirmation_nonce_hash": nonce_hash,
    }


def create_mandate(
    *, caller_owner_id: str, agent_id: str, action_kind: str, reversibility: str,
    max_spend_cents: int, allowed_domains: list[str], action_descriptor: Any,
    private_pem: str | None = None, ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Create an 'issued' mandate. Validates enums + cap at the boundary (fail loud).

    The cap is clamped to the platform ceiling. Signs the sigil if a key is given.
    """
    kind = ActionKind(action_kind).value  # raises ValueError on bad input
    rev = Reversibility(reversibility).value
    cap = int(max_spend_cents)
    if cap < 0:
        raise ValueError("max_spend_cents must be >= 0")
    if not isinstance(allowed_domains, list) or not allowed_domains:
        raise ValueError("allowed_domains must be a non-empty list")
    cap = min(cap, feature_flags.action_web_max_spend_ceiling_cents())
    now = datetime.now(timezone.utc)
    mandate = {
        "mandate_id": _new_id(),
        "caller_owner_id": str(caller_owner_id),
        "agent_id": str(agent_id),
        "action_kind": kind,
        "reversibility": rev,
        "max_spend_cents": cap,
        "currency": "USD",
        "allowed_domains": list(allowed_domains),
        "action_descriptor": action_descriptor,
        "confirmation_nonce": secrets.token_urlsafe(24),
        "expires_at": (now + timedelta(seconds=max(_MIN_TTL_SECONDS, int(ttl_seconds)))).isoformat(),
    }
    signature = crypto.sign_payload(private_pem, build_mandate_sigil(mandate)) if private_pem else None
    _insert_mandate(mandate, signature, now.isoformat())
    mandate["status"] = MandateStatus.ISSUED.value
    mandate["mandate_sig"] = signature
    return mandate


def _insert_mandate(m: dict[str, Any], signature: str | None, issued_at: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO action_mandates (mandate_id, caller_owner_id, agent_id, action_kind,
                reversibility, max_spend_cents, currency, allowed_domains, action_descriptor,
                status, confirmation_nonce, mandate_sig, mandate_sig_alg, issued_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'issued', %s, %s, %s, %s, %s)
            """,
            (m["mandate_id"], m["caller_owner_id"], m["agent_id"], m["action_kind"],
             m["reversibility"], int(m["max_spend_cents"]), m["currency"],
             json.dumps(m["allowed_domains"]), json.dumps(m["action_descriptor"], default=str),
             m["confirmation_nonce"], signature, MANDATE_SIG_SCHEME if signature else None,
             issued_at, m["expires_at"]),
        )


def get_mandate(mandate_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM action_mandates WHERE mandate_id = %s", (mandate_id,)
        ).fetchone()


def authorize_mandate(mandate_id: str, nonce: str) -> bool:
    """issued -> authorized iff the nonce matches and the mandate hasn't expired.

    Returns True iff exactly one row transitioned (rowcount guard = the atomic gate).
    """
    with _conn() as conn:
        result = conn.execute(
            "UPDATE action_mandates SET status = 'authorized' "
            "WHERE mandate_id = %s AND status = 'issued' AND confirmation_nonce = %s "
            "AND expires_at > %s",
            (mandate_id, nonce, _now()),
        )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def consume_mandate(mandate_id: str, nonce: str) -> bool:
    """authorized -> consumed iff nonce matches and not expired. Clears the nonce.

    The rowcount==1 guard is the action-layer idempotency key: a replay finds the
    mandate already consumed (or expired/revoked) and moves no money.
    """
    with _conn() as conn:
        result = conn.execute(
            "UPDATE action_mandates SET status = 'consumed', confirmation_nonce = NULL, "
            "resolved_at = %s WHERE mandate_id = %s AND status = 'authorized' "
            "AND confirmation_nonce = %s AND expires_at > %s",
            (_now(), mandate_id, nonce, _now()),
        )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def revoke_mandate(mandate_id: str) -> bool:
    """Revoke a not-yet-consumed mandate (issued or authorized). Terminal."""
    with _conn() as conn:
        result = conn.execute(
            "UPDATE action_mandates SET status = 'revoked', resolved_at = %s "
            "WHERE mandate_id = %s AND status IN ('issued', 'authorized')",
            (_now(), mandate_id),
        )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def expire_due(now_iso: str | None = None) -> int:
    """Sweep: mark issued/authorized mandates past their TTL as expired. Returns count."""
    now = now_iso or _now()
    with _conn() as conn:
        result = conn.execute(
            "UPDATE action_mandates SET status = 'expired', resolved_at = %s "
            "WHERE status IN ('issued', 'authorized') AND expires_at <= %s",
            (now, now),
        )
    return int(getattr(result, "rowcount", 0) or 0)
