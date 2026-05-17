"""Serial pipeline execution over registered Aztea agents."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from core import crypto, fastpath, jobs, payments, registry, url_security
from core.functional import Err, Ok, Result
from server import pricing_helpers

from . import db
from .resolver import resolve_input_map

_LOG = logging.getLogger(__name__)


def _sign_pipeline_step_output(agent: dict, output: dict) -> dict[str, Any]:
    """Sign a pipeline-step output with the agent's Ed25519 key.

    Mirrors the sync (part_008) and async (part_009) signing paths so
    pipeline steps emit verifiable receipts identical to direct calls.
    The 2026-05-09 stress test caught this gap: 1 of 100 receipts in
    the 24h window was unsigned because pipeline steps called
    ``jobs.update_job_status`` without the signature kwargs. Signing
    must never break completion — any exception drops the signature
    and lets the job complete unsigned (better than no completion).
    """
    sig_b64: str | None = None
    sig_alg: str | None = None
    sig_did: str | None = None
    sig_at: str | None = None
    try:
        private_pem = agent.get("signing_private_key")
        agent_did_value = agent.get("did")
        if not private_pem or not agent_did_value:
            private_pem, _public_pem, agent_did_value = (
                registry.ensure_agent_signing_keys(agent.get("agent_id") or "")
            )
        if private_pem and agent_did_value and output is not None:
            sig_b64 = crypto.sign_payload(private_pem, output)
            sig_alg = str(agent.get("signing_alg") or "ed25519")
            sig_did = agent_did_value
            sig_at = datetime.now(timezone.utc).isoformat()
    except Exception:
        _LOG.exception(
            "Failed to sign pipeline-step output for agent %s",
            agent.get("agent_id"),
        )
        sig_b64 = sig_alg = sig_did = sig_at = None
    return {
        "output_signature": sig_b64,
        "output_signature_alg": sig_alg,
        "output_signed_by_did": sig_did,
        "output_signed_at": sig_at,
    }


def _normalize_pipeline_node(raw_node: Any, ids: set[str]) -> dict[str, Any]:
    """Pure: validate + shape one pipeline-node dict; mutates ``ids`` to track collisions."""
    if not isinstance(raw_node, dict):
        raise ValueError("Each pipeline node must be an object.")
    node_id = str(raw_node.get("id") or "").strip()
    agent_id = str(raw_node.get("agent_id") or raw_node.get("agent") or "").strip()
    if not node_id:
        raise ValueError("Each pipeline node requires an id.")
    if not agent_id:
        raise ValueError(f"Pipeline node '{node_id}' requires agent_id.")
    if node_id in ids:
        raise ValueError(f"Duplicate pipeline node id '{node_id}'.")
    ids.add(node_id)
    input_map = raw_node.get("input_map") or {}
    if not isinstance(input_map, dict):
        raise ValueError(f"Pipeline node '{node_id}' input_map must be an object.")
    depends_on_raw = raw_node.get("depends_on") or []
    if not isinstance(depends_on_raw, list):
        raise ValueError(f"Pipeline node '{node_id}' depends_on must be an array.")
    depends_on = [
        str(item or "").strip()
        for item in depends_on_raw
        if str(item or "").strip()
    ]
    return {
        "id": node_id,
        "agent_id": agent_id,
        "input_map": input_map,
        "depends_on": depends_on,
    }


def _check_dependencies_known(nodes: list[dict[str, Any]]) -> None:
    """Pure: every ``depends_on`` entry must reference a node id from this graph."""
    known_ids = {node["id"] for node in nodes}
    for node in nodes:
        for dep in node["depends_on"]:
            if dep not in known_ids:
                raise ValueError(
                    f"Pipeline node '{node['id']}' depends on unknown node '{dep}'."
                )


def _topological_sort(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure: Kahn's algorithm, deterministic by sorted node id; raises on cycle.

    Why: deterministic ordering keeps pipeline outputs stable across hosts
    even when dependency graphs have parallel branches.
    """
    indegree: dict[str, int] = {node["id"]: 0 for node in nodes}
    outgoing: dict[str, list[str]] = {node["id"]: [] for node in nodes}
    node_map = {node["id"]: node for node in nodes}
    for node in nodes:
        for dep in node["depends_on"]:
            indegree[node["id"]] += 1
            outgoing[dep].append(node["id"])
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    ordered: list[dict[str, Any]] = []
    while queue:
        node_id = queue.popleft()
        ordered.append(node_map[node_id])
        for child_id in outgoing[node_id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                queue.append(child_id)
    if len(ordered) != len(nodes):
        raise ValueError("Pipeline definition contains a cycle.")
    return ordered


def validate_definition(definition: dict) -> dict:
    """Pure: validate a pipeline definition and return the normalised form.

    Why: returns ``{nodes, ordered_nodes, terminal_nodes}`` so the caller
    has both raw nodes and a deterministic execution order; raises
    ``ValueError`` with a descriptive message on any violation.
    """
    raw_nodes = (definition or {}).get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("definition.nodes must be a non-empty array.")
    ids: set[str] = set()
    nodes = [_normalize_pipeline_node(raw, ids) for raw in raw_nodes]
    _check_dependencies_known(nodes)
    ordered = _topological_sort(nodes)
    outgoing_ids: set[str] = set()
    for node in nodes:
        for dep in node["depends_on"]:
            outgoing_ids.add(dep)
    terminal_nodes = [node["id"] for node in nodes if node["id"] not in outgoing_ids]
    return {"nodes": nodes, "ordered_nodes": ordered, "terminal_nodes": terminal_nodes}


def validate_definition_result(definition: dict) -> "Result[dict, str]":
    """Result-returning variant of :func:`validate_definition`.

    Returns ``Ok(normalised_definition)`` or ``Err(message)``.
    """
    try:
        return Ok(validate_definition(definition))
    except ValueError as exc:
        return Err(str(exc))


def _agent_price_and_distribution(agent: dict, payload: dict) -> tuple[int, dict, dict]:
    estimate = pricing_helpers.estimate_variable_charge(agent=agent, payload=payload)
    price_cents = int(estimate["price_cents"])
    distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
        fee_bearer_policy="caller",
    )
    return price_cents, estimate, distribution


