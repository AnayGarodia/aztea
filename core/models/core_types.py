"""Pydantic models (split from legacy models.py for maintainability)."""
from __future__ import annotations

import re
from typing import Annotated, Literal, TypeAlias, TypedDict

try:
    from typing import NotRequired
except ImportError:  # Python 3.10
    from typing_extensions import NotRequired

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
                "pii_safe": True,
                "outputs_not_stored": True,
                "audit_logged": True,
                "region_locked": "us",
                "cacheable": True,
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
    pii_safe: bool = False
    outputs_not_stored: bool = False
    audit_logged: bool = False
    region_locked: str | None = Field(default=None, max_length=32)
    payout_curve: dict | None = None
    cacheable: bool | None = Field(
        default=None,
        description="Whether identical calls may be served from result cache. Null lets the platform decide.",
    )

    @field_validator("name")
    @classmethod
    def name_valid(cls, v: str) -> str:
        """Validate agent name: required, 3–100 chars, alphanumeric + limited punctuation."""
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
        """Validate that description is 10–2000 characters after stripping whitespace."""
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
        """Normalise tags: lowercase, strip whitespace, deduplicate, max 20."""
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

    @field_validator("region_locked")
    @classmethod
    def region_locked_valid(cls, v: str | None) -> str | None:
        """Normalise region_locked to a lowercase slug (max 32 chars). Returns None for empty input."""
        if v is None:
            return None
        normalized = re.sub(r"[^a-z0-9-]+", "-", str(v).strip().lower()).strip("-")
        if not normalized:
            return None
        if len(normalized) > 32:
            raise ValueError("region_locked must be 32 characters or fewer.")
        return normalized

    @field_validator("input_schema", "output_schema")
    @classmethod
    def schema_valid(cls, v: dict) -> dict:
        """Validate an input/output schema dict against JSON Schema Draft 2020-12 (when jsonschema is available)."""
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
            "Required list of {input, output} example pairs. Shown in "
            "discovery so orchestrators can evaluate quality before hiring. "
            "Must contain at least one entry."
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
    pricing_model: Literal["fixed", "per_unit", "tiered"] = Field(
        default="fixed",
        description=(
            "Billing strategy. 'fixed' uses price_per_call_usd per invocation. "
            "'per_unit' and 'tiered' derive the charge from an input field "
            "named in pricing_config.input_field."
        ),
    )
    pricing_config: JSONObject | None = Field(
        default=None,
        description=(
            "Pricing configuration when pricing_model is not 'fixed'. "
            "Shape (per_unit): {unit, rate_cents_per_unit, min_cents, max_cents, input_field, multipliers?}. "
            "Shape (tiered): {unit, input_field, min_cents, max_cents, tiers:[{up_to_units, cents}]}."
        ),
    )

    @model_validator(mode="after")
    def _require_documented_schema(self) -> "AgentRegisterRequest":
        schema = self.input_schema or {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(props, dict) and props:
            undocumented: list[str] = []
            for name, spec in props.items():
                if not isinstance(spec, dict):
                    undocumented.append(str(name))
                    continue
                title = str(spec.get("title") or "").strip()
                description = str(spec.get("description") or "").strip()
                if not title and not description:
                    undocumented.append(str(name))
            if undocumented:
                bad = ", ".join(undocumented[:5])
                raise ValueError(
                    "Every input_schema.properties[*] must have a `title` or "
                    f"`description`. Missing on: {bad}."
                )
        examples = self.output_examples
        # Treat None and [] identically — supply a minimal default so internal
        # and builtin registrations don't require examples in their specs.
        # Public marketplace listings should provide real examples but we
        # enforce this via documentation rather than a hard schema error.
        if not examples:
            self.output_examples = [{"input": {}, "output": {}}]
            return self
        for idx, example in enumerate(examples):
            if not isinstance(example, dict):
                raise ValueError(
                    f"output_examples[{idx}] must be an object with 'input' and 'output' keys."
                )
            if "input" not in example or "output" not in example:
                raise ValueError(
                    f"output_examples[{idx}] must include both 'input' and 'output' keys."
                )
        if self.pricing_model != "fixed" and not self.pricing_config:
            raise ValueError(
                "pricing_config is required when pricing_model is not 'fixed'."
            )
        if self.pricing_model == "fixed" and self.pricing_config:
            raise ValueError(
                "pricing_config must be null when pricing_model is 'fixed'."
            )
        if self.pricing_config is not None:
            try:
                from core.registry.pricing import validate_pricing_config
                validate_pricing_config(self.pricing_model, self.pricing_config)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        return self

    @field_validator("model_provider")
    @classmethod
    def model_provider_valid(cls, value: str | None) -> str | None:
        """Normalise model_provider to a lowercase slug (max 64 chars). Returns None for empty input."""
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


class AgentWalletSettingsRequest(BaseModel):
    """Request body for PATCH /wallets/agents/{agent_id}/settings.

    All fields are optional; only provided fields update. Use ``None`` to clear
    a previously-set value (e.g. ``daily_spend_limit_cents=None`` removes the cap).
    """
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "display_label": "Production code reviewer",
                "guarantor_enabled": True,
                "guarantor_cap_cents": 500,
                "daily_spend_limit_cents": 1000,
            }
        }
    )

    display_label: str | None = None
    guarantor_enabled: bool | None = None
    guarantor_cap_cents: int | None = None
    daily_spend_limit_cents: int | None = None


