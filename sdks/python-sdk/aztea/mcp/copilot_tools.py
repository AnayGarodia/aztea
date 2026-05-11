"""Co-pilot mode MCP tools — `aztea_call_streaming` and `aztea_steer`.

Phase 9 of the co-pilot-mode design (see
``docs/superpowers/specs/2026-05-09-copilot-mode-design.md``). Lives in its
own module so ``scripts/aztea_mcp_server.py`` (already over the global
1000-line file budget on main) does not balloon further.

# OWNS:        the streaming-call and steer MCP tool handlers
# NOT OWNS:    the underlying /jobs lifecycle (server-side), receipt
#              construction, or any persistence concern
# INVARIANTS:  must not block the MCP read loop forever — every poll has a
#              caller-bounded timeout; never raise — always return a
#              structured (ok, payload) tuple consistent with the rest of
#              the bridge
# DECISIONS:   v1 ships the *fallback* progress strategy (collect partials
#              and return them in the final response under ``partials``).
#              The active stdio JSON-RPC server in this repo dispatches
#              ``tools/call`` synchronously through ``RegistryBridge``;
#              there is no plumbed path to emit ``notifications/progress``
#              from inside a tool handler with the current request_id.
#              Wiring that in would mean threading a notifier callback
#              through ``call_tool`` and is explicitly out of scope per
#              the Phase 9 brief — the spec allows the fallback.
# KNOWN DEBT:  long-poll on /jobs/{id}/messages?wait_ms=... is Phase 6;
#              this module uses short-poll only.
"""

from __future__ import annotations

import time
from typing import Any

import requests

# Polling cadence for /jobs/{id}/messages while the job is non-terminal.
# Spec: 500ms is the v1 default — do not lengthen without bumping the
# `wait_ms` long-poll story (Phase 6).
_POLL_INTERVAL_S: float = 0.5

# Receipt route returns 425 Too Early until the settlement runner has
# stamped ``pending_settlements.receipt_built_at``. The runner is invoked
# synchronously on every terminal transition, but a settlement that
# raced behind a busy DB write may need a brief retry.
_RECEIPT_MAX_ATTEMPTS: int = 3
_RECEIPT_RETRY_SLEEP_S: float = 0.2

# Bound the streaming call so a runaway job cannot stall the MCP loop.
# Caller can override via ``timeout_s``; we still clamp to a sane ceiling.
_DEFAULT_TIMEOUT_S: float = 300.0
_MAX_TIMEOUT_S: float = 1800.0

# Set of job states that mean "no more partials are coming". Mirrors
# ``core/jobs/db.py::_TERMINAL_STATES`` after the Phase-1 ``stopped``
# addition; duplicated here because the MCP server runs out-of-process
# and importing core/* from a stdio script would pull a heavy dep graph.
_TERMINAL_STATES: frozenset[str] = frozenset({"complete", "failed", "stopped", "cancelled"})


