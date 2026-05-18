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
    BaseModel,
    ConfigDict,
    Field,
)

from .core_types import *  # noqa: F403


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: JSONValue | None = None


class RateLimitErrorResponse(BaseModel):
    error: Literal["rate_limit_exceeded"]
    retry_after_seconds: int


class DynamicObjectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class DynamicListResponse(BaseModel):
    items: list[JSONObject]


class HealthCheckDetail(BaseModel):
    ok: bool
    latency_ms: float | None = None
    writable: bool | None = None
    rss_mb: float | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    checks: dict[str, HealthCheckDetail] | None = None
    agent_count: int | None = None
    version: str | None = None
    # legacy field kept for backwards compatibility
    agents: int | None = None
    # 1.7.2 — surface the resolved value of AZTEA_RESULT_CACHE_V2 so a
    # cache-disabled prod (N15 in the eval) is visible externally rather
    # than a silent wallet-burning regression. None = enabled (default).
    result_cache_disabled_reason: str | None = None


class ManifestSectionResponse(BaseModel):
    heading: str
    content: str


class ManifestValidationResponse(BaseModel):
    source: str
    sections: dict[str, ManifestSectionResponse]
    registration_metadata: JSONObject


class OnboardingIngestResponse(BaseModel):
    agent_id: str
    source: str
    registration_payload: JSONObject
    agent: JSONObject
    message: str


class AuthRegisterResponse(BaseModel):
    user_id: str
    username: str
    email: str
    raw_api_key: str
    key_id: str
    key_prefix: str
    legal_acceptance_required: bool
    legal_accepted_at: str | None = None
    terms_version_current: str
    privacy_version_current: str
    terms_version_accepted: str | None = None
    privacy_version_accepted: str | None = None


class AuthLoginResponse(BaseModel):
    user_id: str
    username: str
    email: str
    created_at: str
    raw_api_key: str
    key_id: str
    key_prefix: str
    legal_acceptance_required: bool
    legal_accepted_at: str | None = None
    terms_version_current: str
    privacy_version_current: str
    terms_version_accepted: str | None = None
    privacy_version_accepted: str | None = None


class AuthMeMasterResponse(BaseModel):
    type: Literal["master"]
    user_id: None = None
    username: str
    scopes: list[str]


class AuthMeUserResponse(BaseModel):
    user_id: str
    username: str
    email: str
    full_name: str | None = None
    phone: str | None = None
    role: str | None = None
    scopes: list[str]
    legal_acceptance_required: bool
    legal_accepted_at: str | None = None
    terms_version_current: str
    privacy_version_current: str
    terms_version_accepted: str | None = None
    privacy_version_accepted: str | None = None


AuthMeResponse = AuthMeMasterResponse | AuthMeUserResponse


class AuthLegalAcceptResponse(BaseModel):
    user_id: str
    legal_acceptance_required: bool
    legal_accepted_at: str | None = None
    terms_version_current: str
    privacy_version_current: str
    terms_version_accepted: str | None = None
    privacy_version_accepted: str | None = None


class ApiKeyMetadataResponse(BaseModel):
    key_id: str
    key_prefix: str
    name: str
    scopes: list[str]
    max_spend_cents: int | None = None
    per_job_cap_cents: int | None = None
    created_at: str
    last_used_at: str | None = None
    is_active: int


class ApiKeyListResponse(BaseModel):
    keys: list[ApiKeyMetadataResponse]


class ApiKeyCreateResponse(BaseModel):
    raw_key: str
    key_id: str
    key_prefix: str
    name: str
    scopes: list[str]
    max_spend_cents: int | None = None
    per_job_cap_cents: int | None = None


class ApiKeyRotateResponse(BaseModel):
    rotated_key_id: str
    new_key_id: str
    raw_key: str
    key_prefix: str
    name: str
    scopes: list[str]
    max_spend_cents: int | None = None
    per_job_cap_cents: int | None = None


class ApiKeyRevokeResponse(BaseModel):
    revoked: bool


class AgentKeyCreateResponse(BaseModel):
    key_id: str
    agent_id: str
    raw_key: str
    key_prefix: str
    created_at: str


class AgentKeyMetadataResponse(BaseModel):
    key_id: str
    agent_id: str
    key_prefix: str
    name: str
    created_at: str
    revoked_at: str | None = None
    is_active: bool


