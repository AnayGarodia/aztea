"""live_sandbox — the user's real app, booted and pokeable like staging.

# OWNS: the thin agent surface that routes payloads into core.sandbox.dispatch.
# NOT OWNS: any actual sandbox logic — that lives in core/sandbox/.
#
# INVARIANTS:
#   * Every payload must include ``action`` — the verb naming the sandbox op.
#   * Every output carries an Ed25519-signed receipt minted by the engine.
#   * Top-level errors return the project-canonical {"error": {"code","message"}}
#     envelope so the settlement layer can refund on infra failure.

Input:
    {
        "action": "sandbox_start" | "sandbox_exec" | "sandbox_db_query" | ...,
        "input": { ...action-specific payload... },
        "workspace_id": "<reserved-forward-compat>",
        "idempotency_key": "<optional client-supplied key>"
    }

Output:
    Action-specific dict + a top-level ``receipt`` object signed against
    did:web:aztea.ai:agents:live-sandbox. On unrecoverable error, the
    canonical envelope ``{"error": {"code","message",[details]}, "receipt": ...}``.

External dependencies:
    * Docker daemon (required for any "real implementation" action).
    * Optional: libfaketime on the host (for the determinism clock freeze).
    * Optional: rsync on the host (sync_from_local falls back to shutil).

Runtime requirements:
    * The host process needs docker permissions (Unix socket access on Linux,
      docker.app running on macOS, AZTEA_SANDBOX_DOCKER_BIN if non-standard).
    * Per-sandbox state lives under ``/tmp/aztea-sandbox/<sandbox_id>/`` (or
      ``$AZTEA_SANDBOX_STATE_ROOT``).
"""

from __future__ import annotations

import logging
from typing import Any

from agents._contracts import agent_error as _err
from core import sandbox as _sandbox_engine

_LOG = logging.getLogger("aztea.agents.live_sandbox")


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Route ``payload`` into the sandbox engine. Returns a signed envelope.

    Why: one agent verb at the catalogue level (``live_sandbox``) keeps
    the marketplace UX simple; the engine handles the dispatch table so
    each surface stays independently testable.
    """
    if not isinstance(payload, dict):
        return _err(
            "live_sandbox.invalid_input",
            f"payload must be an object; got {type(payload).__name__}",
        )
    try:
        result = _sandbox_engine.dispatch(payload)
    except Exception as exc:  # noqa: BLE001 — top-level boundary
        _LOG.exception("live_sandbox dispatch failed")
        return _err(
            "live_sandbox.unhandled_exception",
            f"{type(exc).__name__}: {exc}",
        )
    return result