def _is_unchargeable_degraded(agent: dict, output: dict) -> bool:
    endpoint = str(agent.get("endpoint_url") or "").strip()
    if endpoint not in {
        "internal://financial",
    }:
        return False
    if bool(output.get("degraded_chargeable")):
        return False
    return bool(output.get("degraded_mode")) and not bool(output.get("llm_used"))


def _output_has_error(output: dict) -> bool:
    error = output.get("error")
    return isinstance(error, dict) or isinstance(error, str)


def _pipeline_contradiction(step_results: dict[str, Any]) -> str | None:
    risky_analysis = False
    clean_review = False
    for result in step_results.values():
        if not isinstance(result, dict):
            continue
        risk_tags = result.get("risk_tags")
        has_risk_tag = isinstance(risk_tags, list) and bool(risk_tags)
        has_secret = bool(result.get("secret_pattern_added"))
        removed_error_handling = bool(result.get("error_handling_removed"))
        if has_risk_tag or has_secret or removed_error_handling:
            risky_analysis = True
        issues = result.get("issues")
        issue_count = result.get("issue_count")
        if issue_count is None and isinstance(issues, list):
            issue_count = len(issues)
        score = result.get("score") or result.get("quality_score")
        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            numeric_score = 0.0
        if issue_count == 0 and numeric_score >= 8:
            clean_review = True
    if risky_analysis and clean_review:
        return (
            "Pipeline contradiction: an earlier stage flagged security or "
            "correctness risk, but a later review stage returned a clean result."
        )
    return None


_MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MiB hard cap to keep a misbehaving agent from OOMing the pipeline
_AGENT_REQUEST_TIMEOUT_S = 120
_AGENT_STREAM_CHUNK_BYTES = 64 * 1024


