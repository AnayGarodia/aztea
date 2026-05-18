"""Typed Python client for the ``live_sandbox`` agent.

Wraps the live_sandbox MCP/HTTP surface with one method per verb. Each
method returns a typed envelope (TypedDict) so IDE autocomplete + mypy
get the contract right.

Usage:
    from aztea import AzteaClient
    from aztea.sandbox import SandboxClient

    client = AzteaClient(api_key="az_...")
    sandbox = SandboxClient(client)

    started = sandbox.start(source={"kind":"git","url":"https://github.com/x/y"})
    out = sandbox.run_command(sandbox_id=started["sandbox_id"], cmd="pytest -q")
    assert out["exit_code"] == 0

# OWNS: the typed wrapper. Methods are 1:1 with the engine's HANDLERS map.
# NOT OWNS: the underlying HTTP path (lives on ``AzteaClient``).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, TypedDict

# Static UUID — duplicated from server/builtin_agents/constants.py so the
# SDK doesn't have to import server-side code. If the constant ever
# changes, the SDK test catches the drift.
LIVE_SANDBOX_AGENT_ID = "3354f7c4-bb9d-55e2-8e8c-df67a64f57a2"


# --- TypedDicts: typed envelopes for the most common verbs --------------------

class Receipt(TypedDict, total=False):
    """Ed25519-signed receipt minted on every action."""

    did: str
    alg: str
    signed_at: int
    payload: dict[str, Any]
    signature: str
    hash: str


class StartResponse(TypedDict, total=False):
    sandbox_id: str
    status: str
    boot_strategy_detected: str
    services: dict[str, Any]
    filesystem_root: str
    boot_timing: dict[str, float]
    expires_at: int
    snapshot_chain: list[str]
    network: dict[str, Any]
    determinism: dict[str, Any]
    isolation: dict[str, Any]
    spending: dict[str, Any]
    unresolved_secrets: list[str]
    workspace_id: str | None
    receipt: Receipt


class ExecResponse(TypedDict, total=False):
    sandbox_id: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: int
    receipt: Receipt


class DbQueryResponse(TypedDict, total=False):
    sandbox_id: str
    rows: list[dict[str, Any]]
    row_count: int
    columns: list[str]
    truncated: bool
    status: str
    explain_analyze: Any
    receipt: Receipt


class SnapshotResponse(TypedDict, total=False):
    sandbox_id: str
    snapshot_id: str
    service_tags: dict[str, str]
    db_dump_label: str | None
    fs_tar_size_bytes: int
    created_at: int
    receipt: Receipt


# --- Client ------------------------------------------------------------------

class SandboxClient:
    """Typed wrapper for every ``live_sandbox`` verb.

    Why: callers using ``AzteaClient`` directly have to dict-build every
    payload and remember which keys go where. This wrapper makes each
    verb a typed Python call.
    """

    def __init__(self, client: Any, *, agent_id: str = LIVE_SANDBOX_AGENT_ID) -> None:
        if client is None:
            raise ValueError("SandboxClient requires an AzteaClient")
        self._client = client
        self._agent_id = agent_id

    # --- lifecycle ----------------------------------------------------------

    def start(
        self,
        *,
        source: Mapping[str, Any],
        boot: Mapping[str, Any] | None = None,
        env: Mapping[str, Any] | None = None,
        network: Mapping[str, Any] | None = None,
        size: Mapping[str, Any] | None = None,
        lifetime: Mapping[str, Any] | None = None,
        clock: Mapping[str, Any] | None = None,
        isolation_backend: str | None = None,
        spending_cap_cents: int | None = None,
        workspace_id: str | None = None,
        region: str | None = None,
        idempotency_key: str | None = None,
    ) -> StartResponse:
        """Spin up a sandbox; returns the full start envelope incl. receipt."""
        inner: dict[str, Any] = {"source": dict(source)}
        if boot is not None:
            inner["boot"] = dict(boot)
        if env is not None:
            inner["env"] = dict(env)
        if network is not None:
            inner["network"] = dict(network)
        if size is not None:
            inner["size"] = dict(size)
        if lifetime is not None:
            inner["lifetime"] = dict(lifetime)
        if clock is not None:
            inner["clock"] = dict(clock)
        if isolation_backend is not None:
            inner["isolation_backend"] = isolation_backend
        if spending_cap_cents is not None:
            inner["spending_cap_cents"] = int(spending_cap_cents)
        if workspace_id is not None:
            inner["workspace_id"] = workspace_id
        if region is not None:
            inner["region"] = region
        return self._invoke("sandbox_start", inner, idempotency_key=idempotency_key)

    def status(self, sandbox_id: str) -> dict[str, Any]:
        return self._invoke("sandbox_status", {"sandbox_id": sandbox_id})

    def stop(
        self,
        sandbox_id: str,
        *,
        final_snapshot: bool | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"sandbox_id": sandbox_id}
        if final_snapshot is not None:
            body["final_snapshot"] = bool(final_snapshot)
        return self._invoke("sandbox_stop", body, idempotency_key=idempotency_key)

    def extend(self, sandbox_id: str, minutes: int) -> dict[str, Any]:
        return self._invoke(
            "sandbox_extend",
            {"sandbox_id": sandbox_id, "minutes": int(minutes)},
        )

    def list_sandboxes(self) -> dict[str, Any]:
        return self._invoke("sandbox_list", {})

    def resume(self, sandbox_id: str) -> dict[str, Any]:
        return self._invoke("sandbox_resume", {"sandbox_id": sandbox_id})

    def batch_start(
        self,
        *,
        matrix: Mapping[str, Iterable[Any]],
        base: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_batch_start",
            {
                "matrix": {k: list(v) for k, v in matrix.items()},
                "base": dict(base),
            },
            idempotency_key=idempotency_key,
        )

    # --- exec ---------------------------------------------------------------

    def run_command(
        self,
        sandbox_id: str,
        cmd: str,
        *,
        cwd: str | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        user: str | None = None,
        timeout_seconds: int | None = None,
    ) -> ExecResponse:
        """Run a command in the sandbox; thin wrapper over the sandbox_exec verb."""
        body: dict[str, Any] = {"sandbox_id": sandbox_id, "cmd": cmd}
        if cwd is not None:
            body["cwd"] = cwd
        if stdin is not None:
            body["stdin"] = stdin
        if env is not None:
            body["env"] = dict(env)
        if user is not None:
            body["user"] = user
        if timeout_seconds is not None:
            body["timeout_seconds"] = int(timeout_seconds)
        return self._invoke("sandbox_exec", body)

    def run_command_in_service(
        self, sandbox_id: str, service: str, cmd: str, **kwargs: Any,
    ) -> ExecResponse:
        body = {"sandbox_id": sandbox_id, "service": service, "cmd": cmd}
        body.update(kwargs)
        return self._invoke("sandbox_exec_in_service", body)

    # --- filesystem ---------------------------------------------------------

    def read_file(self, sandbox_id: str, path: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_read_file", {"sandbox_id": sandbox_id, "path": path},
        )

    def write_file(
        self,
        sandbox_id: str,
        path: str,
        *,
        content: str | None = None,
        content_b64: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"sandbox_id": sandbox_id, "path": path}
        if content is not None:
            body["content"] = content
        if content_b64 is not None:
            body["content_b64"] = content_b64
        return self._invoke("sandbox_write_file", body)

    def delete_file(self, sandbox_id: str, path: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_delete_file", {"sandbox_id": sandbox_id, "path": path},
        )

    def apply_patch(self, sandbox_id: str, patch: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_apply_patch", {"sandbox_id": sandbox_id, "patch": patch},
        )

    def glob(self, sandbox_id: str, pattern: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_glob", {"sandbox_id": sandbox_id, "pattern": pattern},
        )

    def grep(
        self, sandbox_id: str, pattern: str, *, glob_filter: str = "**/*",
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_grep",
            {"sandbox_id": sandbox_id, "pattern": pattern, "glob": glob_filter},
        )

    def sync_from_local(self, sandbox_id: str, local_path: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_sync_from_local",
            {"sandbox_id": sandbox_id, "local_path": local_path},
        )

    # --- database -----------------------------------------------------------

    def db_query(
        self, sandbox_id: str, sql: str, *, explain: bool = False,
    ) -> DbQueryResponse:
        return self._invoke(
            "sandbox_db_query",
            {"sandbox_id": sandbox_id, "sql": sql, "explain": bool(explain)},
        )

    def db_snapshot(self, sandbox_id: str, *, label: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"sandbox_id": sandbox_id}
        if label is not None:
            body["label"] = label
        return self._invoke("sandbox_db_snapshot", body)

    def db_restore(self, sandbox_id: str, label: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_db_restore", {"sandbox_id": sandbox_id, "label": label},
        )

    def db_introspect(self, sandbox_id: str) -> dict[str, Any]:
        return self._invoke("sandbox_db_introspect", {"sandbox_id": sandbox_id})

    def db_seed(
        self, sandbox_id: str, cmd: str, *, service: str | None = None,
    ) -> dict[str, Any]:
        body = {"sandbox_id": sandbox_id, "cmd": cmd}
        if service is not None:
            body["service"] = service
        return self._invoke("sandbox_db_seed", body)

    # --- snapshots ----------------------------------------------------------

    def snapshot(self, sandbox_id: str, *, reason: str | None = None) -> SnapshotResponse:
        body = {"sandbox_id": sandbox_id}
        if reason is not None:
            body["reason"] = reason
        return self._invoke("sandbox_snapshot", body)

    def restore(self, sandbox_id: str, snapshot_id: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_restore",
            {"sandbox_id": sandbox_id, "snapshot_id": snapshot_id},
        )

    def fork(self, source_sandbox_id: str, snapshot_id: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_fork",
            {
                "source_sandbox_id": source_sandbox_id,
                "snapshot_id": snapshot_id,
            },
        )

    def diff_snapshots(
        self, sandbox_id: str, snapshot_a: str, snapshot_b: str,
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_diff_snapshots",
            {
                "sandbox_id": sandbox_id,
                "snapshot_a": snapshot_a,
                "snapshot_b": snapshot_b,
            },
        )

    # --- HTTP + observability ----------------------------------------------

    def http(
        self,
        sandbox_id: str,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: str | None = None,
        jar_key: str = "default",
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "url": url,
            "method": method,
            "jar_key": jar_key,
        }
        if headers is not None:
            payload["headers"] = dict(headers)
        if body is not None:
            payload["body"] = body
        if timeout_seconds is not None:
            payload["timeout_seconds"] = int(timeout_seconds)
        return self._invoke("sandbox_http_request", payload)

    def logs(
        self,
        sandbox_id: str,
        *,
        service: str | None = None,
        tail: int = 500,
        regex: str | None = None,
        level: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"sandbox_id": sandbox_id, "tail": tail}
        if service is not None:
            body["service"] = service
        if regex is not None:
            body["regex"] = regex
        if level is not None:
            body["level"] = level
        return self._invoke("sandbox_logs", body)

    def metrics(self, sandbox_id: str) -> dict[str, Any]:
        return self._invoke("sandbox_metrics", {"sandbox_id": sandbox_id})

    def inspect_process(
        self, sandbox_id: str, *, service: str | None = None, pid: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"sandbox_id": sandbox_id}
        if service is not None:
            body["service"] = service
        if pid is not None:
            body["pid"] = int(pid)
        return self._invoke("sandbox_inspect_process", body)

    # --- vcr + chaos + audit ----------------------------------------------

    def outbound_record(
        self, sandbox_id: str, *, cassette: str = "default",
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_outbound_record",
            {"sandbox_id": sandbox_id, "cassette": cassette},
        )

    def outbound_replay(
        self, sandbox_id: str, *, cassette: str = "default",
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_outbound_replay",
            {"sandbox_id": sandbox_id, "cassette": cassette},
        )

    def inject_failure(
        self,
        sandbox_id: str,
        *,
        kind: str,
        target: str = "",
        value: float | int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "sandbox_id": sandbox_id, "kind": kind, "target": target,
        }
        if value is not None:
            body["value"] = value
        return self._invoke("sandbox_inject_failure", body)

    def audit(self, sandbox_id: str, *, limit: int = 1000) -> dict[str, Any]:
        return self._invoke(
            "sandbox_audit", {"sandbox_id": sandbox_id, "limit": int(limit)},
        )

    def cost(self, sandbox_id: str) -> dict[str, Any]:
        return self._invoke("sandbox_cost", {"sandbox_id": sandbox_id})

    # --- tunnels + webhook + share -----------------------------------------

    def tunnel_open(
        self,
        sandbox_id: str,
        service: str,
        port: int,
        *,
        auth: str = "none",
        hostname_hint: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "service": service,
            "port": int(port),
            "auth": auth,
        }
        if hostname_hint is not None:
            body["hostname_hint"] = hostname_hint
        return self._invoke("sandbox_tunnel_open", body)

    def tunnel_close(self, sandbox_id: str, tunnel_id: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_tunnel_close",
            {"sandbox_id": sandbox_id, "tunnel_id": tunnel_id},
        )

    def webhook_inbox(
        self,
        sandbox_id: str,
        *,
        since: str | None = None,
        limit: int = 100,
        replay_event_id: str | None = None,
        target_service: str | None = None,
        target_path: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"sandbox_id": sandbox_id, "limit": int(limit)}
        if since is not None:
            body["since"] = since
        if replay_event_id is not None:
            body["replay_event_id"] = replay_event_id
        if target_service is not None:
            body["target_service"] = target_service
        if target_path is not None:
            body["target_path"] = target_path
        return self._invoke("sandbox_webhook_inbox", body)

    def share(
        self,
        sandbox_id: str,
        *,
        access: str = "read",
        ttl_minutes: int = 30,
        actor_hint: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "access": access,
            "ttl_minutes": int(ttl_minutes),
        }
        if actor_hint is not None:
            body["actor_hint"] = actor_hint
        return self._invoke("sandbox_share", body)

    def link(self, sandbox_id: str, other_sandbox_id: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_link",
            {"sandbox_id": sandbox_id, "other_sandbox_id": other_sandbox_id},
        )

    def export_snapshot(
        self, sandbox_id: str, snapshot_id: str, destination_uri: str,
        *, include_service_images: bool = True,
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_export_snapshot",
            {
                "sandbox_id": sandbox_id,
                "snapshot_id": snapshot_id,
                "destination_uri": destination_uri,
                "include_service_images": include_service_images,
            },
        )

    # --- privileged (env-gated) --------------------------------------------

    def network_capture(
        self,
        sandbox_id: str,
        *,
        duration_seconds: int = 30,
        filter: str = "",
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_network_capture",
            {
                "sandbox_id": sandbox_id,
                "duration_seconds": int(duration_seconds),
                "filter": filter,
            },
        )

    def trace(
        self,
        sandbox_id: str,
        service: str,
        pid: int,
        *,
        tool: str = "py-spy",
        duration_seconds: int = 20,
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_trace",
            {
                "sandbox_id": sandbox_id,
                "service": service,
                "pid": int(pid),
                "tool": tool,
                "duration_seconds": int(duration_seconds),
            },
        )

    # --- browser -----------------------------------------------------------

    def browser_session(
        self, sandbox_id: str, *, viewport: Mapping[str, int] | None = None,
        headless: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"sandbox_id": sandbox_id, "headless": headless}
        if viewport is not None:
            body["viewport"] = dict(viewport)
        return self._invoke("sandbox_browser_session", body)

    def browser_navigate(
        self, sandbox_id: str, session_id: str, url: str,
        *, wait_until: str = "load",
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_browser_navigate",
            {
                "sandbox_id": sandbox_id, "session_id": session_id,
                "url": url, "wait_until": wait_until,
            },
        )

    def browser_screenshot(
        self, sandbox_id: str, session_id: str, *, full_page: bool = True,
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_browser_screenshot",
            {
                "sandbox_id": sandbox_id, "session_id": session_id,
                "full_page": full_page,
            },
        )

    def browser_click(
        self, sandbox_id: str, session_id: str, selector: str,
        *, button: str = "left", click_count: int = 1,
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_browser_click",
            {
                "sandbox_id": sandbox_id, "session_id": session_id,
                "selector": selector, "button": button,
                "click_count": int(click_count),
            },
        )

    def browser_fill(
        self, sandbox_id: str, session_id: str, selector: str, value: str,
    ) -> dict[str, Any]:
        return self._invoke(
            "sandbox_browser_fill",
            {
                "sandbox_id": sandbox_id, "session_id": session_id,
                "selector": selector, "value": value,
            },
        )

    def browser_evaluate(
        self, sandbox_id: str, session_id: str, js: str,
    ) -> dict[str, Any]:
        """Evaluate JS in the page; thin wrapper over the sandbox_browser_eval verb."""
        return self._invoke(
            "sandbox_browser_eval",
            {"sandbox_id": sandbox_id, "session_id": session_id, "js": js},
        )

    def browser_close(self, sandbox_id: str, session_id: str) -> dict[str, Any]:
        return self._invoke(
            "sandbox_browser_close",
            {"sandbox_id": sandbox_id, "session_id": session_id},
        )

    # --- internal ----------------------------------------------------------

    def _invoke(
        self,
        action: str,
        inner: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action, "input": inner}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self._client.registry.call(self._agent_id, payload)
