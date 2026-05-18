"""sandbox_share — generate a short-lived read-only join link for a sandbox.

# OWNS: minting a signed join token bound to (sandbox_id, access, expiry)
#       and tracking active shares so they can be revoked. Pairs with a
#       small read-only HTTP viewer that lives in the same module — it
#       serves the audit log + receipts + (optionally) a screenshot of
#       the latest browser session.
# NOT OWNS: the edge multiplexer that would make this URL public. For
#           public exposure pair with sandbox_tunnel_open against the
#           share viewer's port. v0 viewer binds to loopback so co-host
#           teammates can join even without a tunnel.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.sandbox.models import SandboxInvalidInput, SandboxNotFound
from core.sandbox.receipts import merkle_root_for, read_audit
from core.sandbox.state import SandboxState, get

_LOG = logging.getLogger("aztea.sandbox.share")
_DEFAULT_TTL_MIN = 30
_HARD_MAX_TTL_MIN = 180
_VALID_ACCESS = ("read", "full")
_VIEWER_BIND_HOST = "127.0.0.1"

# Module-process share registry: share_id → {sandbox_id, access, expires_at,
#   join_secret_hash, actor_hint, viewer_port}
_SHARES: dict[str, dict[str, Any]] = {}
_SHARES_LOCK = threading.RLock()
_VIEWER: dict[str, Any] = {"server": None, "thread": None, "port": None}
_VIEWER_LOCK = threading.RLock()


def share(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate a signed join link granting read-only viewer access.

    Why: the spec's "I'd need to see your face to know if this is right"
    closes with a share primitive. v0 ships a co-host viewer (loopback
    by default) and a tamper-evident join token; combine with
    sandbox_tunnel_open if you need a public URL.
    """
    state = _require(payload)
    access = str(payload.get("access") or "read").strip().lower()
    if access not in _VALID_ACCESS:
        raise SandboxInvalidInput(f"access must be one of {_VALID_ACCESS}")
    if access == "full":
        # ``full`` would require deeper authorisation than the sandbox
        # engine owns today; we don't pretend.
        raise SandboxInvalidInput(
            "access='full' is not implemented in v0 — only 'read' is "
            "safe to grant without the wallet-backed actor table. The "
            "tracking issue covers the full-access surface."
        )
    ttl_minutes = int(payload.get("ttl_minutes") or _DEFAULT_TTL_MIN)
    if not 1 <= ttl_minutes <= _HARD_MAX_TTL_MIN:
        raise SandboxInvalidInput(
            f"ttl_minutes must be in 1..{_HARD_MAX_TTL_MIN}"
        )
    actor_hint = str(payload.get("actor_hint") or "").strip()[:120]
    share_id = f"shr_{secrets.token_hex(6)}"
    join_secret = secrets.token_urlsafe(24)
    join_secret_hash = sha256(join_secret.encode("utf-8")).hexdigest()
    expires_at = int(time.time()) + ttl_minutes * 60
    record = {
        "share_id": share_id,
        "sandbox_id": state.sandbox_id,
        "access": access,
        "expires_at": expires_at,
        "join_secret_hash": join_secret_hash,
        "actor_hint": actor_hint,
        "created_at": int(time.time()),
    }
    with _SHARES_LOCK:
        _SHARES[share_id] = record
    port = _ensure_viewer()
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "share_id": share_id,
        "share_url": (
            f"http://{_VIEWER_BIND_HOST}:{port}/share/{share_id}"
            f"?token={join_secret}"
        ),
        "join_token": join_secret,
        "access": access,
        "expires_at": expires_at,
        "actor_hint": actor_hint or None,
        "note": (
            "Share URL is bound to loopback. To expose externally, pair "
            "with sandbox_tunnel_open(service='aztea-share-viewer'). The "
            "join token is shown ONCE — anyone with it gets read access "
            "until expires_at."
        ),
    }


def revoke(share_id: str) -> bool:
    """Drop a share record so the viewer rejects further requests for it."""
    with _SHARES_LOCK:
        return _SHARES.pop(share_id, None) is not None


def evict_for_sandbox(sandbox_id: str) -> int:
    """Side-effect: drop every share record for ``sandbox_id``.

    Why: ``lifecycle.stop`` calls this so a sandbox teardown also nukes
    its share links — they'd otherwise resolve to a 404 silently.
    """
    with _SHARES_LOCK:
        ids = [sid for sid, r in _SHARES.items() if r.get("sandbox_id") == sandbox_id]
        for sid in ids:
            _SHARES.pop(sid, None)
    return len(ids)


def _ensure_viewer() -> int:
    """Side-effect: lazily start the read-only HTTP viewer; return its port."""
    with _VIEWER_LOCK:
        if _VIEWER["server"] is not None:
            return _VIEWER["port"]
        server = ThreadingHTTPServer((_VIEWER_BIND_HOST, 0), _ShareViewerHandler)
        port = server.server_address[1]
        thread = threading.Thread(
            target=server.serve_forever, daemon=True,
            name="aztea-share-viewer",
        )
        thread.start()
        _VIEWER["server"] = server
        _VIEWER["thread"] = thread
        _VIEWER["port"] = port
        return port


class _ShareViewerHandler(BaseHTTPRequestHandler):
    """Pure-ish: a tiny HTTP server that serves audit logs gated by share token."""

    def log_message(self, _format: str, *_args: Any) -> None:  # noqa: N802
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "share":
            self._reply(404, {"error": "not_found"})
            return
        share_id = parts[1]
        query = parse_qs(parsed.query or "")
        token = (query.get("token") or [""])[0]
        record = _resolve_share(share_id, token)
        if record is None:
            self._reply(401, {"error": "unauthorized"})
            return
        sandbox_id = record["sandbox_id"]
        body = {
            "sandbox_id": sandbox_id,
            "share_id": share_id,
            "access": record["access"],
            "expires_at": record["expires_at"],
            "audit_merkle_root": merkle_root_for(sandbox_id),
            "audit": read_audit(sandbox_id, limit=200),
        }
        self._reply(200, body)

    def _reply(self, code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _resolve_share(share_id: str, token: str) -> dict[str, Any] | None:
    """Pure-ish: validate (share_id, token) against the registry, honour expiry."""
    with _SHARES_LOCK:
        record = _SHARES.get(share_id)
        if record is None:
            return None
        if int(time.time()) >= int(record.get("expires_at") or 0):
            _SHARES.pop(share_id, None)
            return None
        expected_hash = sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected_hash, record["join_secret_hash"]):
            return None
        return dict(record)


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active on this host")
    return state
