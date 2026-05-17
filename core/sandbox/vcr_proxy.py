"""HTTP recorder/replayer proxy that actually intercepts sandbox traffic.

# OWNS: an HTTP/HTTPS-CONNECT proxy per sandbox that:
#         * In record mode → forwards to upstream + writes the interaction
#           into the sandbox's cassette via vcr.vcr_append().
#         * In replay mode → looks up the matching cassette interaction
#           via vcr.vcr_replay_lookup() and serves the recorded response
#           without touching the network.
#         * In off mode → straight pass-through.
# NOT OWNS: HTTPS MITM. Replay of HTTPS works fine when the sandbox uses
#           HTTP_PROXY (curl / requests / httpx all honour it). True
#           HTTPS interception would need our own CA + cert injection;
#           that's tracked as a separate follow-up.
# INVARIANTS:
#   * Proxy is loopback-bound; sandbox containers reach it via the host
#     gateway (host.docker.internal on macOS/Windows; the bridge gateway
#     on Linux) — the start payload sets HTTP_PROXY/HTTPS_PROXY env.
#   * Replay misses return 502 with a clear envelope so deterministic
#     tests fail loudly instead of silently passing through.
"""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from core.sandbox import vcr
from core.sandbox.models import SandboxInvalidInput, SandboxNotFound
from core.sandbox.state import SandboxState, get

_LOG = logging.getLogger("aztea.sandbox.vcr_proxy")
_DEFAULT_BIND = "127.0.0.1"
_FORWARD_TIMEOUT_S = 30

# sandbox_id → {"server": ThreadingHTTPServer, "thread": Thread, "port": int}
_PROXIES: dict[str, dict[str, Any]] = {}
_PROXIES_LOCK = threading.RLock()


def ensure_proxy(sandbox_id: str) -> dict[str, Any]:
    """Lazily start the proxy for ``sandbox_id``; return the running record.

    Why: ``sandbox_outbound_record`` / ``_replay`` keep their old shape
    (they flip the mode file on disk); this proxy is what actually
    consults that file at request time. Operators wire it up by passing
    ``HTTP_PROXY=http://<host>:<port>`` to compose at boot time.
    """
    with _PROXIES_LOCK:
        existing = _PROXIES.get(sandbox_id)
        if existing is not None:
            return _shape(existing)
        state = get(sandbox_id)
        if state is None:
            raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active on this host")
        handler_cls = _make_handler(sandbox_id)
        server = ThreadingHTTPServer((_DEFAULT_BIND, 0), handler_cls)
        port = server.server_address[1]
        thread = threading.Thread(
            target=server.serve_forever, daemon=True,
            name=f"aztea-vcr-proxy-{sandbox_id}",
        )
        thread.start()
        record = {
            "sandbox_id": sandbox_id,
            "server": server,
            "thread": thread,
            "port": port,
        }
        _PROXIES[sandbox_id] = record
        state.touch()
        return _shape(record)


def evict_for_sandbox(sandbox_id: str) -> bool:
    """Side-effect: shut down the VCR proxy for ``sandbox_id``."""
    with _PROXIES_LOCK:
        record = _PROXIES.pop(sandbox_id, None)
    if record is None:
        return False
    server: ThreadingHTTPServer = record["server"]
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        _LOG.debug("vcr proxy shutdown raised", exc_info=True)
    thread: threading.Thread = record["thread"]
    thread.join(timeout=3)
    return True


def env_for_compose(sandbox_id: str) -> dict[str, str]:
    """Return ``HTTP_PROXY``/``HTTPS_PROXY`` env vars pointing at the proxy.

    Why: callers paste these into ``boot.env.vars`` so compose
    containers honour them at start time. Compose passes them straight
    through to the running services.
    """
    record = _PROXIES.get(sandbox_id)
    if record is None:
        return {}
    # On macOS / Windows Docker Desktop, containers reach the host via
    # ``host.docker.internal``. On native Linux, the bridge gateway is
    # ``172.17.0.1`` by default but compose can vary; the caller can
    # override with ``AZTEA_VCR_PROXY_HOST``.
    import os

    host = os.environ.get("AZTEA_VCR_PROXY_HOST") or "host.docker.internal"
    url = f"http://{host}:{record['port']}"
    return {"HTTP_PROXY": url, "HTTPS_PROXY": url}


