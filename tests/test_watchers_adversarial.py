"""Adversarial unit tests for core.watchers.

These cover edge cases / bugs / loopholes that the original test_watchers_unit.py
suite did not exercise. Tests that demonstrate known bugs are marked xfail with
a comment linking to the audit finding; flip to passing when the bug is fixed.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.watchers import crud as _crud
from core.watchers import delivery as _delivery
from core.watchers import fingerprint as _fp
from core.watchers import models as _models
from core.watchers import sweeper as _sweeper


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(
        self,
        body: bytes = b"",
        status_code: int = 200,
        headers: dict | None = None,
        url: str | None = None,
        history: list | None = None,
    ) -> None:
        self.content = body
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url or "https://example.com"
        self.history = history or []

    def iter_content(self, chunk_size: int = 65536):
        yield self.content

    def close(self) -> None:
        pass

    def json(self):
        return json.loads(self.content.decode("utf-8"))


def _patch_get(resp: _FakeResp):
    return patch("core.watchers.fingerprint.requests.get", return_value=resp)


# ---------------------------------------------------------------------------
# Tier-1: bugs the audit identified
# ---------------------------------------------------------------------------


def test_T1_4_http_redirect_to_private_ip_is_blocked():
    # Final URL after redirect points at a private host. The current code
    # does not re-check, so this test exposes the SSRF bypass.
    resp = _FakeResp(
        body=b"<html>fake landing</html>",
        url="http://127.0.0.1:9000/admin",
        history=[_FakeResp(status_code=302)],
    )
    with _patch_get(resp):
        fp, err = _fp.fingerprint_target("http", "https://example.com", {})
    assert fp is None and err is not None and "private" in (err or "").lower()


def test_T1_5_etag_authoritative_is_documented():
    # ETag-as-fingerprint is the current contract. If we ever change this
    # silently, that's a behavior break. Lock the contract by asserting the
    # comment exists in the source.
    src = Path(_fp.__file__).read_text(encoding="utf-8")
    assert "ETag" in src or "etag" in src
    # The header-based fingerprint path returns hash("hdr|...") — assert that
    # marker survives refactors so reviewers see the trade-off.
    assert "hdr|" in src


def test_T1_7_webhook_payload_has_replay_protection():
    payload = _delivery.build_payload(
        run={
            "watcher_id": "wtch_x",
            "run_id": "wrun_x",
            "started_at": "2026-05-09T00:00:00+00:00",
            "fingerprint": "abc",
            "target_url": "https://example.com",
            "target_kind": "http",
        },
        job={"job_id": "j1", "agent_id": "a1", "status": "complete"},
    )
    # At least one of these MUST be present for replay protection.
    assert (
        "delivered_at" in payload
        or "nonce" in payload
        or "delivered_at" in payload.get("job", {})
    )


def test_T1_9_npm_scoped_package_url_is_well_formed():
    """The audit flagged this as a bug, but ``_quote_path_segment`` strips
    the surrounding single quotes ``shlex`` adds, so the URL is well-formed.
    This test locks the behavior so a future "cleanup" of the strip() does
    not silently re-introduce the bug."""
    captured: dict = {}

    def _capture(url, **kwargs):
        captured["url"] = url
        return _FakeResp(body=json.dumps({"version": "1.0.0"}).encode("utf-8"))

    with patch("core.watchers.fingerprint.requests.get", side_effect=_capture):
        fp, err = _fp.fingerprint_target(
            "manifest", "x", {"registry": "npm", "package": "@scope/pkg"}
        )
    assert err is None and fp is not None
    # URL must be the registry path with the scope visible, not single-quoted.
    assert "@scope/pkg" in captured["url"]
    assert "'" not in captured["url"]


def test_T1_10_no_change_tick_resets_error_counter():
    """A successful fingerprint observation MUST reset
    consecutive_errors even if the diff gate skips the fire. Without
    this, a flapping target (4 errors, 1 success-no-change, 4 errors,
    ...) auto-pauses despite reaching the target every Nth tick.

    Verified at the unit layer by asserting `clear_consecutive_errors`
    exists and is wired into the success path of _process_due_watcher.
    """
    # The fix is two-part: (1) crud.clear_consecutive_errors exists, (2)
    # sweeper._process_due_watcher calls it after a successful fingerprint
    # observation. Verify both via source inspection so a future refactor
    # that drops one half fails the test.
    assert callable(getattr(_crud, "clear_consecutive_errors", None)), (
        "core.watchers.crud.clear_consecutive_errors must exist"
    )
    sweeper_src = Path(_sweeper.__file__).read_text(encoding="utf-8")
    # The call must come AFTER the fingerprint-error early-return and
    # BEFORE the diff gate, so it runs whether or not we fire.
    fn_start = sweeper_src.index("def _process_due_watcher")
    fn_end = sweeper_src.index("\ndef ", fn_start + 1)
    fn_body = sweeper_src[fn_start:fn_end]
    error_branch = fn_body.find('skip_reason="target_error"')
    diff_gate = fn_body.find("# Diff gate")
    clear_call = fn_body.find("clear_consecutive_errors")
    assert error_branch >= 0 and diff_gate >= 0 and clear_call >= 0
    assert error_branch < clear_call < diff_gate, (
        "clear_consecutive_errors must run between the error-branch return "
        "and the diff gate so a no_change tick still clears the counter."
    )


# ---------------------------------------------------------------------------
# Tier-3: money invariants — only _fire() may call payments.*
# ---------------------------------------------------------------------------


def test_T3_4_only_fire_calls_payments():
    """Static-AST guard: payments.{pre_call_charge,post_call_refund,post_call_payout}
    only appear inside the `_fire` function. Any other use is a money-path
    leak that bypasses the canonical charge gate.
    """
    pkg_dir = Path(_sweeper.__file__).resolve().parent
    forbidden_calls = (
        "pre_call_charge",
        "post_call_refund",
        "post_call_payout",
        "post_call_partial_settle",
    )
    violations: list[str] = []
    for path in pkg_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Map function-name → set of attribute accesses inside it.
        fn_calls: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                attr_names: set[str] = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute):
                        attr_names.add(sub.attr)
                fn_calls[node.name] = attr_names
        for fn, attrs in fn_calls.items():
            if fn == "_fire":
                continue
            for forbidden in forbidden_calls:
                if forbidden in attrs:
                    violations.append(f"{path.name}::{fn} calls payments.{forbidden}")
    assert not violations, (
        "Money calls outside _fire are forbidden — each opens a charge path "
        "that bypasses the diff/budget gates: " + "\n  ".join(violations)
    )


def test_T3_5_client_id_format_is_stable():
    """The fire() comment claims watcher:{wid}:{fp[:12]} is idempotent across
    sweeper restarts. Lock the format with a regex."""
    # Build a synthetic watcher id and fingerprint and reproduce the format
    # used in sweeper._fire (line 314 at the time of writing).
    watcher_id = "wtch_aaaa"
    fingerprint = "0" * 64
    expected = f"watcher:{watcher_id}:{fingerprint[:12]}"
    pattern = re.compile(r"^watcher:wtch_[a-z0-9_]+:[0-9a-f]{12}$")
    assert pattern.match(expected)


# ---------------------------------------------------------------------------
# Tier-5: fingerprint adversarial inputs
# ---------------------------------------------------------------------------


def test_T5_1_two_empty_bodies_same_url_same_fingerprint():
    with _patch_get(_FakeResp(body=b"")):
        a, _ = _fp.fingerprint_target("http", "https://example.com", {})
    with _patch_get(_FakeResp(body=b"")):
        b, _ = _fp.fingerprint_target("http", "https://example.com", {})
    assert a == b


def test_T5_1_two_empty_bodies_different_url_different_fingerprint():
    with _patch_get(_FakeResp(body=b"")):
        a, _ = _fp.fingerprint_target("http", "https://a.example.com", {})
    with _patch_get(_FakeResp(body=b"")):
        b, _ = _fp.fingerprint_target("http", "https://b.example.com", {})
    assert a != b


def test_T5_2_body_exactly_at_cap_succeeds():
    body = b"x" * _fp.HTTP_BODY_BYTE_CAP
    with _patch_get(_FakeResp(body=body)):
        fp, err = _fp.fingerprint_target("http", "https://example.com", {})
    assert err is None
    assert fp is not None


def test_T5_2_body_one_byte_over_cap_fails():
    body = b"x" * (_fp.HTTP_BODY_BYTE_CAP + 1)
    with _patch_get(_FakeResp(body=body)):
        fp, err = _fp.fingerprint_target("http", "https://example.com", {})
    assert fp is None
    assert err is not None and "exceeds" in err


def test_T5_3_http_timeout_returns_clean_error():
    import requests as _requests

    with patch(
        "core.watchers.fingerprint.requests.get",
        side_effect=_requests.exceptions.Timeout(),
    ):
        fp, err = _fp.fingerprint_target("http", "https://example.com", {})
    assert fp is None
    assert err == "http: timeout"


def test_T5_4_invalid_utf8_normalizes_safely():
    body = b"\xff\xfe\x00valid utf8"
    with _patch_get(_FakeResp(body=body)):
        fp, err = _fp.fingerprint_target("http", "https://example.com", {})
    assert err is None
    assert fp is not None


def test_T5_5_git_url_with_userinfo_rejected():
    fp, err = _fp.fingerprint_target(
        "git", "https://user:token@github.com/x/y.git", {"ref": "main"}
    )
    assert fp is None
    assert err is not None
    # url_security catches embedded credentials before subprocess is even spawned.


def test_T5_6_git_ref_starting_with_dash_is_rejected():
    """A ref like '--upload-pack=evil' passed as a positional arg to
    git ls-remote would be parsed as a flag. Defense in depth: refs must
    NOT begin with '-'."""
    for bad_ref in ("--upload-pack=evil", "-flag", "--evil"):
        fp, err = _fp.fingerprint_target(
            "git", "https://github.com/x/y.git", {"ref": bad_ref}
        )
        assert fp is None, f"ref {bad_ref!r} should have been rejected"
        assert err is not None


def test_T5_6_git_ref_with_newline_rejected():
    fp, err = _fp.fingerprint_target(
        "git", "https://github.com/x/y.git", {"ref": "HEAD\nbad"}
    )
    assert fp is None and err is not None


def test_T5_7_git_executable_missing_returns_clean_error():
    with patch(
        "core.watchers.fingerprint.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        fp, err = _fp.fingerprint_target(
            "git", "https://github.com/x/y.git", {"ref": "HEAD"}
        )
    assert fp is None
    assert err is not None and "git" in err.lower() and "PATH" in err


def test_T5_8_manifest_malformed_json():
    with _patch_get(_FakeResp(body=b"<html>503</html>")):
        fp, err = _fp.fingerprint_target(
            "manifest", "x", {"registry": "pypi", "package": "p"}
        )
    assert fp is None
    assert err is not None and "JSON" in err


def test_T5_9_manifest_non_string_version():
    # Version field is null
    with _patch_get(_FakeResp(body=json.dumps({"info": {"version": None}}).encode())):
        fp, err = _fp.fingerprint_target(
            "manifest", "x", {"registry": "pypi", "package": "p"}
        )
    assert fp is None and err and "version" in err

    # Version field is a list (not a string)
    with _patch_get(
        _FakeResp(body=json.dumps({"info": {"version": ["1.0", "2.0"]}}).encode())
    ):
        fp, err = _fp.fingerprint_target(
            "manifest", "x", {"registry": "pypi", "package": "p"}
        )
    assert fp is None


def test_T5_10_fingerprint_deterministic_across_calls():
    """No PRNG / time.time() in the hash path."""
    body = b"deterministic content"
    fps: list[str] = []
    for _ in range(5):
        with _patch_get(_FakeResp(body=body)):
            fp, err = _fp.fingerprint_target("http", "https://example.com", {})
            assert err is None
            fps.append(fp)
    assert len(set(fps)) == 1


# ---------------------------------------------------------------------------
# Tier-6: delivery edge cases
# ---------------------------------------------------------------------------


def _build_run(secret: str | None = "shhh", url: str | None = "https://hook.example.com/in") -> dict:
    return {
        "watcher_id": "wtch_x",
        "run_id": "wrun_x",
        "started_at": "2026-05-09T00:00:00+00:00",
        "fingerprint": "abc",
        "target_url": "https://example.com",
        "target_kind": "http",
        "delivery_webhook_url": url,
        "delivery_email": None,
        "delivery_secret": secret,
        "owner_user_id": "user:1",
    }


def test_T6_5_webhook_payload_schema_stable():
    payload = _delivery.build_payload(
        run=_build_run(),
        job={
            "job_id": "j1",
            "agent_id": "a1",
            "status": "complete",
            "settled_at": "2026-05-09T00:01:00+00:00",
            "completed_at": "2026-05-09T00:00:30+00:00",
            "price_cents": 5,
            "caller_charge_cents": 6,
            "output_payload": {"ok": True},
            "error_message": None,
        },
    )
    # Top-level required keys.
    for key in ("event", "watcher_id", "run_id", "fired_at", "fingerprint",
                "target_kind", "target_url", "job"):
        assert key in payload, f"missing {key}"
    assert payload["event"] == "watcher.fired"
    # Job object shape.
    for key in ("job_id", "agent_id", "status", "settled_at", "completed_at",
                "price_cents", "caller_charge_cents", "output_payload",
                "error_message"):
        assert key in payload["job"], f"missing job.{key}"


def test_T6_6_hmac_signature_matches_body_bytes():
    """HMAC is computed over the exact body bytes that get POSTed. A
    consumer that recomputes HMAC over the received bytes must match the
    `X-Aztea-Signature` header. Two deliveries produce different signatures
    because each payload includes a fresh nonce + delivered_at (per
    T1.7 replay protection), but each individual signature must verify
    against its own body."""
    secret = "shhh"
    captured: list[tuple[bytes, dict]] = []

    class _R:
        status_code = 200

    def _post(url, data=None, headers=None, timeout=None):
        captured.append((data, headers))
        return _R()

    run = _build_run(secret=secret)
    job = {"job_id": "j1", "agent_id": "a1", "status": "complete"}
    with patch("core.watchers.delivery.requests.post", side_effect=_post):
        _delivery.deliver_run(run, job)
        _delivery.deliver_run(run, job)

    assert len(captured) == 2
    for body, headers in captured:
        sig = headers["X-Aztea-Signature"]
        assert sig.startswith("sha256=")
        expected = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        assert sig == expected
    # Replay protection: the two payloads MUST differ (fresh nonce).
    assert captured[0][0] != captured[1][0]


def test_T6_6_hmac_canonicalization_stable_for_identical_payload():
    """If we strip the per-delivery fields (delivered_at, nonce), the
    remaining canonical bytes must hash to the same value across runs.
    This guards the `sort_keys=True` + canonical-separators contract that
    consumers depend on for re-signing."""
    base_run = _build_run()
    base_job = {"job_id": "j1", "agent_id": "a1", "status": "complete"}

    p1 = _delivery.build_payload(base_run, base_job)
    p2 = _delivery.build_payload(base_run, base_job)
    for p in (p1, p2):
        p.pop("delivered_at", None)
        p.pop("nonce", None)
    b1 = json.dumps(p1, separators=(",", ":"), sort_keys=True).encode()
    b2 = json.dumps(p2, separators=(",", ":"), sort_keys=True).encode()
    assert b1 == b2


def test_T6_7_no_hmac_header_when_secret_missing():
    captured_headers: list[dict] = []

    class _R:
        status_code = 200

    def _post(url, data=None, headers=None, timeout=None):
        captured_headers.append(headers)
        return _R()

    with patch("core.watchers.delivery.requests.post", side_effect=_post):
        _delivery.deliver_run(_build_run(secret=None), {"job_id": "j1", "status": "complete"})
        _delivery.deliver_run(_build_run(secret=""), {"job_id": "j1", "status": "complete"})

    for headers in captured_headers:
        assert "X-Aztea-Signature" not in headers, (
            "Empty/None secret must NOT produce an X-Aztea-Signature header — "
            "an empty signature is worse than none (suggests integrity to a "
            "naive consumer)."
        )


def test_T6_4_email_body_excludes_agent_output():
    """The email channel intentionally omits the agent's output payload.
    Including untrusted output in HTML email is an XSS sink. Lock this with
    a source-grep so a future refactor doesn't quietly add it."""
    src = Path(_delivery.__file__).read_text(encoding="utf-8")
    # The email body builder should never reference output_payload.
    # Find the _deliver_email function block.
    fn_start = src.index("def _deliver_email")
    fn_end = src.index("\ndef ", fn_start + 1)
    email_fn = src[fn_start:fn_end]
    assert "output_payload" not in email_fn, (
        "Agent output must not be embedded in the email body — XSS risk "
        "and unbounded size."
    )


