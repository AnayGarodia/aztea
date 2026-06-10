"""Royalty OBLIGATION recording for the shared-map commons (Phase F).

# OWNS: idempotently recording that a commons reuse owes a royalty to the author.
# NOT OWNS: the ledger money movement. *** NOT WIRED + MOVES NO MONEY in this PR. ***
#           The actual credit — funded from the platform fee so the books stay net-zero
#           — is the deferred write-web/royalty money-PR, which reads these obligation
#           rows. Recording-only here means flipping commons_royalties_enabled can never
#           mint money or drift the ledger; it only accrues a payable for the money-PR.
# INVARIANTS:
#   * Integer cents only.
#   * Idempotent on consumer_job_id (record_usage's UNIQUE anchor): one obligation per
#     consuming job, never double-counted.
#   * This module imports NO payments primitive on purpose — it cannot move money.
"""

from __future__ import annotations

import logging

from core.site_maps import store

_LOG = logging.getLogger(__name__)


def record_map_royalty_obligation(
    *, consumer_job_id: str, site_key: str, royalty_cents: int, author_owner_id: str,
    consumer_owner_id: str, map_id: str | None = None, api_spec_id: str | None = None,
) -> str | None:
    """Idempotently RECORD that a reuse owes ``royalty_cents`` to the author.

    Returns the usage_id, or None when already recorded / non-positive / missing author.
    Moves NO money (see the module header): the funded ledger credit is the deferred
    money-PR. This only accrues the payable, so enabling the flag can't drift the books.
    """
    cents = int(royalty_cents)
    if cents <= 0 or not author_owner_id:
        return None
    usage = store.record_usage(
        map_id=map_id, api_spec_id=api_spec_id, site_key=site_key,
        consumer_job_id=consumer_job_id, consumer_owner_id=consumer_owner_id,
        author_owner_id=author_owner_id, royalty_cents=cents, validated_fresh=True,
    )
    return usage["usage_id"] if usage else None