def proxy_status(sandbox_id: str) -> dict[str, Any]:
    """Pure-ish: current proxy state + mode so callers can audit."""
    state = get(sandbox_id)
    if state is None:
        raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active on this host")
    record = _PROXIES.get(sandbox_id)
    return {
        "sandbox_id": sandbox_id,
        "proxy_running": record is not None,
        "proxy_url": _shape(record)["proxy_url"] if record else None,
        "current_mode": vcr.vcr_mode(sandbox_id),
    }


def _shape(record: dict[str, Any]) -> dict[str, Any]:
    """Pure: serialisable view of a proxy record."""
    return {
        "sandbox_id": record["sandbox_id"],
        "port": record["port"],
        "proxy_url": f"http://{_DEFAULT_BIND}:{record['port']}",
    }


def _make_handler(sandbox_id: str) -> type:
    """Pure-ish: build a handler class closed over ``sandbox_id``.

    Why: each proxy server knows which sandbox it belongs to so the
    cassette read/write goes to the right state directory.
    """

    class _VCRProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:  # noqa: N802
            return

        def _handle(self) -> None:
            mode_info = vcr.vcr_mode(sandbox_id)
            mode = str(mode_info.get("mode") or "off").lower()
            cassette = str(mode_info.get("cassette") or "default")
            length = int(self.headers.get("Content-Length") or 0)
            request_body = self.rfile.read(length) if length else b""
            request_url = self.path  # proxy form: absolute URI from client
            request_headers = _copy_headers(self.headers)
            if mode == "replay":
                self._serve_replay(sandbox_id, cassette, request_url, request_body)
                return
            if mode == "record":
                self._record_and_forward(
                    sandbox_id, cassette, request_url, request_headers, request_body,
                )
                return
            # off / unknown → pass-through (no cassette write)
            self._forward(request_url, request_headers, request_body)

        def _serve_replay(
            self, sid: str, cassette: str, url: str, body: bytes,
        ) -> None:
            hit = vcr.vcr_replay_lookup(
                sid, method=self.command, url=url, request_body=body, cassette=cassette,
            )
            if hit is None:
                self._reply(
                    502,
                    {"Content-Type": "application/json"},
                    (
                        b'{"error":{"code":"vcr.cassette_miss","message":'
                        b'"replay mode: no recorded interaction matches '
                        b'(method, url, body_hash). Switch to record mode '
                        b'to capture this request."}}'
                    ),
                )
                return
            response_headers = {k: str(v) for k, v in (hit.get("response_headers") or {}).items()}
            response_headers.setdefault("X-Aztea-VCR-Replay", "1")
            body_text = hit.get("response_body") or ""
            self._reply(
                int(hit.get("status") or 200),
                response_headers,
                body_text.encode("utf-8"),
            )

        def _record_and_forward(
            self,
            sid: str,
            cassette: str,
            url: str,
            request_headers: dict[str, str],
            request_body: bytes,
        ) -> None:
            try:
                status, response_headers, response_body = _forward_to_upstream(
                    self.command, url, request_headers, request_body,
                )
            except Exception as exc:  # noqa: BLE001
                self._reply(
                    502,
                    {"Content-Type": "application/json"},
                    (
                        b'{"error":{"code":"vcr.upstream_failed","message":'
                        + str(exc).encode("utf-8", "replace")[:512]
                        + b'"}}'
                    ),
                )
                return
            try:
                vcr.vcr_append(
                    sid,
                    method=self.command,
                    url=url,
                    request_headers=request_headers,
                    request_body=request_body.decode("utf-8", "replace"),
                    status=status,
                    response_headers=response_headers,
                    response_body=response_body,
                    cassette=cassette,
                )
            except Exception:
                _LOG.exception("vcr_append failed during record")
            response_headers.setdefault("X-Aztea-VCR-Record", "1")
            self._reply(status, response_headers, response_body.encode("utf-8"))

        def _forward(
            self,
            url: str,
            request_headers: dict[str, str],
            request_body: bytes,
        ) -> None:
            try:
                status, response_headers, response_body = _forward_to_upstream(
                    self.command, url, request_headers, request_body,
                )
            except Exception as exc:  # noqa: BLE001
                self._reply(
                    502,
                    {"Content-Type": "application/json"},
                    (
                        b'{"error":{"code":"vcr.upstream_failed","message":'
                        + str(exc).encode("utf-8", "replace")[:512]
                        + b'"}}'
                    ),
                )
                return
            self._reply(status, response_headers, response_body.encode("utf-8"))

        def _reply(
            self, code: int, headers: dict[str, str], body: bytes,
        ) -> None:
            self.send_response(code)
            for k, v in headers.items():
                if k.lower() in ("transfer-encoding", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def do_PUT(self) -> None:  # noqa: N802
            self._handle()

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle()

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle()

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle()

    return _VCRProxyHandler


def _copy_headers(headers: Any) -> dict[str, str]:
    """Pure-ish: copy proxy-incoming headers, dropping the hop-by-hop set."""
    skip = {
        "host", "proxy-connection", "connection", "transfer-encoding",
        "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization",
    }
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in skip:
            continue
        out[k] = str(v)
    return out


def _forward_to_upstream(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
) -> tuple[int, dict[str, str], str]:
    """Side-effect: HTTP forward via urllib; returns ``(status, headers, body)``."""
    req = urllib.request.Request(  # noqa: S310 — proxy forwarding by design
        url, data=body if body else None, method=method, headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=_FORWARD_TIMEOUT_S) as resp:  # noqa: S310
            status = int(resp.status)
            response_headers = {k: v for k, v in resp.getheaders()}
            response_body = resp.read().decode("utf-8", "replace")
            return status, response_headers, response_body
    except urllib.error.HTTPError as exc:
        # 4xx/5xx still count as "recorded" — the agent's retry logic
        # needs to see them.
        response_headers = {k: v for k, v in (exc.headers or {}).items()}
        body_text = ""
        if hasattr(exc, "read"):
            try:
                body_text = exc.read().decode("utf-8", "replace")
            except Exception:
                body_text = ""
        return int(exc.code), response_headers, body_text


# Public helper so tests don't reach into the private handler.
def proxy_handle_request(
    sandbox_id: str,
    method: str,
    url: str,
    headers: dict[str, str] | None,
    body: bytes,
) -> tuple[int, dict[str, str], str]:
    """Drive one request through the proxy logic WITHOUT a real HTTP server.

    Why: pure-function entry point so the engine + unit tests can
    exercise record/replay deterministically. Mirrors the server's
    handler so any bug fix lands once for both paths.
    """
    if not get(sandbox_id):
        raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active")
    mode_info = vcr.vcr_mode(sandbox_id)
    mode = str(mode_info.get("mode") or "off").lower()
    cassette = str(mode_info.get("cassette") or "default")
    if mode == "replay":
        hit = vcr.vcr_replay_lookup(
            sandbox_id, method=method, url=url, request_body=body, cassette=cassette,
        )
        if hit is None:
            return 502, {"Content-Type": "application/json"}, (
                '{"error":{"code":"vcr.cassette_miss"}}'
            )
        return (
            int(hit.get("status") or 200),
            {k: str(v) for k, v in (hit.get("response_headers") or {}).items()},
            hit.get("response_body") or "",
        )
    if mode == "record":
        status, response_headers, response_body = _forward_to_upstream(
            method, url, headers or {}, body,
        )
        vcr.vcr_append(
            sandbox_id,
            method=method, url=url,
            request_headers=headers or {},
            request_body=body.decode("utf-8", "replace"),
            status=status,
            response_headers=response_headers,
            response_body=response_body,
            cassette=cassette,
        )
        return status, response_headers, response_body
    # off: passthrough
    return _forward_to_upstream(method, url, headers or {}, body)


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active on this host")
    return state