def _stream_remote_agent_response(safe_url: str, payload: dict) -> dict:
    """Side-effect: POST to ``safe_url`` and stream the JSON response under the size cap.

    Why: streaming + Content-Length check stops OOM if a downstream agent
    returns a multi-GB body; redirects are disabled because pipelines must
    not silently follow a hijacked Location header.
    """
    with requests.post(
        safe_url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=_AGENT_REQUEST_TIMEOUT_S,
        allow_redirects=False,
        stream=True,
    ) as response:
        if not response.ok:
            raise RuntimeError(f"Agent endpoint returned HTTP {response.status_code}.")
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("Agent response exceeds size limit.")
        buf = bytearray()
        for chunk in response.iter_content(chunk_size=_AGENT_STREAM_CHUNK_BYTES):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > _MAX_RESPONSE_BYTES:
                raise RuntimeError("Agent response exceeds size limit.")
        try:
            return json.loads(buf.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("Agent returned malformed JSON.") from exc


def _fetch_agent_output(
    agent: dict, payload: dict,
    execute_builtin_agent: Callable[[str, dict[str, Any]], dict] | None,
) -> dict:
    """Side-effect: dispatch to the local fast-path or stream from the remote endpoint."""
    matched_local, local_output = fastpath.run_local_agent(
        agent, payload, execute_builtin_agent=execute_builtin_agent,
    )
    if matched_local:
        output = local_output
    else:
        endpoint_url = str(agent.get("endpoint_url") or "").strip()
        safe_url = url_security.validate_outbound_url(endpoint_url, "endpoint_url")
        output = _stream_remote_agent_response(safe_url, payload)
    if not isinstance(output, dict):
        output = {"output": output}
    if _output_has_error(output):
        raise RuntimeError("Agent returned an error envelope.")
    if _is_unchargeable_degraded(agent, output):
        raise RuntimeError("Agent returned unchargeable degraded fallback output.")
    return output


def _resolve_step_wallets(
    caller_owner_id: str, caller_wallet_id: str, agent_id: str,
) -> tuple[dict, dict, dict]:
    """Side-effect: load/create caller, agent, and platform wallets in one go."""
    caller_wallet = (
        payments.get_wallet(caller_wallet_id)
        or payments.get_or_create_wallet(caller_owner_id)
    )
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent_id}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    return caller_wallet, agent_wallet, platform_wallet


def _create_pipeline_step_job(
    agent: dict, payload: dict, *,
    caller_owner_id: str, client_id: str | None,
    caller_wallet: dict, agent_wallet: dict, platform_wallet: dict,
    price_cents: int, caller_charge_cents: int, charge_tx_id: str,
) -> dict:
    """Side-effect: ``jobs.create_job`` shaped specifically for a pipeline step."""
    return jobs.create_job(
        agent_id=agent["agent_id"],
        caller_owner_id=caller_owner_id,
        caller_wallet_id=caller_wallet["wallet_id"],
        agent_wallet_id=agent_wallet["wallet_id"],
        platform_wallet_id=platform_wallet["wallet_id"],
        price_cents=price_cents,
        caller_charge_cents=caller_charge_cents,
        platform_fee_pct_at_create=int(payments.PLATFORM_FEE_PCT),
        fee_bearer_policy="caller",
        client_id=client_id,
        charge_tx_id=charge_tx_id,
        input_payload=payload,
        agent_owner_id=agent.get("owner_id"),
        max_attempts=1,
        dispute_window_hours=1,
        output_verification_window_seconds=0,
    )


def _charge_pipeline_step(
    agent: dict, payload: dict, *,
    caller_owner_id: str, caller_wallet_id: str, client_id: str | None,
) -> dict[str, Any]:
    """Side-effect: pre-call charge + job creation. Returns the state needed to settle later.

    Why: bundling charge/job creation into one helper keeps the
    settlement-vs-charge symmetry obvious in ``_invoke_agent``.
    """
    price_cents, estimate, distribution = _agent_price_and_distribution(agent, payload)
    caller_charge_cents = int(distribution["caller_charge_cents"])
    caller_wallet, agent_wallet, platform_wallet = _resolve_step_wallets(
        caller_owner_id, caller_wallet_id, agent["agent_id"],
    )
    charge_tx_id = payments.pre_call_charge(
        caller_wallet["wallet_id"], caller_charge_cents, agent["agent_id"],
    )
    job = _create_pipeline_step_job(
        agent, payload,
        caller_owner_id=caller_owner_id, client_id=client_id,
        caller_wallet=caller_wallet, agent_wallet=agent_wallet,
        platform_wallet=platform_wallet,
        price_cents=price_cents, caller_charge_cents=caller_charge_cents,
        charge_tx_id=charge_tx_id,
    )
    return {
        "price_cents": price_cents,
        "estimate": estimate,
        "distribution": distribution,
        "caller_charge_cents": caller_charge_cents,
        "caller_wallet": caller_wallet,
        "agent_wallet": agent_wallet,
        "platform_wallet": platform_wallet,
        "charge_tx_id": charge_tx_id,
        "job": job,
    }


def _refund_pricing_diff_for_step(
    agent: dict, payload: dict, output: dict, state: dict[str, Any],
) -> None:
    """Side-effect: forward to ``pricing_helpers.maybe_refund_pricing_diff`` with the step's wallets."""
    pricing_helpers.maybe_refund_pricing_diff(
        agent=agent,
        payload=payload,
        output=output,
        caller_wallet_id=state["caller_wallet"]["wallet_id"],
        agent_wallet_id=state["agent_wallet"]["wallet_id"],
        platform_wallet_id=state["platform_wallet"]["wallet_id"],
        charge_tx_id=state["charge_tx_id"],
        estimate=state["estimate"],
        caller_charge_cents=state["caller_charge_cents"],
        success_distribution=state["distribution"],
        platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
        fee_bearer_policy="caller",
    )