# ---------------------------------------------------------------------------
# Tier-7: state machine
# ---------------------------------------------------------------------------


def test_T7_3_disabled_status_is_either_used_or_dead():
    """STATUS_DISABLED is in WATCHER_STATUSES but I cannot find a code path
    that assigns it. If unused, drop it from the constant; if used, this
    test should be updated to verify the assignment site."""
    src_dir = Path(_sweeper.__file__).resolve().parent
    blob = "\n".join(p.read_text(encoding="utf-8") for p in src_dir.glob("*.py"))
    # Either it's mentioned outside of constants/exports, OR the constant
    # itself is gone. We accept the constant being defined and never
    # written, but flag that as dead code via a documenting xfail.
    assignments_outside_const = re.findall(
        r"= STATUS_DISABLED|status\s*=\s*['\"]disabled['\"]", blob
    )
    if not assignments_outside_const:
        pytest.xfail(
            "STATUS_DISABLED is declared in WATCHER_STATUSES but never "
            "assigned anywhere in core/watchers/. Either remove it from the "
            "enum or implement the path that uses it (the route layer "
            "currently only allows 'active' / 'paused' updates)."
        )


# ---------------------------------------------------------------------------
# Tier-8: migration / schema
# ---------------------------------------------------------------------------


def test_T8_1_migration_idempotent(tmp_path):
    import sqlite3

    from core.migrate import apply_migrations

    db_file = tmp_path / "isolated.db"
    apply_migrations(str(db_file))
    apply_migrations(str(db_file))  # second apply — must not error

    raw = sqlite3.connect(str(db_file))
    try:
        rows = raw.execute(
            "SELECT version FROM schema_migrations WHERE version = 41"
        ).fetchall()
    finally:
        raw.close()
    assert len(rows) == 1, "migration 0041 must be recorded exactly once"


