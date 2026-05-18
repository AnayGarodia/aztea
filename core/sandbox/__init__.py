"""Public dispatcher for the live_sandbox engine.

# OWNS: the action → handler routing table and the per-action receipt-mint
#       wrapper. Every call into the engine goes through ``dispatch``.
# NOT OWNS: any per-surface logic — see lifecycle/run_ops/filesystem/etc.
# INVARIANTS:
#   * Every dispatch path produces an Ed25519-signed receipt — including
#     errors and stubs.
#   * Unknown actions return a structured envelope, never an unhandled raise.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from core.sandbox import (
    browser,
    chaos,
    database,
    export,
    filesystem,
    http_ops,
    idempotency,
    lifecycle,
    link,
    network_capture as net_capture_mod,
    observability,
    run_ops,
    share as share_mod,
    snapshots,
    stubs,
    sweeper,
    trace as trace_mod,
    tunnels,
    vcr,
    webhook_inbox as webhook_mod,
)
from core.sandbox.models import (
    ALL_ACTIONS,
    SandboxError,
    SandboxInvalidInput,
    error_envelope,
)
from core.sandbox.receipts import (
    merkle_root_for,
    mint_receipt,
    read_audit,
)
from core.sandbox.state import is_valid_sandbox_id

_LOG = logging.getLogger("aztea.sandbox")


Handler = Callable[[dict[str, Any]], dict[str, Any]]


def _audit_action(payload: dict[str, Any]) -> dict[str, Any]:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required for sandbox_audit")
    limit = int(payload.get("limit") or 1000)
    entries = read_audit(sandbox_id, limit=limit)
    return {
        "sandbox_id": sandbox_id,
        "entries": entries,
        "merkle_root": merkle_root_for(sandbox_id),
        "count": len(entries),
    }


def _cost_action(payload: dict[str, Any]) -> dict[str, Any]:
    from core.sandbox.state import get

    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required for sandbox_cost")
    state = get(sandbox_id)
    if state is None:
        raise SandboxInvalidInput(f"sandbox '{sandbox_id}' not active")
    return sweeper.cost_summary(state)


def _quota_action(_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_concurrent_sandboxes": 10,
        "max_lifetime_minutes_per_sandbox": 120,
        "max_disk_gb_per_sandbox": 50,
        "billing_notice": (
            "v0 quotas are static. Per-wallet caps tracked in the same "
            "follow-up issue as wallet integration."
        ),
    }


HANDLERS: dict[str, Handler] = {
    # lifecycle
    "sandbox_start": lifecycle.start,
    "sandbox_status": lifecycle.status,
    "sandbox_stop": lifecycle.stop,
    "sandbox_extend": lifecycle.extend,
    "sandbox_list": lifecycle.list_sandboxes,
    "sandbox_resume": lifecycle.resume,
    "sandbox_batch_start": lifecycle.batch_start,
    # exec
    "sandbox_exec": run_ops.run_command,
    "sandbox_exec_in_service": run_ops.run_command_in_service,
    "sandbox_bg_start": run_ops.bg_start,
    "sandbox_bg_list": run_ops.bg_list,
    "sandbox_bg_kill": run_ops.bg_kill,
    "sandbox_bg_logs": run_ops.bg_logs,
    # filesystem
    "sandbox_read_file": filesystem.read_file,
    "sandbox_write_file": filesystem.write_file,
    "sandbox_delete_file": filesystem.delete_file,
    "sandbox_apply_patch": filesystem.apply_patch,
    "sandbox_glob": filesystem.glob_files,
    "sandbox_grep": filesystem.grep_files,
    "sandbox_sync_from_local": filesystem.sync_from_local,
    # database
    "sandbox_db_query": database.db_query,
    "sandbox_db_snapshot": database.db_snapshot,
    "sandbox_db_restore": database.db_restore,
    "sandbox_db_introspect": database.db_introspect,
    "sandbox_db_seed": database.db_seed,
    # http + observability
    "sandbox_http_request": http_ops.sandbox_http,
    "sandbox_logs": observability.fetch_logs,
    "sandbox_metrics": observability.fetch_metrics,
    "sandbox_inspect_process": observability.inspect_process,
    # snapshots
    "sandbox_snapshot": snapshots.snapshot,
    "sandbox_restore": snapshots.restore,
    "sandbox_fork": snapshots.fork,
    "sandbox_diff_snapshots": snapshots.diff_snapshots,
    # outbound vcr
    "sandbox_outbound_record": vcr.outbound_record,
    "sandbox_outbound_replay": vcr.outbound_replay,
    # browser session — full surface
    "sandbox_browser_session": browser.session_open,
    "sandbox_browser_close": browser.session_close,
    "sandbox_browser_navigate": browser.navigate,
    "sandbox_browser_screenshot": browser.screenshot,
    "sandbox_browser_console_logs": browser.console_logs,
    "sandbox_browser_click": browser.click,
    "sandbox_browser_fill": browser.fill,
    "sandbox_browser_eval": browser.eval_js,
    "sandbox_browser_network": browser.network,
    "sandbox_browser_a11y_tree": browser.a11y_tree,
    "sandbox_browser_axe_audit": browser.axe_audit,
    "sandbox_browser_lighthouse": browser.lighthouse,
    "sandbox_browser_record": browser.record_start,
    "sandbox_browser_replay": browser.replay,
    # multi-sandbox / export / chaos
    "sandbox_link": link.link,
    "sandbox_export_snapshot": export.export_snapshot,
    "sandbox_inject_failure": chaos.inject_failure,
    # tunnels + webhooks + privileged sidecars + share
    "sandbox_tunnel_open": tunnels.tunnel_open,
    "sandbox_tunnel_close": tunnels.tunnel_close,
    "sandbox_webhook_inbox": webhook_mod.webhook_inbox,
    "sandbox_network_capture": net_capture_mod.network_capture,
    "sandbox_trace": trace_mod.trace,
    "sandbox_share": share_mod.share,
    # audit + cost
    "sandbox_audit": _audit_action,
    "sandbox_cost": _cost_action,
    "sandbox_quota": _quota_action,
}


def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """Single entry point: route ``action`` to its handler, mint a receipt.

    Why: every surface goes through here so receipts are guaranteed —
    including for errors and stubs.
    """
    if not isinstance(payload, dict):
        return error_envelope(
            "live_sandbox.invalid_input",
            f"payload must be an object; got {type(payload).__name__}",
        )
    action = str(payload.get("action") or "").strip()
    workspace_id = payload.get("workspace_id") if isinstance(payload.get("workspace_id"), str) else None
    idempotency_key = (
        payload.get("idempotency_key")
        if isinstance(payload.get("idempotency_key"), str)
        else None
    )
    inner_payload = payload.get("input") or payload.get("payload") or {}
    if not isinstance(inner_payload, dict):
        return error_envelope(
            "live_sandbox.invalid_input",
            "input/payload must be an object",
        )
    sandbox_id = _resolve_sandbox_id(action, payload, inner_payload)
    # Audit 2026-05-17 gap #11: dedup retry of mutating actions. If the
    # same idempotency_key has a cached successful response, return it
    # verbatim (with replayed=true) instead of re-executing.
    cached = idempotency.lookup(action, idempotency_key)
    if cached is not None:
        return cached
    handler = HANDLERS.get(action)
    if handler is not None:
        response = _run_handler(
            action=action,
            handler=handler,
            inner_payload=inner_payload,
            sandbox_id=sandbox_id,
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
        )
        idempotency.store(action, idempotency_key, response)
        return response
    if action in stubs.stub_actions():
        response = stubs.stub_for(action)
        return _wrap_with_receipt(action, inner_payload, response, sandbox_id, workspace_id, idempotency_key)
    if not action:
        return error_envelope(
            "live_sandbox.invalid_input",
            "action is required",
            details={"actions": sorted(HANDLERS.keys()) + stubs.stub_actions()},
        )
    return error_envelope(
        "live_sandbox.unknown_action",
        f"action '{action}' is not recognised",
        details={"known_actions": sorted(HANDLERS.keys())},
    )


def _run_handler(
    *,
    action: str,
    handler: Handler,
    inner_payload: dict[str, Any],
    sandbox_id: str | None,
    workspace_id: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    try:
        result = handler(inner_payload)
    except SandboxError as exc:
        envelope = error_envelope(exc.code, exc.message, details=exc.details)
        envelope["receipt"] = mint_receipt(
            sandbox_id=sandbox_id,
            action=action,
            request=inner_payload,
            response=envelope,
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
        )
        return envelope
    except Exception as exc:  # noqa: BLE001 — engine boundary
        _LOG.exception("live_sandbox handler crashed: %s", action)
        envelope = error_envelope(
            "live_sandbox.unhandled_exception",
            f"{type(exc).__name__}: {exc}",
        )
        envelope["receipt"] = mint_receipt(
            sandbox_id=sandbox_id,
            action=action,
            request=inner_payload,
            response=envelope,
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
        )
        return envelope
    if not isinstance(result, dict):
        result = {"value": result}
    return _wrap_with_receipt(
        action, inner_payload, result, sandbox_id, workspace_id, idempotency_key
    )


def _wrap_with_receipt(
    action: str,
    request: dict[str, Any],
    response: dict[str, Any],
    sandbox_id: str | None,
    workspace_id: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Side-effect: mint and attach the receipt to the response."""
    out = dict(response) if isinstance(response, dict) else {"value": response}
    receipt = mint_receipt(
        sandbox_id=sandbox_id or out.get("sandbox_id"),
        action=action,
        request=request,
        response=out,
        workspace_id=workspace_id,
        idempotency_key=idempotency_key,
    )
    out["receipt"] = receipt
    out["action"] = action
    return out


def _resolve_sandbox_id(
    action: str,
    payload: dict[str, Any],
    inner_payload: dict[str, Any],
) -> str | None:
    """Pure: prefer inner_payload.sandbox_id, fall back to outer payload."""
    for src in (inner_payload, payload):
        candidate = src.get("sandbox_id")
        if isinstance(candidate, str) and is_valid_sandbox_id(candidate.strip()):
            return candidate.strip()
    return None


__all__ = [
    "ALL_ACTIONS",
    "dispatch",
    "HANDLERS",
]
