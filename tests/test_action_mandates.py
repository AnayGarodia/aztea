"""Action-mandate lifecycle tests (Phase 4). Typed states, cap clamp, nonce,
single-use consume idempotency, revoke/expire, signed sigil.
"""

from __future__ import annotations

import pytest

from core import action_mandates as am
from core import crypto


@pytest.fixture()
def mandates_db(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "DB_PATH", str(tmp_path / "mandates.db"))
    am.init_action_mandates_db()
    return am


def _create(db, **over):
    kw = dict(
        caller_owner_id="c", agent_id="a", action_kind="purchase",
        reversibility="reversible", max_spend_cents=500,
        allowed_domains=["shop.example.com"], action_descriptor={"sku": "X"},
    )
    kw.update(over)
    return db.create_mandate(**kw)


def test_transition_validator_is_closed():
    assert am.can_transition("issued", "authorized")
    assert am.can_transition("authorized", "consumed")
    assert not am.can_transition("consumed", "authorized")   # terminal
    assert not am.can_transition("issued", "consumed")       # must authorize first
    assert not am.can_transition("revoked", "authorized")
    assert not am.can_transition("bogus", "authorized")      # unknown -> False


def test_create_validates_enums_and_clamps_cap(mandates_db, monkeypatch):
    with pytest.raises(ValueError):
        _create(mandates_db, action_kind="bogus")
    with pytest.raises(ValueError):
        _create(mandates_db, allowed_domains=[])
    monkeypatch.setenv("AZTEA_ACTION_WEB_MAX_SPEND_CEILING_CENTS", "100")
    m = _create(mandates_db, max_spend_cents=999_999)
    assert m["max_spend_cents"] == 100 and m["status"] == "issued"  # clamped to ceiling


def test_authorize_then_consume_is_single_use(mandates_db):
    m = _create(mandates_db)
    nonce = m["confirmation_nonce"]
    assert mandates_db.authorize_mandate(m["mandate_id"], "wrong-nonce") is False
    assert mandates_db.authorize_mandate(m["mandate_id"], nonce) is True
    assert mandates_db.consume_mandate(m["mandate_id"], nonce) is True
    assert mandates_db.consume_mandate(m["mandate_id"], nonce) is False  # terminal/idempotent
    assert mandates_db.get_mandate(m["mandate_id"])["status"] == "consumed"


def test_cannot_consume_before_authorize(mandates_db):
    m = _create(mandates_db)
    assert mandates_db.consume_mandate(m["mandate_id"], m["confirmation_nonce"]) is False


def test_revoke_blocks_authorize(mandates_db):
    m = _create(mandates_db)
    assert mandates_db.revoke_mandate(m["mandate_id"]) is True
    assert mandates_db.authorize_mandate(m["mandate_id"], m["confirmation_nonce"]) is False


def test_mandate_sigil_signs_and_verifies(mandates_db):
    priv, pub = crypto.generate_signing_keypair()
    m = _create(mandates_db, private_pem=priv)
    assert m["mandate_sig"]
    assert crypto.verify_signature(pub, am.build_mandate_sigil(m), m["mandate_sig"]) is True


def test_expire_due_marks_overdue(mandates_db):
    m = _create(mandates_db, ttl_seconds=60)
    assert mandates_db.expire_due(now_iso="2099-01-01T00:00:00+00:00") >= 1
    assert mandates_db.get_mandate(m["mandate_id"])["status"] == "expired"
