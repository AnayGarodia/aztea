"""Pydantic models (split from legacy models.py for maintainability)."""

from __future__ import annotations

from typing import Literal

try:
    from typing import NotRequired
except ImportError:  # Python 3.10
    pass

try:
    import jsonschema as _jsonschema

    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JSONSCHEMA_AVAILABLE = False

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .core_types import *  # noqa: F403


class StopWhenPredicate(BaseModel):
    """One JMESPath predicate that aborts a job when it matches a partial_output.

    The full validation (length, complexity, parse-correctness) lives in
    ``core/copilot_predicates.validate_stop_when`` so the same bounds are
    enforced regardless of which surface accepts the predicate. This class
    only carries the wire shape — strict bounds checks happen at submit time.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "label": "found_high_severity",
                "expr": "severity == 'high'",
            }
        }
    )

    label: str
    expr: str


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "agent_id": "00000000-0000-0000-0000-000000000001",
                "input_payload": {"ticker": "AAPL"},
                "max_attempts": 3,
            }
        },
    )

    agent_id: str
    input_payload: JSONObject = Field(
        default_factory=dict,
        validation_alias=AliasChoices("input_payload", "input", "task"),
    )
    input_artifacts: list[JSONObject] = Field(
        default_factory=list,
        description=(
            "Optional artifact descriptors for non-JSON or binary inputs. "
            "Each artifact should include at least {name, mime, url_or_base64, size_bytes}."
        ),
    )
    preferred_input_formats: list[str] = Field(
        default_factory=list,
        description="Optional ordered format preferences (e.g., ['application/json', 'image/png', 'application/dwg']).",
    )
    preferred_output_formats: list[str] = Field(
        default_factory=list,
        description="Optional ordered desired output formats (e.g., ['video/mp4', 'model/stl', 'application/pdf']).",
    )
    communication_channel: str | None = Field(
        default=None,
        description="Optional logical channel name for multi-agent collaboration threads.",
    )
    client_id: str | None = Field(
        default=None,
        description=(
            "Optional calling-surface identifier for analytics and routing "
            "(for example: claude-code, codex, gemini-cli, cursor)."
        ),
    )
    protocol_metadata: JSONObject = Field(
        default_factory=dict,
        description="Optional protocol metadata carried into worker input payload.",
    )
    private_task: bool = Field(
        default=False,
        description=(
            "When true, output examples from this job are not persisted to the "
            "agent/model work history."
        ),
    )
    max_attempts: int = Field(default=3, ge=1, le=10)
    parent_job_id: str | None = Field(
        default=None,
        description="Optional parent job ID when creating a delegated child job.",
    )
    parent_cascade_policy: Literal["detach", "fail_children_on_parent_fail"] = Field(
        default="detach",
        description=(
            "Behavior when parent reaches terminal failure. "
            "'detach' keeps child running; 'fail_children_on_parent_fail' fails active descendants."
        ),
    )
    clarification_timeout_seconds: int | None = Field(
        default=None,
        ge=0,
        le=7 * 24 * 3600,
        description=(
            "Optional timeout for awaiting caller clarification. "
            "0/null disables timeout-based action."
        ),
    )
    clarification_timeout_policy: Literal["fail", "proceed"] = Field(
        default="fail",
        description=(
            "Action when clarification timeout is reached. "
            "'fail' marks job failed and refunds per failure flow; 'proceed' resumes running."
        ),
    )
    dispute_window_hours: int = Field(default=72, ge=1, le=24 * 30)
    output_verification_window_seconds: int | None = Field(
        default=86400,
        ge=0,
        le=7 * 24 * 3600,
        description=(
            "Optional caller acceptance window after worker completion. "
            "During this window, settlement is HELD; the caller can accept/"
            "reject via manage_job(action='verify_output'). The window closes "
            "the moment settlement occurs — either because the caller decided, "
            "OR because the deadline elapsed and the sweeper auto-settled. "
            "Once `settled_at` is set, verify_output returns 409 'Job is "
            "already settled' even if `created_at + window_seconds` hasn't "
            "elapsed yet — settlement is the gate, not wall-clock. Audit "
            "2026-05-18 bug #4 — keep this docstring honest with the "
            "implementation in part_009.py::jobs_verify_output_decision. "
            "After settle the only recourse for a bad output is POST "
            "/jobs/{job_id}/dispute within `dispute_window_hours`."
        ),
    )
    callback_url: str | None = Field(
        default=None,
        description=(
            "Optional HTTPS URL the platform will POST to when the job reaches a terminal state "
            "(completed, failed). Body: {job_id, status, output_payload, error_message, settled_at}. "
            "Delivered with retry/backoff via the hook delivery worker. "
            "Verify authenticity with the X-Aztea-Signature header (HMAC-SHA256)."
        ),
    )
    callback_secret: str | None = Field(
        default=None,
        description=(
            "Optional secret used to sign the callback POST body. "
            "The platform computes HMAC-SHA256(secret, body) and sends it as "
            "X-Aztea-Signature: sha256=<hex>. Verify on your end to reject spoofed deliveries."
        ),
    )
    budget_cents: int | None = Field(
        default=None,
        ge=0,
        description="Optional max price the caller is willing to pay in cents. Rejected with 400 if agent.price_cents > budget_cents.",
    )
    max_price_cents: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Alias for budget_cents on a single job. Prefer this name when the "
            "intent is a buyer-side per-hire cap."
        ),
    )
    per_job_cap_cents: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Hard ceiling on this single job's caller charge in cents. Combines "
            "with the per-API-key cap via MIN: the smaller of the two wins. The "
            "gate fires BEFORE wallet hold so no refund is needed. Returns 422 "
            "`job.per_job_cap_exceeded` if the agent's price exceeds the cap. "
            "Distinct from `budget_cents` (which is a soft buyer ceiling) — this "
            "is the trust-rail safety net that cannot be silently bypassed by "
            "variable-pricing agents."
        ),
    )
    fee_bearer_policy: Literal["worker", "caller", "split"] = Field(
        default="caller",
        description=(
            "Who bears platform fees. "
            "'caller' charges caller price+fee, worker gets full listed price. "
            "'worker' keeps caller price unchanged and deducts fee from worker payout. "
            "'split' splits fee between caller and worker."
        ),
    )
    stop_when: list[StopWhenPredicate] | None = Field(
        default=None,
        description=(
            "Optional co-pilot mode stop predicates. Each {label, expr} runs as "
            "JMESPath against every partial_output payload. The first match "
            "transitions the job to 'stopped' and settles billing per "
            "billing_unit. Bounds (count, length, complexity) are enforced via "
            "core.copilot_predicates.validate_stop_when at submit time."
        ),
    )
    billing_unit: Literal["call", "partial"] | None = Field(
        default=None,
        description=(
            "Optional co-pilot mode billing unit. 'call' bills the listed "
            "price once at terminal regardless of how many partials fired. "
            "'partial' bills per emitted partial_output up to the listed "
            "price ceiling. Defaults to 'call' when null."
        ),
    )

    @field_validator("input_artifacts")
    @classmethod
    def input_artifacts_valid(cls, value: list[JSONObject]) -> list[JSONObject]:
        normalized: list[JSONObject] = []
        for item in value or []:
            if not isinstance(item, dict):
                raise ValueError("input_artifacts entries must be objects.")
            normalized.append(item)
        return normalized

    @field_validator("preferred_input_formats", "preferred_output_formats")
    @classmethod
    def format_preferences_valid(cls, value: list[str]) -> list[str]:
        """Deduplicate and lowercase preferred format strings; silently drops empty entries."""
        normalized: list[str] = []
        for item in value or []:
            text = str(item).strip().lower()
            if not text:
                continue
            if text not in normalized:
                normalized.append(text)
        return normalized

    @field_validator("communication_channel")
    @classmethod
    def communication_channel_valid(cls, value: str | None) -> str | None:
        """Trim and cap communication_channel at 128 chars. Returns None for blank input."""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if len(text) > 128:
            raise ValueError("communication_channel must be <= 128 characters.")
        return text

    @field_validator("stop_when", mode="before")
    @classmethod
    def stop_when_accepts_documented_wrappers(cls, value):
        """Accept the documented ``{"any": [...]}`` wrapper as list syntax."""
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("any", "all", "predicates"):
                nested = value.get(key)
                if isinstance(nested, list):
                    return nested
        return value


class JobBatchCreateRequest(BaseModel):
    intent: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional caller-facing goal for the parallel delegation. Stored only "
            "in the response trace so coding agents can explain why the batch exists."
        ),
    )
    max_total_cents: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional hard cap for the whole batch. The batch is rejected before "
            "any charge if the sum of caller charges exceeds this value."
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description=(
            "C2 follow-up, 2026-05-19: caller-supplied dedup key. Two batches "
            "with the same (caller_id, idempotency_key) within 24h return the "
            "SAME job_ids and the second submission does not re-execute. The "
            "request bodies must match (same request_hash); a mismatch returns "
            "409 idempotency.payload_mismatch. While a first call is "
            "in_progress, retries get 409 idempotency.in_progress with a "
            "retry_after_seconds hint."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, validate and estimate the batch without opening escrow "
            "or creating jobs."
        ),
    )
    atomic: bool = Field(
        default=False,
        description=(
            "M-2 (audit 2026-05-19): when true, the entire batch is rejected "
            "with 422 if ANY job spec fails preflight (slug typo, schema "
            "violation, depth limit, stop_when error, per-job cap, etc.). "
            "When false (default) the existing partial-success semantics "
            "apply: valid jobs are enqueued and invalid ones reported in "
            "invalid_jobs[]. Use atomic=true for retry-safe submission "
            "where you want all-or-nothing guarantees before any escrow "
            "opens."
        ),
    )
    jobs: list["JobCreateRequest"] = Field(
        description="Array of job specs (max 250). Each is a JobCreateRequest. Single wallet pre-debit for total cost."
    )


class JobCompleteRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "output_payload": {"signal": "positive"},
                "claim_token": "claim-token-123",
            }
        }
    )

    output_payload: JSONObject
    output_artifacts: list[JSONObject] = Field(
        default_factory=list,
        description=(
            "Optional artifact descriptors for non-JSON or binary outputs. "
            "Each artifact should include at least {name, mime, url_or_base64, size_bytes}."
        ),
    )
    output_format: str | None = Field(
        default=None,
        description="Optional primary output MIME type hint (e.g., video/mp4, image/png, application/step).",
    )
    protocol_metadata: JSONObject = Field(
        default_factory=dict,
        description="Optional protocol metadata attached to output payload.",
    )
    claim_token: str | None = Field(default=None, max_length=128)

    @field_validator("output_artifacts")
    @classmethod
    def output_artifacts_valid(cls, value: list[JSONObject]) -> list[JSONObject]:
        normalized: list[JSONObject] = []
        for item in value or []:
            if not isinstance(item, dict):
                raise ValueError("output_artifacts entries must be objects.")
            normalized.append(item)
        return normalized

    @field_validator("output_format")
    @classmethod
    def output_format_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        return text or None


class JobFailRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error_message": "Input field 'ticker' is required but missing.",
                "claim_token": "claim-token-123",
                "refund_fraction": 0.5,
            }
        }
    )

    error_message: str | None = Field(default=None, max_length=2000)
    claim_token: str | None = Field(default=None, max_length=128)
    refund_fraction: float = Field(
        default=1.0,
        ge=0.5,
        le=1.0,
        description=(
            "Fraction of the charge to refund to the caller (0.5–1.0). "
            "Default 1.0 = full refund. Minimum 0.5 — callers always get at least "
            "50% back on a worker-reported failure."
        ),
    )


class JobRetryRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error_message": "Dependency timeout",
                "retry_delay_seconds": 30,
            }
        }
    )

    error_message: str | None = Field(default=None, max_length=2000)
    retry_delay_seconds: int = Field(default=DEFAULT_RETRY_DELAY_SECONDS, ge=0, le=3600)
    claim_token: str | None = Field(default=None, max_length=128)


class JobClaimRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"lease_seconds": 300}})

    lease_seconds: int = Field(default=DEFAULT_LEASE_SECONDS, ge=1, le=3600)


class JobHeartbeatRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"lease_seconds": 300, "claim_token": "claim-token-123"}
        }
    )

    lease_seconds: int = Field(default=DEFAULT_LEASE_SECONDS, ge=1, le=3600)
    claim_token: str | None = Field(default=None, max_length=128)


class JobReleaseRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"claim_token": "claim-token-123"}}
    )

    claim_token: str | None = Field(default=None, max_length=128)


class JobCancelRequest(BaseModel):
    """Buyer-side cancel request for an in-flight async job.

    Owners may abort jobs in pending/claimed/awaiting_clarification status. The
    server returns a structured 409 for terminal states (complete/failed) so the
    client never has to guess whether the cancel succeeded.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"reason": "No longer needed — duplicate submission."}
        }
    )

    reason: str | None = Field(default=None, max_length=200)


class JobRatingRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"rating": 5}})

    rating: int = Field(ge=1, le=5)


class JobDisputeRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "reason": "Output missed key risk factors from the filing.",
                "evidence": "https://example.com/evidence/filing-risk-section",
            }
        }
    )

    reason: str
    evidence: str | None = None

    @field_validator("reason")
    @classmethod
    def dispute_reason_not_empty(cls, value: str) -> str:
        # F20 (red-team 2026-05-19): the validator only stripped whitespace
        # — NUL bytes and other control characters passed through, hit
        # psycopg2's parameter encoder, and surfaced as raw
        # "A string literal cannot contain NUL (0x00) characters."
        # without the structured envelope callers expect. Strip ALL control
        # characters before the empty-check so the validator gives a clean
        # 422 either way.
        if not isinstance(value, str):
            raise ValueError("reason must be a string")
        sanitized = "".join(
            ch for ch in value if ch == "\n" or ch == "\t" or (ord(ch) >= 0x20 and ord(ch) != 0x7F)
        )
        text = sanitized.strip()
        if not text:
            raise ValueError("reason must not be empty")
        return text

    @field_validator("evidence")
    @classmethod
    def dispute_evidence_strip_controls(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("evidence must be a string or null")
        sanitized = "".join(
            ch for ch in value if ch == "\n" or ch == "\t" or (ord(ch) >= 0x20 and ord(ch) != 0x7F)
        )
        return sanitized


class JobVerificationDecisionRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "decision": "reject",
                "reason": "Output omitted required risk analysis section.",
                "evidence": "https://example.com/evidence/risk-section",
            }
        }
    )

    decision: Literal["accept", "reject"]
    reason: str | None = None
    evidence: str | None = None

    @field_validator("reason")
    @classmethod
    def verification_reason_normalize(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def validate_reject_reason(self) -> "JobVerificationDecisionRequest":
        if self.decision == "reject" and not self.reason:
            raise ValueError("reason is required when decision is 'reject'.")
        return self


class JobRateCallerRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "rating": 4,
                "comment": "Clear requirements and fast responses.",
            }
        }
    )

    rating: int = Field(ge=1, le=5)
    comment: str | None = None

    @field_validator("comment")
    @classmethod
    def caller_comment_normalize(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class AdminDisputeRuleRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "outcome": "split",
                "split_caller_cents": 6,
                "split_agent_cents": 4,
                "reasoning": "Both parties partially met obligations.",
            }
        }
    )

    outcome: Literal["caller_wins", "agent_wins", "split", "void"]
    split_caller_cents: int | None = Field(default=None, ge=0)
    split_agent_cents: int | None = Field(default=None, ge=0)
    reasoning: str

    @field_validator("reasoning")
    @classmethod
    def rule_reasoning_not_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("reasoning must not be empty")
        return text

    @model_validator(mode="after")
    def validate_split_fields(self) -> "AdminDisputeRuleRequest":
        if self.outcome == "split":
            if self.split_caller_cents is None or self.split_agent_cents is None:
                raise ValueError(
                    "split outcomes require split_caller_cents and split_agent_cents"
                )
        return self


class AgentReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "decision": "approve",
                "note": "Endpoint passed review checklist.",
            }
        }
    )

    decision: Literal["approve", "reject"]
    note: str | None = None

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class AgentSuspendRequest(BaseModel):
    """Optional body for POST /admin/agents/{id}/suspend."""

    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Human-readable reason for the suspension, stored on the agent row.",
    )