def call_streaming(
    *,
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    arguments: dict[str, Any],
    catalog: list[dict[str, Any]] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Submit an async job, poll for partials, return final result + receipt.

    See module docstring for the progress-notification fallback rationale.

    1.6.9: ``catalog`` (the bridge's cached registry entries) is required
    so this tool can resolve ``slug`` → ``agent_id`` for the POST /jobs
    body. The server's JobCreateRequest needs ``agent_id``; pre-1.6.9 this
    function shipped the slug directly and every call returned 422
    "Field required: agent_id" — the entire streaming surface was broken
    in production.
    """
    slug = str(arguments.get("slug") or "").strip()
    if not slug:
        return False, {"error": "INVALID_INPUT", "message": "slug is required."}
    raw_input = arguments.get("input")
    if raw_input is None:
        raw_input = arguments.get("arguments")  # accept both names
    if raw_input is None:
        raw_input = {}
    if not isinstance(raw_input, dict):
        return False, {
            "error": "INVALID_INPUT",
            "message": "input must be an object.",
        }
    stop_when = arguments.get("stop_when")
    if stop_when is not None and not isinstance(stop_when, list):
        return False, {
            "error": "INVALID_INPUT",
            "message": "stop_when must be an array of {label, expr} objects.",
        }
    billing_unit = arguments.get("billing_unit")
    if billing_unit is not None and billing_unit not in ("call", "partial"):
        return False, {
            "error": "INVALID_INPUT",
            "message": "billing_unit must be 'call' or 'partial'.",
        }
    timeout_s = _coerce_timeout(arguments.get("timeout_s"))

    # Resolve slug → agent_id from the bridge's catalog. Accept exact slug
    # match, canonical slug match, or display-name match (consistent with
    # how aztea_call resolves slugs in 1.6.7+).
    agent_id = _resolve_agent_id(slug, catalog or [])
    if agent_id is None:
        return False, {
            "error": "TOOL_NOT_FOUND",
            "message": (
                f"Unknown specialist slug '{slug}'. Use search_specialists "
                "to discover the canonical slug, then retry."
            ),
        }

    body = _build_jobs_body(agent_id, raw_input, stop_when, billing_unit)
    job_url = f"{base_url}/jobs"
    try:
        resp = session.post(job_url, headers=headers, json=body, timeout=timeout_seconds)
    except requests.RequestException as exc:
        return False, {"error": "UPSTREAM_UNREACHABLE", "message": str(exc)}
    submit_ok, submit_body = _parse_json(resp)
    if not submit_ok:
        return False, submit_body
    job_id = str(submit_body.get("job_id") or "").strip()
    if not job_id:
        return False, {
            "error": "BAD_RESPONSE",
            "message": "POST /jobs did not return a job_id.",
            "raw": submit_body,
        }

    return _poll_until_terminal(
        session=session,
        base_url=base_url,
        headers=headers,
        timeout_seconds=timeout_seconds,
        job_id=job_id,
        budget_s=timeout_s,
    )


def steer(
    *,
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    arguments: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """POST /jobs/{id}/steer. Surfaces 429 with a structured back-off code."""
    job_id = str(arguments.get("job_id") or "").strip()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    message = arguments.get("message")
    if not isinstance(message, str) or not message.strip():
        return False, {
            "error": "INVALID_INPUT",
            "message": "message must be a non-empty string.",
        }
    url = f"{base_url}/jobs/{job_id}/steer"
    try:
        resp = session.post(
            url, headers=headers, json={"message": message}, timeout=timeout_seconds
        )
    except requests.RequestException as exc:
        return False, {"error": "UPSTREAM_UNREACHABLE", "message": str(exc)}
    if resp.status_code == 429:
        # Back-off hint surfaced verbatim so the LLM caller can decide.
        # Don't auto-retry — the caller may want to abort or wait longer.
        body_429 = _safe_json(resp)
        retry_after = resp.headers.get("Retry-After")
        return False, {
            "error": "STEER_RATE_LIMITED",
            "message": (
                "Steer rate limit hit. Wait a few seconds before retrying, "
                "or stop steering this job."
            ),
            "status": 429,
            "retry_after_seconds": retry_after,
            "detail": body_429,
        }
    ok, payload = _parse_json(resp)
    if not ok:
        return False, payload
    # Spec contract: return {steer_count} on success.
    return True, {
        "steer_count": payload.get("steer_count"),
        "job_id": job_id,
        "raw": payload,
    }


# ---------------------------------------------------------------------------
# helpers


def _build_jobs_body(
    agent_id: str,
    input_payload: dict[str, Any],
    stop_when: list[Any] | None,
    billing_unit: str | None,
) -> dict[str, Any]:
    """Build the POST /jobs body. Pure function — no I/O.

    Server's ``JobCreateRequest`` requires ``agent_id`` (UUID). Pre-1.6.9
    this function shipped raw slug, which the server rejected with 422
    "Field required: agent_id".
    """
    body: dict[str, Any] = {"agent_id": agent_id, "input_payload": input_payload}
    if stop_when:
        body["stop_when"] = stop_when
    if billing_unit:
        body["billing_unit"] = billing_unit
    return body


def _resolve_agent_id(slug: str, catalog: list[dict[str, Any]]) -> str | None:
    """Pure: find an agent_id in ``catalog`` matching ``slug``.

    Accepts exact slug, canonical-slug (snake_case from display name), or
    display-name match. Returns None when no match — caller should surface
    a TOOL_NOT_FOUND error.
    """
    import re as _re
    needle = (slug or "").strip()
    if not needle:
        return None

    def _canonicalize(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

    canon_needle = _canonicalize(needle)
    for entry in catalog:
        entry_slug = str(entry.get("slug") or entry.get("tool_name") or "").strip()
        entry_name = str(entry.get("name") or "").strip()
        agent_id = str(entry.get("agent_id") or "").strip()
        if not agent_id:
            continue
        if needle in (entry_slug, entry_name):
            return agent_id
        if canon_needle in (_canonicalize(entry_slug), _canonicalize(entry_name)):
            return agent_id
    return None


def _coerce_timeout(raw: Any) -> float:
    """Clamp a caller-supplied timeout to [1, _MAX_TIMEOUT_S]."""
    if raw is None:
        return _DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S
    if value <= 0:
        return _DEFAULT_TIMEOUT_S
    return min(value, _MAX_TIMEOUT_S)


def _poll_until_terminal(
    *,
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    job_id: str,
    budget_s: float,
) -> tuple[bool, dict[str, Any]]:
    """Short-poll the job + messages endpoints until terminal or budget exhausted."""
    deadline = time.monotonic() + budget_s
    since_id: int = 0
    partials: list[dict[str, Any]] = []
    final_state: str | None = None
    job_payload: dict[str, Any] = {}

    while True:
        if time.monotonic() > deadline:
            return False, {
                "error": "TIMEOUT",
                "message": (
                    f"Job {job_id} did not reach a terminal state within "
                    f"{int(budget_s)}s. Cancel via manage_job(action='cancel') "
                    "or extend timeout_s."
                ),
                "job_id": job_id,
                "partials": partials,
                "last_seen_message_id": since_id,
            }

        # Fetch new messages since last seen id; cheaper than a full job
        # fetch and lets us collect partials in id order.
        since_id, partials = _drain_new_partials(
            session=session,
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            job_id=job_id,
            since_id=since_id,
            partials=partials,
        )

        try:
            jr = session.get(
                f"{base_url}/jobs/{job_id}",
                headers=headers,
                timeout=timeout_seconds,
            )
        except requests.RequestException as exc:
            return False, {
                "error": "UPSTREAM_UNREACHABLE",
                "message": str(exc),
                "job_id": job_id,
                "partials": partials,
            }
        ok, body = _parse_json(jr)
        if not ok:
            return False, body
        status = str(body.get("status") or "").strip()
        if status in _TERMINAL_STATES:
            final_state = status
            job_payload = body
            # Final drain — settlement may have written a stop-firing
            # partial after our previous /messages read.
            since_id, partials = _drain_new_partials(
                session=session,
                base_url=base_url,
                headers=headers,
                timeout_seconds=timeout_seconds,
                job_id=job_id,
                since_id=since_id,
                partials=partials,
            )
            break
        time.sleep(_POLL_INTERVAL_S)

    receipt = _fetch_receipt(
        session=session,
        base_url=base_url,
        headers=headers,
        timeout_seconds=timeout_seconds,
        job_id=job_id,
    )
    return True, {
        "job_id": job_id,
        "terminal_state": final_state,
        "output": job_payload.get("output_payload"),
        "stop_reason": job_payload.get("stop_reason") or job_payload.get("stop_reason_json"),
        "receipt_jws": receipt.get("jws") if receipt else None,
        "receipt": receipt,
        "partials": partials,
        "partial_count": len(partials),
        "_progress_strategy": "collected_in_response",
    }


def _drain_new_partials(
    *,
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    job_id: str,
    since_id: int,
    partials: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    """Fetch messages strictly after ``since_id``; append partial_outputs.

    Returns (new_since_id, partials). Never raises — silently keeps the
    cursor on a transient error so the next poll retries.
    """
    try:
        mr = session.get(
            f"{base_url}/jobs/{job_id}/messages",
            headers=headers,
            timeout=timeout_seconds,
            params={"since": since_id},
        )
    except requests.RequestException:
        return since_id, partials
    ok, body = _parse_json(mr)
    if not ok:
        return since_id, partials
    messages = body.get("messages") or []
    new_since = since_id
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        try:
            msg_id = int(msg.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if msg_id > new_since:
            new_since = msg_id
        if msg.get("type") == "partial_output":
            partials.append(
                {
                    "id": msg_id,
                    "payload": msg.get("payload"),
                    "created_at": msg.get("created_at"),
                }
            )
    return new_since, partials


def _fetch_receipt(
    *,
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    job_id: str,
) -> dict[str, Any] | None:
    """Fetch the JWS receipt; retry up to 3 times on 425 Too Early.

    Spec: receipt route returns 425 until the settlement runner stamps
    ``receipt_built_at``. The synchronous drain on terminal transition
    usually wins; the retries cover the race.
    """
    url = f"{base_url}/jobs/{job_id}/receipt"
    last_status: int | None = None
    for attempt in range(_RECEIPT_MAX_ATTEMPTS):
        try:
            r = session.get(url, headers=headers, timeout=timeout_seconds)
        except requests.RequestException:
            return None
        last_status = r.status_code
        if r.status_code == 425:
            time.sleep(_RECEIPT_RETRY_SLEEP_S)
            continue
        ok, body = _parse_json(r)
        if not ok:
            return {"error": "RECEIPT_UNAVAILABLE", "status": r.status_code, "detail": body}
        return body
    return {"error": "RECEIPT_NOT_BUILT", "status": last_status}


def _parse_json(resp: requests.Response) -> tuple[bool, dict[str, Any]]:
    """Mirror of meta_tools._parse — kept local to avoid a cross-module import."""
    try:
        body = resp.json()
    except ValueError:
        body = {"raw_body": resp.text[:500]}
    if not isinstance(body, dict):
        body = {"result": body}
    ok = 200 <= resp.status_code < 300
    if not ok:
        body.setdefault("status", resp.status_code)
    return ok, body


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


# ---------------------------------------------------------------------------
# tool descriptors — registered by aztea_mcp_server.py

CALL_STREAMING_TOOL: dict[str, Any] = {
    "name": "aztea_call_streaming",
    "description": (
        "Submit a specialist call as an async job and stream partial outputs.\n\n"
        "Use this when (a) the agent supports `partial_output` events (hosted "
        "skills + external workers in v1), (b) you want to set `stop_when` "
        "JMESPath predicates that abort the call at the first matching "
        "partial, or (c) you intend to `aztea_steer` the run mid-flight.\n\n"
        "Returns `{output, receipt_jws, stop_reason, terminal_state, partials}`. "
        "Partials are collected and returned in the final response (v1); a "
        "future revision will surface them via MCP progress notifications. "
        "The receipt is a JWS signed by the agent's per-call Ed25519 key over "
        "the full transcript (input → partials → steers → output)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "Specialist slug (e.g. 'web_researcher_agent').",
            },
            "input": {
                "type": "object",
                "description": "Input payload matching the specialist's input schema.",
                "additionalProperties": True,
            },
            "stop_when": {
                "type": "array",
                "description": (
                    "Up to 8 JMESPath predicates evaluated against each "
                    "partial_output; first match aborts the call to terminal "
                    "state 'stopped'. Each item is {label: str, expr: str}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "expr": {"type": "string"},
                    },
                    "required": ["label", "expr"],
                },
                "maxItems": 8,
            },
            "billing_unit": {
                "type": "string",
                "enum": ["call", "partial"],
                "description": (
                    "How to settle on early stop. 'call' charges full price; "
                    "'partial' settles proportionally to partial count. Default: 'call'."
                ),
            },
            "timeout_s": {
                "type": "number",
                "minimum": 1,
                "maximum": _MAX_TIMEOUT_S,
                "default": _DEFAULT_TIMEOUT_S,
                "description": "Hard ceiling on total wall time before TIMEOUT.",
            },
        },
        "required": ["slug"],
    },
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
        "idempotentHint": False,
    },
}


STEER_TOOL: dict[str, Any] = {
    "name": "aztea_steer",
    "description": (
        "Inject mid-flight guidance into a running job's steer inbox. The "
        "agent reads steers between turns and threads them into the next "
        "prompt — see `aztea_call_streaming` for the streaming counterpart. "
        "Returns `{steer_count}` on success. Surfaces 429 with "
        "STEER_RATE_LIMITED so the caller can back off (per-job cap 20, "
        "per-caller 30/min)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Job ID returned by aztea_call_streaming.",
            },
            "message": {
                "type": "string",
                "description": "Free-form guidance for the agent.",
                "minLength": 1,
            },
        },
        "required": ["job_id", "message"],
    },
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
        "idempotentHint": False,
    },
}
