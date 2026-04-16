"""
api_models.py — Request body schemas shared by server routes.
"""

from typing import Annotated, Any, Literal, NotRequired, TypeAlias, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from core import auth as _auth

DEFAULT_LEASE_SECONDS = 300
DEFAULT_RETRY_DELAY_SECONDS = 30
DEFAULT_SLA_SECONDS = 900
DEFAULT_HOOK_DELIVERY_BATCH_SIZE = 50
JSONValue: TypeAlias = JsonValue
JSONObject: TypeAlias = dict[str, JsonValue]


class AuthUser(TypedDict):
    key_id: str
    user_id: str
    username: str
    email: str
    key_name: str
    scopes: list[str]


class CallerContext(TypedDict):
    type: Literal["master", "user", "agent_key"]
    owner_id: str
    scopes: list[str]
    user: NotRequired[AuthUser]
    agent_id: NotRequired[str]
    key_id: NotRequired[str]
LEGACY_JOB_MESSAGE_TYPE_ALIASES = {
    "clarification_needed": "clarification_request",
    "clarification": "clarification_response",
}
TYPED_JOB_MESSAGE_TYPES = frozenset(
    {
        "clarification_request",
        "clarification_response",
        "progress",
        "partial_result",
        "artifact",
        "tool_call",
        "tool_result",
        "note",
    }
)


def _normalize_message_type(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        raise ValueError("type must not be empty")
    return text


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class FinancialRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"ticker": "AAPL"}})

    ticker: str


class CodeReviewRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": "def add(a, b):\n    return a + b\n",
                "language": "python",
                "focus": "bugs",
            }
        }
    )

    code: str
    language: str = "auto"
    focus: str = "all"

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v):
        if not v.strip():
            raise ValueError("code must not be empty")
        return v

    @field_validator("focus")
    @classmethod
    def focus_valid(cls, v):
        valid = {"all", "security", "performance", "bugs", "style"}
        if v not in valid:
            raise ValueError(f"focus must be one of: {', '.join(sorted(valid))}")
        return v


class TextIntelRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "text": "Revenue grew 30% year-over-year while margins compressed.",
                "mode": "quick",
            }
        }
    )

    text: str
    mode: str = "full"

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v):
        if not v.strip():
            raise ValueError("text must not be empty")
        return v

    @field_validator("mode")
    @classmethod
    def mode_valid(cls, v):
        if v not in ("full", "quick"):
            raise ValueError("mode must be 'full' or 'quick'")
        return v


class WikiRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"topic": "Capital asset pricing model"}})

    topic: str

    @field_validator("topic")
    @classmethod
    def topic_not_empty(cls, v):
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v.strip()


class NegotiationRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "objective": "Renew enterprise contract with +15% ARR and annual prepay.",
                "counterparty_profile": "Procurement-led buyer with strict legal review.",
                "constraints": ["No discount above 10%", "Need 24-month commitment"],
                "context": "Incumbent vendor status but increased competitive pressure.",
            }
        }
    )

    objective: str
    counterparty_profile: str = ""
    constraints: list[str] | str = Field(default_factory=list)
    context: str = ""

    @field_validator("objective")
    @classmethod
    def objective_not_empty(cls, v):
        if not v.strip():
            raise ValueError("objective must not be empty")
        return v.strip()

    @field_validator("constraints", mode="before")
    @classmethod
    def constraints_normalized(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        if not text:
            return []
        return [line.strip() for line in text.splitlines() if line.strip()]


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "decision": "Expand to EU market with a direct sales team.",
                "assumptions": "Current ARR is $5M with 30% YoY growth and limited brand awareness in EU.",
                "horizon": "18 months",
                "risk_tolerance": "balanced",
            }
        }
    )

    decision: str
    assumptions: str = ""
    horizon: str = "12 months"
    risk_tolerance: str = "balanced"

    @field_validator("decision")
    @classmethod
    def decision_not_empty(cls, v):
        if not v.strip():
            raise ValueError("decision must not be empty")
        return v.strip()

    @field_validator("risk_tolerance")
    @classmethod
    def risk_tolerance_valid(cls, v):
        valid = {"conservative", "balanced", "aggressive"}
        value = str(v).strip().lower()
        if value not in valid:
            raise ValueError(f"risk_tolerance must be one of: {', '.join(sorted(valid))}")
        return value


class ProductStrategyRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "product_idea": "AI-first customer success copilot for SaaS teams.",
                "target_users": "CSMs at B2B SaaS companies with 20-200 customer accounts.",
                "market_context": "Crowded tooling space, but weak proactive churn prevention.",
                "horizon_quarters": 3,
            }
        }
    )

    product_idea: str
    target_users: str
    market_context: str = ""
    horizon_quarters: int = 2

    @field_validator("product_idea", "target_users")
    @classmethod
    def required_text_not_empty(cls, v):
        if not v.strip():
            raise ValueError("product_idea and target_users must not be empty")
        return v.strip()

    @field_validator("horizon_quarters")
    @classmethod
    def horizon_quarters_valid(cls, v):
        if v < 1 or v > 8:
            raise ValueError("horizon_quarters must be between 1 and 8")
        return v


class PortfolioRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "investment_goal": "Build a diversified long-term portfolio for financial independence.",
                "risk_profile": "balanced",
                "time_horizon_years": 10,
                "capital_usd": 50000,
            }
        }
    )

    investment_goal: str
    risk_profile: str = "balanced"
    time_horizon_years: int = 5
    capital_usd: float = 100000.0

    @field_validator("investment_goal")
    @classmethod
    def investment_goal_not_empty(cls, v):
        if not v.strip():
            raise ValueError("investment_goal must not be empty")
        return v.strip()

    @field_validator("risk_profile")
    @classmethod
    def portfolio_risk_profile_valid(cls, v):
        valid = {"conservative", "balanced", "aggressive"}
        value = str(v).strip().lower()
        if value not in valid:
            raise ValueError(f"risk_profile must be one of: {', '.join(sorted(valid))}")
        return value

    @field_validator("time_horizon_years")
    @classmethod
    def horizon_years_valid(cls, v):
        if v < 1 or v > 50:
            raise ValueError("time_horizon_years must be between 1 and 50")
        return v

    @field_validator("capital_usd")
    @classmethod
    def capital_non_negative(cls, v):
        if v < 0:
            raise ValueError("capital_usd must be non-negative")
        return v


class AgentRegisterRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Financial Filing Analyst",
                "description": "Summarizes SEC 10-Q filings into investment briefs.",
                "endpoint_url": "https://example.com/analyze",
                "price_per_call_usd": 0.05,
                "tags": ["financial-research", "sec"],
                "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}},
                "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
                "output_verifier_url": "https://example.com/verify",
            }
        }
    )

    name: str
    description: str
    endpoint_url: str
    price_per_call_usd: float
    tags: list[str] = Field(default_factory=list)
    input_schema: JSONObject = Field(default_factory=dict)
    output_schema: JSONObject = Field(default_factory=dict)
    output_verifier_url: str | None = None


class DepositRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"wallet_id": "user:abc123", "amount_cents": 5000, "memo": "initial funding"}}
    )

    wallet_id: str
    amount_cents: int
    memo: str = "manual deposit"


class TopupSessionRequest(BaseModel):
    """Request body for POST /wallets/topup/session (Stripe Checkout)."""
    model_config = ConfigDict(
        json_schema_extra={"example": {"wallet_id": "wlt-abc123", "amount_cents": 1000}}
    )

    wallet_id: str
    amount_cents: int  # Must be 100–50000 ($1.00–$500.00)


class ConnectOnboardRequest(BaseModel):
    """Request body for POST /wallets/connect/onboard."""
    model_config = ConfigDict(
        json_schema_extra={"example": {"return_url": "https://agentmarket.dev/wallet", "refresh_url": "https://agentmarket.dev/wallet"}}
    )
    return_url: str | None = None
    refresh_url: str | None = None


