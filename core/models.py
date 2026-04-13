"""
api_models.py — Request body schemas shared by server routes.
"""

from pydantic import BaseModel, Field, field_validator

from core import auth as _auth

DEFAULT_LEASE_SECONDS = 300
DEFAULT_RETRY_DELAY_SECONDS = 30
DEFAULT_SLA_SECONDS = 900
DEFAULT_HOOK_DELIVERY_BATCH_SIZE = 50


class FinancialRequest(BaseModel):
    ticker: str


class CodeReviewRequest(BaseModel):
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
    topic: str

    @field_validator("topic")
    @classmethod
    def topic_not_empty(cls, v):
        if not v.strip():
            raise ValueError("topic must not be empty")
        return v.strip()


class AgentRegisterRequest(BaseModel):
    name: str
    description: str
    endpoint_url: str
    price_per_call_usd: float
    tags: list[str] = Field(default_factory=list)
    input_schema: dict = Field(default_factory=dict)


class DepositRequest(BaseModel):
    wallet_id: str
    amount_cents: int
    memo: str = "manual deposit"


class UserRegisterRequest(BaseModel):
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
    email: str
    password: str


class CreateKeyRequest(BaseModel):
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


class JobCreateRequest(BaseModel):
    agent_id: str
    input_payload: dict = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)


class JobCompleteRequest(BaseModel):
    output_payload: dict
    claim_token: str | None = None


class JobFailRequest(BaseModel):
    error_message: str | None = None
    claim_token: str | None = None


class JobRetryRequest(BaseModel):
    error_message: str | None = None
    retry_delay_seconds: int = Field(default=DEFAULT_RETRY_DELAY_SECONDS, ge=0, le=3600)
    claim_token: str | None = None


class JobClaimRequest(BaseModel):
    lease_seconds: int = Field(default=DEFAULT_LEASE_SECONDS, ge=1, le=3600)


class JobHeartbeatRequest(BaseModel):
    lease_seconds: int = Field(default=DEFAULT_LEASE_SECONDS, ge=1, le=3600)
    claim_token: str | None = None


class JobReleaseRequest(BaseModel):
    claim_token: str | None = None


class JobRatingRequest(BaseModel):
    rating: int = Field(ge=1, le=5)


class JobMessageRequest(BaseModel):
    type: str
    payload: dict = Field(default_factory=dict)
    from_id: str | None = None


class OnboardingValidateRequest(BaseModel):
    manifest_content: str | None = None
    manifest_url: str | None = None


class JobEventHookCreateRequest(BaseModel):
    target_url: str
    secret: str | None = None


class HookDeliveryProcessRequest(BaseModel):
    limit: int = Field(default=DEFAULT_HOOK_DELIVERY_BATCH_SIZE, ge=1, le=500)


class JobsSweepRequest(BaseModel):
    retry_delay_seconds: int = Field(default=DEFAULT_RETRY_DELAY_SECONDS, ge=0, le=3600)
    sla_seconds: int = Field(default=DEFAULT_SLA_SECONDS, ge=60, le=7 * 24 * 3600)
    limit: int = Field(default=100, ge=1, le=500)


class ReconciliationRunRequest(BaseModel):
    max_mismatches: int = Field(default=100, ge=1, le=1000)
