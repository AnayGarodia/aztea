"""Ed25519 signing-key backfill for built-in agents.

Called from server startup to ensure every curated built-in agent has a
signing keypair so completed jobs produce verifiable receipts. The UPDATE
is a no-op when the key is already present.

`ensure_agent_signing_keys` is the same operation per-agent; it's safe to
call inline at sign time so a missing key on a single agent doesn't
permanently break receipts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .core_schema import _conn  # type: ignore[attr-defined]

_logger = logging.getLogger(__name__)


def backfill_agent_signing_keys(agent_ids: list[str], now: str) -> None:
    """Generate Ed25519 keypairs for any agent in *agent_ids* with a NULL key."""
    if not agent_ids:
        return
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT agent_id FROM agents WHERE agent_id IN ({}) AND (signing_private_key IS NULL OR signing_private_key = '')".format(
                    ",".join(["%s"] * len(agent_ids))
                ),
                agent_ids,
            ).fetchall()
        for row in rows:
            try:
                aid = row["agent_id"]
            except (TypeError, KeyError):
                aid = row[0]
            ensure_agent_signing_keys(aid, now=now)
    except Exception:
        _logger.exception("Failed to backfill signing keypairs for built-in agents")


def ensure_agent_signing_keys(
    agent_id: str, *, now: str | None = None
) -> tuple[str | None, str | None, str | None]:
    """Idempotently provision Ed25519 keys for *agent_id* and return
    ``(private_pem, public_pem, did)``. Returns ``(None, None, None)`` if
    the agent does not exist or key generation fails.
    """
    try:
        from core import crypto as _crypto
        from core.identity import build_agent_did as _build_agent_did

        with _conn() as conn:
            row = conn.execute(
                "SELECT signing_private_key, signing_public_key, did FROM agents WHERE agent_id = %s",
                (agent_id,),
            ).fetchone()
        if row is None:
            return (None, None, None)
        try:
            private_pem = row["signing_private_key"]
            public_pem = row["signing_public_key"]
            did_value = row["did"]
        except (TypeError, KeyError):
            private_pem, public_pem, did_value = row[0], row[1], row[2]
        if private_pem and public_pem and did_value:
            return (private_pem, public_pem, did_value)
        new_private, new_public = _crypto.generate_signing_keypair()
        new_did = did_value or _build_agent_did(agent_id)
        new_now = now or datetime.now(timezone.utc).isoformat()
        with _conn() as conn:
            conn.execute(
                "UPDATE agents SET did = %s, signing_public_key = %s, signing_private_key = %s, signing_keys_created_at = %s "
                "WHERE agent_id = %s AND (signing_private_key IS NULL OR signing_private_key = '')",
                (new_did, new_public, new_private, new_now, agent_id),
            )
        _logger.info("Lazy-provisioned Ed25519 keypair for agent %s", agent_id)
        return (new_private, new_public, new_did)
    except Exception:
        _logger.exception("Failed to lazy-provision signing keys for agent %s", agent_id)
        return (None, None, None)


# Plan B Phase 1 (2026-05-27): silent backfill of endpoint_signing_secret
# for every agent registered before migration 0074. The default decision
# (D1.a) is "silent backfill"; an agent owner discovers the secret next
# time they hit MyAgentsPage (which calls /registry/agents/{id} and shows
# the secret if endpoint_signing_secret_rotated_at is missing on the
# previous read — see frontend MyAgentsPage).


def backfill_endpoint_signing_secrets() -> dict[str, int]:
    """Idempotent: assign endpoint_signing_secret to every agent missing one.

    Skips agents whose endpoint_url uses ``internal://`` or ``skill://``
    (Aztea-hosted; no outbound HTTP, no HMAC use).

    Returns a small summary dict for log/metrics emission. Never raises.
    """
    summary = {"assigned": 0, "skipped_internal": 0, "errors": 0}
    try:
        from core import crypto as _crypto

        now_iso = datetime.now(timezone.utc).isoformat()
        with _conn() as conn:
            rows = conn.execute(
                "SELECT agent_id, endpoint_url FROM agents "
                "WHERE (endpoint_signing_secret IS NULL OR endpoint_signing_secret = '')",
            ).fetchall()
        for row in rows:
            try:
                aid = row["agent_id"]
                endpoint_url = row["endpoint_url"]
            except (TypeError, KeyError):
                aid, endpoint_url = row[0], row[1]
            raw_url = str(endpoint_url or "").strip().lower()
            if not raw_url or raw_url.startswith(("internal://", "skill://")):
                summary["skipped_internal"] += 1
                continue
            try:
                secret = _crypto.generate_endpoint_signing_secret()
                with _conn() as conn:
                    conn.execute(
                        "UPDATE agents SET endpoint_signing_secret = %s, "
                        "endpoint_signing_secret_rotated_at = %s "
                        "WHERE agent_id = %s AND "
                        "(endpoint_signing_secret IS NULL OR endpoint_signing_secret = '')",
                        (secret, now_iso, aid),
                    )
                summary["assigned"] += 1
            except Exception:
                _logger.exception(
                    "Failed to backfill endpoint_signing_secret for agent %s", aid,
                )
                summary["errors"] += 1
        if summary["assigned"]:
            _logger.info(
                "Backfilled endpoint_signing_secret on %d agent(s); skipped %d Aztea-hosted",
                summary["assigned"], summary["skipped_internal"],
            )
        return summary
    except Exception:
        _logger.exception("endpoint_signing_secret backfill aborted")
        return summary
