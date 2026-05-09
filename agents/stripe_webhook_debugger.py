"""stripe_webhook_debugger.py — Send real Stripe-signed test events to a webhook endpoint.

# OWNS: constructing valid (and intentionally invalid) Stripe-signed webhook events locally
#        and firing them at a caller-supplied endpoint to surface common handler bugs.
# NOT OWNS: Stripe API communication, billing, or actual Stripe event retrieval.
# INVARIANTS:
#   - No Stripe API key is needed or used; signatures are constructed with HMAC-SHA256
#     exactly as Stripe does, using the caller-supplied webhook_secret.
#   - All outbound HTTP goes through validate_outbound_url; private IPs are blocked unless
#     ALLOW_PRIVATE_OUTBOUND_URLS=1 (which callers MUST set for localhost/staging testing).
#   - webhook_secret MUST start with "whsec_"; the prefix is stripped before signing,
#     matching Stripe's own verification logic.
# DECISIONS:
#   - Only HMAC-SHA256 / v1 scheme is tested — Stripe's current default. v0 (SHA1) is
#     intentionally excluded as it is legacy and disabled by default.
#   - Replay detection is inferred from HTTP status divergence on the second send, not
#     body inspection (bodies are opaque and handler-specific).
#   - timeout_seconds is capped at _MAX_TIMEOUT_SECONDS so a slow handler cannot block
#     the agent indefinitely.

Input:
  endpoint_url     (str, required) — e.g. "http://localhost:3000/webhooks/stripe"
  webhook_secret   (str, required) — signing secret from Stripe dashboard ("whsec_...")
  event_types      (list[str], opt) — which event types to fire; defaults to three common types
  timeout_seconds  (int, opt) — per-request timeout, default 10, max 30

Output (success):
  {endpoint_url, tests_run, passed, failed, results: [...], common_issues_detected, summary}

Output (error):
  {"error": {"code": "stripe_webhook_debugger.<reason>", "message": "..."}}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import requests

from core.url_security import validate_outbound_url
from agents._contracts import agent_error as _err

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_TIMEOUT_SECONDS = 30
_DEFAULT_TIMEOUT_SECONDS = 10
_MAX_EVENT_TYPES = 10

_DEFAULT_EVENT_TYPES = [
    "checkout.session.completed",
    "customer.subscription.updated",
    "invoice.payment_failed",
]

# Minimal but realistic payloads per event type.  The "id" key is injected at
# construction time (_make_event) so each test gets a unique Stripe-style ID.
_EVENT_TEMPLATES: dict[str, dict[str, Any]] = {
    "checkout.session.completed": {
        "type": "checkout.session.completed",
        "api_version": "2023-10-16",
        "data": {"object": {
            "object": "checkout.session",
            "payment_status": "paid",
            "amount_total": 1000,
            "currency": "usd",
            "customer": "cus_test_placeholder",
            "payment_intent": "pi_test_placeholder",
        }},
    },
    "customer.subscription.updated": {
        "type": "customer.subscription.updated",
        "api_version": "2023-10-16",
        "data": {"object": {
            "object": "subscription",
            "status": "active",
            "customer": "cus_test_placeholder",
            "current_period_end": 9999999999,
        }},
    },
    "invoice.payment_failed": {
        "type": "invoice.payment_failed",
        "api_version": "2023-10-16",
        "data": {"object": {
            "object": "invoice",
            "status": "open",
            "amount_due": 2000,
            "currency": "usd",
            "customer": "cus_test_placeholder",
            "subscription": "sub_test_placeholder",
        }},
    },
    "payment_intent.succeeded": {
        "type": "payment_intent.succeeded",
        "api_version": "2023-10-16",
        "data": {"object": {
            "object": "payment_intent",
            "status": "succeeded",
            "amount": 5000,
            "currency": "usd",
        }},
    },
    "payment_intent.payment_failed": {
        "type": "payment_intent.payment_failed",
        "api_version": "2023-10-16",
        "data": {"object": {
            "object": "payment_intent",
            "status": "requires_payment_method",
            "amount": 5000,
            "currency": "usd",
            "last_payment_error": {"message": "Your card was declined."},
        }},
    },
    "customer.created": {
        "type": "customer.created",
        "api_version": "2023-10-16",
        "data": {"object": {
            "object": "customer",
            "email": "test@example.com",
        }},
    },
}


# ---------------------------------------------------------------------------
# Stripe signature helpers
# ---------------------------------------------------------------------------


def _make_event(event_type: str, event_id: str) -> dict[str, Any]:
    """Return a complete Stripe-shaped event dict for *event_type*.

    Falls back to a generic envelope for unknown types so callers can test
    custom or future event types without the agent hard-failing.
    """
    template = _EVENT_TEMPLATES.get(event_type)
    if template:
        event = json.loads(json.dumps(template))  # deep copy via JSON round-trip
    else:
        event = {
            "type": event_type,
            "api_version": "2023-10-16",
            "data": {"object": {"object": "unknown"}},
        }
    event.update({
        "id": f"evt_{event_id}",
        "object": "event",
        "created": int(time.time()),
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": None, "idempotency_key": None},
    })
    return event


def _make_stripe_signature(payload_bytes: bytes, secret: str, timestamp: int) -> str:
    """Construct a Stripe-compatible Stripe-Signature header value.

    Stripe strips the "whsec_" prefix from the signing secret before using it
    as the HMAC key.  The signed payload is ``{timestamp}.{raw_json_body}``.
    """
    signing_secret = secret.removeprefix("whsec_")
    signed_payload = f"{timestamp}.{payload_bytes.decode('utf-8')}"
    mac = hmac.new(
        signing_secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    )
    return f"t={timestamp},v1={mac.hexdigest()}"


def _fire(
    session: requests.Session,
    url: str,
    payload_bytes: bytes,
    signature_header: str,
    timeout: float,
) -> tuple[int | None, int, str | None]:
    """POST one signed webhook payload.

    Returns (http_status_or_None, response_time_ms, error_message_or_None).
    """
    start = time.monotonic()
    try:
        resp = session.post(
            url,
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": signature_header,
                "User-Agent": "Stripe/1.0 (+https://stripe.com/docs/webhooks)",
            },
            timeout=timeout,
            allow_redirects=False,
        )
        return resp.status_code, int((time.monotonic() - start) * 1000), None
    except requests.exceptions.Timeout:
        return None, int(timeout * 1000), f"Request timed out after {timeout}s"
    except requests.RequestException as exc:
        return None, int((time.monotonic() - start) * 1000), str(exc)


# ---------------------------------------------------------------------------
# Per-test helpers (keep run() slim)
# ---------------------------------------------------------------------------



def _result(test_name: str, event_type: str, status: str, http_status: int,
            response_time_ms: int, failure_reason: str, diagnosis: str) -> dict[str, Any]:
    return {
        "test_name": test_name,
        "event_type": event_type,
        "status": status,
        "http_status": http_status,
        "response_time_ms": response_time_ms,
        "failure_reason": failure_reason,
        "diagnosis": diagnosis,
    }


def _test_valid_sig(
    session: requests.Session, url: str, event_type: str,
    payload_bytes: bytes, secret: str, now_ts: int, timeout: float,
) -> tuple[dict[str, Any], int | None, bool]:
    """Run the valid-signature test.  Returns (result_dict, http_status, passed)."""
    sig = _make_stripe_signature(payload_bytes, secret, now_ts)
    status, elapsed_ms, net_err = _fire(session, url, payload_bytes, sig, timeout)
    name = f"{event_type} — valid signature"

    if net_err and status is None:
        r = _result(name, event_type, "error", 0, elapsed_ms, net_err,
                    f"Could not reach the endpoint. Verify it is running and accepts POST. ({net_err})")
        return r, None, False

    if status == 200:
        return _result(name, event_type, "pass", status, elapsed_ms, "", ""), status, True

    # Any non-200 is a failure; build a targeted diagnosis.
    if status == 400:
        diag = ("Handler returned 400 on a correctly signed event. "
                "Signature is verifying OK but the payload shape may not match expectations.")
    elif status in (401, 403):
        diag = (f"Handler returned {status}. The endpoint may require extra auth, "
                "or the webhook_secret does not match the server's configured secret.")
    elif status == 404:
        diag = "Endpoint URL returned 404. Verify the path and that the server listens on this route."
    elif status == 500:
        diag = ("Handler returned 500 — an unhandled exception in your webhook code. "
                "Check server logs; this means a valid Stripe event crashes your handler.")
    elif status is not None and status >= 500:
        diag = (f"Handler returned {status}. A server-side error on a valid event "
                "will cause Stripe to retry, potentially triggering duplicate processing.")
    elif status is not None and status >= 300:
        diag = (f"Handler returned {status} (redirect). "
                "Stripe does not follow redirects — the webhook route must be the final URL.")
    else:
        diag = f"Unexpected status {status} on a valid signed event."

    r = _result(name, event_type, "fail", status or 0, elapsed_ms,
                f"Expected 200, got {status}", diag)
    return r, status, False


def _test_invalid_sig(
    session: requests.Session, url: str, event_type: str,
    payload_bytes: bytes, now_ts: int, timeout: float,
) -> tuple[dict[str, Any], bool | None]:
    """Run the invalid-signature test.  Returns (result_dict, passed_or_None_if_network_error)."""
    bad_sig = _make_stripe_signature(payload_bytes, "whsec_wrongsecretwillnotmatch", now_ts)
    status, elapsed_ms, net_err = _fire(session, url, payload_bytes, bad_sig, timeout)
    name = f"{event_type} — invalid signature"

    if net_err and status is None:
        r = _result(name, event_type, "error", 0, elapsed_ms, net_err,
                    "Network error on invalid-signature probe; cannot assess signature verification.")
        return r, None  # network error — do not count as pass or fail

    if status == 200:
        diag = (f"Handler returned {status} on a deliberately WRONG signature. "
                "This means your endpoint is NOT verifying the Stripe-Signature header. "
                "Anyone who knows your webhook URL can send fake events and trigger "
                "payments, subscription changes, or other critical business logic.")
        r = _result(name, event_type, "fail", status, elapsed_ms,
                    "Handler accepted an invalid Stripe signature", diag)
        return r, False

    if status is not None and 400 <= status < 500:
        return _result(name, event_type, "pass", status, elapsed_ms, "", ""), True

    diag = (f"Handler returned {status} on an invalid signature. "
            "Stripe expects 4xx when verification fails. "
            "A 5xx may mean the handler crashed before reaching signature verification.")
    r = _result(name, event_type, "fail", status or 0, elapsed_ms,
                f"Expected 4xx rejection of invalid signature, got {status}", diag)
    return r, False


def _test_replay(
    session: requests.Session, url: str, event_type: str,
    payload_bytes: bytes, secret: str, now_ts: int, timeout: float,
    first_status: int | None,
) -> tuple[dict[str, Any], bool]:
    """Re-send the identical signed event and check for idempotency via status divergence."""
    replay_sig = _make_stripe_signature(payload_bytes, secret, now_ts)
    replay_status, replay_ms, replay_err = _fire(session, url, payload_bytes, replay_sig, timeout)
    name = f"{event_type} — replay (idempotency)"

    if replay_err and replay_status is None:
        r = _result(name, event_type, "error", 0, replay_ms, replay_err,
                    "Network error on replay probe; cannot assess idempotency.")
        return r, False  # cannot assess — treat as not-passed for summary

    if first_status is not None and replay_status is not None and first_status != replay_status:
        diag = ("Handler returns different HTTP status codes for the same event ID. "
                "Stripe retries on any non-2xx response; a handler that processes on first "
                "delivery but errors on replay will double-process on every retry. "
                "Use the event ID (evt_...) as an idempotency key stored in your database.")
        r = _result(name, event_type, "fail", replay_status, replay_ms,
                    f"First send returned {first_status}, replay returned {replay_status}", diag)
        return r, False

    return _result(name, event_type, "pass", replay_status or 0, replay_ms, "", ""), True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _parse_inputs(payload: dict[str, Any]) -> dict[str, Any] | tuple[list[str], str, int]:
    """Validate and extract run() inputs.  Returns error dict or (event_types, url, timeout)."""
    endpoint_url = str(payload.get("endpoint_url") or "").strip()
    if not endpoint_url:
        return _err("stripe_webhook_debugger.missing_endpoint", "endpoint_url is required.")

    webhook_secret = str(payload.get("webhook_secret") or "").strip()
    if not webhook_secret:
        return _err("stripe_webhook_debugger.missing_secret", "webhook_secret is required.")
    if not webhook_secret.startswith("whsec_"):
        return _err(
            "stripe_webhook_debugger.invalid_secret",
            "webhook_secret must start with 'whsec_' — copy it from the Stripe dashboard "
            "under Webhooks → your endpoint → Signing secret.",
        )

    raw_event_types = payload.get("event_types")
    if raw_event_types is None:
        event_types = list(_DEFAULT_EVENT_TYPES)
    elif not isinstance(raw_event_types, list) or not raw_event_types:
        return _err(
            "stripe_webhook_debugger.invalid_event_types",
            "event_types must be a non-empty list of Stripe event type strings.",
        )
    else:
        event_types = [str(e).strip() for e in raw_event_types if str(e).strip()]
        if len(event_types) > _MAX_EVENT_TYPES:
            return _err(
                "stripe_webhook_debugger.too_many_event_types",
                f"event_types may contain at most {_MAX_EVENT_TYPES} entries.",
            )

    raw_timeout = payload.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = max(1, min(int(raw_timeout), _MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECONDS

    # SSRF guard — validate_outbound_url respects ALLOW_PRIVATE_OUTBOUND_URLS.
    # Private/localhost URLs are often exactly what this agent needs (testing local handlers),
    # so we surface a clear actionable error when the env var is missing.
    try:
        validated_url = validate_outbound_url(endpoint_url, "endpoint_url")
    except ValueError as exc:
        msg = str(exc)
        is_private = "private" in msg.lower() or "localhost" in msg.lower()
        hint = (" Since this agent tests local/staging handlers, set ALLOW_PRIVATE_OUTBOUND_URLS=1 "
                "on the Aztea server to allow private-IP and localhost targets." if is_private else "")
        return _err("stripe_webhook_debugger.invalid_url", msg + hint)

    return event_types, validated_url, webhook_secret, timeout


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Send Stripe-signed test events to a webhook endpoint and diagnose common bugs.

    Required: ``endpoint_url`` (str), ``webhook_secret`` (str, must start with "whsec_").
    Optional: ``event_types`` (list[str]), ``timeout_seconds`` (int, default 10, max 30).

    Returns a structured report: per-test status, HTTP codes, response times, and
    actionable diagnoses for failures.
    """
    parsed = _parse_inputs(payload)
    if isinstance(parsed, dict):  # error envelope from _parse_inputs
        return parsed
    event_types, validated_url, webhook_secret, timeout = parsed

    session = requests.Session()
    results: list[dict[str, Any]] = []
    common_issues: list[str] = []
    seen_issues: set[str] = set()
    passed = 0
    failed = 0

    def _add_issue(issue: str) -> None:
        if issue not in seen_issues:
            seen_issues.add(issue)
            common_issues.append(issue)

    for event_type in event_types:
        event_id = uuid.uuid4().hex[:16]
        event_dict = _make_event(event_type, event_id)
        payload_bytes = json.dumps(event_dict, separators=(",", ":")).encode("utf-8")
        now_ts = int(time.time())

        # Test 1: valid signature
        r_valid, first_status, ok = _test_valid_sig(
            session, validated_url, event_type, payload_bytes, webhook_secret, now_ts, float(timeout)
        )
        results.append(r_valid)
        if ok:
            passed += 1
        else:
            failed += 1
            if first_status == 500:
                _add_issue(f"Endpoint returned 500 on a valid {event_type} event — handler is crashing")
            elif first_status is not None and first_status >= 300:
                _add_issue(f"Endpoint returned non-200 ({first_status}) on a valid event — Stripe will retry indefinitely")
            elif first_status is None:
                _add_issue(f"Endpoint unreachable: {r_valid.get('failure_reason', '')}")

        # Test 2: invalid signature
        r_invalid, invalid_ok = _test_invalid_sig(
            session, validated_url, event_type, payload_bytes, now_ts, float(timeout)
        )
        results.append(r_invalid)
        if invalid_ok is True:
            passed += 1
        elif invalid_ok is False:
            failed += 1
            if r_invalid.get("failure_reason", "").startswith("Handler accepted"):
                _add_issue("Endpoint accepted invalid Stripe signature — signature verification is missing")

        # Test 3: replay / idempotency
        r_replay, replay_ok = _test_replay(
            session, validated_url, event_type, payload_bytes,
            webhook_secret, now_ts, float(timeout), first_status,
        )
        results.append(r_replay)
        if replay_ok:
            passed += 1
        elif r_replay["status"] == "fail":
            failed += 1
            _add_issue("No idempotency handling detected — replayed event produced a different response code")

    session.close()

    tests_run = len(results)
    if failed == 0:
        summary = (
            f"All {passed} tests passed. The webhook handler correctly verifies Stripe "
            "signatures, returns 200 on valid events, and produces consistent responses on replay."
        )
    else:
        issue_fragment = "; ".join(common_issues[:3]) if common_issues else "see results for details"
        summary = f"{passed}/{tests_run} tests passed, {failed} failed. Key issues: {issue_fragment}."

    return {
        "endpoint_url": validated_url,
        "tests_run": tests_run,
        "passed": passed,
        "failed": failed,
        "results": results,
        "common_issues_detected": common_issues,
        "summary": summary,
    }