class AgentKeyListResponse(BaseModel):
    keys: list[AgentKeyMetadataResponse]


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str
    name: str
    description: str
    endpoint_url: str
    healthcheck_url: str | None = None
    price_per_call_usd: float
    caller_charge_cents: int | None = None
    tags: list[str] = Field(default_factory=list)
    input_schema: JSONObject = Field(default_factory=dict)
    output_schema: JSONObject = Field(default_factory=dict)
    output_verifier_url: str | None = None
    output_examples: list | None = None
    verified: bool = False
    endpoint_health_status: str | None = None
    endpoint_consecutive_failures: int | None = None
    endpoint_last_checked_at: str | None = None
    endpoint_last_error: str | None = None
    status: str = "active"
    review_status: str = "approved"
    review_note: str | None = None
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    pii_safe: bool = False
    outputs_not_stored: bool = False
    audit_logged: bool = False
    region_locked: str | None = None
    payout_curve: dict | None = None
    caller_trust_min: float | None = None
    # Discovery signals for orchestrators
    trust_score: float | None = None
    total_calls: int | None = None
    avg_latency_ms: float | None = None
    success_rate: float | None = None
    dispute_rate: float | None = None
    by_client: dict[str, float] | None = None


class RegistryRegisterResponse(BaseModel):
    agent_id: str
    message: str
    review_status: str | None = None
    agent: AgentResponse | None = None


class RegistryAgentsResponse(BaseModel):
    agents: list[AgentResponse]
    count: int


class RegistrySearchResult(BaseModel):
    agent: AgentResponse
    similarity: float
    trust: float
    blended_score: float
    match_reasons: list[str]


class RegistrySearchResponse(BaseModel):
    results: list[RegistrySearchResult]
    count: int


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: str
    agent_id: str
    status: str
    price_cents: int
    caller_charge_cents: int | None = None
    platform_fee_pct_at_create: int | None = None
    fee_bearer_policy: str | None = None
    client_id: str | None = None
    input_payload: JSONObject
    output_payload: JSONObject | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
    claim_owner_id: str | None = None
    claim_token: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    last_heartbeat_at: str | None = None
    attempt_count: int
    max_attempts: int
    parent_job_id: str | None = None
    tree_depth: int | None = None
    parent_cascade_policy: str | None = None
    retry_count: int
    next_retry_at: str | None = None
    last_retry_at: str | None = None
    timeout_count: int
    last_timeout_at: str | None = None
    clarification_timeout_seconds: int | None = None
    clarification_timeout_policy: str | None = None
    clarification_requested_at: str | None = None
    clarification_deadline_at: str | None = None
    latest_message_id: int | None = None
    dispute_window_hours: int | None = None
    dispute_outcome: str | None = None
    judge_verdict: str | None = None
    quality_score: int | None = None
    judge_agent_id: str | None = None
    callback_url: str | None = None
    output_verification_window_seconds: int | None = None
    output_verification_status: str | None = None
    output_verification_deadline_at: str | None = None
    output_verification_decided_at: str | None = None
    output_verification_decision_owner_id: str | None = None
    output_verification_reason: str | None = None


class JobsListResponse(BaseModel):
    jobs: list[JobResponse]
    next_cursor: str | None = None


class A2ATaskSendRequest(BaseModel):
    skill_id: str = Field(
        description="The Aztea agent_id to hire (skill ID in A2A terms)."
    )
    input: JSONObject = Field(
        default_factory=dict, description="Input payload for the agent."
    )
    callback_url: str | None = Field(
        default=None, description="Optional webhook URL for task completion push."
    )
    client_id: str | None = Field(
        default=None,
        description="Optional calling-surface identifier for analytics and routing.",
    )
    metadata: JSONObject = Field(
        default_factory=dict, description="Optional A2A passthrough metadata."
    )


class JobMessageResponse(BaseModel):
    message_id: int
    job_id: str
    from_id: str
    type: str
    payload: JSONObject
    correlation_id: str | None = None
    created_at: str


class JobMessagesResponse(BaseModel):
    messages: list[JobMessageResponse]


class JobRatingResponse(BaseModel):
    rating: JSONObject
    agent_reputation: JSONObject
    clawback: JSONObject | None = None


class JobCallerRatingResponse(BaseModel):
    rating: JSONObject
    caller_reputation: JSONObject