def _settle_step_success(
    agent: dict, payload: dict, output: dict, *, state: dict[str, Any], started_at: float,
) -> None:
    """Side-effect: success path — sign output, payout, settle, update stats, refund pricing diff."""
    jobs.update_job_status(
        state["job"]["job_id"],
        "complete",
        output_payload=output,
        completed=True,
        **_sign_pipeline_step_output(agent, output),
    )
    payments.post_call_payout(
        state["agent_wallet"]["wallet_id"],
        state["platform_wallet"]["wallet_id"],
        state["charge_tx_id"],
        state["price_cents"],
        agent["agent_id"],
        platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
        fee_bearer_policy="caller",
    )
    jobs.mark_settled(state["job"]["job_id"])
    registry.update_call_stats(
        agent["agent_id"],
        latency_ms=(time.monotonic() - started_at) * 1000.0,
        success=True,
        price_cents=state["price_cents"],
    )
    _refund_pricing_diff_for_step(agent, payload, output, state)


def _settle_step_failure(
    agent: dict, *, state: dict[str, Any], started_at: float,
) -> None:
    """Side-effect: failure path — mark failed, refund the caller, update stats."""
    jobs.update_job_status(
        state["job"]["job_id"],
        "failed",
        error_message="Pipeline step failed.",
        completed=True,
    )
    payments.post_call_refund(
        state["caller_wallet"]["wallet_id"],
        state["charge_tx_id"],
        state["caller_charge_cents"],
        agent["agent_id"],
    )
    jobs.mark_settled(state["job"]["job_id"])
    registry.update_call_stats(
        agent["agent_id"],
        latency_ms=(time.monotonic() - started_at) * 1000.0,
        success=False,
        price_cents=state["price_cents"],
    )


def _invoke_agent(
    *,
    agent: dict,
    payload: dict,
    caller_owner_id: str,
    caller_wallet_id: str,
    client_id: str | None,
    execute_builtin_agent: Callable[[str, dict[str, Any]], dict] | None,
) -> dict:
    """Side-effect: orchestrate one pipeline step — charge, fetch, settle (success or refund).

    Why: split into charge/fetch/settle helpers so the money-flow ordering
    stays auditable; the invariant is "charge before fetch, settle exactly
    once after."
    """
    state = _charge_pipeline_step(
        agent, payload,
        caller_owner_id=caller_owner_id,
        caller_wallet_id=caller_wallet_id,
        client_id=client_id,
    )
    started_at = time.monotonic()
    try:
        output = _fetch_agent_output(agent, payload, execute_builtin_agent)
        _settle_step_success(agent, payload, output, state=state, started_at=started_at)
        return output
    except Exception:
        _settle_step_failure(agent, state=state, started_at=started_at)
        raise


def _reset_thread_db_state() -> None:
    """Best-effort rollback of any aborted transaction on the thread-local DB
    connection.

    Why: a step's DB-touching helper (charge, registry lookup, settlement)
    might raise after Postgres has started a transaction. The connection
    is then in ``InFailedSqlTransaction`` state and every subsequent
    DB call on the same thread fails with the canonical Postgres
    "current transaction is aborted, commands ignored until end of
    transaction block" message — *including* the next pipeline step's
    perfectly innocent ``registry.get_agent`` call. The 2026-05-17 test
    report observed this leaking out of the domain-health recipe.

    The Postgres pool in core.db already rolls back when it hands the
    connection out fresh, but in-flight pipeline code reuses the same
    connection across many calls. Calling rollback() between steps closes
    the gap. Safe on a clean connection (no-op).
    """
    try:
        from core import db as _db_module
        # core/db.py exposes a private _local for the thread-local connection.
        local = getattr(_db_module, "_local", None)
        wrapper = getattr(local, "conn", None) if local is not None else None
        if wrapper is None:
            return
        try:
            wrapper.rollback()
        except Exception as roll_exc:  # noqa: BLE001
            _LOG.warning(
                "pipelines.rollback_between_steps_failed: %s", roll_exc
            )
    except Exception as exc:  # noqa: BLE001 — never let cleanup raise
        _LOG.warning("pipelines.reset_thread_db_state_failed: %s", exc)


