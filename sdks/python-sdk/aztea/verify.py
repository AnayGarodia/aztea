"""Verify Aztea-signed inbound requests to your agent endpoint.

When you register an HTTP-endpoint agent on Aztea, registration returns an
``endpoint_signing_secret`` exactly once. Every subsequent call Aztea routes
to your endpoint carries:

  X-Aztea-Signature: sha256=<hex HMAC-SHA256 of "timestamp.body" with your secret>
  X-Aztea-Timestamp: <ISO-8601 UTC, e.g. 2026-05-27T20:15:30Z>
  X-Aztea-Job-Id:    <opaque job id, useful for log correlation>
  X-Aztea-Caller:    <buyer's owner_id, "anonymous" for unauthenticated callers>

Your endpoint MUST verify the signature before running any agent code.
Without verification, anyone who learns your URL can call you directly and
bypass Aztea billing.

Usage (FastAPI):

    from fastapi import FastAPI, Request, HTTPException
    from aztea.verify import verify_request, InvalidSignature

    SECRET = os.environ["AZTEA_ENDPOINT_SIGNING_SECRET"]
    app = FastAPI()

    @app.post("/run")
    async def run(request: Request):
        body = await request.body()
        try:
            verify_request(
                body=body,
                signature=request.headers.get("x-aztea-signature", ""),
                timestamp=request.headers.get("x-aztea-timestamp", ""),
                secret=SECRET,
            )
        except InvalidSignature as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        payload = json.loads(body)
        # ... run your agent here ...
        return {"output": ...}

If the signature is missing, malformed, the HMAC mismatches, or the timestamp
is outside the 5-minute replay window, :func:`verify_request` raises
:class:`InvalidSignature`. The helper does not raise on any other error path.

Pure module: no I/O, no global state. Safe to import in any context.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone


class InvalidSignature(Exception):
    """Raised when an inbound request's signature, secret, or timestamp fails verification."""


# Default replay window. Five minutes is the same value Aztea's server uses
# in ``core/crypto.ENDPOINT_SIGNATURE_MAX_AGE_SECONDS``. Override per-call if
# your environment has unusually large clock skew, but never widen past 15
# minutes — beyond that a captured signature becomes a real replay risk.
DEFAULT_MAX_AGE_SECONDS = 300


def verify_request(
    body: bytes,
    signature: str,
    timestamp: str,
    secret: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now_epoch: float | None = None,
) -> None:
    """Raise :class:`InvalidSignature` unless the inbound request is authentic.

    Arguments:
      body: the raw request bytes (NOT a parsed dict — must match what Aztea
            sent on the wire, which is canonical JSON: sorted keys, compact
            separators, UTF-8 encoded).
      signature: the value of the ``X-Aztea-Signature`` header, e.g.
                 ``"sha256=abcdef..."``.
      timestamp: the value of the ``X-Aztea-Timestamp`` header, ISO-8601 UTC.
      secret:    the per-agent secret Aztea gave you at registration.
      max_age_seconds: replay window (default 300s = 5 minutes). Reject
                      requests whose timestamp is older than this.
      now_epoch: override "now" for testing. Defaults to ``time.time()``.

    Constant-time HMAC compare prevents timing oracles. Stale-timestamp check
    runs before HMAC math so an attacker can't probe latency for valid
    secret bytes.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise InvalidSignature("body must be bytes")
    if not isinstance(signature, str) or not signature.startswith("sha256="):
        raise InvalidSignature("signature header missing or wrong format")
    if not isinstance(timestamp, str) or not timestamp:
        raise InvalidSignature("timestamp header missing")
    if not isinstance(secret, str) or not secret:
        raise InvalidSignature("secret must be a non-empty string")

    try:
        ts_epoch = _parse_iso_or_epoch(timestamp)
    except ValueError as exc:
        raise InvalidSignature(f"timestamp not parseable: {exc}") from exc

    current = now_epoch if now_epoch is not None else time.time()
    if abs(current - ts_epoch) > max_age_seconds:
        raise InvalidSignature(
            f"timestamp outside +/-{max_age_seconds}s replay window"
        )

    signed_string = timestamp.encode("ascii") + b"." + bytes(body)
    expected_mac = hmac.new(
        secret.encode("utf-8"), signed_string, hashlib.sha256,
    ).hexdigest()
    expected = f"sha256={expected_mac}"
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignature("HMAC mismatch")


def _parse_iso_or_epoch(timestamp: str) -> float:
    """Pure: accept ISO-8601 (``"2026-05-27T20:15:30Z"``) or epoch seconds.

    Aztea always sends ISO. The epoch fallback is defence-in-depth for buggy
    proxies or downstream services that re-stamp.
    """
    stripped = timestamp.strip()
    if not stripped:
        raise ValueError("empty timestamp")
    # 2026-05-27: reject NaN/Inf so a malformed-timestamp probe can't
    # silently bypass the staleness window via float("nan").
    try:
        candidate = float(stripped)
    except ValueError:
        pass
    else:
        import math
        if not math.isfinite(candidate):
            raise ValueError("non-finite timestamp")
        return candidate
    try:
        normalized = stripped[:-1] + "+00:00" if stripped.endswith("Z") else stripped
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(str(exc))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


__all__ = ["InvalidSignature", "verify_request", "DEFAULT_MAX_AGE_SECONDS"]
