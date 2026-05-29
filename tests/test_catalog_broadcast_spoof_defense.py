# SPDX-License-Identifier: Apache-2.0
"""Tests for the spoofed-NOTIFY defense in core/registry/catalog_broadcast.py.

Anyone with DB write access can issue ``NOTIFY aztea_catalog_version, '<x>'``.
The handler must:
  - Reject non-integer payloads
  - Reject non-positive payloads
  - Bound the local version advance per broadcast so a pathological payload
    cannot silently confuse downstream decision-cache keys
"""
from __future__ import annotations

import pytest

from core.registry import catalog_broadcast


@pytest.fixture(autouse=True)
def _reset_version() -> None:
    """Each test starts with version 0 and no subscribed callbacks."""
    # Direct reset — module owns its state, the lock guards mutation.
    with catalog_broadcast._lock:
        catalog_broadcast._version = 0
    yield
    with catalog_broadcast._lock:
        catalog_broadcast._version = 0


def test_non_integer_payload_ignored() -> None:
    """Garbage payloads must not change the local version."""
    catalog_broadcast._handle_notify("not-a-number")
    assert catalog_broadcast.current_version() == 0


def test_non_positive_payload_ignored() -> None:
    """Zero or negative payloads are not legitimate version numbers."""
    catalog_broadcast._handle_notify("0")
    catalog_broadcast._handle_notify("-42")
    assert catalog_broadcast.current_version() == 0


def test_pathological_payload_clamped() -> None:
    """A huge version jump is clamped to current+1 to bound the damage."""
    catalog_broadcast._handle_notify("9999999999")
    # Clamp policy: advance by at most +1 above current when jump > 1000.
    assert catalog_broadcast.current_version() == 1


def test_legitimate_advance_is_honored() -> None:
    """Reasonable advances pass through unchanged."""
    catalog_broadcast._handle_notify("42")
    assert catalog_broadcast.current_version() == 42
    catalog_broadcast._handle_notify("100")
    assert catalog_broadcast.current_version() == 100


def test_stale_payload_does_not_regress_version() -> None:
    """A NOTIFY arriving out-of-order must not roll the version backward."""
    catalog_broadcast._handle_notify("50")
    catalog_broadcast._handle_notify("20")  # stale; less than current
    assert catalog_broadcast.current_version() == 50