class DisputeJudgmentResponse(BaseModel):
    judgment_id: str
    dispute_id: str
    judge_kind: str
    verdict: str
    reasoning: str
    model: str | None = None
    admin_user_id: str | None = None
    created_at: str


class DisputeResponse(BaseModel):
    dispute_id: str
    job_id: str
    filed_by_owner_id: str
    side: str
    reason: str
    evidence: str | None = None
    filing_deposit_cents: int = 0
    status: str
    outcome: str | None = None
    split_caller_cents: int | None = None
    split_agent_cents: int | None = None
    filed_at: str
    resolved_at: str | None = None
    judgments: list[DisputeJudgmentResponse] = Field(default_factory=list)
    judgments_required: int | None = None
    judgments_queued: int | None = None
    judges_completed: int | None = None
    resolution_by: str | None = None
    next_judge_run_by: str | None = None
    eta_hint: str | None = None


class DisputeJudgeResponse(BaseModel):
    dispute: DisputeResponse
    settlement: JSONObject | None = None


class JobSettlementTraceResponse(BaseModel):
    job_id: str
    agent_id: str
    status: str
    charge_tx_id: str
    price_cents: int
    expected_agent_payout_cents: int
    expected_platform_fee_cents: int
    settled_at: str | None = None
    transactions: list[JSONObject]


class JobEventsResponse(BaseModel):
    events: list[JSONObject]


class JobEventHookListResponse(BaseModel):
    hooks: list[JSONObject]


class JobEventHookDeleteResponse(BaseModel):
    deleted: bool
    hook_id: str


class JobEventHookDeadLetterResponse(BaseModel):
    deliveries: list[JSONObject]
    count: int


class WalletDepositResponse(BaseModel):
    tx_id: str
    wallet_id: str
    balance_cents: int


class WalletHoldResponse(BaseModel):
    """One reserve-hold row; surfaces in WalletResponse.holds."""

    hold_id: str
    job_id: str
    amount_cents: int
    hold_until: str


class WalletResponse(BaseModel):
    wallet_id: str
    owner_id: str
    balance_cents: int
    # Reserve-hold pattern: held_cents is the sum of active wallet_holds
    # rows for this wallet; available_cents is balance_cents - held_cents.
    # Both default to 0 so existing consumers that don't know about holds
    # continue to read balance_cents unchanged.
    held_cents: int = 0
    available_cents: int = 0
    holds: list[WalletHoldResponse] = Field(default_factory=list)
    caller_trust: float | None = None
    daily_spend_limit_cents: int | None = None
    transactions: list[JSONObject] = Field(default_factory=list)


class WalletDailySpendLimitRequest(BaseModel):
    # 1.7.2 — strict-field rejection. Pre-1.7.2 the model accepted unknown
    # fields silently; callers sending {"limit_cents": 50} (wrong name)
    # got a 200 with the cap CLEARED to null. That's a wallet-control
    # path; silent acceptance of a typo is dangerous. The MCP
    # manage_budget(set_daily_limit) tool was also using "limit_cents" —
    # fixed in the SDK so both ends agree on `daily_spend_limit_cents`.
    model_config = ConfigDict(extra="forbid")

    # Upper bound mirrors the MCP `set_daily_limit` JSON schema (`maximum: 1_000_000`,
    # i.e. $10 000). Pre-1.7.20 the server accepted ~$1M values that the schema rejected,
    # so a misbehaving orchestrator could uncap itself by skipping the MCP shim.
    daily_spend_limit_cents: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        description="Optional rolling 24h spend cap in cents. null clears the cap. Max 1 000 000 cents ($10 000).",
    )


class WalletDailySpendLimitResponse(BaseModel):
    wallet_id: str
    daily_spend_limit_cents: int | None = None


class WalletWithdrawalResponse(BaseModel):
    transfer_id: str
    wallet_id: str
    amount_cents: int
    stripe_tx_id: str
    memo: str | None = None
    created_at: str
    status: str


class WalletWithdrawalsResponse(BaseModel):
    withdrawals: list[WalletWithdrawalResponse] = Field(default_factory=list)
    count: int = 0


class RunsResponse(BaseModel):
    runs: list[JSONObject]
    skipped_lines: int = 0
    skipped_line_numbers: list[int] = Field(default_factory=list)