class WithdrawRequest(BaseModel):
    """Request body for POST /wallets/withdraw."""
    model_config = ConfigDict(
        json_schema_extra={"example": {"amount_cents": 500}}
    )
    amount_cents: int  # Minimum 100 ($1.00)


class UserRegisterRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "agent_builder",
                "email": "builder@example.com",
                "password": "password123",
            }
        }
    )

    username: str
    email: str
    password: str

    @field_validator("username")
    @classmethod
    def username_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Username cannot be empty")
        return v.strip()

    @field_validator("email")
    @classmethod
    def email_valid(cls, v):
        if "@" not in v or "." not in v:
            raise ValueError("Enter a valid email address")
        return v.strip().lower()

    @field_validator("password")
    @classmethod
    def password_length(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class UserLoginRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"email": "builder@example.com", "password": "password123"}})

    email: str
    password: str


class CreateKeyRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"name": "Worker key", "scopes": ["worker", "caller"]}}
    )

    name: str = "New key"
    scopes: list[str] = Field(default_factory=lambda: list(_auth.DEFAULT_KEY_SCOPES))

    @field_validator("scopes")
    @classmethod
    def scopes_valid(cls, scopes):
        valid = _auth.VALID_KEY_SCOPES
        normalized: list[str] = []
        for scope in scopes:
            value = str(scope).strip().lower()
            if value not in valid:
                raise ValueError(f"Invalid scope '{value}'. Valid scopes: {', '.join(sorted(valid))}")
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ValueError("At least one scope is required.")
        return normalized


class RotateKeyRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"name": "Rotated worker key", "scopes": ["worker"]}}
    )

    name: str | None = None
    scopes: list[str] | None = None

    @field_validator("scopes")
    @classmethod
    def rotate_scopes_valid(cls, scopes):
        if scopes is None:
            return None
        valid = _auth.VALID_KEY_SCOPES
        normalized: list[str] = []
        for scope in scopes:
            value = str(scope).strip().lower()
            if value not in valid:
                raise ValueError(f"Invalid scope '{value}'. Valid scopes: {', '.join(sorted(valid))}")
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ValueError("At least one scope is required.")
        return normalized


class AgentKeyCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"name": "Agent worker key"}}
    )

    name: str = "Agent worker key"


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "agent_id": "00000000-0000-0000-0000-000000000001",
                "input_payload": {"ticker": "AAPL"},
                "max_attempts": 3,
            }
        }
    )

    agent_id: str
    input_payload: JSONObject = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)
    dispute_window_hours: int = Field(default=72, ge=1, le=24 * 30)
    callback_url: str | None = Field(
        default=None,
        description=(
            "Optional HTTPS URL the platform will POST to when the job reaches a terminal state "
            "(completed, failed). Body: {job_id, status, output_payload, error_message, settled_at}. "
            "Delivered with retry/backoff via the hook delivery worker. "
            "Verify authenticity with the X-AgentMarket-Signature header (HMAC-SHA256)."
        ),
    )


class JobCompleteRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"output_payload": {"signal": "positive"}, "claim_token": "claim-token-123"}}
    )

    output_payload: JSONObject
    claim_token: str | None = None


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

    error_message: str | None = None
    claim_token: str | None = None
    refund_fraction: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of the charge to refund to the caller (0.0–1.0). "
            "Default 1.0 = full refund. Set lower when the agent spent compute "
            "before failing, e.g. 0.0 for bad-input rejections the agent "
            "couldn't have avoided."
        ),
    )


class JobRetryRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"error_message": "Dependency timeout", "retry_delay_seconds": 30}}
    )

    error_message: str | None = None
    retry_delay_seconds: int = Field(default=DEFAULT_RETRY_DELAY_SECONDS, ge=0, le=3600)
    claim_token: str | None = None


class JobClaimRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"lease_seconds": 300}})

    lease_seconds: int = Field(default=DEFAULT_LEASE_SECONDS, ge=1, le=3600)


class JobHeartbeatRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"lease_seconds": 300, "claim_token": "claim-token-123"}})

    lease_seconds: int = Field(default=DEFAULT_LEASE_SECONDS, ge=1, le=3600)
    claim_token: str | None = None


class JobReleaseRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"claim_token": "claim-token-123"}})

    claim_token: str | None = None


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
        text = value.strip()
        if not text:
            raise ValueError("reason must not be empty")
        return text


class JobRateCallerRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"rating": 4, "comment": "Clear requirements and fast responses."}}
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
                raise ValueError("split outcomes require split_caller_cents and split_agent_cents")
        return self


class ClarificationRequestPayload(BaseModel):
    question: str
    input_schema: JSONObject | None = None

    @field_validator("question")
    @classmethod
    def question_not_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("question must not be empty")
        return text


class ClarificationResponsePayload(BaseModel):
    answer: JSONObject | str
    request_message_id: int = Field(ge=1)

    @field_validator("answer")
    @classmethod
    def answer_valid(cls, value: JSONObject | str) -> JSONObject | str:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("answer must not be empty")
            return text
        return value


class LegacyClarificationResponsePayload(BaseModel):
    answer: JSONObject | str
    request_message_id: int | None = Field(default=None, ge=1)

    @field_validator("answer")
    @classmethod
    def legacy_answer_valid(cls, value: JSONObject | str) -> JSONObject | str:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("answer must not be empty")
            return text
        return value


class ProgressPayload(BaseModel):
    percent: int = Field(ge=0, le=100)
    note: str | None = None

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class PartialResultPayload(BaseModel):
    payload: JSONObject = Field(default_factory=dict)
    is_final: Literal[False] = False


class ArtifactPayload(BaseModel):
    name: str
    mime: str
    url_or_base64: str
    size_bytes: int = Field(ge=0)

    @field_validator("name", "mime", "url_or_base64")
    @classmethod
    def text_fields_not_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("value must not be empty")
        return text


