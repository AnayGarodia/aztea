"""Finding B-CRIT-01 (2026-05-30 review): privilege escalation to admin scope.

A self-registered user (caller-type) can mint an API key with `scopes=["admin"]`:

  1. POST /auth/register is unauthenticated (part_006.py:972-980, rate-limited only) →
     anyone can create a `type == "user"` account.
  2. POST /auth/keys (auth_create_key, part_006.py:1444) only gates `caller["type"] != "user"`;
     it passes `body.scopes` straight to `_auth.create_api_key` with NO admin filtering
     (part_006.py:1470-1477).
  3. `_normalize_scopes` (schema.py:600-624) accepts "admin" because it is in
     VALID_KEY_SCOPES (schema.py:45).
  4. `create_api_key` persists the normalized scopes unfiltered (users.py:484,495-503).
  5. `verify_api_key` loads them back into the CallerContext (users.py:643).
  6. `_caller_has_scope` (part_002.py:194-206) returns True for ANY required scope when
     "admin" is in the caller's scopes.
  7. The admin IP allowlist (part_002.py:167-179) is opt-in and empty by default, and
     `_require_admin_caller` only calls `_require_scope(caller, "admin")` — it does NOT
     invoke the IP check. So nothing else stops the escalated key.

This DEMONSTRATION test proves links 3 and 6 (the security-relevant core) without a server.
It is report-only and passes against current source (it documents the hole).
"""

from __future__ import annotations


def test_normalize_scopes_accepts_admin_from_arbitrary_caller():
    """A user-supplied scope list of ['admin'] is accepted, not rejected."""
    from core.auth.schema import _normalize_scopes, VALID_KEY_SCOPES

    assert "admin" in VALID_KEY_SCOPES  # admin is a mintable scope...
    # ...and _normalize_scopes does NOT require any caller privilege to grant it:
    assert _normalize_scopes(["admin"]) == ["admin"]


def test_caller_has_scope_grants_everything_with_admin_scope():
    """A key carrying the admin scope satisfies every scope check.

    The shard modules only resolve symbols inside the assembled `server.application`
    namespace, so we exercise the helper through that module (not the raw shard).
    """
    import server.application as app

    escalated_caller = {
        "type": "user",
        "user": {"user_id": "user:attacker"},
        "scopes": ["admin"],
    }
    # The user never had admin authority — they just asked for the scope — yet:
    assert app._caller_has_scope(escalated_caller, "admin") is True
    assert app._caller_has_scope(escalated_caller, "worker") is True
    assert app._caller_has_scope(escalated_caller, "caller") is True


def test_admin_ip_allowlist_is_opt_in_and_off_by_default():
    """The one possible mitigation (IP allowlist) is empty by default → no-op."""
    import server.application as app

    # Empty allowlist means the guard returns immediately for any request.
    assert not app._ADMIN_IP_ALLOWLIST_NETWORKS
