"""Serial pipeline execution over registered Aztea agents."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

import requests

from core import fastpath
from core import jobs
from core import payments
from core import registry
from core import url_security
from server import pricing_helpers

from . import db
from .resolver import resolve_input_map


def validate_definition(definition: dict) -> dict:
    normalized = dict(definition or {})
    raw_nodes = normalized.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("definition.nodes must be a non-empty array.")

    nodes: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw_node in raw_nodes:
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
        depends_on = [str(item or "").strip() for item in depends_on_raw if str(item or "").strip()]
        nodes.append(
            {
                "id": node_id,
                "agent_id": agent_id,
                "input_map": input_map,
                "depends_on": depends_on,
            }
        )

    known_ids = {node["id"] for node in nodes}
    for node in nodes:
        for dep in node["depends_on"]:
            if dep not in known_ids:
                raise ValueError(f"Pipeline node '{node['id']}' depends on unknown node '{dep}'.")

    indegree: dict[str, int] = {node["id"]: 0 for node in nodes}
    outgoing: dict[str, list[str]] = {node["id"]: [] for node in nodes}
    node_map = {node["id"]: node for node in nodes}
    for node in nodes:
        for dep in node["depends_on"]:
            indegree[node["id"]] += 1
            outgoing[dep].append(node["id"])

    queue = deque(sorted([node_id for node_id, degree in indegree.items() if degree == 0]))
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

    terminal_nodes = [node["id"] for node in nodes if not outgoing[node["id"]]]
    return {"nodes": nodes, "ordered_nodes": ordered, "terminal_nodes": terminal_nodes}


def _agent_price_and_distribution(agent: dict, payload: dict) -> tuple[int, dict, dict]:
    estimate = pricing_helpers.estimate_variable_charge(agent=agent, payload=payload)
    price_cents = int(estimate["price_cents"])
    distribution = payments.compute_success_distribution(
        price_cents,
        platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
        fee_bearer_policy="caller",
    )
    return price_cents, estimate, distribution


def _invoke_agent(
    *,
    agent: dict,
    payload: dict,
    caller_owner_id: str,
    caller_wallet_id: str,
    client_id: str | None,
    execute_builtin_agent: Callable[[str, dict[str, Any]], dict] | None,
) -> dict:
    price_cents, estimate, distribution = _agent_price_and_distribution(agent, payload)
    caller_charge_cents = int(distribution["caller_charge_cents"])
    caller_wallet = payments.get_wallet(caller_wallet_id) or payments.get_or_create_wallet(caller_owner_id)
    agent_wallet = payments.get_or_create_wallet(f"agent:{agent['agent_id']}")
    platform_wallet = payments.get_or_create_wallet(payments.PLATFORM_OWNER_ID)
    charge_tx_id = payments.pre_call_charge(caller_wallet["wallet_id"], caller_charge_cents, agent["agent_id"])
    job = jobs.create_job(
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
    started_at = time.monotonic()
    try:
        matched_local, local_output = fastpath.run_local_agent(
            agent,
            payload,
            execute_builtin_agent=execute_builtin_agent,
        )
        if matched_local:
            output = local_output
        else:
            endpoint_url = str(agent.get("endpoint_url") or "").strip()
            safe_url = url_security.validate_outbound_url(endpoint_url, "endpoint_url")
            response = requests.post(
                safe_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=120,
                allow_redirects=False,
            )
            if not response.ok:
                raise RuntimeError(f"Agent endpoint returned HTTP {response.status_code}.")
            try:
                output = response.json()
            except ValueError as exc:
                raise RuntimeError("Agent returned malformed JSON.") from exc
        if not isinstance(output, dict):
            output = {"output": output}
        jobs.update_job_status(job["job_id"], "complete", output_payload=output, completed=True)
        payments.post_call_payout(
            agent_wallet["wallet_id"],
            platform_wallet["wallet_id"],
            charge_tx_id,
            price_cents,
            agent["agent_id"],
            platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
            fee_bearer_policy="caller",
        )
        jobs.mark_settled(job["job_id"])
        registry.update_call_stats(
            agent["agent_id"],
            latency_ms=(time.monotonic() - started_at) * 1000.0,
            success=True,
            price_cents=price_cents,
        )
        pricing_helpers.maybe_refund_pricing_diff(
            agent=agent,
            payload=payload,
            output=output,
            caller_wallet_id=caller_wallet["wallet_id"],
            agent_wallet_id=agent_wallet["wallet_id"],
            platform_wallet_id=platform_wallet["wallet_id"],
            charge_tx_id=charge_tx_id,
            estimate=estimate,
            caller_charge_cents=caller_charge_cents,
            success_distribution=distribution,
            platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
            fee_bearer_policy="caller",
        )
        return output
    except Exception:
        jobs.update_job_status(job["job_id"], "failed", error_message="Pipeline step failed.", completed=True)
        payments.post_call_refund(caller_wallet["wallet_id"], charge_tx_id, caller_charge_cents, agent["agent_id"])
        jobs.mark_settled(job["job_id"])
        registry.update_call_stats(
            agent["agent_id"],
            latency_ms=(time.monotonic() - started_at) * 1000.0,
            success=False,
            price_cents=price_cents,
        )
        raise


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
    validated = validate_definition(pipeline.get("definition") or {})
    step_results: dict[str, Any] = {}
    try:
        for node in validated["ordered_nodes"]:
            payload = resolve_input_map(node["input_map"], input_payload, step_results)
            agent = registry.get_agent(node["agent_id"], include_unapproved=True)
            if agent is None:
                raise ValueError(f"Pipeline node '{node['id']}' agent '{node['agent_id']}' was not found.")
            output = _invoke_agent(
                agent=agent,
                payload=payload,
                caller_owner_id=caller_owner_id,
                caller_wallet_id=caller_wallet_id,
                client_id=client_id,
                execute_builtin_agent=execute_builtin_agent,
            )
            step_results[node["id"]] = output
            db.update_run_step(run_id, node["id"], output)
        terminal_nodes = validated["terminal_nodes"]
        if len(terminal_nodes) == 1:
            final_output = step_results.get(terminal_nodes[0])
        else:
            final_output = {node_id: step_results.get(node_id) for node_id in terminal_nodes}
        db.complete_run(run_id, final_output)
    except Exception as exc:
        # Include the exception class so the error_message is self-explanatory
        # in the run record (e.g. "ValueError: Pipeline node 'step1' ...").
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
