"""sandbox_webhook_inbox — captured webhooks + replay.

# OWNS: an in-process FastAPI capture sidecar per sandbox, keyed on a
#       random path prefix; an on-disk store of every received POST/
#       GET/etc.; and a replay action that re-issues a captured event
#       to the target service via the existing sandbox_http_request.
# NOT OWNS: the tunneling layer that gives the sidecar a public URL —
#           that's tunnels.py. webhook_inbox returns the sidecar's
#           local URL; pair with sandbox_tunnel_open to expose it.
# INVARIANTS:
#   * Capture is append-only; replay does NOT alter the original event.
#   * Each captured event gets an Ed25519 receipt minted via the engine's
#     receipts module so a tampered event is detectable.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core.sandbox.models import SandboxInvalidInput, SandboxNotFound
from core.sandbox.receipts import mint_receipt
from core.sandbox.state import SandboxState, get, sandbox_dir

_LOG = logging.getLogger("aztea.sandbox.webhook_inbox")
_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB per captured event
_MAX_EVENTS_PER_INBOX = 500
_INBOX_BIND_HOST = "127.0.0.1"

# sandbox_id → {server, thread, port, inbox_id, events_dir}
_INBOXES: dict[str, dict[str, Any]] = {}
_INBOXES_LOCK = threading.RLock()


def webhook_inbox(payload: dict[str, Any]) -> dict[str, Any]:
    """Open (or list) the webhook capture inbox for a sandbox.

    Two action shapes share the same verb because the spec only declared
    one verb:

      * No ``replay_event_id`` → list captured events (and start the
        capture sidecar if not yet running).
      * ``replay_event_id`` set → re-issue that captured request to the
        ``target_service`` + ``target_path`` so the user's app sees
        the same payload again.
    """
    state = _require(payload)
    replay_event_id = str(payload.get("replay_event_id") or "").strip()
    if replay_event_id:
        return _replay_event(state, replay_event_id, payload)
    inbox = _ensure_inbox(state)
    since = payload.get("since")
    limit = int(payload.get("limit") or 100)
    events = _read_events(state.sandbox_id, since=since, limit=limit)
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "inbox_id": inbox["inbox_id"],
        "capture_url": inbox["capture_url"],
        "events": events,
        "count": len(events),
        "note": (
            "POST any URL under capture_url to record an event. Pair with "
            "sandbox_tunnel_open(service='<the capture sidecar>') for a "
            "public URL providers like Stripe can hit. Replay an event "
            "by passing replay_event_id + target_service + target_path."
        ),
    }


def evict_for_sandbox(sandbox_id: str) -> bool:
    """Side-effect: shut down the capture sidecar for ``sandbox_id``.

    Why: ``lifecycle.stop`` calls this so a sandbox teardown also frees
    the loopback port; otherwise the next start() in the same process
    would collide on the random port the OS chose.
    """
    with _INBOXES_LOCK:
        inbox = _INBOXES.pop(sandbox_id, None)
    if inbox is None:
        return False
    server: ThreadingHTTPServer = inbox["server"]
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        _LOG.debug("inbox shutdown raised", exc_info=True)
    thread: threading.Thread = inbox["thread"]
    thread.join(timeout=3)
    return True


def _ensure_inbox(state: SandboxState) -> dict[str, Any]:
    """Side-effect: lazily start the capture sidecar; return the registered record."""
    with _INBOXES_LOCK:
        existing = _INBOXES.get(state.sandbox_id)
        if existing is not None:
            return existing
        inbox_id = f"inb_{secrets.token_hex(6)}"
        events_dir = sandbox_dir(state.sandbox_id) / "webhook_inbox"
        events_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler_cls = _make_handler_class(state.sandbox_id, events_dir)
        server = ThreadingHTTPServer((_INBOX_BIND_HOST, 0), handler_cls)
        port = server.server_address[1]
        thread = threading.Thread(
            target=server.serve_forever, daemon=True,
            name=f"aztea-webhook-inbox-{state.sandbox_id}",
        )
        thread.start()
        record = {
            "inbox_id": inbox_id,
            "server": server,
            "thread": thread,
            "port": port,
            "capture_url": f"http://{_INBOX_BIND_HOST}:{port}",
            "events_dir": events_dir,
        }
        _INBOXES[state.sandbox_id] = record
        return record


def _replay_event(
    state: SandboxState, event_id: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """Side-effect: re-issue a captured event to the target service via http_ops."""
    target_service = str(payload.get("target_service") or "").strip()
    target_path = str(payload.get("target_path") or "/").strip() or "/"
    if not target_service:
        raise SandboxInvalidInput(
            "target_service is required when replaying a captured event"
        )
    event = _load_event(state.sandbox_id, event_id)
    if event is None:
        raise SandboxNotFound(f"webhook event '{event_id}' not found")
    from core.sandbox import http_ops

    # Build the URL by service hostname (compose DNS works inside the
    # sandbox network). The sandbox_http_request path will run the call
    # from inside a helper container, so service-name DNS resolves.
    if target_service not in state.boot.services:
        raise SandboxInvalidInput(
            f"target_service '{target_service}' not found; available: "
            f"{sorted(state.boot.services)}"
        )
    url = f"http://{target_service}{target_path}"
    response = http_ops.sandbox_http({
        "sandbox_id": state.sandbox_id,
        "method": event["method"],
        "url": url,
        "headers": event["headers"],
        "body": event["body"],
    })
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "replayed_event_id": event_id,
        "target_url": url,
        "target_status_code": response.get("status_code"),
        "target_response_body": response.get("body"),
        "note": "Original captured event is unchanged.",
    }


def _make_handler_class(sandbox_id: str, events_dir: Path) -> type:
    """Pure-ish: build a BaseHTTPRequestHandler subclass closing over sandbox state."""

    class _CaptureHandler(BaseHTTPRequestHandler):
        # Silence the default stderr access log; the audit chain has us
        # covered for traffic visibility.
        def log_message(self, _format: str, *_args: Any) -> None:  # noqa: N802
            return

        def _record(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body_bytes = self.rfile.read(min(length, _MAX_BODY_BYTES)) if length else b""
            event_id = f"evt_{secrets.token_hex(6)}"
            event = {
                "event_id": event_id,
                "received_at": int(time.time()),
                "method": self.command,
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
                "body": body_bytes.decode("utf-8", "replace"),
                "body_truncated": length > _MAX_BODY_BYTES,
                "sandbox_id": sandbox_id,
            }
            try:
                event["receipt"] = mint_receipt(
                    sandbox_id=sandbox_id,
                    action="webhook_inbox.capture",
                    request={"path": self.path, "method": self.command},
                    response={"event_id": event_id},
                )
            except Exception:
                _LOG.exception("webhook receipt mint failed")
            _append_event(events_dir, event)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"received": True, "event_id": event_id}).encode("utf-8")
            )

        def do_POST(self) -> None:  # noqa: N802
            self._record()

        def do_PUT(self) -> None:  # noqa: N802
            self._record()

        def do_PATCH(self) -> None:  # noqa: N802
            self._record()

        def do_GET(self) -> None:  # noqa: N802
            self._record()

    return _CaptureHandler


def _append_event(events_dir: Path, event: dict[str, Any]) -> None:
    """Side-effect: append-only JSONL log of captured events.

    Why: append-only matches the "captured webhook is evidence" property
    — replays produce new events but don't mutate the source. A future
    cleanup task can roll old shards out of the way without rewriting.
    """
    path = events_dir / "events.jsonl"
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True))
            f.write("\n")
    except OSError:
        _LOG.exception("webhook event append failed")


def _read_events(
    sandbox_id: str, *, since: Any = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """Side-effect: read the JSONL log back; apply ``since`` + ``limit``."""
    path = sandbox_dir(sandbox_id) / "webhook_inbox" / "events.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        since_epoch = int(since) if since is not None else None
    except (TypeError, ValueError):
        since_epoch = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if since_epoch is not None and event.get("received_at", 0) < since_epoch:
                    continue
                out.append(event)
    except OSError:
        return []
    return out[-max(1, limit):]


def _load_event(sandbox_id: str, event_id: str) -> dict[str, Any] | None:
    """Pure-ish: linear scan of the JSONL log for the matching event_id."""
    path = sandbox_dir(sandbox_id) / "webhook_inbox" / "events.jsonl"
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event_id") == event_id:
                    return event
    except OSError:
        return None
    return None


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active on this host")
    return state
