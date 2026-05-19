"""Behavior tests for the 2026-05-19 red-team fixes (F1–F44+).

These deliberately exercise the runtime — not source-grep — because the
prior sprint's source-anchored tests passed while prod still broke. Each
test calls the actual function with realistic inputs and asserts on the
returned dict.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import Any


# ===========================================================================
# F2 — callback_secret stripped from JobResponse
# ===========================================================================


def test_f2_callback_secret_not_echoed_in_job_response(monkeypatch):
    """_job_response must strip callback_secret from the response dict."""
    import server.application as server

    # Build a fake job row that mimics what crud._row_to_dict returns,
    # including the sensitive callback_secret column.
    job = {
        "job_id": "j-test-f2",
        "agent_id": "a-test",
        "caller_owner_id": "u-test",
        "claim_owner_id": None,
        "status": "complete",
        "price_cents": 5,
        "caller_charge_cents": 5,
        "input_payload": {},
        "output_payload": {"result": "ok"},
        "created_at": "2026-05-19T00:00:00Z",
        "updated_at": "2026-05-19T00:00:00Z",
        "callback_secret": "shhhh-do-not-leak",
        "callback_url": "https://example.com/hook",
        "max_attempts": 3,
        "attempt_count": 1,
        "retry_count": 0,
        "timeout_count": 0,
        "dispute_window_hours": 72,
        "completed_at": "2026-05-19T00:00:10Z",
    }
    caller = {"type": "user", "owner_id": "u-test", "scopes": ["caller"]}
    # disable disputable annotation to keep the test free of DB I/O
    monkeypatch.setattr(
        server, "_attach_disputable", lambda *a, **kw: None
    )
    out = server._job_response(job, caller)
    assert "callback_secret" not in out, (
        f"callback_secret leaked into response: {out.get('callback_secret')!r}"
    )
    # callback_url is fine to echo back; only the secret is sensitive.
    assert out.get("callback_url") == "https://example.com/hook"


# ===========================================================================
# F3 — sensitive fields redacted from work-example recorder
# ===========================================================================


def test_f3_recorder_redacts_sensitive_output_fields():
    """_redact_sensitive_for_example must replace token/secret-named fields."""
    import server.application as server

    sandbox_share_output = {
        "share_id": "shr_abc",
        "join_token": "JUv5BF_sl3MAgR91OFQg0hPizLiLQlb9",
        "public_url": "https://tunnel.example.com/abc",
        "access": "read",
        "expires_at": 1779182942,
        "signed_payload_b64": "eyJhbGciOiJFZDI1NTE5...",
        "auth_token": "Bearer sk-abc",
        "sandbox_id": "sbx_ok_to_show",
        "service": "ok_to_show",
    }
    redacted = server._redact_sensitive_for_example(sandbox_share_output)
    assert redacted["join_token"] == "<redacted>"
    assert redacted["share_id"] == "<redacted>"
    assert redacted["signed_payload_b64"] == "<redacted>"
    assert redacted["auth_token"] == "<redacted>"
    assert redacted["public_url"] == "<redacted>"
    # Non-sensitive fields pass through.
    assert redacted["sandbox_id"] == "sbx_ok_to_show"
    assert redacted["service"] == "ok_to_show"
    assert redacted["expires_at"] == 1779182942
    assert redacted["access"] == "read"  # 'access' alone is borderline; we keep it


def test_f3_recorder_redacts_nested_sensitive_fields():
    """Nested dict / list values must also be walked and redacted."""
    import server.application as server

    nested = {
        "outer": {
            "inner_token": "secret-thing",
            "session_cookie": "abc=def",
            "safe": "hello",
        },
        "list_of_things": [
            {"api_key": "sk-XXX"},
            {"description": "fine"},
        ],
    }
    redacted = server._redact_sensitive_for_example(nested)
    assert redacted["outer"]["inner_token"] == "<redacted>"
    assert redacted["outer"]["session_cookie"] == "<redacted>"
    assert redacted["outer"]["safe"] == "hello"
    assert redacted["list_of_things"][0]["api_key"] == "<redacted>"
    assert redacted["list_of_things"][1]["description"] == "fine"


def test_f3_redaction_does_not_mutate_input():
    """Original payload must be unchanged after redaction."""
    import server.application as server

    original = {"join_token": "abc", "nested": {"secret": "xyz"}}
    server._redact_sensitive_for_example(original)
    assert original["join_token"] == "abc", "Recorder mutated caller's dict!"
    assert original["nested"]["secret"] == "xyz"


# ===========================================================================
# F4 — dispute on PENDING job rejected at write path
# ===========================================================================


def _temp_db_with_disputes_schema():
    """Create a fresh sqlite file + run migrations through the dispute init.

    Returns ``(path, teardown)``. Callers MUST invoke ``teardown()`` in a
    finally block — without it the patched ``DB_PATH`` env var + reloaded
    module attribute leak into the next test, which previously poisoned
    ``test_dispute_consensus_caller_wins_full_refund`` (the
    ``transactions.charged_by_key_id`` column got added in a later
    migration that this helper's truncated schema doesn't apply, so
    subsequent tests that hit ``core.db`` saw a stale, partial schema).
    """
    import importlib
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    prior_env = os.environ.get("DB_PATH")
    from core import db as _db
    from core import migrate
    prior_db_path = getattr(_db, "DB_PATH", None)

    os.environ["DB_PATH"] = path
    importlib.reload(_db)
    _db.DB_PATH = path
    importlib.reload(migrate)
    migrate.apply_migrations(path)

    def _teardown() -> None:
        # Restore env first so any module reload reads the prior value.
        if prior_env is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = prior_env
        # Reload core.db so subsequent tests pick up a fresh thread-local
        # pool against the restored DB_PATH (the env-derived value, not
        # the temp path that's about to be unlinked).
        importlib.reload(_db)
        if prior_db_path is not None:
            _db.DB_PATH = prior_db_path
        importlib.reload(migrate)
        # Remove the temp file (incl. WAL siblings) — caller will unlink
        # the canonical .db file itself for back-compat with old call
        # sites, but we tidy any -shm / -wal artefacts here.
        for suffix in ("-shm", "-wal"):
            try:
                os.unlink(f"{path}{suffix}")
            except FileNotFoundError:
                pass

    return path, _teardown


_WALLET_INSERT_SQL = (
    "INSERT INTO wallets (wallet_id, owner_id, balance_cents, created_at) "
    "VALUES (%s, %s, %s, %s)"
)

_JOB_INSERT_PENDING_SQL = (
    "INSERT INTO jobs (job_id, agent_id, agent_owner_id, caller_owner_id, "
    "caller_wallet_id, agent_wallet_id, platform_wallet_id, status, "
    "price_cents, caller_charge_cents, charge_tx_id, input_payload, "
    "created_at, updated_at, max_attempts) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_JOB_INSERT_COMPLETE_SQL = (
    "INSERT INTO jobs (job_id, agent_id, agent_owner_id, caller_owner_id, "
    "caller_wallet_id, agent_wallet_id, platform_wallet_id, status, "
    "price_cents, caller_charge_cents, charge_tx_id, input_payload, "
    "output_payload, created_at, updated_at, completed_at, max_attempts) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def _seed_three_wallets(conn, ids: list[tuple[str, str, int]]) -> None:
    for wid, oid, bal in ids:
        conn.execute(_WALLET_INSERT_SQL, (wid, oid, bal, "2026-05-19"))


def test_f4_create_dispute_rejects_pending_job():
    """create_dispute must raise ValueError when the target job has no completed_at."""
    path, _teardown = _temp_db_with_disputes_schema()
    try:
        from core import db as _db, disputes

        with _db.get_db_connection() as conn:
            _seed_three_wallets(conn, [
                ("w-caller", "u-caller", 100),
                ("w-agent", "u-agent", 0),
                ("w-platform", "platform:fees", 0),
            ])
            conn.execute(
                _JOB_INSERT_PENDING_SQL,
                (
                    "j-pending", "a-x", "u-agent", "u-caller",
                    "w-caller", "w-agent", "w-platform",
                    "pending", 5, 5, "tx-1", "{}",
                    "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z", 3,
                ),
            )

        try:
            disputes.create_dispute(
                job_id="j-pending",
                filed_by_owner_id="u-caller",
                side="caller",
                reason="trying to dispute a pending job",
                filing_deposit_cents=25,
            )
            raised = False
        except ValueError as exc:
            raised = True
            assert "dispute.not_completed" in str(exc), str(exc)
        assert raised, "create_dispute should have raised on a pending job"
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        _teardown()


def test_f4_create_dispute_accepts_completed_job():
    """create_dispute must succeed when completed_at IS set."""
    path, _teardown = _temp_db_with_disputes_schema()
    try:
        from core import db as _db, disputes

        with _db.get_db_connection() as conn:
            _seed_three_wallets(conn, [
                ("w-c", "u-c", 100),
                ("w-a", "u-a", 0),
                ("w-p", "platform:fees", 0),
            ])
            conn.execute(
                _JOB_INSERT_COMPLETE_SQL,
                (
                    "j-done", "a-x", "u-a", "u-c",
                    "w-c", "w-a", "w-p",
                    "complete", 5, 5, "tx-1", "{}", '{"r":"ok"}',
                    "2026-05-19T00:00:00Z", "2026-05-19T00:00:10Z",
                    "2026-05-19T00:00:10Z", 3,
                ),
            )

        created = disputes.create_dispute(
            job_id="j-done",
            filed_by_owner_id="u-c",
            side="caller",
            reason="real grievance",
            filing_deposit_cents=25,
        )
        assert created is not None
        assert created.get("dispute_id")
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        _teardown()


# ===========================================================================
# F20 — dispute reason with NUL byte must surface a clean validator error,
# not a raw psycopg2 ValueError leak.
# ===========================================================================


def test_f20_dispute_reason_strips_nul_bytes():
    """A reason containing only NUL bytes must reject as empty; a reason
    with valid text plus a NUL must keep the text."""
    from core.models.job_requests import JobDisputeRequest

    # Only control bytes -> empty after strip -> 422 "reason must not be empty"
    try:
        JobDisputeRequest(reason="\x00\x07\x1b")
        assert False, "Pure control-byte reason should have failed validation"
    except Exception as exc:
        assert "reason must not be empty" in str(exc), str(exc)

    # Real text with embedded NUL -> NUL stripped, valid reason persists.
    req = JobDisputeRequest(reason="Output broken\x00 in production")
    assert "\x00" not in req.reason
    assert "Output broken" in req.reason


def test_f20_dispute_evidence_strips_controls():
    """Evidence field gets the same sanitization."""
    from core.models.job_requests import JobDisputeRequest

    req = JobDisputeRequest(
        reason="Real reason here",
        evidence="https://example.com/proof\x00.html",
    )
    assert "\x00" not in (req.evidence or "")


# ===========================================================================
# F9 — describe_specialist on a sunset slug returns a structured
# agent.sunset hint rather than the bare "Unknown tool".
# ===========================================================================


def test_f9_describe_specialist_returns_sunset_hint():
    """The B25 sunset map was only consulted on the call path. Now the
    describe path uses the same map so callers asking about a sunset slug
    get the recommended replacement instead of a TOOL_NOT_FOUND."""
    from aztea.mcp.server import _SUNSET_AGENT_REPLACEMENTS, RegistryBridge

    import threading
    srv = RegistryBridge.__new__(RegistryBridge)
    srv._catalog_cache = []
    srv._entries = []
    srv._lock = threading.Lock()
    srv._catalog_at = 0.0

    out = srv._describe_catalog_entry("docs_grounder")
    assert out.get("error") == "agent.sunset", (
        f"Expected agent.sunset, got {out!r}"
    )
    # Suggestion text must come from the map.
    assert out.get("suggestion") == _SUNSET_AGENT_REPLACEMENTS["docs_grounder"]


def test_f9_describe_specialist_returns_tool_not_found_for_unknown():
    """Real unknown slugs still 404 with TOOL_NOT_FOUND — the sunset
    shortcut must not swallow genuine misses."""
    from aztea.mcp.server import RegistryBridge

    import threading
    srv = RegistryBridge.__new__(RegistryBridge)
    srv._catalog_cache = []
    srv._entries = []
    srv._lock = threading.Lock()
    srv._catalog_at = 0.0

    out = srv._describe_catalog_entry("this_agent_is_made_up_xyz")
    assert out.get("error") == "TOOL_NOT_FOUND", out


# ===========================================================================
# F5 — deterministic fallback judge must not bias toward the filer
# ===========================================================================


def test_f5_fallback_judge_no_caller_side_bonus():
    """Pure test on the deterministic fallback: with zero per-side hits,
    the fallback must default to agent_wins regardless of who filed."""
    from core import judges

    # Caller filed; no hint tokens in the reason at all.
    context_caller_filed = {
        "dispute": {
            "side": "caller",
            "reason": "I am not satisfied with this output.",
            "evidence": "",
        },
        "job": {"output_payload": {"result": "x"}, "error_message": None},
    }
    out = judges._local_dispute_fallback(context_caller_filed)
    assert out["verdict"] == "agent_wins", (
        "Pre-F5 the fallback added +1 to caller_score whenever side='caller'. "
        f"Got verdict={out['verdict']!r} reasoning={out.get('reasoning')!r}"
    )


def test_f5_fallback_judge_caller_signals_still_win_with_evidence():
    """Strong caller-side evidence (e.g. missing_output + agent crash markers)
    must still pass the delta threshold and produce caller_wins."""
    from core import judges

    context = {
        "dispute": {
            "side": "caller",
            "reason": "Output is missing. Agent threw exception. Endpoint timed out.",
            "evidence": "Empty body, server returned a stack trace.",
        },
        "job": {
            "output_payload": None,
            "error_message": "Traceback: TimeoutError raised",
        },
    }
    out = judges._local_dispute_fallback(context)
    assert out["verdict"] == "caller_wins", (
        f"Real caller-side signal must win, got verdict={out['verdict']!r}"
    )


def test_f5_fallback_judge_no_agent_side_bonus():
    """Sanity: removing the bonus must symmetrically not penalize agent
    when the agent filed (e.g. caller's evidence is frivolous)."""
    from core import judges

    context = {
        "dispute": {
            "side": "agent",
            "reason": "This is silly and a frivolous accusation.",
            "evidence": "",
        },
        "job": {"output_payload": {"r": "fine"}, "error_message": None},
    }
    out = judges._local_dispute_fallback(context)
    # _FRIVOLOUS_PHRASES injects "frivolous_dispute" + "accurate_output"
    # into agent_hits, so agent should still win without any side bonus.
    assert out["verdict"] == "agent_wins"


def test_f4_internal_bypass_token_works():
    """allow_pre_terminal_dispute_create lets the internal verification
    flow file a dispute against a non-completed job."""
    path, _teardown = _temp_db_with_disputes_schema()
    try:
        from core import db as _db, disputes

        with _db.get_db_connection() as conn:
            _seed_three_wallets(conn, [
                ("w1", "u-c", 100),
                ("w2", "u-a", 0),
                ("w3", "platform:fees", 0),
            ])
            conn.execute(
                _JOB_INSERT_PENDING_SQL,
                (
                    "j-pre", "a-x", "u-a", "u-c", "w1", "w2", "w3",
                    "pending", 5, 5, "tx-1", "{}",
                    "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z", 3,
                ),
            )

        # Without the bypass, it must fail.
        try:
            disputes.create_dispute(
                job_id="j-pre",
                filed_by_owner_id="u-c",
                side="caller",
                reason="x",
                filing_deposit_cents=25,
            )
            assert False, "should have raised"
        except ValueError:
            pass
        # With the bypass token held, it succeeds.
        token = disputes.allow_pre_terminal_dispute_create()
        try:
            created = disputes.create_dispute(
                job_id="j-pre",
                filed_by_owner_id="u-c",
                side="caller",
                reason="x",
                filing_deposit_cents=25,
            )
            assert created.get("dispute_id")
        finally:
            disputes.reset_pre_terminal_bypass(token)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        _teardown()
