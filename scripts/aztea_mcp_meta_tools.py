"""
aztea_mcp_meta_tools.py — Platform meta-tools exposed via MCP.

These tools wrap Aztea's wallet, async job lifecycle, rating/dispute, discovery,
and batch-hiring APIs. Unlike registry agent tools (which call 3rd-party workers),
these are always present when authenticated and talk directly to the Aztea platform.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

_REQUEST_VERSION_HEADER = "X-Aztea-Version"
_CLIENT_ID_HEADER = "X-Aztea-Client"
_AZTEA_PROTOCOL_VERSION = "1.0"
_DEFAULT_CLIENT_ID = (
    os.environ.get("AZTEA_CLIENT_ID", "claude-code") or "claude-code"
).strip()
_PYDANTIC_HELP_URL_RE = re.compile(
    r"\s*For further information visit https://errors\.pydantic\.dev/[^\s]+",
    re.IGNORECASE,
)
_DISCOVERY_INTENTS: dict[str, set[str]] = {
    "image": {
        "image",
        "generator",
        "generate",
        "picture",
        "png",
        "jpeg",
        "jpg",
        "visual",
        "art",
    },
    "browser": {
        "browser",
        "playwright",
        "screenshot",
        "crawl",
        "page",
        "dom",
        "headless",
    },
    "dns": {"dns", "ssl", "tls", "certificate", "domain", "http", "hsts"},
    "code_search": {"semantic", "codebase", "repo", "repository", "symbols"},
}


def _word_truncate(text: str, max_len: int, suffix: str = "…") -> str:
    """Trim ``text`` to at most ``max_len`` chars, breaking on a word boundary.

    Avoids the mid-word ellipsis ("…code-level f", "…claude-code ") that the
    2026-05-01 audit flagged. Returns the input unchanged if it is already short
    enough.
    """
    s = str(text or "")
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return s[:max_len]
    cutoff = max(0, max_len - len(suffix))
    head = s[:cutoff]
    last_space = head.rfind(" ")
    if last_space >= max(1, cutoff - 40):
        head = head[:last_space].rstrip(" ,;:.-—–")
    else:
        head = head.rstrip(" ,;:.-—–")
    return head + suffix


def _annotations(
    *,
    read_only: bool,
    destructive: bool = False,
    open_world: bool = True,
    idempotent: bool = False,
) -> dict[str, Any]:
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
            "Set a soft spend ceiling (in cents) for the current MCP session. "
            "Once cumulative spending since session start reaches this cap, further "
            "tool calls that cost money are blocked with a clear warning. "
            "Pass 0 to clear the cap. Use before starting expensive workflows to prevent "
            "runaway spend. Check current session spend with aztea_session_summary. "
            "The argument name is `budget_cents` — not `limit_cents`."
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
            "description": "Provide either agent_id (UUID) or slug (tool name like 'linter_agent').",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "UUID of the agent to estimate.",
                },
                "slug": {
                    "type": "string",
                    "description": "Slug / tool name (e.g. 'linter_agent'). Use this when you only have the slug from aztea_search.",
                },
                "input_payload": {
                    "type": "object",
                    "description": "Optional task input used for variable-pricing estimates. `input` is also accepted.",
                    "additionalProperties": True,
                },
                "input": {
                    "type": "object",
                    "description": "Alias for input_payload.",
                    "additionalProperties": True,
                },
            },
            "anyOf": [
                {"required": ["agent_id"]},
                {"required": ["slug"]},
            ],
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
    {
        "name": "aztea_list_agents",
        "description": (
            "List every public Aztea agent in one shot — slug, name, short "
            "description, category, price, trust score. Use this when a "
            "single aztea_search query won't surface what you need (e.g. "
            "browsing the marketplace, building a tool index, picking "
            "agents for a batch). Optionally filter by category."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional category filter, e.g. 'Security', 'Developer Tools', 'Research'.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 100,
                    "description": "Max agents to return.",
                },
            },
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
                "slug": {
                    "type": "string",
                    "description": "Agent slug returned by aztea_search. Prefer this when available.",
                },
                "input_payload": {
                    "type": "object",
                    "description": "Task input matching the agent's input schema. `input` is also accepted.",
                    "additionalProperties": True,
                },
                "input": {
                    "type": "object",
                    "description": "Alias for input_payload. Prefer this in grouped aztea_workflow calls.",
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
            "required": [],
            "anyOf": [{"required": ["agent_id"]}, {"required": ["slug"]}],
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
        "name": "aztea_batch_status",
        "description": (
            "Poll a parallel marketplace hire from aztea_hire_batch. Prefer batch_id; "
            "job_ids are accepted for older clients. Returns parallel_hire_trace so Claude can show which specialists were hired, settlement state, and receipt availability."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "batch_id": {
                    "type": "string",
                    "description": "Batch ID returned by aztea_hire_batch. Preferred.",
                },
                "job_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "description": "Job IDs returned by aztea_hire_batch.",
                },
                "since_message_id": {
                    "type": "integer",
                    "description": "Return only messages with id > this value for each job.",
                    "minimum": 0,
                },
            },
            "anyOf": [{"required": ["batch_id"]}, {"required": ["job_ids"]}],
        },
    },
    {
        "name": "aztea_cancel_job",
        "description": (
            "Abort an in-flight Aztea async job and refund any unsettled charge. "
            "Works for jobs in pending, running, or awaiting_clarification status. "
            "Use this when a long-running compare/arxiv/research run is no longer needed, "
            "or after a misconfigured job submission. Already-complete or already-failed jobs "
            "return a clear 409 instead of being silently re-cancelled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by aztea_hire_async or aztea_hire_batch.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional one-line reason recorded with the cancellation.",
                    "maxLength": 200,
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "aztea_follow_job",
        "description": (
            "Poll an async job until it reaches a terminal state (complete/failed/cancelled), "
            "then return the final result. Saves round-trips compared to calling aztea_job_status "
            "in a loop. Use this right after aztea_hire_async when you want to wait for the "
            "result inline. If the job is already terminal the call returns immediately. "
            "Maximum wait is 3 minutes (timeout_seconds default=180, max=300)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by aztea_hire_async.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Max seconds to wait before returning current status. Default 180, max 300.",
                    "minimum": 5,
                    "maximum": 300,
                    "default": 180,
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
            "Filtered registry discovery for Aztea agents by task description. "
            "Returns ranked candidates with trust scores, pricing, and match explanations, "
            "and suppresses low-relevance demo/toy agents. Prefer aztea_search for Claude routing; "
            "use this when you need trust or price filters."
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
        "name": "aztea_data_retention_policy",
        "description": (
            "Show the data-retention and privacy posture for a specific Aztea agent. "
            "Returns whether the agent is PII-safe, whether outputs are stored, whether "
            "calls are audit-logged, and any region-locking. Buyers handling secrets, "
            "regulated data, or production logs should call this before hiring."
        ),
        "input_schema": {
            "type": "object",
            "description": "Provide either agent_id (UUID) or slug (tool name).",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "UUID of the agent.",
                },
                "slug": {
                    "type": "string",
                    "description": "Slug / tool name. Resolves to agent_id via /registry/search.",
                },
            },
            "anyOf": [
                {"required": ["agent_id"]},
                {"required": ["slug"]},
            ],
        },
    },
    {
        "name": "aztea_verify_job",
        "description": (
            "Cryptographically verify a completed job's signed receipt against the agent's "
            "did:web document. Returns verified=true only if the agent's published Ed25519 "
            "public key signed exactly this output payload — the platform cannot have "
            "tampered with the result. Use this when audit/compliance/forensics matter, "
            "when re-presenting a result to a third party, or when paying out based on it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Completed job_id whose signature you want to verify.",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "aztea_get_examples",
        "description": (
            "Fetch public work examples for a specific Aztea agent. "
            "Examples show real inputs and outputs from past jobs, letting you verify "
            "an agent produces the quality and format you expect before hiring. "
            "Security-category agents (e.g. secret_scanner) never publish work examples to "
            "protect caller-submitted credentials."
        ),
        "input_schema": {
            "type": "object",
            "description": "Provide either agent_id (UUID) or slug (tool name).",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "UUID of the agent whose examples to fetch.",
                },
                "slug": {
                    "type": "string",
                    "description": "Slug / tool name (e.g. 'linter_agent'). Use when you only have the slug from aztea_search.",
                },
            },
            "anyOf": [
                {"required": ["agent_id"]},
                {"required": ["slug"]},
            ],
        },
    },
    # 1.6 Batch hiring
    {
        "name": "aztea_hire_batch",
        "description": (
            "Hire up to 50 independent marketplace specialists in parallel under one atomic batch rail. "
            "Aztea opens escrow per job, tracks settlement/refunds, and returns a visible parallel_hire_trace. "
            "Use this when a task naturally splits by file, package, endpoint, test case, or independent specialist role."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "One-line goal for the batch so Claude can explain why these specialists were hired.",
                },
                "max_total_cents": {
                    "type": "integer",
                    "description": "Hard total spend cap for the batch. Rejected before charge if exceeded.",
                    "minimum": 0,
                },
                "jobs": {
                    "type": "array",
                    "description": "List of job specs (max 50). Each spec must include agent_id or slug, plus input_payload.",
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_id": {
                                "type": "string",
                                "description": "UUID of the Aztea agent to hire.",
                            },
                            "slug": {
                                "type": "string",
                                "description": "Agent slug returned by aztea_search.",
                            },
                            "input_payload": {
                                "type": "object",
                                "description": "Task input matching the agent's input schema. `input` is also accepted.",
                                "additionalProperties": True,
                            },
                            "input": {
                                "type": "object",
                                "description": "Alias for input_payload.",
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
                        "required": [],
                        "anyOf": [{"required": ["agent_id"]}, {"required": ["slug"]}],
                        "additionalProperties": False,
                    },
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, validates jobs and returns estimated charges without opening escrow.",
                    "default": False,
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
            "Poll an existing pipeline or recipe run by run_id. "
            "Use this after aztea_run_pipeline or aztea_run_recipe if the initial wait window expires; "
            "do not start a new run just to poll. pipeline_id is optional — run_id alone is sufficient."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run ID returned by aztea_run_pipeline or aztea_run_recipe.",
                },
                "pipeline_id": {
                    "type": "string",
                    "description": "Optional. Pipeline ID — only needed for legacy callers; run_id alone resolves the pipeline.",
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "aztea_run_recipe",
        "description": (
            "Run one of Aztea's curated public recipes and wait for the run to finish. "
            "Recipes are built-in pipeline templates for common coding workflows. "
            "Call aztea_list_recipes first if you do not already know the recipe id. "
            "Pass the recipe identifier as `recipe_id` (preferred) — `recipe_name` is accepted "
            "as a deprecated alias for backward compatibility."
        ),
        "input_schema": {
            "type": "object",
            "description": "Provide `recipe_id` (the canonical identifier from /recipes). `recipe_name` is accepted as a legacy alias.",
            "properties": {
                "recipe_id": {
                    "type": "string",
                    "description": "Recipe identifier from GET /recipes, e.g. 'review-and-lint', 'modernize-python', 'audit-deps'. Same string accepted as recipe_name historically.",
                },
                "recipe_name": {
                    "type": "string",
                    "description": "Deprecated alias for recipe_id. Prefer recipe_id; both accept the same kebab-case identifiers.",
                    "deprecated": True,
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

# ─── Resource-grouped tools ─────────────────────────────────────────────────
#
# These three tools collapse 22 of the underlying meta-tools into a small,
# always-visible surface. Each takes an `action` enum that the dispatcher maps
# to the underlying meta-tool. The action's required arguments live alongside
# `action` in the same call — no nesting. Token cost is far lower than
# exposing 22 distinct tools, while every capability stays reachable.
#
# Mapping (grouped → underlying):
#   aztea_job(action=rate)           → aztea_rate_job
#   aztea_job(action=dispute)        → aztea_dispute_job
#   aztea_job(action=verify)         → aztea_verify_job
#   aztea_job(action=verify_output)  → aztea_verify_output
#   aztea_job(action=cancel)         → aztea_cancel_job
#   aztea_job(action=status)         → aztea_job_status
#   aztea_job(action=follow)         → aztea_follow_job
#   aztea_job(action=clarify)        → aztea_clarify
#   aztea_job(action=examples)       → aztea_get_examples
#
#   aztea_budget(action=balance)        → aztea_wallet_balance
#   aztea_budget(action=estimate)       → aztea_estimate_cost
#   aztea_budget(action=topup_url)      → aztea_topup_url
#   aztea_budget(action=set_daily_limit)→ aztea_set_daily_limit
#   aztea_budget(action=set_session_budget) → aztea_set_session_budget
#   aztea_budget(action=session_summary)→ aztea_session_summary
#   aztea_budget(action=spend_summary)  → aztea_spend_summary
#   aztea_budget(action=retention)      → aztea_data_retention_policy
#
#   aztea_workflow(action=hire_async)   → aztea_hire_async
#   aztea_workflow(action=hire_batch)   → aztea_hire_batch
#   aztea_workflow(action=batch_status) → aztea_batch_status
#   aztea_workflow(action=run_pipeline) → aztea_run_pipeline
#   aztea_workflow(action=pipeline_status)→ aztea_pipeline_status
#   aztea_workflow(action=run_recipe)   → aztea_run_recipe
#   aztea_workflow(action=list_pipelines)→ aztea_list_pipelines
#   aztea_workflow(action=list_recipes) → aztea_list_recipes
#   aztea_workflow(action=compare)      → aztea_compare_agents
#   aztea_workflow(action=compare_status)→ aztea_compare_status
#   aztea_workflow(action=compare_select)→ aztea_select_compare_winner

def _validate_grouped_action_inputs(
    tool_name: str, action: str, sub_args: dict[str, Any]
) -> tuple[bool, dict[str, Any] | None]:
    """Reject grouped-tool actions that the JSON schema can't constrain.

    Some grouped tools have actions that require fields the top-level schema
    can't enforce (e.g. `aztea_budget(action="estimate")` needs a slug or
    agent_id). Without this check the request reaches the server, which
    returns a terse 400. Catching it here lets us return a structured,
    Claude-readable hint that points to discovery.
    """
    if tool_name == "aztea_budget" and action == "estimate":
        if not str(sub_args.get("slug") or sub_args.get("agent_id") or "").strip():
            return False, {
                "error": "INVALID_INPUT",
                "message": (
                    "aztea_budget(action='estimate') requires `slug` or `agent_id`. "
                    "Estimate is per-agent so the platform can apply variable pricing."
                ),
                "required_one_of": ["slug", "agent_id"],
                "next_step": (
                    "Call aztea_search(query='...') to find the slug, then "
                    "aztea_budget(action='estimate', slug='<slug>', input={...})."
                ),
            }
    return True, None


_GROUPED_DISPATCH: dict[str, dict[str, str]] = {
    "aztea_job": {
        "rate": "aztea_rate_job",
        "dispute": "aztea_dispute_job",
        "dispute_status": "aztea_dispute_status",
        "verify": "aztea_verify_job",
        "verify_output": "aztea_verify_output",
        "full_output": "aztea_job_full_output",
        "cancel": "aztea_cancel_job",
        "status": "aztea_job_status",
        "follow": "aztea_follow_job",
        "clarify": "aztea_clarify",
        "examples": "aztea_get_examples",
    },
    "aztea_budget": {
        "balance": "aztea_wallet_balance",
        "estimate": "aztea_estimate_cost",
        "topup_url": "aztea_topup_url",
        "set_daily_limit": "aztea_set_daily_limit",
        "set_session_budget": "aztea_set_session_budget",
        "session_summary": "aztea_session_summary",
        "spend_summary": "aztea_spend_summary",
        "retention": "aztea_data_retention_policy",
    },
    "aztea_workflow": {
        "hire_async": "aztea_hire_async",
        "hire_batch": "aztea_hire_batch",
        "batch_status": "aztea_batch_status",
        "run_pipeline": "aztea_run_pipeline",
        "pipeline_status": "aztea_pipeline_status",
        "run_recipe": "aztea_run_recipe",
        "list_pipelines": "aztea_list_pipelines",
        "list_recipes": "aztea_list_recipes",
        "list_agents": "aztea_list_agents",
        "compare": "aztea_compare_agents",
        "compare_status": "aztea_compare_status",
        "compare_select": "aztea_select_compare_winner",
        "session_audit": "aztea_session_audit",
    },
}

GROUPED_TOOL_NAMES: frozenset[str] = frozenset(_GROUPED_DISPATCH.keys())


_GROUPED_TOOLS: list[dict[str, Any]] = [
    {
        "name": "aztea_job",
        "description": (
            "Post-call operations on an Aztea job. Pick action by what you need:\n"
            "  • rate(job_id, rating[1-5], comment?) — rate the agent's output, feeds trust signals.\n"
            "  • dispute(job_id, reason, evidence?) — open a dispute; clawback escrow.\n"
            "  • verify(job_id) — fetch the Ed25519-signed receipt to prove provenance.\n"
            "  • verify_output(job_id, accept|reject, reason?) — accept/reject inside the verification window.\n"
            "  • full_output(job_id, offset?=0, limit?=50000) — fetch the untruncated output in chunks. "
            "Returns {chunk, total_size, offset, next_offset, has_more}; pass next_offset back as offset until has_more=False, "
            "then json.loads(concatenated chunks) to reconstruct output_payload. limit is capped at 50000 chars.\n"
            "  • cancel(job_id) — abort a pending or running job and refund the pre-charge.\n"
            "  • status(job_id) — get current state of an async job.\n"
            "  • follow(job_id, max_wait_seconds?) — long-poll until the job terminates.\n"
            "  • clarify(job_id, response) — answer a clarification request from the agent.\n"
            "  • examples(slug, limit?) — fetch recent public work examples for an agent slug."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "rate",
                        "dispute",
                        "verify",
                        "verify_output",
                        "full_output",
                        "cancel",
                        "status",
                        "follow",
                        "clarify",
                        "examples",
                    ],
                    "description": "Which post-call operation to run.",
                },
                "job_id": {"type": "string", "description": "Job ID for job-targeted actions."},
                "rating": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "rate: integer 1-5.",
                },
                "comment": {"type": "string", "description": "rate: optional free-text feedback."},
                "reason": {"type": "string", "description": "dispute / verify_output reason."},
                "evidence": {"type": "string", "description": "dispute: optional evidence text."},
                "decision": {
                    "type": "string",
                    "enum": ["accept", "reject"],
                    "description": "verify_output: accept or reject the agent's output.",
                },
                "response": {
                    "type": "string",
                    "description": "clarify: free-text response to the agent's question.",
                },
                "slug": {"type": "string", "description": "examples: agent slug whose examples to fetch."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "examples: max examples to return (default 5).",
                },
                "max_wait_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                    "description": "follow: long-poll cap.",
                },
            },
            "required": ["action"],
            "additionalProperties": True,
        },
    },
    {
        "name": "aztea_budget",
        "description": (
            "Wallet, spend, and budget operations. Pick action by what you need:\n"
            "  • balance — current wallet balance + recent transactions.\n"
            "  • estimate(slug, input?) — pre-call cost estimate for a specific agent.\n"
            "  • topup_url(amount_cents) — Stripe Checkout URL to add credit ($1-$500).\n"
            "  • set_daily_limit(limit_cents) — rolling 24h spend cap (0 to clear).\n"
            "  • set_session_budget(budget_cents) — soft cap for this MCP session (0 to clear).\n"
            "  • session_summary — today's spend + remaining balance.\n"
            "  • spend_summary(period?) — breakdown over 1d|7d|30d|90d.\n"
            "  • retention — data retention policy for caller-supplied inputs/outputs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "balance",
                        "estimate",
                        "topup_url",
                        "set_daily_limit",
                        "set_session_budget",
                        "session_summary",
                        "spend_summary",
                        "retention",
                    ],
                    "description": "Which wallet/budget operation to run.",
                },
                "slug": {"type": "string", "description": "estimate (REQUIRED unless agent_id given) / retention (optional, omit for global policy): agent slug."},
                "agent_id": {"type": "string", "description": "estimate (REQUIRED unless slug given) / retention (optional): agent UUID."},
                "input": {
                    "type": "object",
                    "description": "estimate: optional input payload for variable-priced agents.",
                    "additionalProperties": True,
                },
                "amount_cents": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 50000,
                    "description": "topup_url: amount in cents.",
                },
                "limit_cents": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1000000,
                    "description": "set_daily_limit: cents (0 to clear).",
                },
                "budget_cents": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "set_session_budget: cents (0 to clear).",
                },
                "period": {
                    "type": "string",
                    "enum": ["1d", "7d", "30d", "90d"],
                    "description": "spend_summary: time window (default 7d).",
                },
            },
            "required": ["action"],
            "additionalProperties": True,
        },
    },
    {
        "name": "aztea_workflow",
        "description": (
            "Multi-call orchestration: async, batch, compare, pipelines, recipes. Pick action:\n"
            "  • hire_async(slug, input, ...) — fire-and-poll an agent for long jobs.\n"
            "  • hire_batch(jobs[]) — hire multiple agents in parallel.\n"
            "  • batch_status(batch_id) — progress of a batch.\n"
            "  • run_pipeline(pipeline_id, input_payload, ...) — execute a saved pipeline.\n"
            "  • pipeline_status(run_id) — pipeline run progress.\n"
            "  • run_recipe(recipe_id, input_payload, ...) — execute a curated recipe.\n"
            "  • list_pipelines — saved pipeline templates available to you.\n"
            "  • list_recipes — curated recipe catalog.\n"
            "  • compare(intent, slugs[]) — run the same task on multiple agents.\n"
            "  • compare_status(compare_id) — fetch compare-run progress.\n"
            "  • compare_select(compare_id, winner_slug) — finalize the comparison."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "hire_async",
                        "hire_batch",
                        "batch_status",
                        "run_pipeline",
                        "pipeline_status",
                        "run_recipe",
                        "list_pipelines",
                        "list_recipes",
                        "compare",
                        "compare_status",
                        "compare_select",
                    ],
                    "description": "Which workflow operation to run.",
                },
                "slug": {"type": "string", "description": "hire_async: target agent slug."},
                "slugs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "compare: agent slugs to compare.",
                },
                "intent": {"type": "string", "description": "compare: natural-language intent."},
                "input": {
                    "type": "object",
                    "description": "hire_async: input payload.",
                    "additionalProperties": True,
                },
                "input_payload": {
                    "type": "object",
                    "description": "run_pipeline / run_recipe: input payload.",
                    "additionalProperties": True,
                },
                "jobs": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                    "description": "hire_batch: list of {slug, input} job specs.",
                },
                "pipeline_id": {"type": "string", "description": "run_pipeline target."},
                "recipe_id": {"type": "string", "description": "run_recipe target."},
                "batch_id": {"type": "string", "description": "batch_status target."},
                "run_id": {"type": "string", "description": "pipeline_status target."},
                "compare_id": {"type": "string", "description": "compare_status / compare_select target."},
                "winner_slug": {"type": "string", "description": "compare_select: chosen agent slug."},
            },
            "required": ["action"],
            "additionalProperties": True,
        },
    },
]


META_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in _TOOLS) | GROUPED_TOOL_NAMES

_META_TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "aztea_wallet_balance": _annotations(read_only=True, idempotent=True),
    "aztea_spend_summary": _annotations(read_only=True, idempotent=True),
    "aztea_set_daily_limit": _annotations(read_only=False, idempotent=True),
    "aztea_topup_url": _annotations(read_only=False, idempotent=False),
    "aztea_session_summary": _annotations(read_only=True, idempotent=False),
    "aztea_set_session_budget": _annotations(
        read_only=False, idempotent=True, open_world=False
    ),
    "aztea_estimate_cost": _annotations(read_only=True, idempotent=False),
    "aztea_list_recipes": _annotations(read_only=True, idempotent=True),
    "aztea_list_pipelines": _annotations(read_only=True, idempotent=True),
    "aztea_hire_async": _annotations(read_only=False, idempotent=False),
    "aztea_job_status": _annotations(read_only=True, idempotent=False),
    "aztea_job_full_output": _annotations(read_only=True, idempotent=False),
    "aztea_batch_status": _annotations(read_only=True, idempotent=False),
    "aztea_cancel_job": _annotations(
        read_only=False, destructive=True, idempotent=True, open_world=False
    ),
    "aztea_follow_job": _annotations(read_only=True, idempotent=False),
    "aztea_data_retention_policy": _annotations(read_only=True, idempotent=True),
    "aztea_verify_job": _annotations(read_only=True, idempotent=True),
    "aztea_clarify": _annotations(read_only=False, idempotent=False),
    "aztea_rate_job": _annotations(read_only=False, idempotent=False),
    "aztea_dispute_job": _annotations(read_only=False, idempotent=False),
    "aztea_dispute_status": _annotations(read_only=True, idempotent=True),
    "aztea_list_agents": _annotations(read_only=True, idempotent=True),
    "aztea_session_audit": _annotations(read_only=True, idempotent=True),
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
    # Grouped tools dispatch to varied actions; mark as non-read-only.
    "aztea_job": _annotations(read_only=False, idempotent=False),
    "aztea_budget": _annotations(read_only=False, idempotent=True),
    "aztea_workflow": _annotations(read_only=False, idempotent=False),
}


def get_meta_tools() -> list[dict[str, Any]]:
    """All meta-tools surfaced to the MCP client.

    Grouped tools (aztea_job/budget/workflow) come first because they are the
    expected entry point; the underlying singular tools remain reachable for
    callers that already know the exact name.
    """
    enriched: list[dict[str, Any]] = []
    for tool in _GROUPED_TOOLS + _TOOLS:
        item = dict(tool)
        item["annotations"] = dict(
            _META_TOOL_ANNOTATIONS.get(item["name"], _annotations(read_only=False))
        )
        enriched.append(item)
    return enriched


def always_visible_tools() -> list[dict[str, Any]]:
    """Subset of meta-tools that should be visible even in lazy MCP mode.

    Returns the three grouped resource dispatchers — they cover 22 of the 28
    underlying singular tools at low token cost. The remaining singular tools
    stay discoverable through aztea_search.
    """
    enriched: list[dict[str, Any]] = []
    for tool in _GROUPED_TOOLS:
        item = dict(tool)
        item["annotations"] = dict(
            _META_TOOL_ANNOTATIONS.get(item["name"], _annotations(read_only=False))
        )
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

    # Resource-grouped tools dispatch by `action` to an underlying meta-tool.
    # Strip `action` from the args before recursing so the underlying handler
    # receives only the fields it expects.
    if tool_name in GROUPED_TOOL_NAMES:
        action = str(arguments.get("action") or "").strip()
        action_map = _GROUPED_DISPATCH.get(tool_name, {})
        if not action:
            return False, {
                "error": "INVALID_INPUT",
                "message": f"`action` is required for {tool_name}.",
                "allowed_actions": sorted(action_map.keys()),
            }
        underlying = action_map.get(action)
        if not underlying:
            return False, {
                "error": "INVALID_INPUT",
                "message": f"Unknown action '{action}' for {tool_name}.",
                "allowed_actions": sorted(action_map.keys()),
            }
        sub_args = {k: v for k, v in arguments.items() if k != "action"}
        # Per-action input contract checks for grouped tools. Catches the
        # schema/server drift where aztea_budget(action="estimate") needed a
        # slug or agent_id but the JSON schema only marked `action` required.
        ok_action, action_error = _validate_grouped_action_inputs(
            tool_name, action, sub_args
        )
        if not ok_action:
            return False, action_error
        return call_meta_tool(
            underlying,
            sub_args,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            session=session,
            session_state=session_state,
        )

    # aztea_set_session_budget: pure client-side state change, no API call needed
    if tool_name == "aztea_set_session_budget":
        unknown = sorted(set(arguments) - {"budget_cents"})
        if unknown:
            return False, {
                "error": "INVALID_INPUT",
                "message": f"Unknown field(s): {', '.join(unknown)}. Use budget_cents; pass 0 explicitly to clear.",
                "allowed_fields": ["budget_cents"],
            }
        if "budget_cents" not in arguments:
            return False, {
                "error": "INVALID_INPUT",
                "message": "budget_cents is required. Pass 0 explicitly to clear the session budget.",
            }
        try:
            budget = int(arguments.get("budget_cents") or 0)
        except (TypeError, ValueError):
            return False, {
                "error": "INVALID_INPUT",
                "message": "budget_cents must be an integer.",
            }
        if budget < 0:
            return False, {
                "error": "INVALID_INPUT",
                "message": "budget_cents must be >= 0.",
            }
        session_state["budget_cents"] = budget if budget > 0 else None
        spent = int(session_state.get("spent_cents") or 0)
        msg = (
            (
                f"Session budget set to ${budget / 100:.2f}. "
                f"Current session spend: ${spent / 100:.2f}."
            )
            if budget > 0
            else "Session budget cleared."
        )
        return True, {
            "budget_cents": budget or None,
            "spent_cents": spent,
            "message": msg,
        }

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
            # _wallet_balance accepts an optional args dict (for tx_limit /
            # include_transactions). Tests may monkey-patch the older
            # 4-positional-arg signature; tolerate that for compatibility.
            try:
                ok, result = _wallet_balance(session, base, hdrs, timeout, arguments)
            except TypeError:
                ok, result = _wallet_balance(session, base, hdrs, timeout)
            return ok, _compact_wallet(result) if ok else result
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
        if tool_name == "aztea_list_agents":
            return _list_agents(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_session_audit":
            return _session_audit(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_hire_async":
            ok, result = _hire_async(session, base, hdrs, timeout, arguments)
            if ok:
                _accrue_from_result(session_state, result)
                result = _compact_job_submission(result)
            return ok, result
        if tool_name == "aztea_job_status":
            return _job_status(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_job_full_output":
            return _job_full_output(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_batch_status":
            return _batch_status(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_cancel_job":
            ok, result = _cancel_job(session, base, hdrs, timeout, arguments)
            if ok:
                _refund_from_result(session_state, result)
            return ok, result
        if tool_name == "aztea_follow_job":
            return _follow_job(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_data_retention_policy":
            return _data_retention_policy(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_verify_job":
            return _verify_job_signature(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_clarify":
            return _clarify(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_rate_job":
            return _rate_job(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_dispute_job":
            return _dispute_job(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_dispute_status":
            return _dispute_status(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_verify_output":
            return _verify_output(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_discover":
            return _discover(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_get_examples":
            return _get_examples(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_hire_batch":
            ok, result = _hire_batch(session, base, hdrs, timeout, arguments)
            if ok:
                _accrue_from_result(session_state, result)
            return ok, result
        if tool_name == "aztea_compare_agents":
            ok, result = _compare_agents(session, base, hdrs, timeout, arguments)
            if ok:
                _accrue_from_result(session_state, result)
            return ok, result
        if tool_name == "aztea_compare_status":
            return _compare_status(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_select_compare_winner":
            ok, result = _select_compare_winner(session, base, hdrs, timeout, arguments)
            if ok:
                _refund_from_result(session_state, result)
            return ok, result
        if tool_name == "aztea_run_pipeline":
            ok, result = _run_pipeline(session, base, hdrs, timeout, arguments)
            if ok:
                _accrue_from_result(session_state, result)
                result = _compact_recipe_or_pipeline(result)
            return ok, result
        if tool_name == "aztea_pipeline_status":
            return _pipeline_status(session, base, hdrs, timeout, arguments)
        if tool_name == "aztea_run_recipe":
            ok, result = _run_recipe(session, base, hdrs, timeout, arguments)
            if ok:
                _accrue_from_result(session_state, result)
                result = _compact_recipe_or_pipeline(result)
            return ok, result
    except requests.RequestException as exc:
        return False, {"error": "NETWORK_ERROR", "message": str(exc)}
    except Exception as exc:
        return False, {"error": "META_TOOL_ERROR", "message": str(exc)}

    return False, {"error": "UNKNOWN_META_TOOL", "tool": tool_name}


def _accrue(session_state: dict[str, Any], amount_cents: Any) -> None:
    if amount_cents is not None:
        session_state["spent_cents"] = int(session_state.get("spent_cents") or 0) + int(
            amount_cents
        )


def _refund(session_state: dict[str, Any], amount_cents: Any) -> None:
    if amount_cents is None:
        return
    session_state["spent_cents"] = max(
        0,
        int(session_state.get("spent_cents") or 0) - int(amount_cents),
    )


def _as_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _charge_from_result(result: Any) -> int | None:
    if not isinstance(result, dict):
        return None
    for key in (
        "caller_charge_cents",
        "total_charged_cents",
        "total_price_cents",
        "price_cents",
    ):
        amount = _as_int(result.get(key))
        if amount is not None:
            return amount
    jobs = result.get("jobs")
    if isinstance(jobs, list):
        total = 0
        for job in jobs:
            amount = _charge_from_result(job)
            if amount is not None:
                total += amount
        if total:
            return total
    step_results = result.get("step_results")
    if isinstance(step_results, dict):
        total = 0
        for step in step_results.values():
            amount = _charge_from_result(step)
            if amount is not None:
                total += amount
        if total:
            return total
    return None


def _refund_from_payload(result: Any) -> int | None:
    if not isinstance(result, dict):
        return None
    amount = _as_int(result.get("refund_amount_cents") or result.get("refunded_cents"))
    if amount is not None:
        return amount
    if result.get("refunded") is True:
        return _as_int(result.get("price_cents") or result.get("caller_charge_cents"))
    jobs = result.get("jobs")
    if isinstance(jobs, list):
        total = 0
        for job in jobs:
            amount = _refund_from_payload(job)
            if amount is not None:
                total += amount
        if total:
            return total
    return None


def _accrue_from_result(session_state: dict[str, Any], result: Any) -> None:
    _accrue(session_state, _charge_from_result(result))


def _refund_from_result(session_state: dict[str, Any], result: Any) -> None:
    _refund(session_state, _refund_from_payload(result))


def _clean_text(value: Any) -> Any:
    if isinstance(value, str):
        return _PYDANTIC_HELP_URL_RE.sub("", value).strip()
    if isinstance(value, list):
        return [_clean_text(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean_text(item) for key, item in value.items()}
    return value


def _input_arg(args: dict[str, Any], *, default: dict[str, Any] | None = None) -> Any:
    if "input_payload" in args:
        return args.get("input_payload")
    if "input" in args:
        return args.get("input")
    return {} if default is None else default


def _compact_wallet(result: dict[str, Any], *, limit: int = 5) -> dict[str, Any]:
    compact = dict(result)
    for key in ("transactions", "transaction_history"):
        txs = compact.get(key)
        if isinstance(txs, list):
            compact["recent_transactions"] = txs[:limit]
            compact["transaction_count"] = len(txs)
            compact["transactions_omitted"] = max(0, len(txs) - limit)
            compact.pop(key, None)
            compact.setdefault(
                "note",
                f"Showing {min(limit, len(txs))} most recent transactions; use aztea_spend_summary for audits.",
            )
            break
    return compact


def _compact_job_submission(result: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "job_id",
        "agent_id",
        "status",
        "price_cents",
        "caller_charge_cents",
        "created_at",
        "updated_at",
        "note",
        "messages",
        "batch_id",
    }
    compact = {
        key: value for key, value in result.items() if key in keep and value is not None
    }
    omitted = sorted(
        key for key, value in result.items() if key not in compact and value is not None
    )
    if omitted:
        compact["omitted_fields"] = omitted[:20]
        compact.setdefault("full_status_available_via", "aztea_job_status")
    return compact or result


def _compact_recipe_or_pipeline(result: dict[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    step_results = compact.get("step_results")
    output_payload = compact.get("output_payload")
    if (
        isinstance(step_results, dict)
        and len(step_results) == 1
        and output_payload is not None
    ):
        only_step = next(iter(step_results.values()))
        if only_step == output_payload:
            compact.pop("output_payload", None)
            compact["output_payload_omitted_reason"] = (
                "Same as the single step result; see step_results."
            )
    return compact


# ─── Handlers ────────────────────────────────────────────────────────────────


def _get(
    session: requests.Session, url: str, hdrs: dict, timeout: float, **kwargs: Any
) -> tuple[bool, dict]:
    r = session.get(url, headers=hdrs, timeout=timeout, **kwargs)
    return _parse(r)


def _post(
    session: requests.Session, url: str, hdrs: dict, timeout: float, body: Any
) -> tuple[bool, dict]:
    r = session.post(url, headers=hdrs, timeout=timeout, json=body)
    return _parse(r)


def _parse(r: requests.Response) -> tuple[bool, dict]:
    try:
        body = r.json()
    except Exception:
        body = {"raw_body": r.text}
    body = _clean_text(body)
    if r.ok:
        return True, body if isinstance(body, dict) else {"result": body}
    detail = body if isinstance(body, dict) else {"detail": body}
    detail.setdefault("status_code", r.status_code)
    if isinstance(body, dict):
        nested = body.get("detail") if isinstance(body.get("detail"), dict) else None
        nested_data = (
            nested.get("data")
            if isinstance(nested, dict) and isinstance(nested.get("data"), dict)
            else None
        )
        if nested and "message" in nested and "message" not in detail:
            detail["message"] = nested["message"]
        for source in (body, nested or {}, nested_data or {}):
            for key in (
                "refunded",
                "refund_amount_cents",
                "cost_usd",
                "wallet_balance_cents",
            ):
                if key in source and key not in detail:
                    detail[key] = source[key]
    return False, {"error": "API_ERROR", **detail}


def _wallet_balance(
    session: requests.Session,
    base: str,
    hdrs: dict,
    timeout: float,
    args: dict | None = None,
) -> tuple[bool, dict]:
    """Wallet balance with bounded transaction history.

    The /wallets/me endpoint returns full transaction history which can be
    50+ rows = ~50KB on a busy account, burning the MCP context budget for
    a routine balance check. We trim the response to the most recent
    `tx_limit` (default 10) transactions and add a `transactions_omitted`
    indicator so callers know more exist. Pass `include_transactions=false`
    or `tx_limit=0` to drop the array entirely.
    """
    args = args or {}
    ok, payload = _get(session, f"{base}/wallets/me", hdrs, timeout)
    if not ok or not isinstance(payload, dict):
        return ok, payload
    transactions = payload.get("transactions")
    include_tx = args.get("include_transactions")
    if include_tx is None:
        include_tx = True
    try:
        tx_limit = int(args.get("tx_limit") if args.get("tx_limit") is not None else 10)
    except (TypeError, ValueError):
        tx_limit = 10
    tx_limit = max(0, min(tx_limit, 200))
    if isinstance(transactions, list):
        total = len(transactions)
        if not include_tx or tx_limit == 0:
            payload["transactions"] = []
            payload["transactions_omitted"] = total
            payload["transactions_total"] = total
        elif total > tx_limit:
            payload["transactions"] = transactions[:tx_limit]
            payload["transactions_omitted"] = total - tx_limit
            payload["transactions_total"] = total
        payload["transactions_hint"] = (
            "Default: 10 most recent transactions. "
            "Pass tx_limit=N (max 200) for more, or include_transactions=false to omit."
        )
    return ok, payload


def _spend_summary(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    period = str(args.get("period") or "7d")
    if period not in ("1d", "7d", "30d", "90d"):
        period = "7d"
    return _get(
        session,
        f"{base}/wallets/spend-summary",
        hdrs,
        timeout,
        params={"period": period},
    )


def _set_daily_limit(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    limit_cents = int(args.get("limit_cents") or 0)
    # API field is daily_spend_limit_cents; None clears the cap (0 maps to None)
    daily_limit = limit_cents if limit_cents > 0 else None
    return _post(
        session,
        f"{base}/wallets/me/daily-spend-limit",
        hdrs,
        timeout,
        {"daily_spend_limit_cents": daily_limit},
    )


def _topup_url(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    amount_cents = int(args.get("amount_cents") or 500)
    if not (100 <= amount_cents <= 50000):
        return False, {
            "error": "INVALID_INPUT",
            "message": "amount_cents must be 100–50 000 ($1–$500).",
        }
    # The topup endpoint requires wallet_id from the caller's wallet
    ok_wallet, wallet = _get(session, f"{base}/wallets/me", hdrs, timeout)
    if not ok_wallet:
        return False, {
            "error": "WALLET_FETCH_FAILED",
            "message": "Could not retrieve your wallet to create the topup session.",
            **wallet,
        }
    wallet_id = wallet.get("wallet_id")
    if not wallet_id:
        return False, {
            "error": "WALLET_FETCH_FAILED",
            "message": "wallet_id not found in wallet response.",
        }
    ok, result = _post(
        session,
        f"{base}/wallets/topup/session",
        hdrs,
        timeout,
        {
            "wallet_id": wallet_id,
            "amount_cents": amount_cents,
        },
    )
    if ok:
        result.setdefault("note", "Open checkout_url in a browser to complete payment.")
    return ok, result


def _session_summary(
    session: requests.Session,
    base: str,
    hdrs: dict,
    timeout: float,
    session_state: dict[str, Any],
) -> tuple[bool, dict]:
    ok_bal, balance = _get(session, f"{base}/wallets/me", hdrs, timeout)
    ok_spend, spend = _get(
        session, f"{base}/wallets/spend-summary", hdrs, timeout, params={"period": "1d"}
    )
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
    result["session_budget_usd"] = (
        round(float(budget) / 100, 4) if budget is not None else None
    )
    return True, result


def _resolve_agent_id(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[str, dict | None]:
    """Accept either ``agent_id`` (UUID) or ``slug`` (tool name) for buyer ergonomics.

    Returns (agent_id, error_payload). On success error_payload is None; on
    failure agent_id is empty and the error dict is suitable for direct return.
    """
    agent_id = str(args.get("agent_id") or "").strip()
    if agent_id:
        return agent_id, None
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "", {
            "error": "INVALID_INPUT",
            "message": "Provide agent_id (UUID) or slug (tool name).",
        }
    # Slug resolution must be EXACT-match. We hit the semantic search endpoint
    # only to enumerate candidate slugs; we do NOT accept the top-ranked match
    # unless its slug equals the requested one byte-for-byte (case-insensitive).
    # Money-routing must never fall through to a similarly-named agent. We also
    # raise the limit so a typo'd slug that happens to rank below the top 5
    # still resolves correctly when it exists in the catalog.
    ok, payload = _post(
        session,
        f"{base}/registry/search",
        hdrs,
        timeout,
        {"query": slug, "limit": 50},
    )
    if not ok:
        return "", {
            "error": "AGENT_LOOKUP_FAILED",
            "message": "Could not resolve slug to agent_id.",
            **payload,
        }
    slug_lower = slug.lower()
    candidates_seen = []
    for item in payload.get("results") or []:
        agent = item.get("agent") or {}
        candidate_slug = (
            str(agent.get("slug") or "").strip().lower()
            or str(agent.get("agent_slug") or "").strip().lower()
        )
        candidates_seen.append(candidate_slug)
        if candidate_slug and candidate_slug == slug_lower:
            resolved = str(agent.get("agent_id") or "").strip()
            if resolved:
                return resolved, None
    # Fallback: try the registry list endpoint for a definitive catalog scan.
    # This protects against a search index that doesn't return an exact
    # registered slug (rare, but observed when the search index is stale).
    ok_list, list_payload = _get(
        session, f"{base}/registry/agents", hdrs, timeout
    )
    if ok_list:
        for agent in list_payload.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            cand = (
                str(agent.get("slug") or "").strip().lower()
                or str(agent.get("agent_slug") or "").strip().lower()
            )
            if cand == slug_lower:
                resolved = str(agent.get("agent_id") or "").strip()
                if resolved:
                    return resolved, None
    return "", {
        "error": "AGENT_NOT_FOUND",
        "message": (
            f"No agent has the exact slug {slug!r}. "
            "Slug matching is strict (no fuzzy fallback) to prevent "
            "money-routing to a similarly-named agent. "
            "Use aztea_search to find the right slug, then retry."
        ),
        "search_returned_candidates": candidates_seen[:10],
    }


def _estimate_cost(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    agent_id, err = _resolve_agent_id(session, base, hdrs, timeout, args)
    if err is not None:
        return False, err
    body = _input_arg(args) or {}
    if not isinstance(body, dict):
        return False, {
            "error": "INVALID_INPUT",
            "message": "input/input_payload must be an object when provided.",
        }
    ok, result = _post(
        session, f"{base}/agents/{agent_id}/estimate", hdrs, timeout, body
    )
    if ok:
        result.setdefault("note", "This is a preview only. No charge has been applied.")
    return ok, result


def _list_recipes(
    session: requests.Session, base: str, hdrs: dict, timeout: float
) -> tuple[bool, dict]:
    ok, result = _get(session, f"{base}/recipes", hdrs, timeout)
    if ok:
        recipes = result.get("recipes") or []
        result.setdefault("count", len(recipes))
        result.setdefault(
            "note",
            "Use recipe_id with aztea_run_recipe to execute one of these workflows.",
        )
    return ok, result


def _list_agents(
    session: requests.Session,
    base: str,
    hdrs: dict,
    timeout: float,
    args: dict,
) -> tuple[bool, dict]:
    """One-shot enumeration of public agents (replaces 8+ search calls)."""
    category_filter = str(args.get("category") or "").strip().lower()
    try:
        limit = max(1, min(200, int(args.get("limit") or 100)))
    except (TypeError, ValueError):
        limit = 100
    ok, result = _get(
        session,
        f"{base}/registry/agents",
        hdrs,
        timeout,
        params={"include_reputation": "true"},
    )
    if not ok:
        return ok, result
    raw = result.get("agents") or []
    rows = []
    for agent in raw:
        if not isinstance(agent, dict):
            continue
        cat = str(agent.get("category") or "").strip()
        if category_filter and cat.lower() != category_filter:
            continue
        rows.append(
            {
                "slug": agent.get("slug") or agent.get("agent_slug"),
                "agent_id": agent.get("agent_id"),
                "name": agent.get("name"),
                "category": cat or None,
                "description": (str(agent.get("description") or "")[:240]),
                "price_per_call_usd": agent.get("price_per_call_usd"),
                "trust_score": agent.get("trust_score"),
                "success_rate": agent.get("success_rate"),
                "tags": agent.get("tags") or [],
            }
        )
        if len(rows) >= limit:
            break
    return True, {
        "count": len(rows),
        "category_filter": category_filter or None,
        "agents": rows,
        "note": (
            "All public Aztea agents in one shot. Filter further with category. "
            "Pick a slug and call aztea_describe(slug=...) for the full schema."
        ),
    }


def _session_audit(
    session: requests.Session,
    base: str,
    hdrs: dict,
    timeout: float,
    args: dict,
) -> tuple[bool, dict]:
    """Aggregate spend/refund/receipt audit for a window of caller activity.

    Composed from existing endpoints (wallet transactions + signed receipts)
    so no new server endpoint is required for the v1.
    """
    period = str(args.get("period") or "1d").strip().lower()
    if period not in {"1d", "7d", "30d", "90d"}:
        period = "1d"
    ok, spend = _get(
        session,
        f"{base}/wallet/spend-summary",
        hdrs,
        timeout,
        params={"period": period},
    )
    if not ok:
        return ok, spend
    # Pull recent jobs for receipt-status visibility.
    ok2, recent = _get(
        session,
        f"{base}/jobs",
        hdrs,
        timeout,
        params={"limit": 50, "status": "complete"},
    )
    receipts = []
    if ok2 and isinstance(recent, dict):
        for job in (recent.get("jobs") or [])[:50]:
            if not isinstance(job, dict):
                continue
            receipts.append(
                {
                    "job_id": job.get("job_id"),
                    "agent_id": job.get("agent_id"),
                    "agent_name": job.get("agent_name"),
                    "charge_cents": job.get("caller_charge_cents")
                    or job.get("price_cents"),
                    "settled_at": job.get("settled_at"),
                    "signature_endpoint": (
                        f"/jobs/{job.get('job_id')}/signature"
                        if job.get("output_signature")
                        else None
                    ),
                }
            )
    return True, {
        "period": period,
        "spend": spend,
        "recent_signed_receipts": receipts,
        "audit_signature_method": "per-job Ed25519 (call aztea_job(action=verify, job_id=...) to verify each)",
        "next_step": (
            "For an authoritative audit log, verify each receipt individually. "
            "v2 will return a session-level signed manifest."
        ),
    }


def _list_pipelines(
    session: requests.Session, base: str, hdrs: dict, timeout: float
) -> tuple[bool, dict]:
    ok, result = _get(session, f"{base}/pipelines", hdrs, timeout)
    if ok:
        pipeline_rows = result.get("pipelines") or []
        result.setdefault("count", len(pipeline_rows))
        if not pipeline_rows:
            result["note"] = (
                "No user-created pipelines yet. Curated public workflows live "
                "under recipes — call aztea_workflow(action='list_recipes')."
            )
        else:
            result.setdefault(
                "note",
                "Use pipeline_id with aztea_run_pipeline to execute one of these workflows.",
            )
    return ok, result


def _hire_async(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    agent_id, err = _resolve_agent_id(session, base, hdrs, timeout, args)
    if err is not None:
        return False, err
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "input_payload": _input_arg(args) or {},
    }
    if not isinstance(body["input_payload"], dict):
        return False, {
            "error": "INVALID_INPUT",
            "message": "input/input_payload must be an object.",
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
        result.setdefault(
            "note",
            (
                f"Job submitted. Poll with aztea_job(action='status', job_id='{result.get('job_id', '')}') "
                "to track progress and retrieve results."
            ),
        )
    return ok, result


def _job_status(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
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
    ok_msgs, msgs_body = _get(
        session, f"{base}/jobs/{job_id}/messages", hdrs, timeout, params=msg_params
    )
    if ok_msgs:
        messages = msgs_body.get("messages") or []
        result["messages"] = messages
        # Surface clarification requests prominently
        clarifications = [
            m for m in messages if m.get("type") == "clarification_request"
        ]
        if clarifications:
            result["clarification_needed"] = clarifications[-1].get("payload", {})
            result["note"] = (
                "Agent is awaiting clarification. "
                "Call aztea_clarify(job_id=..., message=...) to respond and resume the job."
            )
    else:
        result["messages"] = []
    return True, result


def _job_full_output(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    # Chunked pagination: pass through offset/limit so callers can page through
    # outputs that exceed MCP's per-response token budget. Backend caps per-chunk
    # size; callers loop until has_more=False then json.loads the concatenation.
    params: dict[str, Any] = {}
    raw_offset = args.get("offset")
    if raw_offset is not None:
        try:
            params["offset"] = max(0, int(raw_offset))
        except (TypeError, ValueError):
            return False, {
                "error": "INVALID_INPUT",
                "message": "offset must be a non-negative integer.",
            }
    raw_limit = args.get("limit")
    if raw_limit is not None:
        try:
            params["limit"] = max(0, int(raw_limit))
        except (TypeError, ValueError):
            return False, {
                "error": "INVALID_INPUT",
                "message": "limit must be a non-negative integer.",
            }
    if params:
        return _get(session, f"{base}/jobs/{job_id}/full", hdrs, timeout, params=params)
    return _get(session, f"{base}/jobs/{job_id}/full", hdrs, timeout)


def _batch_status(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    batch_id = str(args.get("batch_id") or "").strip()
    if batch_id:
        # Caller can opt into 'full' for first poll, then 'minimal' for
        # subsequent polls. Default minimal (drops duplicated job list inside
        # parallel_hire_trace) which keeps payloads small enough for MCP
        # token budgets even on 30-job batches.
        include = str(args.get("include") or "minimal").strip().lower()
        if include not in {"minimal", "compact", "full"}:
            include = "minimal"
        ok, result = _get(
            session,
            f"{base}/jobs/batch/{batch_id}",
            hdrs,
            timeout,
            params={"include": include},
        )
        if ok:
            result.setdefault(
                "note",
                "Parallel marketplace hire status returned. Use parallel_hire_trace to explain which specialists were hired, what settled, and which receipts are available. Pass include='full' to retrieve full per-job outputs once.",
            )
        return ok, result

    raw_job_ids = args.get("job_ids")
    if not isinstance(raw_job_ids, list) or not raw_job_ids:
        return False, {
            "error": "INVALID_INPUT",
            "message": "batch_id or job_ids must be provided.",
        }
    job_ids = [
        str(job_id or "").strip() for job_id in raw_job_ids if str(job_id or "").strip()
    ]
    if not job_ids:
        return False, {
            "error": "INVALID_INPUT",
            "message": "job_ids must include at least one non-empty job_id.",
        }
    if len(job_ids) > 50:
        return False, {
            "error": "INVALID_INPUT",
            "message": "Batch status is limited to 50 jobs.",
        }

    jobs: list[dict[str, Any]] = []
    counts = {"complete": 0, "failed": 0, "running": 0, "pending": 0, "other": 0}
    since = args.get("since_message_id")
    for job_id in job_ids:
        ok, status = _job_status(
            session,
            base,
            hdrs,
            timeout,
            {
                "job_id": job_id,
                **({"since_message_id": since} if since is not None else {}),
            },
        )
        if not ok:
            status = {
                "job_id": job_id,
                "status": "failed",
                "error": status.get("error"),
                "message": status.get("message"),
            }
        normalized = str(status.get("status") or "other").strip().lower()
        counts[normalized if normalized in counts else "other"] += 1
        jobs.append(status)
    return True, {
        "job_count": len(jobs),
        "complete_count": counts["complete"],
        "failed_count": counts["failed"],
        "running_count": counts["running"],
        "pending_count": counts["pending"],
        "other_count": counts["other"],
        "jobs": jobs,
        "note": "All requested job statuses returned in one MCP call.",
    }


def _data_retention_policy(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    """Surface the privacy posture buyers care about before sending sensitive data.

    Reads the per-agent privacy columns added in migration 0026 plus the
    sensitivity flags from the spec layer. Composed entirely from data the
    server already exposes — no new endpoint required.
    """
    if not str(args.get("agent_id") or args.get("slug") or "").strip():
        return True, {
            "scope": "global",
            "private_task_supported": True,
            "default_policy": (
                "Aztea stores job records for settlement, receipts, disputes, and audit logs. "
                "Sensitive built-in agents do not publish work examples; pass private_task=true "
                "on any hire to suppress work-example recording for that call."
            ),
            "recommended_for_sensitive_inputs": {
                "private_task": True,
                "verify_receipt_after_completion": True,
            },
            "next_step": (
                "Pass slug or agent_id for an agent-specific retention answer, "
                "or hire with private_task=true for sensitive data."
            ),
        }
    agent_id, err = _resolve_agent_id(session, base, hdrs, timeout, args)
    if err is not None:
        return False, err
    ok, agent = _get(session, f"{base}/registry/agents/{agent_id}", hdrs, timeout)
    if not ok:
        return False, agent
    category = str(agent.get("category") or "").strip()
    examples_blocked = (
        bool(agent.get("examples_sensitive")) or category.lower() == "security"
    )
    # Compose a category-aware retention policy. Security-category agents (or
    # any with examples_sensitive=True) get the strict default since they
    # routinely ingest credentials, PII-adjacent text, or proprietary code;
    # the non-decision posture of "policy not specified" was the wrong
    # default for these. General-purpose agents fall back to a clear
    # platform-wide statement.
    if examples_blocked:
        retention_policy = (
            "Strict mode (Security or examples_sensitive=true). "
            "Caller inputs are not replayed in public work examples. "
            "Receipts contain only output hashes, not full input/output bodies. "
            "Pass private_task=true to additionally suppress per-call example storage."
        )
    elif bool(agent.get("outputs_not_stored")):
        retention_policy = (
            "Outputs are not retained beyond the settlement window. "
            "Inputs may be retained for dispute eligibility (default 72h) and "
            "are then purged. Pass private_task=true to opt out of work-example "
            "publication entirely."
        )
    else:
        retention_policy = (
            "Default mode. Job records are retained for settlement, signed "
            "receipts, dispute resolution, and audit logs. Redacted work "
            "examples may be published unless private_task=true is set per call."
        )
    privacy = {
        "agent_id": agent_id,
        "name": agent.get("name"),
        "category": category or None,
        "pii_safe": bool(agent.get("pii_safe")),
        "outputs_not_stored": bool(agent.get("outputs_not_stored")),
        "audit_logged": bool(agent.get("audit_logged")),
        "region_locked": agent.get("region_locked"),
        "publishes_work_examples": not examples_blocked,
        "examples_sensitive": bool(agent.get("examples_sensitive")),
        "stores_inputs_for_examples": (not examples_blocked),
        "data_retention_policy": retention_policy,
        "summary": (
            "This agent does not publish work examples — caller inputs are not replayed publicly."
            if examples_blocked
            else "This agent may publish redacted work examples derived from past calls. "
            "Pass private_task=true on hire to suppress example recording for a specific call."
        ),
        "note": (
            "This is what Aztea publishes about the agent. The agent owner is responsible for "
            "any internal handling beyond the platform — verify in the agent's documentation if "
            "you are subject to specific regulatory or contractual requirements."
        ),
    }
    return True, privacy


def _verify_job_signature(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    """Verify a job's signed receipt against the agent's published DID document.

    Mirrors :pyfunc:`aztea.AzteaClient.verify_job` so the same guarantee is
    accessible from any MCP client without writing custom code.
    """
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    ok_sig, signature_payload = _get(
        session, f"{base}/jobs/{job_id}/signature", hdrs, timeout
    )
    if not ok_sig:
        return False, {
            "verified": False,
            "verification_error": "signature unavailable",
            **(signature_payload or {}),
        }
    agent_did = str(signature_payload.get("agent_did") or "").strip()
    signature_b64 = str(signature_payload.get("signature") or "").strip()
    output_hash = str(signature_payload.get("output_hash") or "").strip()
    if not (agent_did and signature_b64 and output_hash):
        return False, {
            "verified": False,
            "verification_error": "incomplete signature payload",
            "signature_payload": signature_payload,
        }
    agent_id = agent_did.rsplit(":", 1)[-1] if ":agents:" in agent_did else None
    if not agent_id:
        return False, {
            "verified": False,
            "verification_error": f"could not parse agent_id from did {agent_did!r}",
        }
    ok_job, job_payload = _get(session, f"{base}/jobs/{job_id}", hdrs, timeout)
    if not ok_job:
        return False, {
            "verified": False,
            "verification_error": "job output unavailable",
            "agent_did": agent_did,
            **(job_payload or {}),
        }
    output_payload = job_payload.get("output_payload")
    try:
        import base64
        import json

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:
        return True, {
            "verified": False,
            "verification_error": "cryptography library not installed in MCP runtime (pip install cryptography)",
            "agent_did": agent_did,
            "output_hash": output_hash,
            "signature_payload": signature_payload,
        }
    # Prefer the JWK embedded in the signature response: it removes a second
    # HTTP round-trip and works even when the did:web hostname is not
    # reachable from the MCP verifier's network. Fall back to the DID
    # document for older signature responses that omit the field.
    public_key_b64: str | None = None
    verification_method = "embedded-jwk"
    embedded_jwk = signature_payload.get("public_key_jwk")
    if (
        isinstance(embedded_jwk, dict)
        and embedded_jwk.get("crv") == "Ed25519"
        and embedded_jwk.get("x")
    ):
        public_key_b64 = str(embedded_jwk.get("x"))
    did_doc: dict | None = None
    if not public_key_b64:
        verification_method = "did-document"
        ok_did, did_doc_payload = _get(
            session, f"{base}/agents/{agent_id}/did.json", hdrs, timeout
        )
        if not ok_did:
            return False, {
                "verified": False,
                "verification_error": "no embedded public_key_jwk and DID document unavailable",
                "agent_did": agent_did,
                **(did_doc_payload or {}),
            }
        did_doc = did_doc_payload
        for method in did_doc.get("verificationMethod") or []:
            if not isinstance(method, dict):
                continue
            jwk = method.get("publicKeyJwk")
            if isinstance(jwk, dict) and jwk.get("crv") == "Ed25519" and jwk.get("x"):
                public_key_b64 = str(jwk.get("x"))
                break
            raw = method.get("publicKeyBase64") or method.get("publicKeyMultibase")
            if isinstance(raw, str) and raw:
                public_key_b64 = raw.lstrip("z")
                break
    if not public_key_b64:
        return True, {
            "verified": False,
            "verification_error": "no Ed25519 publicKeyJwk on DID document and none embedded in signature response",
            "agent_did": agent_did,
            "did_doc": did_doc,
        }
    try:
        pad = "=" * (-len(public_key_b64) % 4)
        try:
            public_key_bytes = base64.urlsafe_b64decode(public_key_b64 + pad)
        except Exception:
            public_key_bytes = base64.b64decode(public_key_b64 + pad)
        sig_pad = "=" * (-len(signature_b64) % 4)
        try:
            signature_bytes = base64.urlsafe_b64decode(signature_b64 + sig_pad)
        except Exception:
            signature_bytes = base64.b64decode(signature_b64 + sig_pad)
        signed_bytes = json.dumps(
            output_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, signed_bytes)
    except Exception as exc:
        return True, {
            "verified": False,
            "verification_error": f"signature verification failed: {exc}",
            "agent_did": agent_did,
            "output_hash": output_hash,
            "verification_method": verification_method,
        }
    return True, {
        "verified": True,
        "agent_did": agent_did,
        "output_hash": output_hash,
        "signed_at": signature_payload.get("signed_at"),
        "verification_method": verification_method,
        "note": (
            "Signature verified locally against the agent's Ed25519 public key. "
            "Aztea cannot alter this output without breaking the signature."
        ),
    }


def _cancel_job(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    body: dict[str, Any] = {}
    reason = str(args.get("reason") or "").strip()
    if reason:
        body["reason"] = reason[:200]
    ok, result = _post(session, f"{base}/jobs/{job_id}/cancel", hdrs, timeout, body)
    if ok:
        result.setdefault(
            "note", "Job cancelled. Any pre-call charge has been refunded."
        )
    return ok, result


def _follow_job(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    """Poll a job until terminal, then return the final aztea_job_status result."""
    import time as _time

    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    timeout_secs = min(
        int(args.get("timeout_seconds") or args.get("max_wait_seconds") or 180),
        300,
    )
    poll_interval = 4  # seconds between polls
    deadline = _time.monotonic() + timeout_secs
    _TERMINAL = {"complete", "failed", "cancelled"}

    while True:
        ok, result = _job_status(session, base, hdrs, timeout, {"job_id": job_id})
        if not ok:
            return False, result
        status = str(result.get("status") or "")
        if status in _TERMINAL:
            return True, result
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            result.setdefault(
                "note",
                f"Timeout after {timeout_secs}s — job still running. Call aztea_follow_job again or use aztea_job_status to poll manually.",
            )
            return True, result
        _time.sleep(min(poll_interval, remaining))


def _clarify(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    message = str(args.get("message") or args.get("response") or "").strip()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    if not message:
        return False, {"error": "INVALID_INPUT", "message": "message is required."}
    request_message_id = args.get("request_message_id")
    if request_message_id is None:
        ok_msgs, msgs_body = _get(
            session, f"{base}/jobs/{job_id}/messages", hdrs, timeout
        )
        if not ok_msgs:
            return False, {
                "error": "CLARIFICATION_LOOKUP_FAILED",
                "message": "Could not retrieve clarification requests for this job.",
                **msgs_body,
            }
        messages = msgs_body.get("messages") or []
        latest_request = next(
            (
                msg
                for msg in reversed(messages)
                if msg.get("type") == "clarification_request" and msg.get("message_id")
            ),
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


def _rate_job(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    rating = int(args.get("rating") or 0)
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    if rating < 1 or rating > 5:
        return False, {"error": "INVALID_INPUT", "message": "rating must be 1–5."}
    return _post(
        session, f"{base}/jobs/{job_id}/rating", hdrs, timeout, {"rating": rating}
    )


def _dispute_job(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
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
        result.setdefault(
            "note",
            "Dispute filed. An LLM judge will review the evidence and determine the outcome. "
            "Poll with aztea_job(action='dispute_status', dispute_id=...) to track resolution.",
        )
    return ok, result


def _dispute_status(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    """Poll a filed dispute's resolution path. Buyer-side recourse visibility."""
    dispute_id = str(args.get("dispute_id") or "").strip()
    if not dispute_id:
        return False, {
            "error": "INVALID_INPUT",
            "message": "dispute_id is required (returned by aztea_job(action='dispute', ...)).",
        }
    ok, result = _get(session, f"{base}/disputes/{dispute_id}", hdrs, timeout)
    if not ok:
        return ok, result
    status = str(result.get("status") or "").lower()
    judgments = result.get("judgments") or []
    eta_hint = None
    if status == "pending":
        eta_hint = (
            "Pending. LLM judges run on a 60s interval; expect first verdict "
            "within 1-2 minutes. If 2 judges agree, dispute resolves immediately."
        )
    elif status == "tied":
        eta_hint = "Tied after 2 rounds. Will auto-resolve to caller in 48h per policy."
    elif status == "resolved":
        eta_hint = "Resolved. Outcome and split visible in this response."
    result.setdefault(
        "note",
        f"Dispute status: {status}. Judges so far: {len(judgments)}.",
    )
    if eta_hint:
        result["eta_hint"] = eta_hint
    return True, result


