"""Pydantic models (split from legacy models.py for maintainability)."""
from __future__ import annotations

import re
from typing import Annotated, Literal, NotRequired, TypeAlias, TypedDict

try:
    import jsonschema as _jsonschema
    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JSONSCHEMA_AVAILABLE = False

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

"""
api_models.py — Request body schemas shared by server routes.
"""

import re
from typing import Annotated, Literal, NotRequired, TypeAlias, TypedDict

try:
    import jsonschema as _jsonschema
    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JSONSCHEMA_AVAILABLE = False

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
    max_spend_cents: NotRequired[int | None]
    per_job_cap_cents: NotRequired[int | None]
    legal_acceptance_required: NotRequired[bool]
    legal_accepted_at: NotRequired[str | None]
    terms_version_current: NotRequired[str]
    privacy_version_current: NotRequired[str]
    terms_version_accepted: NotRequired[str | None]
    privacy_version_accepted: NotRequired[str | None]


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
        "agent_message",
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
    context: str = ""

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
        if v not in ("full", "quick", "claims", "rhetoric"):
            raise ValueError("mode must be one of: full, quick, claims, rhetoric")
        return v


class WikiRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"topic": "Capital asset pricing model", "depth": "standard"}})

    topic: str
    depth: str = "standard"

    @field_validator("topic")
    @classmethod
    def topic_not_empty(cls, v):
        s = v.strip()
        if not s:
            raise ValueError("topic must not be empty.")
        if len(s) > 300:
            raise ValueError("topic must be 300 characters or fewer.")
        return s

    @field_validator("depth")
    @classmethod
    def depth_valid(cls, v):
        if v not in ("standard", "deep"):
            raise ValueError("depth must be 'standard' or 'deep'")
        return v


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
    style: str = "principled"

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
    key_variables: list[str] = Field(default_factory=list)

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
    stage: str = "seed"

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
    existing_holdings: str = ""
    constraints: str = ""

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
                "healthcheck_url": "https://example.com/health",
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
    healthcheck_url: str | None = None
    price_per_call_usd: float
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def name_valid(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Agent name is required.")
        if len(s) < 3:
            raise ValueError("Agent name must be at least 3 characters.")
        if len(s) > 100:
            raise ValueError("Agent name must be 100 characters or fewer.")
        letters = [c for c in s if c.isalpha()]
        if letters and sum(1 for c in letters if c.isupper()) / len(letters) >= 0.8:
            raise ValueError("Agent name appears to be all-caps. Use title case, e.g. 'Financial Analyst'.")
        if not re.search(r'[A-Za-z]', s):
            raise ValueError("Agent name must contain at least one letter.")
        return s

    @field_validator("description")
    @classmethod
    def description_valid(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Description is required.")
        if len(s) < 10:
            raise ValueError("Description must be at least 10 characters.")
        if len(s) > 2000:
            raise ValueError("Description must be 2000 characters or fewer.")
        return s

    @field_validator("description")
    @classmethod
    def description_quality(cls, v: str) -> str:
        s = v.strip()
        words = s.split()
        if len(words) < 3:
            raise ValueError("Description must be at least 3 words — help callers understand what your agent does.")
        if not re.search(r'[A-Za-z]', s):
            raise ValueError("Description must contain at least one letter.")
        return s

    @field_validator("price_per_call_usd")
    @classmethod
    def price_valid(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Price cannot be negative.")
        if v > 25.0:
            raise ValueError("Price per call cannot exceed $25.00.")
        return v

    @field_validator("tags")
    @classmethod
    def tags_valid(cls, v: list[str]) -> list[str]:
        cleaned = [t.strip().lower() for t in v if t.strip()]
        seen: set[str] = set()
        deduped = []
        for t in cleaned:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        if len(deduped) > 10:
            raise ValueError("At most 10 tags are allowed.")
        for t in deduped:
            if len(t) > 32:
                raise ValueError(f"Tag '{t[:20]}...' is too long — tags must be 32 characters or fewer.")
        return deduped

    @field_validator("input_schema", "output_schema")
    @classmethod
    def schema_valid(cls, v: dict) -> dict:
        if not v:
            return v
        if _JSONSCHEMA_AVAILABLE:
            try:
                _jsonschema.Draft202012Validator.check_schema(v)
            except _jsonschema.exceptions.SchemaError as exc:
                raise ValueError(f"Invalid JSON schema: {exc.message}") from exc
        # Depth and property count guards
        def _depth(obj, current=0):
            if current > 5:
                return current
            if isinstance(obj, dict):
                return max((_depth(vv, current + 1) for vv in obj.values()), default=current)
            if isinstance(obj, list):
                return max((_depth(item, current + 1) for item in obj), default=current)
            return current
        if _depth(v) > 5:
            raise ValueError("Schema nesting depth exceeds 5 levels. Flatten your schema.")
        props = v.get("properties", {})
        if isinstance(props, dict) and len(props) > 50:
            raise ValueError(f"Schema defines {len(props)} properties — maximum is 50.")
        return v
    input_schema: JSONObject = Field(default_factory=dict)
    output_schema: JSONObject = Field(default_factory=dict)
    output_verifier_url: str | None = None
    output_examples: list[JSONObject] | None = Field(
        default=None,
        description=(
            "Optional list of {input, output} example pairs. Shown in discovery "
            "so orchestrators can evaluate quality before hiring."
        ),
    )
    model_provider: str | None = Field(
        default=None,
        description="LLM provider used by this agent, if any.",
    )
    model_id: str | None = Field(
        default=None,
        max_length=128,
        description="Specific model identifier (e.g. 'llama-3.3-70b-versatile').",
    )

    @field_validator("model_provider")
    @classmethod
    def model_provider_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value).strip().lower()).strip("-")
        if not normalized:
            return None
        if len(normalized) > 64:
            raise ValueError("model_provider must be <= 64 characters.")
        return normalized

    @field_validator("model_id")
    @classmethod
    def model_id_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


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
        json_schema_extra={"example": {"return_url": "https://aztea.dev/wallet", "refresh_url": "https://aztea.dev/wallet"}}
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
        s = v.strip()
        if not s:
            raise ValueError("Username is required.")
        if len(s) < 3:
            raise ValueError("Username must be at least 3 characters.")
        if len(s) > 32:
            raise ValueError("Username must be 32 characters or fewer.")
        if not re.match(r'^[a-zA-Z0-9_-]+$', s):
            raise ValueError("Username may only contain letters, numbers, underscores, and hyphens.")
        return s

    @field_validator("email")
    @classmethod
    def email_valid(cls, v):
        s = v.strip().lower()
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$', s):
            raise ValueError("Enter a valid email address.")
        return s

    @field_validator("password")
    @classmethod
    def password_length(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters.")
        if len(v) > 1024:
            raise ValueError("Password must be at most 1024 characters.")
        if not re.search(r'[A-Za-z]', v):
            raise ValueError("Password must contain at least one letter.")
        if not re.search(r'\d', v):
            raise ValueError("Password must contain at least one number.")
        return v


class UserLoginRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"email": "builder@example.com", "password": "password123"}})

    email: str
    password: str

    @field_validator("password")
    @classmethod
    def login_password_length(cls, v):
        if len(v) > 1024:
            raise ValueError("Password must be at most 1024 characters")
        return v


class AuthLegalAcceptRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"terms_version": "2026-04-19", "privacy_version": "2026-04-19"}}
    )

    terms_version: str
    privacy_version: str


class CreateKeyRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Worker key",
                "scopes": ["worker", "caller"],
                "max_spend_cents": 5000,
                "per_job_cap_cents": 1000,
            }
        }
    )

    name: str = Field(default="New key", max_length=64)
    scopes: list[str] = Field(default_factory=lambda: list(_auth.DEFAULT_KEY_SCOPES))
    max_spend_cents: int | None = Field(default=None, ge=0, le=1_000_000)
    per_job_cap_cents: int | None = Field(default=None, ge=0, le=1_000_000)

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
        json_schema_extra={
            "example": {
                "name": "Rotated worker key",
                "scopes": ["worker"],
                "max_spend_cents": 10000,
                "per_job_cap_cents": 2500,
            }
        }
    )

    name: str | None = None
    scopes: list[str] | None = None
    max_spend_cents: int | None = Field(default=None, ge=0)
    per_job_cap_cents: int | None = Field(default=None, ge=0)

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


