"""Watcher.fired delivery — webhook (HMAC-signed) + email.

Best-effort: each delivery channel is attempted independently. A failure on
one channel never aborts the other, and never raises out of ``deliver_run``
— callers (the sweeper) will re-attempt on the next tick if we don't mark
the run finished.

# OWNS: webhook + email payload composition for watcher.fired
# NOT OWNS: HTTP retry/backoff (handled by the sweeper marking the run
#           delivered only on success)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

import requests

from core import email as _email
from core import url_security as _url_security

_LOG = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_SECONDS = 10
WEBHOOK_USER_AGENT = "aztea-watcher-delivery/1.0"


def deliver_run(run: dict, job: dict) -> None:
    """Send watcher.fired notifications. Never raises."""
    try:
        webhook_url = run.get("delivery_webhook_url")
        delivery_email = run.get("delivery_email")
        if webhook_url:
            _deliver_webhook(run, job, webhook_url)
        if delivery_email:
            _deliver_email(run, job, delivery_email)
    except Exception:
        _LOG.exception("Watcher delivery threw unexpectedly.")


def build_payload(run: dict, job: dict) -> dict[str, Any]:
    # delivered_at and nonce give consumers replay protection without forcing
    # them to track per-watcher state. Two replays of the same wire-bytes
    # share the same nonce, so a consumer's idempotency key (run_id + nonce)
    # rejects duplicates; a fresh delivery (e.g. retry after a 5xx) produces
    # a new nonce because we re-call build_payload at delivery time.
    return {
        "event": "watcher.fired",
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "nonce": secrets.token_urlsafe(16),
        "watcher_id": run.get("watcher_id"),
        "run_id": run.get("run_id"),
        "fired_at": run.get("started_at"),
        "fingerprint": run.get("fingerprint"),
        "target_kind": run.get("target_kind"),
        "target_url": run.get("target_url"),
        "job": {
            "job_id": job.get("job_id"),
            "agent_id": job.get("agent_id"),
            "status": job.get("status"),
            "settled_at": job.get("settled_at"),
            "completed_at": job.get("completed_at"),
            "price_cents": job.get("price_cents"),
            "caller_charge_cents": job.get("caller_charge_cents"),
            "output_payload": _safe_json_load(job.get("output_payload"), None),
            "error_message": job.get("error_message"),
        },
    }


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def _deliver_webhook(run: dict, job: dict, webhook_url: str) -> None:
    try:
        safe_url = _url_security.validate_outbound_url(webhook_url, "delivery_webhook_url")
    except ValueError as exc:
        _LOG.warning(
            "Webhook URL failed url_security at delivery time for watcher %s: %s",
            run.get("watcher_id"),
            exc,
        )
        return

    payload = build_payload(run, job)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    secret = run.get("delivery_secret") or ""
    signature = (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if secret
        else ""
    )
    headers = {
        "Content-Type": "application/json",
        "User-Agent": WEBHOOK_USER_AGENT,
        "X-Aztea-Event": "watcher.fired",
        "X-Aztea-Watcher-Id": str(run.get("watcher_id") or ""),
        "X-Aztea-Run-Id": str(run.get("run_id") or ""),
    }
    if signature:
        headers["X-Aztea-Signature"] = signature

    try:
        resp = requests.post(
            safe_url,
            data=body,
            headers=headers,
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        _LOG.warning(
            "Watcher webhook delivery failed for %s: %s",
            run.get("watcher_id"),
            type(exc).__name__,
        )
        raise

    if resp.status_code >= 400:
        _LOG.warning(
            "Watcher webhook %s returned HTTP %s for watcher %s",
            safe_url,
            resp.status_code,
            run.get("watcher_id"),
        )
        # Surface as an exception so the sweeper does NOT mark the run
        # finished_at; we want re-delivery next tick.
        raise RuntimeError(f"webhook HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _deliver_email(run: dict, job: dict, to_email: str) -> None:
    if not _email.ENABLED:
        # Silently no-op when SMTP isn't configured; the run is still
        # marked delivered by the sweeper because there's nothing more
        # we can do without SMTP credentials.
        return
    target_url = run.get("target_url") or "(unknown target)"
    job_status = (job.get("status") or "").strip().lower()
    subject = f"Watcher fired: {target_url[:80]}"
    safe_target = _email._esc(target_url)
    safe_status = _email._esc(job_status or "complete")
    safe_job_id = _email._esc(job.get("job_id") or "")
    safe_run_id = _email._esc(run.get("run_id") or "")
    html_body = (
        f"<p>Your watcher detected a change.</p>"
        f"<p><strong>Target:</strong> <code>{safe_target}</code></p>"
        f"<p><strong>Job status:</strong> {safe_status}</p>"
        f"<p style='color:#666;font-size:0.9em'>Job ID: <code>{safe_job_id}</code><br>"
        f"Run ID: <code>{safe_run_id}</code></p>"
    )
    text_body = (
        f"Your watcher detected a change.\n"
        f"Target: {target_url}\n"
        f"Job status: {job_status}\n"
        f"Job ID: {job.get('job_id') or ''}\n"
        f"Run ID: {run.get('run_id') or ''}\n"
    )
    _email.send(to_email, subject, html_body, text_body)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json_load(blob: Any, default: Any) -> Any:
    if blob is None:
        return default
    if isinstance(blob, (dict, list)):
        return blob
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return default


__all__ = ["build_payload", "deliver_run"]