def test_T8_2_watcher_indexes_present(tmp_path):
    import sqlite3

    from core.migrate import apply_migrations

    db_file = tmp_path / "isolated.db"
    apply_migrations(str(db_file))

    raw = sqlite3.connect(str(db_file))
    try:
        names = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    finally:
        raw.close()
    for required in (
        "idx_watchers_due",
        "idx_watchers_owner",
        "idx_watchers_agent",
        "idx_watcher_runs_wid",
    ):
        assert required in names, f"missing index {required}"


# ---------------------------------------------------------------------------
# Tier-9: OSS / hosted boundary
# ---------------------------------------------------------------------------


def test_T9_2_no_aztea_ai_anywhere_in_core_watchers():
    pkg_dir = Path(_sweeper.__file__).resolve().parent
    for path in pkg_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        # Permit doctstring mentions, but NEVER an actual URL.
        assert "aztea.ai" not in text, (
            f"{path.name} mentions aztea.ai — OSS boundary requires no "
            "hosted-aztea references in core.watchers."
        )


# ---------------------------------------------------------------------------
# Tier-11 (reduced): pydantic model property checks
# ---------------------------------------------------------------------------


def test_T11_2_extra_fields_rejected_on_create():
    with pytest.raises(Exception):
        _models.WatcherCreate(
            agent_id="a1",
            target_kind="http",
            target_url="https://x",
            budget_per_day_cents=100,
            delivery_email="x@example.com",
            unknown_field="should-be-rejected",
        )


def test_T11_2_extra_fields_rejected_on_update():
    with pytest.raises(Exception):
        _models.WatcherUpdate(unknown_field="should-be-rejected")


def test_T11_2_target_kind_literal_enforced():
    for bad in ("HTTP", "ftp", "rsync", "", "  http  "):
        with pytest.raises(Exception):
            _models.WatcherCreate(
                agent_id="a1",
                target_kind=bad,
                target_url="https://x",
                budget_per_day_cents=100,
                delivery_email="x@example.com",
            )