class ToolCallPayload(BaseModel):
    tool_name: str
    args: JSONObject = Field(default_factory=dict)
    correlation_id: str | None = None

    @field_validator("tool_name")
    @classmethod
    def tool_name_not_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("tool_name must not be empty")
        return text

    @field_validator("correlation_id")
    @classmethod
    def normalize_correlation(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class ToolResultPayload(BaseModel):
    correlation_id: str
    payload: JSONObject = Field(default_factory=dict)
    error: str | None = None

    @field_validator("correlation_id")
    @classmethod
    def correlation_required(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("correlation_id must not be empty")
        return text

    @field_validator("error")
    @classmethod
    def normalize_error(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class NotePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("text must not be empty")
        return text


class _TypedJobMessageBase(BaseModel):
    correlation_id: str | None = None

    @field_validator("correlation_id")
    @classmethod
    def normalize_message_correlation(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class ClarificationRequestMessage(_TypedJobMessageBase):
    type: Literal["clarification_request"]
    payload: ClarificationRequestPayload


class ClarificationResponseMessage(_TypedJobMessageBase):
    type: Literal["clarification_response"]
    payload: ClarificationResponsePayload


class ProgressMessage(_TypedJobMessageBase):
    type: Literal["progress"]
    payload: ProgressPayload


class PartialResultMessage(_TypedJobMessageBase):
    type: Literal["partial_result"]
    payload: PartialResultPayload


class ArtifactMessage(_TypedJobMessageBase):
    type: Literal["artifact"]
    payload: ArtifactPayload


class ToolCallMessage(_TypedJobMessageBase):
    type: Literal["tool_call"]
    payload: ToolCallPayload

    @model_validator(mode="after")
    def sync_correlation_id(self):
        corr = _normalize_optional_text(self.correlation_id or self.payload.correlation_id)
        self.correlation_id = corr
        self.payload.correlation_id = corr
        return self


class ToolResultMessage(_TypedJobMessageBase):
    type: Literal["tool_result"]
    payload: ToolResultPayload

    @model_validator(mode="after")
    def sync_correlation_id(self):
        corr = _normalize_optional_text(self.correlation_id or self.payload.correlation_id)
        if corr is None:
            raise ValueError("correlation_id is required for tool_result messages")
        self.correlation_id = corr
        self.payload.correlation_id = corr
        return self


class NoteMessage(_TypedJobMessageBase):
    type: Literal["note"]
    payload: NotePayload


TypedJobMessage = Annotated[
    (
        ClarificationRequestMessage
        | ClarificationResponseMessage
        | ProgressMessage
        | PartialResultMessage
        | ArtifactMessage
        | ToolCallMessage
        | ToolResultMessage
        | NoteMessage
    ),
    Field(discriminator="type"),
]

_TYPED_JOB_MESSAGE_ADAPTER = TypeAdapter(TypedJobMessage)


def parse_typed_job_message(message_body: JSONObject) -> TypedJobMessage:
    if not isinstance(message_body, dict):
        raise ValueError("message_body must be an object.")
    return _TYPED_JOB_MESSAGE_ADAPTER.validate_python(message_body)


def canonical_job_message_type(msg_type: str, *, allow_legacy: bool = True) -> str:
    normalized_type = _normalize_message_type(msg_type)
    if not allow_legacy:
        return normalized_type
    return LEGACY_JOB_MESSAGE_TYPE_ALIASES.get(normalized_type, normalized_type)


def _normalize_typed_payload_for_compat(msg_type: str, payload: JSONObject) -> JSONObject:
    normalized = dict(payload)

    if msg_type == "clarification_request":
        question = str(normalized.get("question") or "").strip()
        if not question:
            raise ValueError("clarification_request payload.question is required.")
        normalized["question"] = question
        schema = normalized.get("input_schema") or normalized.get("schema")
        if schema is not None and not isinstance(schema, dict):
            raise ValueError("clarification_request payload.input_schema must be an object.")
        if schema is not None:
            normalized["input_schema"] = schema
            normalized.pop("schema", None)
        return normalized

    if msg_type == "clarification_response":
        answer = normalized.get("answer")
        if isinstance(answer, str):
            answer = answer.strip()
        if answer in (None, ""):
            raise ValueError("clarification_response payload.answer is required.")
        normalized["answer"] = answer
        return normalized

    if msg_type == "progress":
        percent_raw = normalized.get("percent")
        if percent_raw is None:
            raise ValueError("progress payload.percent is required.")
        try:
            percent = int(percent_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("progress payload.percent must be an integer between 0 and 100.") from exc
        if percent < 0 or percent > 100:
            raise ValueError("progress payload.percent must be an integer between 0 and 100.")
        normalized["percent"] = percent
        note = str(normalized.get("note") or normalized.get("message") or "").strip()
        if note:
            normalized["note"] = note
        return normalized

    if msg_type == "partial_result":
        partial_payload = normalized.get("payload")
        if partial_payload is None:
            normalized["payload"] = {}
        elif not isinstance(partial_payload, dict):
            raise ValueError("partial_result payload.payload must be an object.")
        if "is_final" not in normalized:
            normalized["is_final"] = False
        return normalized

    if msg_type == "note":
        text = str(normalized.get("text") or normalized.get("note") or normalized.get("message") or "").strip()
        if not text:
            raise ValueError("note payload.text is required.")
        normalized["text"] = text
        return normalized

    if msg_type == "tool_call":
        tool_name = str(normalized.get("tool_name") or normalized.get("name") or "").strip()
        if not tool_name:
            raise ValueError("tool_call payload.tool_name is required.")
        normalized["tool_name"] = tool_name
        args = normalized.get("args")
        if args is None:
            args = normalized.get("arguments")
        if args is None:
            normalized["args"] = {}
        elif not isinstance(args, dict):
            raise ValueError("tool_call payload.args must be an object.")
        else:
            normalized["args"] = args
        corr = _normalize_optional_text(normalized.get("correlation_id"))
        if corr is None:
            normalized.pop("correlation_id", None)
        else:
            normalized["correlation_id"] = corr
        return normalized

    if msg_type == "tool_result":
        corr = _normalize_optional_text(normalized.get("correlation_id"))
        if corr is None:
            raise ValueError("tool_result payload.correlation_id is required.")
        normalized["correlation_id"] = corr
        tool_payload = normalized.get("payload")
        if tool_payload is None:
            tool_payload = normalized.get("result")
        if tool_payload is None:
            normalized["payload"] = {}
        elif not isinstance(tool_payload, dict):
            raise ValueError("tool_result payload.payload must be an object.")
        else:
            normalized["payload"] = tool_payload
        return normalized

    return normalized


def normalize_job_message_body(
    *,
    msg_type: str,
    payload: JSONObject | None = None,
    correlation_id: str | None = None,
    allow_legacy: bool = True,
) -> dict:
    normalized_type = _normalize_message_type(msg_type)
    normalized_payload = payload if payload is not None else {}
    if not isinstance(normalized_payload, dict):
        raise ValueError("payload must be an object.")

    normalized_correlation = _normalize_optional_text(correlation_id)
    canonical_type = canonical_job_message_type(normalized_type, allow_legacy=allow_legacy)

    if normalized_type == "clarification_needed" and allow_legacy:
        payload_model = ClarificationRequestPayload.model_validate(normalized_payload)
        return {
            "type": normalized_type,
            "canonical_type": canonical_type,
            "payload": payload_model.model_dump(),
            "correlation_id": normalized_correlation,
        }

    if normalized_type == "clarification" and allow_legacy:
        if "request_message_id" in normalized_payload:
            payload_model = ClarificationResponsePayload.model_validate(normalized_payload)
            normalized_data = payload_model.model_dump()
        else:
            payload_model = LegacyClarificationResponsePayload.model_validate(normalized_payload)
            normalized_data = payload_model.model_dump(exclude_none=True)
        return {
            "type": normalized_type,
            "canonical_type": canonical_type,
            "payload": normalized_data,
            "correlation_id": normalized_correlation,
        }

    if canonical_type in TYPED_JOB_MESSAGE_TYPES:
        typed_payload = _normalize_typed_payload_for_compat(canonical_type, normalized_payload)
        try:
            parsed = parse_typed_job_message(
                {
                    "type": canonical_type,
                    "payload": typed_payload,
                    "correlation_id": normalized_correlation,
                }
            )
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        dumped = parsed.model_dump()
        return {
            "type": normalized_type,
            "canonical_type": dumped["type"],
            "payload": dumped["payload"],
            "correlation_id": dumped.get("correlation_id"),
        }

    if not allow_legacy:
        raise ValueError(f"Unsupported job message type: {normalized_type}")

    return {
        "type": normalized_type,
        "canonical_type": canonical_type,
        "payload": normalized_payload,
        "correlation_id": normalized_correlation,
    }


class JobMessageRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "progress",
                "payload": {"percent": 42, "note": "Working on sections 2 and 3"},
                "from_id": "user:worker-id",
                "correlation_id": None,
            }
        }
    )

    type: str
    payload: JSONObject = Field(default_factory=dict)
    from_id: str | None = None
    correlation_id: str | None = None


class OnboardingValidateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"manifest_url": "https://example.com/agent.md"}}
    )

    manifest_content: str | None = None
    manifest_url: str | None = None


class JobEventHookCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"target_url": "https://hooks.example.com/job-events", "secret": "hook_secret"}}
    )

    target_url: str
    secret: str | None = None


class HookDeliveryProcessRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"limit": 50}})

    limit: int = Field(default=DEFAULT_HOOK_DELIVERY_BATCH_SIZE, ge=1, le=500)


class JobsSweepRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"retry_delay_seconds": 30, "sla_seconds": 900, "limit": 100}}
    )

    retry_delay_seconds: int = Field(default=DEFAULT_RETRY_DELAY_SECONDS, ge=0, le=3600)
    sla_seconds: int = Field(default=DEFAULT_SLA_SECONDS, ge=60, le=7 * 24 * 3600)
    limit: int = Field(default=100, ge=1, le=500)


class ReconciliationRunRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"max_mismatches": 100}})

    max_mismatches: int = Field(default=100, ge=1, le=1000)


class RegistryCallRequest(RootModel[JSONObject]):
    model_config = ConfigDict(json_schema_extra={"example": {"ticker": "AAPL"}})


class MCPInvokeRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tool_name": "financial_research_agent",
                "input": {"ticker": "AAPL"},
                "api_key": "am_...",
            }
        }
    )

    tool_name: str
    input: JSONObject = Field(default_factory=dict)
    api_key: str

    @field_validator("tool_name")
    @classmethod
    def tool_name_not_empty(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            raise ValueError("tool_name must not be empty")
        return text

    @field_validator("api_key")
    @classmethod
    def api_key_not_empty(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("api_key must not be empty")
        return text


class RegistrySearchRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "query": "I need to summarize a 10-K filing for AAPL",
                "limit": 10,
                "min_trust": 0.2,
                "max_price_cents": 50,
                "required_input_fields": ["ticker"],
                "respect_caller_trust_min": True,
            }
        }
    )

    query: str
    limit: int = Field(default=10, ge=1, le=50)
    min_trust: float = Field(default=0.0, ge=0.0, le=1.0)
    max_price_cents: int | None = Field(default=None, ge=0)
    required_input_fields: list[str] | None = None
    respect_caller_trust_min: bool = False

    @field_validator("query")
    @classmethod
    def query_not_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("query must not be empty")
        return text

    @field_validator("required_input_fields")
    @classmethod
    def required_fields_non_empty(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            field_name = str(item).strip()
            if not field_name:
                raise ValueError("required_input_fields entries must be non-empty strings")
            if field_name in seen:
                continue
            seen.add(field_name)
            normalized.append(field_name)
        return normalized


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


class AuthLoginResponse(BaseModel):
    user_id: str
    username: str
    email: str
    created_at: str
    raw_api_key: str
    key_id: str
    key_prefix: str


class AuthMeMasterResponse(BaseModel):
    type: Literal["master"]
    user_id: None = None
    username: str
    scopes: list[str]


class AuthMeUserResponse(BaseModel):
    user_id: str
    username: str
    email: str
    scopes: list[str]


AuthMeResponse = AuthMeMasterResponse | AuthMeUserResponse


class ApiKeyMetadataResponse(BaseModel):
    key_id: str
    key_prefix: str
    name: str
    scopes: list[str]
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


class ApiKeyRotateResponse(BaseModel):
    rotated_key_id: str
    new_key_id: str
    raw_key: str
    key_prefix: str
    name: str
    scopes: list[str]


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
    price_per_call_usd: float
    tags: list[str] = Field(default_factory=list)
    input_schema: JSONObject = Field(default_factory=dict)
    output_schema: JSONObject = Field(default_factory=dict)
    output_verifier_url: str | None = None
    status: str = "active"
    caller_trust_min: float | None = None
    # Discovery signals for orchestrators
    trust_score: float | None = None
    total_calls: int | None = None
    avg_latency_ms: float | None = None
    success_rate: float | None = None


class RegistryRegisterResponse(BaseModel):
    agent_id: str
    message: str
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
    retry_count: int
    next_retry_at: str | None = None
    last_retry_at: str | None = None
    timeout_count: int
    last_timeout_at: str | None = None
    latest_message_id: int | None = None
    dispute_window_hours: int | None = None
    dispute_outcome: str | None = None
    judge_verdict: str | None = None
    quality_score: int | None = None
    judge_agent_id: str | None = None
    callback_url: str | None = None


class JobsListResponse(BaseModel):
    jobs: list[JobResponse]
    next_cursor: str | None = None


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
    status: str
    outcome: str | None = None
    split_caller_cents: int | None = None
    split_agent_cents: int | None = None
    filed_at: str
    resolved_at: str | None = None
    judgments: list[DisputeJudgmentResponse] = Field(default_factory=list)


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


class WalletResponse(BaseModel):
    wallet_id: str
    owner_id: str
    balance_cents: int
    caller_trust: float | None = None
    transactions: list[JSONObject] = Field(default_factory=list)


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
