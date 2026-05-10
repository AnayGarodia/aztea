"""Pydantic models (split from legacy models.py for maintainability)."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from core.functional import Err, Ok, Result

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
    RootModel,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from . import core_types as _core_types_pkg
from .core_types import *  # noqa: F403

for _k, _v in vars(_core_types_pkg).items():
    if _k.startswith("_") and not _k.startswith("__"):
        globals()[_k] = _v


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
    model_config = ConfigDict(extra="allow")

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


class AgentMessagePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    channel: str
    body: JSONObject | str
    to_id: str | None = None

    @field_validator("channel")
    @classmethod
    def channel_not_empty(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("channel must not be empty")
        if len(text) > 128:
            raise ValueError("channel must be <= 128 characters")
        return text

    @field_validator("body")
    @classmethod
    def body_valid(cls, value: JSONObject | str) -> JSONObject | str:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("body must not be empty")
            return text
        return value

    @field_validator("to_id")
    @classmethod
    def to_id_normalized(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class AgentMessage(_TypedJobMessageBase):
    type: Literal["agent_message"]
    payload: AgentMessagePayload


class ToolCallMessage(_TypedJobMessageBase):
    type: Literal["tool_call"]
    payload: ToolCallPayload

    @model_validator(mode="after")
    def sync_correlation_id(self):
        corr = _normalize_optional_text(
            self.correlation_id or self.payload.correlation_id
        )
        self.correlation_id = corr
        self.payload.correlation_id = corr
        return self


class ToolResultMessage(_TypedJobMessageBase):
    type: Literal["tool_result"]
    payload: ToolResultPayload

    @model_validator(mode="after")
    def sync_correlation_id(self):
        corr = _normalize_optional_text(
            self.correlation_id or self.payload.correlation_id
        )
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
        | AgentMessage
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


_PROGRESS_PERCENT_MIN = 0
_PROGRESS_PERCENT_MAX = 100


def _normalize_clarification_request_payload(normalized: JSONObject) -> JSONObject:
    """Pure: enforce ``question`` required and ``input_schema`` object-typed."""
    question = str(normalized.get("question") or "").strip()
    if not question:
        raise ValueError("clarification_request payload.question is required.")
    normalized["question"] = question
    schema = normalized.get("input_schema") or normalized.get("schema")
    if schema is not None and not isinstance(schema, dict):
        raise ValueError(
            "clarification_request payload.input_schema must be an object."
        )
    if schema is not None:
        normalized["input_schema"] = schema
        normalized.pop("schema", None)
    return normalized


def _normalize_clarification_response_payload(normalized: JSONObject) -> JSONObject:
    """Pure: ``answer`` required and stripped if string."""
    answer = normalized.get("answer")
    if isinstance(answer, str):
        answer = answer.strip()
    if answer in (None, ""):
        raise ValueError("clarification_response payload.answer is required.")
    normalized["answer"] = answer
    return normalized


def _normalize_progress_payload(normalized: JSONObject) -> JSONObject:
    """Pure: ``percent`` integer in [0, 100]; optional ``note`` collapsed from aliases."""
    percent_raw = normalized.get("percent")
    if percent_raw is None:
        raise ValueError("progress payload.percent is required.")
    try:
        percent = int(percent_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "progress payload.percent must be an integer between 0 and 100."
        ) from exc
    if percent < _PROGRESS_PERCENT_MIN or percent > _PROGRESS_PERCENT_MAX:
        raise ValueError(
            "progress payload.percent must be an integer between 0 and 100."
        )
    normalized["percent"] = percent
    note = str(normalized.get("note") or normalized.get("message") or "").strip()
    if note:
        normalized["note"] = note
    return normalized


def _normalize_partial_result_payload(normalized: JSONObject) -> JSONObject:
    """Pure: nested ``payload`` defaults to {}; ``is_final`` defaults to False."""
    partial_payload = normalized.get("payload")
    if partial_payload is None:
        normalized["payload"] = {}
    elif not isinstance(partial_payload, dict):
        raise ValueError("partial_result payload.payload must be an object.")
    if "is_final" not in normalized:
        normalized["is_final"] = False
    return normalized


def _normalize_note_payload(normalized: JSONObject) -> JSONObject:
    """Pure: ``text`` required; collapsed from text/note/message aliases."""
    text = str(
        normalized.get("text")
        or normalized.get("note")
        or normalized.get("message")
        or ""
    ).strip()
    if not text:
        raise ValueError("note payload.text is required.")
    normalized["text"] = text
    return normalized


def _normalize_agent_message_body(normalized: JSONObject) -> None:
    """Side-effect (mutating ``normalized``): enforce body shape (object or non-empty str)."""
    body = normalized.get("body")
    if isinstance(body, str):
        body_text = body.strip()
        if not body_text:
            raise ValueError("agent_message payload.body must not be empty.")
        normalized["body"] = body_text
        return
    if body is None:
        raise ValueError("agent_message payload.body is required.")
    if not isinstance(body, dict):
        raise ValueError(
            "agent_message payload.body must be an object or non-empty string."
        )


def _normalize_agent_message_payload(normalized: JSONObject) -> JSONObject:
    """Pure-ish: enforce channel + body, normalise optional ``to_id``."""
    channel = str(normalized.get("channel") or "").strip()
    if not channel:
        raise ValueError("agent_message payload.channel is required.")
    normalized["channel"] = channel
    _normalize_agent_message_body(normalized)
    to_id = _normalize_optional_text(normalized.get("to_id"))
    if to_id is None:
        normalized.pop("to_id", None)
    else:
        normalized["to_id"] = to_id
    return normalized


def _normalize_tool_call_payload(normalized: JSONObject) -> JSONObject:
    """Pure-ish: ``tool_name`` required, ``args`` defaulted to {}, optional correlation_id."""
    tool_name = str(
        normalized.get("tool_name") or normalized.get("name") or ""
    ).strip()
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


def _normalize_tool_result_payload(normalized: JSONObject) -> JSONObject:
    """Pure-ish: ``correlation_id`` required, nested ``payload`` defaulted to {}."""
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


_TYPED_PAYLOAD_NORMALIZERS = {
    "clarification_request": _normalize_clarification_request_payload,
    "clarification_response": _normalize_clarification_response_payload,
    "progress": _normalize_progress_payload,
    "partial_result": _normalize_partial_result_payload,
    "note": _normalize_note_payload,
    "agent_message": _normalize_agent_message_payload,
    "tool_call": _normalize_tool_call_payload,
    "tool_result": _normalize_tool_result_payload,
}


def _normalize_typed_payload_for_compat(
    msg_type: str, payload: JSONObject,
) -> JSONObject:
    """Pure: dispatch to the per-type normaliser; unknown types are returned unchanged.

    Why: each typed message has its own validation rules; a dispatch
    table replaces the if/elif chain so adding a new type touches only
    one helper plus the table.
    """
    normalized = dict(payload)
    handler = _TYPED_PAYLOAD_NORMALIZERS.get(msg_type)
    if handler is None:
        return normalized
    return handler(normalized)


def _legacy_clarification_request_body(
    normalized_type: str, canonical_type: str,
    normalized_payload: dict, normalized_correlation: str | None,
) -> dict:
    """Pure: shape a legacy ``clarification_needed`` payload via the request schema."""
    payload_model = ClarificationRequestPayload.model_validate(normalized_payload)
    return {
        "type": normalized_type,
        "canonical_type": canonical_type,
        "payload": payload_model.model_dump(),
        "correlation_id": normalized_correlation,
    }


def _legacy_clarification_response_body(
    normalized_type: str, canonical_type: str,
    normalized_payload: dict, normalized_correlation: str | None,
) -> dict:
    """Pure: shape a legacy ``clarification`` payload via the response schema.

    Why: when ``request_message_id`` is present we use the canonical schema;
    older clients omit it, so we fall back to the legacy schema and drop
    null fields so the resulting payload remains schema-valid downstream.
    """
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


def _typed_message_body(
    normalized_type: str, canonical_type: str,
    normalized_payload: dict, normalized_correlation: str | None,
) -> dict:
    """Pure: validate a canonical typed payload against its pydantic model."""
    typed_payload = _normalize_typed_payload_for_compat(canonical_type, normalized_payload)
    try:
        parsed = parse_typed_job_message({
            "type": canonical_type,
            "payload": typed_payload,
            "correlation_id": normalized_correlation,
        })
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    dumped = parsed.model_dump()
    return {
        "type": normalized_type,
        "canonical_type": dumped["type"],
        "payload": dumped["payload"],
        "correlation_id": dumped.get("correlation_id"),
    }


def _validate_normalize_inputs(
    msg_type: str, payload: JSONObject | None, correlation_id: str | None,
) -> tuple[str, dict, str | None]:
    """Pure: lower-case + strip msg_type, ensure payload is a dict, normalise correlation."""
    normalized_type = _normalize_message_type(msg_type)
    normalized_payload = payload if payload is not None else {}
    if not isinstance(normalized_payload, dict):
        raise ValueError("payload must be an object.")
    return normalized_type, dict(normalized_payload), _normalize_optional_text(correlation_id)


def normalize_job_message_body(
    *,
    msg_type: str,
    payload: JSONObject | None = None,
    correlation_id: str | None = None,
    allow_legacy: bool = True,
) -> dict:
    """Pure: validate + normalise the body for an outgoing job message.

    Why: resolves legacy ``msg_type`` aliases, dispatches to the right
    schema-validating helper, and shapes the result into the same dict
    shape regardless of input legacy/canonical form. ``ValueError`` on
    unknown types or structurally invalid payloads.
    """
    normalized_type, normalized_payload, normalized_correlation = (
        _validate_normalize_inputs(msg_type, payload, correlation_id)
    )
    canonical_type = canonical_job_message_type(
        normalized_type, allow_legacy=allow_legacy,
    )
    if normalized_type == "clarification_needed" and allow_legacy:
        return _legacy_clarification_request_body(
            normalized_type, canonical_type, normalized_payload, normalized_correlation,
        )
    if normalized_type == "clarification" and allow_legacy:
        return _legacy_clarification_response_body(
            normalized_type, canonical_type, normalized_payload, normalized_correlation,
        )
    if canonical_type in TYPED_JOB_MESSAGE_TYPES:
        return _typed_message_body(
            normalized_type, canonical_type, normalized_payload, normalized_correlation,
        )
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
    channel: str | None = None
    to_id: str | None = None

    @field_validator("channel")
    @classmethod
    def channel_valid(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("to_id")
    @classmethod
    def to_id_valid(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class OnboardingValidateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"manifest_url": "https://example.com/agent.md"}}
    )

    manifest_content: str | None = None
    manifest_url: str | None = None


class JobEventHookCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "target_url": "https://hooks.example.com/job-events",
                "secret": "hook_secret",
            }
        }
    )

    target_url: str
    secret: str | None = None


class HookDeliveryProcessRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"limit": 50}})

    limit: int = Field(default=DEFAULT_HOOK_DELIVERY_BATCH_SIZE, ge=1, le=500)


class JobsSweepRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"retry_delay_seconds": 30, "sla_seconds": 900, "limit": 100}
        }
    )

    retry_delay_seconds: int = Field(default=DEFAULT_RETRY_DELAY_SECONDS, ge=0, le=3600)
    sla_seconds: int = Field(default=DEFAULT_SLA_SECONDS, ge=60, le=7 * 24 * 3600)
    limit: int = Field(default=100, ge=1, le=500)


class ReconciliationRunRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"max_mismatches": 100}})

    max_mismatches: int = Field(default=100, ge=1, le=1000)


_PAYLOAD_MAX_BYTES = 64 * 1024
_PAYLOAD_MAX_DEPTH = 8
_PAYLOAD_MAX_KEYS = 120
_PAYLOAD_MAX_ARRAY_ITEMS = 200
# WHY: legitimate uses (multi-file Python executor, shell executors) ship
# source code that easily exceeds 4 KB. The 64 KB total cap still bounds
# the request; this per-string cap exists to prevent a single oversized
# text field from skewing storage.
_PAYLOAD_MAX_STRING_LEN = 50_000
_PAYLOAD_MAX_KEY_LEN = 100


def _check_dict_shape(value: dict, state: dict[str, int]) -> None:
    """Pure: enforce key-count + per-key length constraints inside a dict node."""
    state["keys"] += len(value)
    if state["keys"] > _PAYLOAD_MAX_KEYS:
        raise ValueError(
            f"Input payload has too many fields (max {_PAYLOAD_MAX_KEYS} total keys)."
        )
    for raw_key in value.keys():
        key = str(raw_key).strip()
        if not key:
            raise ValueError("Input payload contains an empty field name.")
        if len(key) > _PAYLOAD_MAX_KEY_LEN:
            raise ValueError(
                f"Input field names must be {_PAYLOAD_MAX_KEY_LEN} characters or fewer."
            )


def _walk_payload_shape(payload: Any) -> None:
    """Pure: recursive depth/keys/array/string-length guard.

    Why: shared between every RegistryCallRequest validator so limits are
    single-sourced; bails out at the first violation with one actionable
    message.
    """
    state = {"keys": 0}

    def _walk(value: Any, depth: int) -> None:
        if depth > _PAYLOAD_MAX_DEPTH:
            raise ValueError(
                f"Input payload is too deeply nested (max depth {_PAYLOAD_MAX_DEPTH})."
            )
        if isinstance(value, str):
            if len(value) > _PAYLOAD_MAX_STRING_LEN:
                raise ValueError(
                    f"Input text is too long (max {_PAYLOAD_MAX_STRING_LEN} chars per field)."
                )
            return
        if isinstance(value, list):
            if len(value) > _PAYLOAD_MAX_ARRAY_ITEMS:
                raise ValueError(
                    f"Input array has too many items (max {_PAYLOAD_MAX_ARRAY_ITEMS} per array)."
                )
            for item in value:
                _walk(item, depth + 1)
            return
        if isinstance(value, dict):
            _check_dict_shape(value, state)
            for nested in value.values():
                _walk(nested, depth + 1)

    _walk(payload, depth=0)


class RegistryCallRequest(RootModel[JSONObject]):
    model_config = ConfigDict(json_schema_extra={"example": {"ticker": "AAPL"}})

    @model_validator(mode="after")
    def guard_payload_shape(self):
        """Pure: raise ValueError if the invoke payload exceeds size or structural caps.

        Why: a single ``_walk`` recursion enforces depth/array/key/string
        caps in one pass, bailing out at the first violation so callers
        get one actionable message per call.
        """
        encoded = json.dumps(self.root, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _PAYLOAD_MAX_BYTES:
            raise ValueError(
                f"Input payload is too large (max {_PAYLOAD_MAX_BYTES // 1024}KB). "
                "Reduce field count or text size."
            )
        _walk_payload_shape(self.root)
        return self


class MCPInvokeRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tool_name": "financial_research_agent",
                "input": {"ticker": "AAPL"},
                "api_key": "az_...",
            }
        }
    )

    tool_name: str
    input: JSONObject = Field(default_factory=dict)
    api_key: str
    # Optional, MCP-attached workspace summary for the caller's local cwd.
    # Forwarded into the agent payload by mcp_invoke; never persisted.
    # See core/workspace_bundle.py for the shape and core/workspace_helpers.py
    # for the consumer contract.
    workspace_context: dict | None = None
    workspace_context_fingerprint: str | None = None

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
                "pii_safe": True,
                "region_locked": "us",
            }
        }
    )

    query: str
    limit: int = Field(default=10, ge=1, le=50)
    min_trust: float = Field(default=0.0, ge=0.0, le=1.0)
    max_price_cents: int | None = Field(default=None, ge=0)
    required_input_fields: list[str] | None = None
    respect_caller_trust_min: bool = False
    model_provider: str | None = None
    kind: str | None = None
    pii_safe: bool | None = None
    outputs_not_stored: bool | None = None
    audit_logged: bool | None = None
    region_locked: str | None = None

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
        """Deduplicate and validate required_fields list; None is allowed (means no required fields)."""
        if value is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            field_name = str(item).strip()
            if not field_name:
                raise ValueError(
                    "required_input_fields entries must be non-empty strings"
                )
            if field_name in seen:
                continue
            seen.add(field_name)
            normalized.append(field_name)
        return normalized

    @field_validator("model_provider")
    @classmethod
    def search_model_provider_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value).strip().lower()).strip(
            "-"
        )
        return normalized or None

    @field_validator("region_locked")
    @classmethod
    def search_region_locked_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = re.sub(r"[^a-z0-9-]+", "-", str(value).strip().lower()).strip("-")
        return normalized or None


def normalize_job_message_body_result(
    *,
    msg_type: str,
    payload: "JSONObject | None" = None,
    correlation_id: str | None = None,
    allow_legacy: bool = True,
) -> "Result[dict, str]":
    """Result-returning variant of ``normalize_job_message_body``."""
    try:
        return Ok(
            normalize_job_message_body(
                msg_type=msg_type,
                payload=payload,
                correlation_id=correlation_id,
                allow_legacy=allow_legacy,
            )
        )
    except ValueError as exc:
        return Err(str(exc))