class AgentWalletSweepRequest(BaseModel):
    """Request body for POST /wallets/agents/{agent_id}/sweep.

    Omit ``amount_cents`` to sweep the full current balance.
    """
    model_config = ConfigDict(
        json_schema_extra={"example": {"amount_cents": 1000}}
    )

    amount_cents: int | None = None


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
    role: str = "both"

    @field_validator("role")
    @classmethod
    def role_valid(cls, v):
        if v not in {"builder", "hirer", "both"}:
            raise ValueError("role must be 'builder', 'hirer', or 'both'.")
        return v

    @field_validator("username")
    @classmethod
    def username_not_empty(cls, v):
        """Validate username: required, 3–50 chars, no spaces."""
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
        """Validate password is at least 8 characters."""
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
    # Accept either ``email`` or ``username`` as the identifier so callers using
    # either field name don't get a confusing 422. Internally we always resolve
    # to the email address — username lookups go through a quick pre-flight on
    # the route. ``rotate=true`` opts the caller into the legacy behaviour of
    # minting a fresh session key on every login; default behaviour now reuses
    # the existing active session key when one exists.
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "builder@example.com",
                "password": "password123",
            }
        }
    )

    email: str | None = None
    username: str | None = None
    password: str
    rotate: bool = False

    @model_validator(mode="after")
    def require_identifier(self):
        if not (self.email or self.username):
            raise ValueError("Provide either 'email' or 'username'.")
        return self

    @field_validator("password")
    @classmethod
    def login_password_length(cls, v):
        if len(v) > 1024:
            raise ValueError("Password must be at most 1024 characters")
        return v


class GoogleAuthRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"id_token": "eyJhbGciOi..."}})

    id_token: str

    @field_validator("id_token")
    @classmethod
    def id_token_nonempty(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("Google ID token is required.")
        if len(v) > 8192:
            raise ValueError("Google ID token is unexpectedly large.")
        return v


class AuthLegalAcceptRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"terms_version": "2026-04-19", "privacy_version": "2026-04-19"}}
    )

    terms_version: str
    privacy_version: str


class CreateKeyRequest(BaseModel):
    # extra="forbid" surfaces typos like {"scope": "worker"} (singular) instead of letting
    # them silently fall through to the default scopes — which previously meant every key
    # was minted as caller+worker regardless of intent.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "name": "Worker key",
                "scopes": ["worker", "caller"],
                "max_spend_cents": 5000,
                "per_job_cap_cents": 1000,
            }
        },
    )

    name: str = Field(default="New key", max_length=64)
    scopes: list[str] = Field(default_factory=lambda: list(_auth.DEFAULT_KEY_SCOPES))
    max_spend_cents: int | None = Field(default=None, ge=0, le=1_000_000)
    per_job_cap_cents: int | None = Field(default=None, ge=0, le=1_000_000)

    @model_validator(mode="before")
    @classmethod
    def coerce_singular_scope(cls, data):
        # Friendly coercion: SDKs and ad-hoc curl users often send a singular ``scope``.
        # Promote it into the canonical ``scopes`` list before field validation so the
        # request is honored instead of silently using defaults.
        if isinstance(data, dict) and "scope" in data and "scopes" not in data:
            data = dict(data)
            scope_value = data.pop("scope")
            if isinstance(scope_value, str):
                data["scopes"] = [scope_value]
            elif isinstance(scope_value, list):
                data["scopes"] = scope_value
            else:
                raise ValueError("'scope' must be a string or list of strings.")
        return data

    @field_validator("scopes")
    @classmethod
    def scopes_valid(cls, scopes):
        """Validate API key scopes against the platform's allowed scope list."""
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
        """Validate scopes for a key rotation request; None means inherit existing scopes."""
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