def _drive_pipeline_nodes(
    validated: dict, input_payload: dict, *,
    run_id: str, caller_owner_id: str, caller_wallet_id: str,
    client_id: str | None,
    execute_builtin_agent: Callable[[str, dict[str, Any]], dict] | None,
) -> dict[str, Any]:
    """Side-effect: invoke each pipeline node in topological order; returns ``step_results``.

    Each step's DB state is reset (rollback any aborted transaction) before
    the next step starts, so a single step's failure can't poison the
    thread's connection for the rest of the recipe. Closes the
    ``InFailedSqlTransaction`` leak observed in domain-health on
    2026-05-17.
    """
    step_results: dict[str, Any] = {}
    for node in validated["ordered_nodes"]:
        # Defensive: clear any aborted transaction from the prior step
        # before we read the next node's agent row. Pure no-op on a
        # clean connection.
        _reset_thread_db_state()
        payload = resolve_input_map(node["input_map"], input_payload, step_results)
        agent = registry.get_agent(node["agent_id"], include_unapproved=True)
        if agent is None:
            raise ValueError(
                f"Pipeline node '{node['id']}' agent '{node['agent_id']}' was not found."
            )
        try:
            output = _invoke_agent(
                agent=agent,
                payload=payload,
                caller_owner_id=caller_owner_id,
                caller_wallet_id=caller_wallet_id,
                client_id=client_id,
                execute_builtin_agent=execute_builtin_agent,
            )
        except Exception:
            # Roll back before bubbling so fail_run() in _execute_run can
            # write the run row without hitting InFailedSqlTransaction.
            _reset_thread_db_state()
            raise
        step_results[node["id"]] = output
        db.update_run_step(run_id, node["id"], output)
    return step_results


def _build_final_output(
    step_results: dict[str, Any], terminal_nodes: list[str],
) -> Any:
    """Pure: pick the single terminal output, or shape multi-terminal results into a dict."""
    if len(terminal_nodes) == 1:
        return step_results.get(terminal_nodes[0])
    return {node_id: step_results.get(node_id) for node_id in terminal_nodes}


def _execute_run(
    *,
    run_id: str,
    pipeline: dict,
    input_payload: dict,
    caller_owner_id: str,
    caller_wallet_id: str,
    client_id: str | None,
    execute_builtin_agent: Callable[[str, dict[str, Any]], dict] | None,
) -> None:
    """Side-effect: run a whole pipeline end-to-end; records final state on the run row.

    Why: the run record's error_message includes the exception class so
    operators can tell at a glance whether a failure was a ValueError
    (definition issue) or a RuntimeError (a step's agent rejected).
    """
    validated = validate_definition(pipeline.get("definition") or {})
    try:
        step_results = _drive_pipeline_nodes(
            validated, input_payload,
            run_id=run_id, caller_owner_id=caller_owner_id,
            caller_wallet_id=caller_wallet_id, client_id=client_id,
            execute_builtin_agent=execute_builtin_agent,
        )
        contradiction = _pipeline_contradiction(step_results)
        if contradiction:
            raise ValueError(contradiction)
        final_output = _build_final_output(step_results, validated["terminal_nodes"])
        db.complete_run(run_id, final_output)
    except Exception as exc:
        db.fail_run(run_id, f"{type(exc).__name__}: {exc}")


def run_pipeline(
    pipeline_id: str,
    input_payload: dict,
    caller_owner_id: str,
    caller_wallet_id: str,
    *,
    client_id: str | None = None,
    execute_builtin_agent: Callable[[str, dict[str, Any]], dict] | None = None,
) -> str:
    """Execute a pipeline step-by-step and return the run_id.

    Validates the pipeline definition, creates a run record, then calls each
    node's agent in DAG order, passing outputs forward as inputs to dependents.
    Returns the ``run_id`` of the created run record. Raises ``ValueError`` if
    the pipeline is not found or the definition fails validation.
    """
    pipeline = db.get_pipeline(pipeline_id)
    if pipeline is None:
        raise ValueError(f"Pipeline '{pipeline_id}' not found.")
    validated = validate_definition(pipeline.get("definition") or {})
    del validated
    created = db.create_run(pipeline_id, caller_owner_id, input_payload)
    thread = threading.Thread(
        target=_execute_run,
        kwargs={
            "run_id": created["run_id"],
            "pipeline": pipeline,
            "input_payload": input_payload,
            "caller_owner_id": caller_owner_id,
            "caller_wallet_id": caller_wallet_id,
            "client_id": client_id,
            "execute_builtin_agent": execute_builtin_agent,
        },
        name=f"aztea-pipeline-{created['run_id'][:8]}",
        daemon=True,
    )
    thread.start()
    return created["run_id"]
