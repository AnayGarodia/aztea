"""Ed25519 signing-key backfill for built-in agents.

Called from server startup to ensure every curated built-in agent has a
signing keypair so completed jobs produce verifiable receipts. The UPDATE
is a no-op when the key is already present.
"""

from __future__ import annotations

import logging

from .core_schema import _conn  # type: ignore[attr-defined]

_logger = logging.getLogger(__name__)


def backfill_agent_signing_keys(agent_ids: list[str], now: str) -> None:
    """Generate Ed25519 keypairs for any agent in *agent_ids* with a NULL key."""
    if not agent_ids:
        return
    try:
        from core import crypto as _crypto
        from core.identity import build_agent_did as _build_agent_did

        with _conn() as conn:
            rows = conn.execute(
                "SELECT agent_id FROM agents WHERE agent_id IN ({}) AND (signing_private_key IS NULL OR signing_private_key = '')".format(
                    ",".join("?" * len(agent_ids))
                ),
                agent_ids,
            ).fetchall()

        for (aid,) in rows:
            private_pem, public_pem = _crypto.generate_signing_keypair()
            agent_did_value = _build_agent_did(aid)
            with _conn() as conn:
                conn.execute(
                    "UPDATE agents SET did = ?, signing_public_key = ?, signing_private_key = ?, signing_keys_created_at = ? "
                    "WHERE agent_id = ? AND (signing_private_key IS NULL OR signing_private_key = '')",
                    (agent_did_value, public_pem, private_pem, now, aid),
                )
            _logger.info("Provisioned Ed25519 keypair for built-in agent %s", aid)
    except Exception:
        _logger.exception("Failed to backfill signing keypairs for built-in agents")