def _verify_output(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    job_id = str(args.get("job_id") or "").strip()
    decision = str(args.get("decision") or "").strip().lower()
    if not job_id:
        return False, {"error": "INVALID_INPUT", "message": "job_id is required."}
    if decision not in ("accept", "reject"):
        return False, {
            "error": "INVALID_INPUT",
            "message": "decision must be 'accept' or 'reject'.",
        }
    body: dict[str, Any] = {"decision": decision}
    if args.get("reason"):
        body["reason"] = str(args["reason"])
    elif decision == "reject":
        return False, {
            "error": "INVALID_INPUT",
            "message": "reason is required when decision is 'reject'.",
        }
    return _post(session, f"{base}/jobs/{job_id}/verification", hdrs, timeout, body)


def _intent_for_query(query: str) -> tuple[str | None, set[str]]:
    terms = set(re.findall(r"[a-z0-9_+-]+", query.lower()))
    for intent, required_terms in _DISCOVERY_INTENTS.items():
        if terms & required_terms:
            return intent, required_terms
    return None, set()


def _agent_text(agent: dict[str, Any]) -> str:
    tags = agent.get("tags") or []
    return " ".join(
        [
            str(agent.get("slug") or ""),
            str(agent.get("name") or ""),
            str(agent.get("description") or ""),
            str(agent.get("category") or ""),
            " ".join(str(tag) for tag in tags if isinstance(tag, str)),
        ]
    ).lower()


def _intent_matches(
    agent: dict[str, Any], intent: str | None, intent_terms: set[str]
) -> bool:
    if intent is None:
        return True
    text = _agent_text(agent)
    if intent == "image":
        return "image" in text or "visual" in text or "generator" in text
    if intent == "browser":
        return "browser" in text or "playwright" in text or "screenshot" in text
    if intent == "dns":
        return (
            "dns" in text or "ssl" in text or "certificate" in text or "domain" in text
        )
    if intent == "code_search":
        return (
            ("semantic" in text and "code" in text)
            or "codebase" in text
            or "repository" in text
        )
    return bool(intent_terms & set(re.findall(r"[a-z0-9_+-]+", text)))


def _discover(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
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
        intent, intent_terms = _intent_for_query(query)
        compact = []
        for item in result["results"]:
            agent = item.get("agent") or {}
            if not _intent_matches(agent, intent, intent_terms):
                continue
            input_schema = agent.get("input_schema") if isinstance(agent, dict) else {}
            if not isinstance(input_schema, dict):
                input_schema = {}
            properties = input_schema.get("properties")
            fields = sorted(properties.keys()) if isinstance(properties, dict) else []
            compact.append(
                {
                    "agent_id": agent.get("agent_id"),
                    "slug": agent.get("slug") or agent.get("agent_slug"),
                    "name": agent.get("name"),
                    "description": _word_truncate(agent.get("description") or "", 240),
                    "category": agent.get("category"),
                    "price_per_call_usd": agent.get("price_per_call_usd"),
                    "price_cents": agent.get("price_cents"),
                    "trust_score": agent.get("trust_score"),
                    "success_rate": agent.get("success_rate"),
                    "avg_latency_ms": agent.get("avg_latency_ms"),
                    "required_fields": list(input_schema.get("required") or []),
                    "input_fields": fields[:12],
                    "pricing_model": agent.get("pricing_model"),
                    "pricing_config": agent.get("pricing_config"),
                    "blended_score": item.get("blended_score"),
                    "match_reasons": item.get("match_reasons"),
                }
            )
        result["results"] = compact
        result["count"] = len(compact)
        if intent and not compact:
            result["note"] = (
                f"No high-confidence {intent.replace('_', ' ')} agent was returned by discovery. "
                "Use aztea_search with a direct slug query or try a narrower task description; no low-relevance toy matches were returned."
            )
    return ok, result


def _get_examples(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    agent_id, err = _resolve_agent_id(session, base, hdrs, timeout, args)
    if err is not None:
        return False, err
    ok, agent = _get(session, f"{base}/registry/agents/{agent_id}", hdrs, timeout)
    if not ok:
        return False, agent
    examples = []
    for raw in (agent.get("output_examples") or [])[:10]:
        if isinstance(raw, dict):
            examples.append(
                {
                    "job_id": raw.get("job_id"),
                    "latency_ms": raw.get("latency_ms"),
                    "model_provider": raw.get("model_provider"),
                    "model_id": raw.get("model_id"),
                    "input": raw.get("input"),
                    "output": raw.get("output"),
                }
            )
    return True, {
        "agent_id": agent_id,
        "name": agent.get("name"),
        "example_count": len(agent.get("output_examples") or []),
        "examples": examples,
        "note": (
            "These are real inputs and outputs from past jobs. "
            "Review them to verify the agent's quality before hiring."
        )
        if examples
        else "No public work examples are available for this agent yet.",
    }


def _hire_batch(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    raw_jobs = args.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        return False, {
            "error": "INVALID_INPUT",
            "message": "jobs must be a non-empty array.",
        }
    if len(raw_jobs) > 50:
        return False, {
            "error": "INVALID_INPUT",
            "message": "Batch size is limited to 50 jobs.",
        }
    jobs_body = []
    for spec in raw_jobs:
        if not isinstance(spec, dict):
            return False, {
                "error": "INVALID_INPUT",
                "message": "Each job spec must be an object.",
            }
        agent_id, err = _resolve_agent_id(session, base, hdrs, timeout, spec)
        if err is not None:
            return False, err
        job: dict[str, Any] = {
            "agent_id": agent_id,
            "input_payload": _input_arg(spec) or {},
        }
        if not isinstance(job["input_payload"], dict):
            return False, {
                "error": "INVALID_INPUT",
                "message": "jobs[].input/input_payload must be an object.",
            }
        if spec.get("budget_cents") is not None:
            job["budget_cents"] = int(spec["budget_cents"])
        if spec.get("private_task") is not None:
            job["private_task"] = bool(spec["private_task"])
        jobs_body.append(job)
    body: dict[str, Any] = {"jobs": jobs_body}
    intent = str(args.get("intent") or "").strip()
    if intent:
        body["intent"] = intent
    if args.get("max_total_cents") is not None:
        body["max_total_cents"] = int(args["max_total_cents"])
    if args.get("dry_run") is not None:
        body["dry_run"] = bool(args["dry_run"])
    ok, result = _post(session, f"{base}/jobs/batch", hdrs, timeout, body)
    if ok:
        job_ids = [
            j.get("job_id") for j in (result.get("jobs") or []) if isinstance(j, dict)
        ]
        result.setdefault(
            "note",
            (
                f"Parallel marketplace hire submitted: {len(jobs_body)} specialists. "
                "Poll batch_id with aztea_batch_status to track escrow, settlement, and receipt status."
            ),
        )
        result.setdefault("job_ids", job_ids)
    return ok, result


def _compare_agents(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    # Accept either `agent_ids` (UUIDs) or `slugs` (human names from
    # aztea_search). The grouped tool surface documents `slugs`, so when
    # callers pass them we resolve client-side instead of 422-ing.
    raw_agent_ids = args.get("agent_ids")
    raw_slugs = args.get("slugs")
    if (raw_agent_ids is None or raw_agent_ids == []) and isinstance(raw_slugs, list):
        resolved: list[str] = []
        for slug in raw_slugs:
            slug_str = str(slug or "").strip()
            if not slug_str:
                continue
            resolved_id, err = _resolve_agent_id(
                session, base, hdrs, timeout, {"slug": slug_str}
            )
            if err is not None:
                return False, err
            resolved.append(resolved_id)
        raw_agent_ids = resolved
    if not isinstance(raw_agent_ids, list):
        return False, {
            "error": "INVALID_INPUT",
            "message": "Provide either agent_ids[] (UUIDs) or slugs[] (tool names).",
        }
    agent_ids = [
        str(item or "").strip() for item in raw_agent_ids if str(item or "").strip()
    ]
    if len(agent_ids) < 2 or len(agent_ids) > 3:
        return False, {
            "error": "INVALID_INPUT",
            "message": "Provide 2 or 3 unique agent_ids/slugs to compare.",
        }
    body: dict[str, Any] = {
        "agent_ids": agent_ids,
        "input_payload": _input_arg(args) or {},
    }
    if not isinstance(body["input_payload"], dict):
        return False, {
            "error": "INVALID_INPUT",
            "message": "input/input_payload must be an object.",
        }
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
        ok_status, status = _get(
            session, f"{base}/jobs/compare/{compare_id}", hdrs, timeout
        )
        if not ok_status:
            return False, status
        latest = status
        if str(status.get("status") or "").strip().lower() == "complete":
            latest.setdefault(
                "note",
                "Compare session completed. Review the results, then call aztea_select_compare_winner to finalize payment.",
            )
            if (
                created.get("total_charged_cents") is not None
                and latest.get("total_charged_cents") is None
            ):
                latest["total_charged_cents"] = created.get("total_charged_cents")
            return True, latest
        time.sleep(poll_interval)
    latest.setdefault(
        "note",
        "Compare session is still running. Poll it with aztea_compare_status or wait longer with wait_seconds.",
    )
    if (
        created.get("total_charged_cents") is not None
        and latest.get("total_charged_cents") is None
    ):
        latest["total_charged_cents"] = created.get("total_charged_cents")
    return True, latest


def _compare_status(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    compare_id = str(args.get("compare_id") or "").strip()
    if not compare_id:
        return False, {"error": "INVALID_INPUT", "message": "compare_id is required."}
    ok, result = _get(session, f"{base}/jobs/compare/{compare_id}", hdrs, timeout)
    if ok:
        status = str(result.get("status") or "").strip().lower()
        if status == "complete":
            result.setdefault(
                "note",
                "Compare session completed. Review the results, then call aztea_select_compare_winner to finalize payment.",
            )
        elif status == "running":
            result.setdefault("note", "Compare session is still running.")
    return ok, result


def _select_compare_winner(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    compare_id = str(args.get("compare_id") or "").strip()
    winner_agent_id = str(args.get("winner_agent_id") or "").strip()
    winner_slug = str(args.get("winner_slug") or "").strip()
    if not compare_id:
        return False, {"error": "INVALID_INPUT", "message": "compare_id is required."}
    if not winner_agent_id and winner_slug:
        resolved_id, err = _resolve_agent_id(
            session, base, hdrs, timeout, {"slug": winner_slug}
        )
        if err is not None:
            return False, err
        winner_agent_id = resolved_id
    if not winner_agent_id:
        return False, {
            "error": "INVALID_INPUT",
            "message": "Provide winner_agent_id (UUID) or winner_slug (tool name).",
        }
    ok, result = _post(
        session,
        f"{base}/jobs/compare/{compare_id}/select",
        hdrs,
        timeout,
        {"winner_agent_id": winner_agent_id},
    )
    if ok:
        result.setdefault(
            "note", "Compare session finalized. Only the winner was paid."
        )
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
    latest: dict[str, Any] = {
        "run_id": run_id,
        "pipeline_id": pipeline_id,
        "status": "running",
    }
    while time.monotonic() < deadline:
        ok_status, status = _get(
            session, f"{base}/pipelines/{pipeline_id}/runs/{run_id}", hdrs, timeout
        )
        if not ok_status:
            return False, status
        latest = status
        normalized = str(status.get("status") or "").strip().lower()
        if normalized in {"complete", "failed", "cancelled"}:
            if normalized == "complete":
                latest.setdefault("note", "Pipeline run completed.")
            elif normalized == "failed":
                latest.setdefault(
                    "note",
                    "Pipeline run failed. Inspect error_message and step_results.",
                )
            else:
                latest.setdefault("note", "Pipeline run was cancelled.")
            return True, latest
        time.sleep(poll_interval_seconds)
    latest.setdefault(
        "note",
        "Pipeline run is still running. Poll it with aztea_pipeline_status or wait longer with wait_seconds.",
    )
    return True, latest


def _pipeline_status(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    pipeline_id = str(args.get("pipeline_id") or "").strip()
    run_id = str(args.get("run_id") or "").strip()
    if not run_id:
        return False, {"error": "INVALID_INPUT", "message": "run_id is required."}
    # run_id is sufficient — the server can resolve pipeline_id from it. Keep the
    # qualified path for backward compatibility when both are provided.
    if pipeline_id:
        ok, result = _get(
            session, f"{base}/pipelines/{pipeline_id}/runs/{run_id}", hdrs, timeout
        )
    else:
        ok, result = _get(session, f"{base}/pipelines/runs/{run_id}", hdrs, timeout)
    if ok:
        status = str(result.get("status") or "").strip().lower()
        if status == "complete":
            result.setdefault("note", "Pipeline run completed.")
        elif status == "failed":
            result.setdefault(
                "note", "Pipeline run failed. Inspect error_message and step_results."
            )
        elif status == "running":
            result.setdefault("note", "Pipeline run is still running.")
    return ok, result


def _run_pipeline(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    pipeline_id = str(args.get("pipeline_id") or "").strip()
    if not pipeline_id:
        return False, {"error": "INVALID_INPUT", "message": "pipeline_id is required."}
    input_payload = args.get("input_payload")
    if not isinstance(input_payload, dict):
        return False, {
            "error": "INVALID_INPUT",
            "message": "input_payload must be an object.",
        }
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


def _run_recipe(
    session: requests.Session, base: str, hdrs: dict, timeout: float, args: dict
) -> tuple[bool, dict]:
    recipe_id = str(args.get("recipe_id") or args.get("recipe_name") or "").strip()
    if not recipe_id:
        return False, {
            "error": "INVALID_INPUT",
            "message": "recipe_id or recipe_name is required.",
        }
    input_payload = args.get("input_payload")
    if not isinstance(input_payload, dict):
        return False, {
            "error": "INVALID_INPUT",
            "message": "input_payload must be an object.",
        }
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
