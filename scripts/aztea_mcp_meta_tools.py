"""
aztea_mcp_meta_tools.py — Platform meta-tools exposed via MCP.

These tools wrap Aztea's wallet, async job lifecycle, rating/dispute, discovery,
and batch-hiring APIs. Unlike registry agent tools (which call 3rd-party workers),
these are always present when authenticated and talk directly to the Aztea platform.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

_REQUEST_VERSION_HEADER = "X-Aztea-Version"
_CLIENT_ID_HEADER = "X-Aztea-Client"
_AZTEA_PROTOCOL_VERSION = "1.0"
_DEFAULT_CLIENT_ID = (os.environ.get("AZTEA_CLIENT_ID", "claude-code") or "claude-code").strip()


def _annotations(*, read_only: bool, destructive: bool = False, open_world: bool = True, idempotent: bool = False) -> dict[str, Any]:
    return {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "openWorldHint": open_world,
        "idempotentHint": idempotent,
    }

# ─── Tool schemas ────────────────────────────────────────────────────────────

_TOOLS: list[dict[str, Any]] = [
    # 1.2 Wallet & budget
    {
        "name": "aztea_wallet_balance",
        "description": (
            "Return your current Aztea wallet balance and recent transaction history. "
            "Call this before long-running or expensive workflows to confirm you have enough credit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "aztea_spend_summary",
        "description": (
            "Return a breakdown of your Aztea spending over a time period — total cost, job count, "
            "and cost split by agent. Useful for auditing workflow cost before committing to more work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "Time window: '1d', '7d', '30d', or '90d'. Default: '7d'.",
                    "enum": ["1d", "7d", "30d", "90d"],
                    "default": "7d",
                },
            },
            "required": [],
        },
    },
    {
        "name": "aztea_set_daily_limit",
        "description": (
            "Set a rolling 24-hour spend cap on your Aztea wallet in cents (100 = $1.00). "
            "Use this to guard against runaway orchestration loops. Pass 0 to remove the limit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit_cents": {
                    "type": "integer",
                    "description": "Daily cap in cents (0 = no limit, max 1 000 000 = $10 000).",
                    "minimum": 0,
                    "maximum": 1000000,
                },
            },
            "required": ["limit_cents"],
        },
    },
    {
        "name": "aztea_topup_url",
        "description": (
            "Generate a Stripe Checkout URL to top up your Aztea wallet. "
            "Returns a checkout_url the user can open in a browser to add credit. "
            "Amount must be between $1.00 and $500.00 (100–50 000 cents)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount_cents": {
                    "type": "integer",
                    "description": "Amount to add in cents (e.g. 500 = $5.00). Min 100 ($1), max 50 000 ($500).",
                    "minimum": 100,
                    "maximum": 50000,
                },
            },
            "required": ["amount_cents"],
        },
    },
    {
        "name": "aztea_session_summary",
        "description": (
            "Return today's Aztea spend alongside current wallet balance — a quick health check "
            "before a multi-agent workflow. Shows how much has been spent today and what is left."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "aztea_set_session_budget",
        "description": (
            "Set a soft spend cap (in cents) for the current MCP session. "
            "Once cumulative spending since session start reaches this cap, further "
            "tool calls that cost money are blocked with a warning. "
            "Pass 0 to clear the limit. Use this before starting expensive workflows "
            "to prevent runaway spend. Check current session spend with aztea_session_summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "budget_cents": {
                    "type": "integer",
                    "description": "Session spend ceiling in cents (0 = no limit). E.g. 500 = $5.00.",
                    "minimum": 0,
                },
            },
            "required": ["budget_cents"],
        },
    },
    {
        "name": "aztea_estimate_cost",
        "description": (
            "Preview the all-in caller charge and expected latency for an Aztea agent before hiring. "
            "Use this to compare agents, decide whether a task fits your budget, and avoid surprise spend."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "UUID of the agent to estimate.",
                },
                "input_payload": {
                    "type": "object",
                    "description": "Optional task input used for variable-pricing estimates.",
                    "additionalProperties": True,
                },
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "aztea_list_recipes",
        "description": (
            "List Aztea's built-in public recipes, including recipe IDs, descriptions, and default input schemas. "
            "Call this before aztea_run_recipe if you do not already know the recipe ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "aztea_list_pipelines",
        "description": (
            "List pipelines visible to you, including their IDs, names, descriptions, and DAG definitions. "
            "Use this to discover existing reusable workflows before creating or running a new one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # 1.3 Async jobs + clarification
    {
        "name": "aztea_hire_async",
        "description": (
            "Submit an async job to an Aztea marketplace agent and return immediately with a job_id. "
            "The agent works in the background; poll with aztea_job_status to get progress and results. "
            "Use this by default for long-running tasks, for work that may need clarification, or whenever you want to manage several agents without blocking on each call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "UUID of the registered Aztea agent to hire.",
                },
                "input_payload": {
                    "type": "object",
                    "description": "Task input matching the agent's input schema.",
                    "additionalProperties": True,
                },
                "callback_url": {
                    "type": "string",
                    "description": "Optional HTTPS URL to POST the result when the job completes.",
                },
                "max_attempts": {
                    "type": "integer",
                    "description": "Retry limit (1–10). Default: 3.",
                    "minimum": 1,
                    "maximum": 10,
                },
                "budget_cents": {
                    "type": "integer",
                    "description": "Optional ceiling in cents — job is rejected if agent price exceeds this.",
                    "minimum": 0,
                },
                "private_task": {
                    "type": "boolean",
                    "description": "If true, this job's output is not recorded as a public work example.",
                    "default": False,
                },
            },
            "required": ["agent_id", "input_payload"],
        },
    },
    {
        "name": "aztea_job_status",
        "description": (
            "Poll the status of an async job. Returns current status (pending/running/"
            "awaiting_clarification/complete/failed), the output payload when complete, "
            "and any messages (progress updates, clarification requests, partial results) "
            "posted since since_message_id. If the job is awaiting_clarification, "
            "read the clarification_request message and reply with aztea_clarify."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by aztea_hire_async or aztea_hire_batch.",
                },
                "since_message_id": {
                    "type": "integer",
                    "description": "Return only messages with id > this value. Use the last seen message id to avoid duplicates.",
                    "minimum": 0,
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "aztea_clarify",
        "description": (
            "Send a clarification response to an agent whose job is awaiting_clarification. "
            "The agent will resume running after receiving this message. "
            "Read the clarification_request from aztea_job_status first to know what to respond."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID of the job awaiting clarification.",
                },
                "message": {
                    "type": "string",
                    "description": "Your response to the agent's clarification question.",
                },
                "request_message_id": {
                    "type": "integer",
                    "description": (
                        "Optional clarification_request message id being answered. "
                        "If omitted, Aztea uses the latest open clarification request on the job."
                    ),
                    "minimum": 1,
                },
            },
            "required": ["job_id", "message"],
        },
    },
    # 1.4 Rating & dispute
    {
        "name": "aztea_rate_job",
        "description": (
            "Rate a completed Aztea job on a 1–5 star scale. "
            "Ratings improve the platform's trust scores and help other callers pick good agents. "
            "Call this after reviewing the output of a completed async job."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID of the completed job to rate.",
                },
                "rating": {
                    "type": "integer",
                    "description": "Star rating from 1 (poor) to 5 (excellent).",
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "required": ["job_id", "rating"],
        },
    },
    {
        "name": "aztea_dispute_job",
        "description": (
            "File a dispute for a completed job whose output was incorrect, incomplete, or harmful. "
            "An LLM judge reviews the evidence and may issue a full or partial refund. "
            "Use when aztea_verify_output is past its window or when there is a clear factual error."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID of the job to dispute.",
                },
                "reason": {
                    "type": "string",
                    "description": "Clear description of what went wrong with the agent's output.",
                },
                "evidence": {
                    "type": "string",
                    "description": "Optional URL or text snippet supporting the dispute.",
                },
            },
            "required": ["job_id", "reason"],
        },
    },
    {
        "name": "aztea_verify_output",
        "description": (
            "Accept or reject a job's output within the verification window (default 24h after completion). "
            "Rejecting triggers an immediate refund without needing a full dispute. "
            "Accepting releases payment to the agent. If you do nothing, payment releases automatically when the window expires."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID of the completed job to verify.",
                },
                "decision": {
                    "type": "string",
                    "description": "'accept' to release payment, 'reject' to trigger a refund.",
                    "enum": ["accept", "reject"],
                },
                "reason": {
                    "type": "string",
                    "description": "Required when decision is 'reject'. Explain why the output is unacceptable.",
                },
            },
            "required": ["job_id", "decision"],
        },
    },
    # 1.5 Discovery
    {
        "name": "aztea_discover",
        "description": (
            "Semantic search for Aztea agents by task description. "
            "Returns ranked candidates with trust scores, pricing, and match explanations. "
            "Use this before hiring to find the best agent for an unfamiliar task, "
            "or to filter by trust threshold or price cap."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description of the task you want to accomplish.",
                },
                "min_trust_score": {
                    "type": "number",
                    "description": "Only return agents with trust_score >= this value (0–100). Default: 0.",
                    "minimum": 0,
                    "maximum": 100,
                },
                "max_price_cents": {
                    "type": "integer",
                    "description": "Only return agents charging <= this price in cents per call.",
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (1–20). Default: 5.",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "aztea_get_examples",
        "description": (
            "Fetch public work examples for a specific Aztea agent. "
            "Examples show real inputs and outputs from past jobs, letting you verify "
            "an agent produces the quality and format you expect before hiring."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "UUID of the agent whose examples to fetch.",
                },
            },
            "required": ["agent_id"],
        },
    },
    # 1.6 Batch hiring
    {
        "name": "aztea_hire_batch",
        "description": (
            "Submit up to 50 async jobs in one atomic request with a single wallet debit. "
            "All jobs run in parallel. Returns a list of job_ids to poll individually with aztea_job_status. "
            "Use for workflows like 'review all 10 files' or 'audit every dependency' — "
            "far faster and cheaper than sequential hiring. This is the default tool for large independent task sets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "description": "List of job specs (max 50). Each spec must include agent_id and input_payload.",
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_id": {
                                "type": "string",
                                "description": "UUID of the Aztea agent to hire.",
                            },
                            "input_payload": {
                                "type": "object",
                                "description": "Task input matching the agent's input schema.",
                                "additionalProperties": True,
                            },
                            "budget_cents": {
                                "type": "integer",
                                "description": "Optional per-job price ceiling in cents.",
                                "minimum": 0,
                            },
                            "private_task": {
                                "type": "boolean",
                                "description": "If true, output is not recorded as a public work example.",
                                "default": False,
                            },
                        },
                        "required": ["agent_id", "input_payload"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["jobs"],
        },
    },
    {
        "name": "aztea_compare_agents",
        "description": (
            "Run the same task against 2-3 Aztea agents, wait for the compare session to finish, "
            "and return all results side by side. Use this before choosing a single winner to pay. "
            "If the compare is still running when wait_seconds expires, poll it with aztea_compare_status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 3,
                    "description": "2-3 unique agent IDs to compare.",
                },
                "input_payload": {
                    "type": "object",
                    "description": "Shared task input sent to every compared agent.",
                    "additionalProperties": True,
                },
                "max_attempts": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Optional retry limit per job. Default: 3.",
                },
                "private_task": {
                    "type": "boolean",
                    "description": "If true, suppress public work-example recording for these jobs.",
                    "default": False,
                },
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "description": "How long to poll for results before returning partial status. Default: 30.",
                },
                "poll_interval_seconds": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 10,
                    "description": "Polling interval while waiting for results. Default: 2.",
                },
            },
            "required": ["agent_ids", "input_payload"],
        },
    },
    {
        "name": "aztea_compare_status",
        "description": (
            "Poll an existing Aztea compare session by compare_id. "
            "Use this after aztea_compare_agents if the initial wait window expires; do not start a new compare just to poll."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "compare_id": {
                    "type": "string",
                    "description": "Compare session ID returned by aztea_compare_agents.",
                },
            },
            "required": ["compare_id"],
        },
    },
    {
        "name": "aztea_select_compare_winner",
        "description": (
            "Finalize a compare session by selecting the winning agent. "
            "Aztea pays only the winner and refunds the completed non-winners in full."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "compare_id": {
                    "type": "string",
                    "description": "Compare session ID returned by aztea_compare_agents.",
                },
                "winner_agent_id": {
                    "type": "string",
                    "description": "The chosen winning agent from that compare session.",
                },
            },
            "required": ["compare_id", "winner_agent_id"],
        },
    },
    {
        "name": "aztea_run_pipeline",
        "description": (
            "Run an Aztea pipeline DAG, wait for the run to finish, and return the final output plus step results. "
            "Use this for repeatable multi-agent workflows that Aztea orchestrates on the platform side. "
            "If the run is still executing when wait_seconds expires, poll it with aztea_pipeline_status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pipeline_id": {
                    "type": "string",
                    "description": "Pipeline ID returned by /pipelines or listed in the Aztea API.",
                },
                "input_payload": {
                    "type": "object",
                    "description": "Pipeline input object.",
                    "additionalProperties": True,
                },
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "description": "How long to poll before returning an in-progress run. Default: 30.",
                },
                "poll_interval_seconds": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 10,
                    "description": "Polling interval while waiting for the run to complete. Default: 2.",
                },
            },
            "required": ["pipeline_id", "input_payload"],
        },
    },
    {
        "name": "aztea_pipeline_status",
        "description": (
            "Poll an existing pipeline or recipe run by pipeline_id and run_id. "
            "Use this after aztea_run_pipeline or aztea_run_recipe if the initial wait window expires; do not start a new run just to poll."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pipeline_id": {
                    "type": "string",
                    "description": "Pipeline ID or recipe-backed pipeline ID.",
                },
                "run_id": {
                    "type": "string",
                    "description": "Run ID returned by aztea_run_pipeline or aztea_run_recipe.",
                },
            },
            "required": ["pipeline_id", "run_id"],
        },
    },
    {
        "name": "aztea_run_recipe",
        "description": (
            "Run one of Aztea's curated public recipes and wait for the run to finish. "
            "Recipes are built-in pipeline templates for common coding workflows. "
            "Call aztea_list_recipes first if you do not already know the recipe ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe_id": {
                    "type": "string",
                    "description": "Recipe ID from GET /recipes, such as 'review-and-test'.",
                },
                "recipe_name": {
                    "type": "string",
                    "description": "Optional recipe name alias. If recipe_id is omitted, Aztea uses this value.",
                },
                "input_payload": {
                    "type": "object",
                    "description": "Recipe input object.",
                    "additionalProperties": True,
                },
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "description": "How long to poll before returning an in-progress run. Default: 30.",
                },
                "poll_interval_seconds": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 10,
                    "description": "Polling interval while waiting for the run to complete. Default: 2.",
                },
            },
            "required": ["input_payload"],
        },
    },
]

META_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in _TOOLS)

_META_TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "aztea_wallet_balance": _annotations(read_only=True, idempotent=True),
    "aztea_spend_summary": _annotations(read_only=True, idempotent=True),
    "aztea_set_daily_limit": _annotations(read_only=False, idempotent=True),
    "aztea_topup_url": _annotations(read_only=False, idempotent=False),
    "aztea_session_summary": _annotations(read_only=True, idempotent=False),
    "aztea_set_session_budget": _annotations(read_only=False, idempotent=True, open_world=False),
    "aztea_estimate_cost": _annotations(read_only=True, idempotent=False),
    "aztea_list_recipes": _annotations(read_only=True, idempotent=True),
    "aztea_list_pipelines": _annotations(read_only=True, idempotent=True),
    "aztea_hire_async": _annotations(read_only=False, idempotent=False),
    "aztea_job_status": _annotations(read_only=True, idempotent=False),
    "aztea_clarify": _annotations(read_only=False, idempotent=False),
    "aztea_rate_job": _annotations(read_only=False, idempotent=False),
    "aztea_dispute_job": _annotations(read_only=False, idempotent=False),
    "aztea_verify_output": _annotations(read_only=False, idempotent=False),
    "aztea_discover": _annotations(read_only=True, idempotent=True),
    "aztea_get_examples": _annotations(read_only=True, idempotent=True),
    "aztea_hire_batch": _annotations(read_only=False, idempotent=False),
    "aztea_compare_agents": _annotations(read_only=False, idempotent=False),
    "aztea_compare_status": _annotations(read_only=True, idempotent=False),
    "aztea_select_compare_winner": _annotations(read_only=False, idempotent=False),
    "aztea_run_pipeline": _annotations(read_only=False, idempotent=False),
    "aztea_pipeline_status": _annotations(read_only=True, idempotent=False),
    "aztea_run_recipe": _annotations(read_only=False, idempotent=False),
}


def get_meta_tools() -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for tool in _TOOLS:
        item = dict(tool)
        item["annotations"] = dict(_META_TOOL_ANNOTATIONS.get(item["name"], _annotations(read_only=False)))
        enriched.append(item)
    return enriched


# ─── Dispatcher ──────────────────────────────────────────────────────────────

def call_meta_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    timeout: float,
    session: requests.Session,
    session_state: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Route a meta-tool call to the appropriate Aztea API endpoint.

    session_state is a mutable dict shared across all calls in this MCP session:
      - "budget_cents": int | None  — cap set by aztea_set_session_budget
      - "spent_cents":  int         — accumulated paid spend this session

    Returns (ok, result_dict). On HTTP errors the dict contains an 'error' key.
    """
    hdrs = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        _REQUEST_VERSION_HEADER: _AZTEA_PROTOCOL_VERSION,
        _CLIENT_ID_HEADER: _DEFAULT_CLIENT_ID,
    }
    base = base_url.rstrip("/")

    # aztea_set_session_budget: pure client-side state change, no API call needed
    if tool_name == "aztea_set_session_budget":
        budget = int(arguments.get("budget_cents") or 0)
        session_state["budget_cents"] = budget if budget > 0 else None
        spent = int(session_state.get("spent_cents") or 0)
        msg = (
            f"Session budget set to ${budget / 100:.2f}. "
            f"Current session spend: ${spent / 100:.2f}."
        ) if budget > 0 else "Session budget cleared."
        return True, {"budget_cents": budget or None, "spent_cents": spent, "message": msg}

    # Session budget gate — block paid calls when cap is exhausted
    budget_cents = session_state.get("budget_cents")
    if budget_cents is not None:
        spent = int(session_state.get("spent_cents") or 0)
        if spent >= budget_cents:
            return False, {
                "error": "SESSION_BUDGET_EXCEEDED",
                "message": (
                    f"Session budget of ${budget_cents / 100:.2f} reached "
                    f"(spent ${spent / 100:.2f}). "
                    "Raise the limit with aztea_set_session_budget or check "
                    "spend with aztea_session_summary."
                ),
                "budget_cents": budget_cents,
                "spent_cents": spent,
            }

    try:
        if tool_name == "aztea_wallet_balance":
            return _wallet_balance(session, base, hdrs, timeout)
        if tool_name == "aztea_spend_summary":
            return _spend_summary(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_set_daily_limit":
            return _set_daily_limit(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_topup_url":
            return _topup_url(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_session_summary":
            return _session_summary(session, base, hdrs, timeout, session_state)
        if tool_name == "aztea_estimate_cost":
            return _estimate_cost(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_list_recipes":
            return _list_recipes(session, base, hdrs, timeout)
        if tool_name == "aztea_list_pipelines":
            return _list_pipelines(session, base, hdrs, timeout)
        if tool_name == "aztea_hire_async":
            ok, result = _hire_async(session, base, hdrs, timeout, arguments)
            if ok:
                _accrue(session_state, result.get("caller_charge_cents", result.get("price_cents")))
            return ok, result
        if tool_name == "aztea_job_status":
            return _job_status(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_clarify":
            return _clarify(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_rate_job":
            return _rate_job(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_dispute_job":
            return _dispute_job(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_verify_output":
            return _verify_output(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_discover":
            return _discover(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_get_examples":
            return _get_examples(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_hire_batch":
            ok, result = _hire_batch(session, base, hdrs, timeout, arguments)
            if ok:
                _accrue(session_state, result.get("total_price_cents"))
            return ok, result
        if tool_name == "aztea_compare_agents":
            ok, result = _compare_agents(session, base, hdrs, timeout, arguments)
            if ok:
                _accrue(session_state, result.get("total_charged_cents"))
            return ok, result
        if tool_name == "aztea_compare_status":
            return _compare_status(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_select_compare_winner":
            return _select_compare_winner(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_run_pipeline":
            return _run_pipeline(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_pipeline_status":
            return _pipeline_status(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_run_recipe":
            return _run_recipe(session, base, hdrs, timeout, arguments)
    except requests.RequestException as exc:
        return False, {"error": "NETWORK_ERROR", "message": str(exc)}
    except Exception as exc:
        return False, {"error": "META_TOOL_ERROR", "message": str(exc)}

    return False, {"error": "UNKNOWN_META_TOOL", "tool": tool_name}


def _accrue(session_state: dict[str, Any], amount_cents: Any) -> None:
    if amount_cents is not None:
        session_state["spent_cents"] = int(session_state.get("spent_cents") or 0) + int(amount_cents)


# ─── Handlers ────────────────────────────────────────────────────────────────

def _get(session: requests.Session, url: str, hdrs: dict, timeout: float, **kwargs: Any) -> tuple[bool, dict]:
    r = session.get(url, headers=hdrs, timeout=timeout, **kwargs)
    return _parse(r)


def _post(session: requests.Session, url: str, hdrs: dict, timeout: float, body: Any) -> tuple[bool, dict]:
    r = session.post(url, headers=hdrs, timeout=timeout, json=body)
    return _parse(r)


def _parse(r: requests.Response) -> tuple[bool, dict]:
    try:
        body = r.json()
    except Exception:
        body = {"raw_body": r.text}
    if r.ok:
        return True, body if isinstance(body, dict) else {"result": body}
    detail = body if isinstance(body, dict) else {"detail": body}
    detail.setdefault("status_code", r.status_code)
    if isinstance(body, dict):
        nested = body.get("detail") if isinstance(body.get("detail"), dict) else None
        nested_data = nested.get("data") if isinstance(nested, dict) and isinstance(nested.get("data"), dict) else None
        if nested and "message" in nested and "message" not in detail:
            detail["message"] = nested["message"]
        for source in (body, nested or {}, nested_data or {}):
            for key in ("refunded", "refund_amount_cents", "cost_usd", "wallet_balance_cents"):
                if key in source and key not in detail:
                    detail[key] = source[key]
    return False, {"error": "API_ERROR", **detail}


def _wallet_balance(session: requests.Session, base: str, hdrs: dict, timeout: float) -> tuple[bool, dict]:
    return _get(session, f"{base}/wallets/me", hdrs, timeout)


def _spend_summary(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    period = str(args.get("period") or "7d")
    if period not in ("1d", "7d", "30d", "90d"):
        period = "7d"
    return _get(session, f"{base}/wallets/spend-summary", hdrs, timeout, params={"period": period})


def _set_daily_limit(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    limit_cents = int(args.get("limit_cents") or 0)
    # API field is daily_spend_limit_cents; None clears the cap (0 maps to None)
    daily_limit = limit_cents if limit_cents > 0 else None
    return _post(session, f"{base}/wallets/me/daily-spend-limit", hdrs, timeout, {"daily_spend_limit_cents": daily_limit})


def _topup_url(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    amount_cents = int(args.get("amount_cents") or 500)
    if not (100 <= amount_cents <= 50000):
        return False, {"error": "INVALID_INPUT", "message": "amount_cents must be 100–50 000 ($1–$500)."}
    # The topup endpoint requires wallet_id from the caller's wallet
    ok_wallet, wallet = _get(session, f"{base}/wallets/me", hdrs, timeout)
    if not ok_wallet:
        return False, {"error": "WALLET_FETCH_FAILED", "message": "Could not retrieve your wallet to create the topup session.", **wallet}
    wallet_id = wallet.get("wallet_id")
    if not wallet_id:
        return False, {"error": "WALLET_FETCH_FAILED", "message": "wallet_id not found in wallet response."}
    ok, result = _post(session, f"{base}/wallets/topup/session", hdrs, timeout, {
        "wallet_id": wallet_id,
        "amount_cents": amount_cents,
    })
    if ok:
        result.setdefault("note", "Open checkout_url in a browser to complete payment.")
    return ok, result


def _session_summary(
    session: requests.Session, base: str, hdrs: dict, timeout: float, session_state: dict[str, Any]
) -> tuple[bool, dict]:
    ok_bal, balance = _get(session, f"{base}/wallets/me", hdrs, timeout)
    ok_spend, spend = _get(session, f"{base}/wallets/spend-summary", hdrs, timeout, params={"period": "1d"})
    result: dict[str, Any] = {}
    if ok_bal:
        result["balance_cents"] = balance.get("balance_cents")
        result["balance_usd"] = round(float(balance.get("balance_cents") or 0) / 100, 4)
    if ok_spend:
        result["today_spend_cents"] = spend.get("total_cents")
        result["today_jobs"] = spend.get("total_jobs")
        result["today_by_agent"] = spend.get("by_agent")
    # Include this-session spend tracking
    result["session_spent_cents"] = int(session_state.get("spent_cents") or 0)
    result["session_spent_usd"] = round(float(result["session_spent_cents"]) / 100, 4)
    budget = session_state.get("budget_cents")
    result["session_budget_cents"] = budget
    result["session_budget_usd"] = round(float(budget) / 100, 4) if budget is not None else None
    return True, result


def _estimate_cost(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    agent_id = str(args.get("agent_id") or "").strip()
    if not agent_id:
        return False, {"error": "INVALID_INPUT", "message": "agent_id is required."}
    body = args.get("input_payload") or {}
    if not isinstance(body, dict):
        return False, {"error": "INVALID_INPUT", "message": "input_payload must be an object when provided."}
    ok, result = _post(session, f"{base}/agents/{agent_id}/estimate", hdrs, timeout, body)
    if ok:
        result.setdefault("note", "This is a preview only. No charge has been applied.")
    return ok, result


def _list_recipes(session: requests.Session, base: str, hdrs: dict, timeout: float) -> tuple[bool, dict]:
    ok, result = _get(session, f"{base}/recipes", hdrs, timeout)
    if ok:
        recipes = result.get("recipes") or []
        result.setdefault("count", len(recipes))
        result.setdefault("note", "Use recipe_id with aztea_run_recipe to execute one of these workflows.")
    return ok, result


def _list_pipelines(session: requests.Session, base: str, hdrs: dict, timeout: float) -> tuple[bool, dict]:
    ok, result = _get(session, f"{base}/pipelines", hdrs, timeout)
    if ok:
        pipelines = result.get("pipelines") or []
        result.setdefault("count", len(pipelines))
        result.setdefault("note", "Use pipeline_id with aztea_run_pipeline to execute one of these workflows.")
    return ok, result


def _hire_async(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    agent_id = str(args.get("agent_id") or "").strip()
    if not agent_id:
        return False, {"error": "INVALID_INPUT", "message": "agent_id is required."}
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "input_payload": args.get("input_payload") or {},
    }
    if args.get("callback_url"):
        body["callback_url"] = str(args["callback_url"])
    if args.get("max_attempts") is not None:
        body["max_attempts"] = int(args["max_attempts"])
    if args.get("budget_cents") is not None:
        body["budget_cents"] = int(args["budget_cents"])
    if args.get("private_task") is not None:
        body["private_task"] = bool(args["private_task"])
    ok, result = _post(session, f"{base}/jobs", hdrs, timeout, body)
    if ok:
        result.setdefault("note", (
            f"Job submitted. Poll with aztea_job_status(job_id='{result.get('job_id', '')}') "
            "to track progress and retrieve results."
        ))
    return ok, result


def _job_status(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    ok_job, job = _get(session, f"{base}/jobs/{job_id}", hdrs, timeout)
    if not ok_job:
        return False, job
    result: dict[str, Any] = {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "agent_id": job.get("agent_id"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "price_cents": job.get("price_cents"),
        "output_payload": job.get("output_payload"),
        "error_message": job.get("error_message"),
        "output_verification_status": job.get("output_verification_status"),
        "output_verification_deadline_at": job.get("output_verification_deadline_at"),
    }
    # Fetch messages since given id
    since = args.get("since_message_id")
    msg_params: dict[str, Any] = {}
    if since is not None:
        msg_params["since"] = int(since)
    ok_msgs, msgs_body = _get(session, f"{base}/jobs/{job_id}/messages", hdrs, timeout, params=msg_params)
    if ok_msgs:
        messages = msgs_body.get("messages") or []
        result["messages"] = messages
        # Surface clarification requests prominently
        clarifications = [m for m in messages if m.get("type") == "clarification_request"]
        if clarifications:
            result["clarification_needed"] = clarifications[-1].get("payload", {})
            result["note"] = (
                "Agent is awaiting clarification. "
                "Call aztea_clarify(job_id=..., message=...) to respond and resume the job."
            )
    else:
        result["messages"] = []
    return True, result


def _clarify(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    message = str(args.get("message") or "").strip()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    if not message:
        return False, {"error": "INVALID_INPUT", "message": "message is required."}
    request_message_id = args.get("request_message_id")
    if request_message_id is None:
        ok_msgs, msgs_body = _get(session, f"{base}/jobs/{job_id}/messages", hdrs, timeout)
        if not ok_msgs:
            return False, {
                "error": "CLARIFICATION_LOOKUP_FAILED",
                "message": "Could not retrieve clarification requests for this job.",
                **msgs_body,
            }
        messages = msgs_body.get("messages") or []
        latest_request = next(
            (msg for msg in reversed(messages) if msg.get("type") == "clarification_request" and msg.get("message_id")),
            None,
        )
        if latest_request is None:
            return False, {
                "error": "INVALID_INPUT",
                "message": "No clarification_request message found for this job. Pass request_message_id explicitly if needed.",
            }
        request_message_id = latest_request["message_id"]
    body = {
        "type": "clarification_response",
        "payload": {"answer": message, "request_message_id": int(request_message_id)},
    }
    ok, result = _post(session, f"{base}/jobs/{job_id}/messages", hdrs, timeout, body)
    if ok:
        result.setdefault("note", "Clarification sent. The agent will resume shortly.")
    return ok, result


def _rate_job(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    rating = int(args.get("rating") or 0)
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    if rating < 1 or rating > 5:
        return False, {"error": "INVALID_INPUT", "message": "rating must be 1–5."}
    return _post(session, f"{base}/jobs/{job_id}/rating", hdrs, timeout, {"rating": rating})


def _dispute_job(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    if not reason:
        return False, {"error": "INVALID_INPUT", "message": "reason is required."}
    body: dict[str, Any] = {"reason": reason}
    if args.get("evidence"):
        body["evidence"] = str(args["evidence"])
    ok, result = _post(session, f"{base}/jobs/{job_id}/dispute", hdrs, timeout, body)
    if ok:
        result.setdefault("note", "Dispute filed. An LLM judge will review the evidence and determine the outcome.")
    return ok, result


def _verify_output(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    decision = str(args.get("decision") or "").strip().lower()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    if decision not in ("accept", "reject"):
        return False, {"error": "INVALID_INPUT", "message": "decision must be 'accept' or 'reject'."}
    body: dict[str, Any] = {"decision": decision}
    if args.get("reason"):
        body["reason"] = str(args["reason"])
    elif decision == "reject":
        return False, {"error": "INVALID_INPUT", "message": "reason is required when decision is 'reject'."}
    return _post(session, f"{base}/jobs/{job_id}/verification", hdrs, timeout, body)


def _discover(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    query = str(args.get("query") or "").strip()
    if not query:
        return False, {"error": "INVALID_INPUT", "message": "query is required."}
    body: dict[str, Any] = {"query": query}
    limit = args.get("limit")
    if limit is not None:
        body["limit"] = max(1, min(20, int(limit)))
    else:
        body["limit"] = 5
    min_trust = args.get("min_trust_score")
    if min_trust is not None:
        body["min_trust"] = float(min_trust) / 100.0  # API takes 0-1 fraction
    max_price = args.get("max_price_cents")
    if max_price is not None:
        body["max_price_cents"] = int(max_price)
    ok, result = _post(session, f"{base}/registry/search", hdrs, timeout, body)
    if ok and isinstance(result.get("results"), list):
        # Surface a concise summary per result
        compact = []
        for item in result["results"]:
            agent = item.get("agent") or {}
            compact.append({
                "agent_id": agent.get("agent_id"),
                "name": agent.get("name"),
                "description": (agent.get("description") or "")[:200],
                "price_per_call_usd": agent.get("price_per_call_usd"),
                "trust_score": agent.get("trust_score"),
                "success_rate": agent.get("success_rate"),
                "blended_score": item.get("blended_score"),
                "match_reasons": item.get("match_reasons"),
            })
        result["results"] = compact
    return ok, result


def _get_examples(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    agent_id = str(args.get("agent_id") or "").strip()
    if not agent_id:
        return False, {"error": "INVALID_INPUT", "message": "agent_id is required."}
    ok, agent = _get(session, f"{base}/registry/agents/{agent_id}", hdrs, timeout)
    if not ok:
        return False, agent
    examples = agent.get("output_examples") or []
    return True, {
        "agent_id": agent_id,
        "name": agent.get("name"),
        "example_count": len(examples),
        "examples": examples[:10],
        "note": (
            "These are real inputs and outputs from past jobs. "
            "Review them to verify the agent's quality before hiring."
        ) if examples else "No public work examples are available for this agent yet.",
    }


def _hire_batch(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    raw_jobs = args.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        return False, {"error": "INVALID_INPUT", "message": "jobs must be a non-empty array."}
    if len(raw_jobs) > 50:
        return False, {"error": "INVALID_INPUT", "message": "Batch size is limited to 50 jobs."}
    jobs_body = []
    for spec in raw_jobs:
        if not isinstance(spec, dict):
            return False, {"error": "INVALID_INPUT", "message": "Each job spec must be an object."}
        job: dict[str, Any] = {
            "agent_id": str(spec.get("agent_id") or ""),
            "input_payload": spec.get("input_payload") or {},
        }
        if spec.get("budget_cents") is not None:
            job["budget_cents"] = int(spec["budget_cents"])
        if spec.get("private_task") is not None:
            job["private_task"] = bool(spec["private_task"])
        jobs_body.append(job)
    ok, result = _post(session, f"{base}/jobs/batch", hdrs, timeout, {"jobs": jobs_body})
    if ok:
        job_ids = [j.get("job_id") for j in (result.get("jobs") or []) if isinstance(j, dict)]
        result.setdefault("note", (
            f"Batch of {len(jobs_body)} jobs submitted. "
            "Poll each job_id with aztea_job_status to track progress."
        ))
        result.setdefault("job_ids", job_ids)
    return ok, result


def _compare_agents(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    raw_agent_ids = args.get("agent_ids")
    if not isinstance(raw_agent_ids, list):
        return False, {"error": "INVALID_INPUT", "message": "agent_ids must be an array."}
    agent_ids = [str(item or "").strip() for item in raw_agent_ids if str(item or "").strip()]
    if len(agent_ids) < 2 or len(agent_ids) > 3:
        return False, {"error": "INVALID_INPUT", "message": "agent_ids must contain 2 or 3 values."}
    body: dict[str, Any] = {
        "agent_ids": agent_ids,
        "input_payload": args.get("input_payload") or {},
    }
    if not isinstance(body["input_payload"], dict):
        return False, {"error": "INVALID_INPUT", "message": "input_payload must be an object."}
    if args.get("max_attempts") is not None:
        body["max_attempts"] = int(args["max_attempts"])
    if args.get("private_task") is not None:
        body["private_task"] = bool(args["private_task"])
    ok, created = _post(session, f"{base}/jobs/compare", hdrs, timeout, body)
    if not ok:
        return False, created
    compare_id = str(created.get("compare_id") or "").strip()
    if not compare_id:
        return True, created
    wait_seconds = max(1, min(int(args.get("wait_seconds") or 30), 300))
    poll_interval = max(0.5, min(float(args.get("poll_interval_seconds") or 2.0), 10.0))
    deadline = time.monotonic() + wait_seconds
    latest = created
    while time.monotonic() < deadline:
        ok_status, status = _get(session, f"{base}/jobs/compare/{compare_id}", hdrs, timeout)
        if not ok_status:
            return False, status
        latest = status
        if str(status.get("status") or "").strip().lower() == "complete":
            latest.setdefault(
                "note",
                "Compare session completed. Review the results, then call aztea_select_compare_winner to finalize payment."
            )
            if created.get("total_charged_cents") is not None and latest.get("total_charged_cents") is None:
                latest["total_charged_cents"] = created.get("total_charged_cents")
            return True, latest
        time.sleep(poll_interval)
    latest.setdefault(
        "note",
        "Compare session is still running. Poll it with aztea_compare_status or wait longer with wait_seconds."
    )
    if created.get("total_charged_cents") is not None and latest.get("total_charged_cents") is None:
        latest["total_charged_cents"] = created.get("total_charged_cents")
    return True, latest


def _compare_status(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    compare_id = str(args.get("compare_id") or "").strip()
    if not compare_id:
        return False, {"error": "INVALID_INPUT", "message": "compare_id is required."}
    ok, result = _get(session, f"{base}/jobs/compare/{compare_id}", hdrs, timeout)
    if ok:
        status = str(result.get("status") or "").strip().lower()
        if status == "complete":
            result.setdefault(
                "note",
                "Compare session completed. Review the results, then call aztea_select_compare_winner to finalize payment."
            )
        elif status == "running":
            result.setdefault("note", "Compare session is still running.")
    return ok, result


def _select_compare_winner(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    compare_id = str(args.get("compare_id") or "").strip()
    winner_agent_id = str(args.get("winner_agent_id") or "").strip()
    if not compare_id:
        return False, {"error": "INVALID_INPUT", "message": "compare_id is required."}
    if not winner_agent_id:
        return False, {"error": "INVALID_INPUT", "message": "winner_agent_id is required."}
    ok, result = _post(
        session,
        f"{base}/jobs/compare/{compare_id}/select",
        hdrs,
        timeout,
        {"winner_agent_id": winner_agent_id},
    )
    if ok:
        result.setdefault("note", "Compare session finalized. Only the winner was paid.")
    return ok, result


def _poll_pipeline_run(
    session: requests.Session,
    base: str,
    hdrs: dict,
    timeout: float,
    *,
    pipeline_id: str,
    run_id: str,
    wait_seconds: int,
    poll_interval_seconds: float,
) -> tuple[bool, dict]:
    deadline = time.monotonic() + wait_seconds
    latest: dict[str, Any] = {"run_id": run_id, "pipeline_id": pipeline_id, "status": "running"}
    while time.monotonic() < deadline:
        ok_status, status = _get(session, f"{base}/pipelines/{pipeline_id}/runs/{run_id}", hdrs, timeout)
        if not ok_status:
            return False, status
        latest = status
        normalized = str(status.get("status") or "").strip().lower()
        if normalized in {"complete", "failed", "cancelled"}:
            if normalized == "complete":
                latest.setdefault("note", "Pipeline run completed.")
            elif normalized == "failed":
                latest.setdefault("note", "Pipeline run failed. Inspect error_message and step_results.")
            else:
                latest.setdefault("note", "Pipeline run was cancelled.")
            return True, latest
        time.sleep(poll_interval_seconds)
    latest.setdefault("note", "Pipeline run is still running. Poll it with aztea_pipeline_status or wait longer with wait_seconds.")
    return True, latest


def _pipeline_status(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    pipeline_id = str(args.get("pipeline_id") or "").strip()
    run_id = str(args.get("run_id") or "").strip()
    if not pipeline_id:
        return False, {"error": "INVALID_INPUT", "message": "pipeline_id is required."}
    if not run_id:
        return False, {"error": "INVALID_INPUT", "message": "run_id is required."}
    ok, result = _get(session, f"{base}/pipelines/{pipeline_id}/runs/{run_id}", hdrs, timeout)
    if ok:
        status = str(result.get("status") or "").strip().lower()
        if status == "complete":
            result.setdefault("note", "Pipeline run completed.")
        elif status == "failed":
            result.setdefault("note", "Pipeline run failed. Inspect error_message and step_results.")
        elif status == "running":
            result.setdefault("note", "Pipeline run is still running.")
    return ok, result


def _run_pipeline(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    pipeline_id = str(args.get("pipeline_id") or "").strip()
    if not pipeline_id:
        return False, {"error": "INVALID_INPUT", "message": "pipeline_id is required."}
    input_payload = args.get("input_payload")
    if not isinstance(input_payload, dict):
        return False, {"error": "INVALID_INPUT", "message": "input_payload must be an object."}
    ok, created = _post(
        session,
        f"{base}/pipelines/{pipeline_id}/run",
        hdrs,
        timeout,
        {"input_payload": input_payload},
    )
    if not ok:
        return False, created
    run_id = str(created.get("run_id") or "").strip()
    if not run_id:
        return True, created
    wait_seconds = max(1, min(int(args.get("wait_seconds") or 30), 300))
    poll_interval = max(0.5, min(float(args.get("poll_interval_seconds") or 2.0), 10.0))
    return _poll_pipeline_run(
        session,
        base,
        hdrs,
        timeout,
        pipeline_id=pipeline_id,
        run_id=run_id,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval,
    )


def _run_recipe(session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict) -> tuple[bool, dict]:
    recipe_id = str(args.get("recipe_id") or args.get("recipe_name") or "").strip()
    if not recipe_id:
        return False, {"error": "INVALID_INPUT", "message": "recipe_id or recipe_name is required."}
    input_payload = args.get("input_payload")
    if not isinstance(input_payload, dict):
        return False, {"error": "INVALID_INPUT", "message": "input_payload must be an object."}
    ok, created = _post(
        session,
        f"{base}/recipes/{recipe_id}/run",
        hdrs,
        timeout,
        {"input_payload": input_payload},
    )
    if not ok:
        return False, created
    pipeline_id = str(created.get("pipeline_id") or recipe_id).strip()
    run_id = str(created.get("run_id") or "").strip()
    if not run_id:
        return True, created
    wait_seconds = max(1, min(int(args.get("wait_seconds") or 30), 300))
    poll_interval = max(0.5, min(float(args.get("poll_interval_seconds") or 2.0), 10.0))
    ok_status, status = _poll_pipeline_run(
        session,
        base,
        hdrs,
        timeout,
        pipeline_id=pipeline_id,
        run_id=run_id,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval,
    )
    if ok_status:
        status.setdefault("recipe_id", recipe_id)
    return ok_status, status
