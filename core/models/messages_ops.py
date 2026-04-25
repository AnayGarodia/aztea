"""Pydantic models (split from legacy models.py for maintainability)."""
from __future__ import annotations

import json
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

from .core_types import *  # noqa: F403
from . import core_types as _core_types_pkg

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

    if msg_type == "agent_message":
        channel = str(normalized.get("channel") or "").strip()
        if not channel:
            raise ValueError("agent_message payload.channel is required.")
        normalized["channel"] = channel
        body = normalized.get("body")
        if isinstance(body, str):
            body_text = body.strip()
            if not body_text:
                raise ValueError("agent_message payload.body must not be empty.")
            normalized["body"] = body_text
        elif body is None:
            raise ValueError("agent_message payload.body is required.")
        elif not isinstance(body, dict):
            raise ValueError("agent_message payload.body must be an object or non-empty string.")
        to_id = _normalize_optional_text(normalized.get("to_id"))
        if to_id is None:
            normalized.pop("to_id", None)
        else:
            normalized["to_id"] = to_id
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

    @model_validator(mode="after")
    def guard_payload_shape(self):
        payload = self.root
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("Input payload is too large (max 64KB). Reduce field count or text size.")

        max_depth = 8
        max_keys = 120
        max_array_items = 200
        max_string_len = 4000

        state = {"keys": 0}

        def _walk(value, depth: int) -> None:
            if depth > max_depth:
                raise ValueError(f"Input payload is too deeply nested (max depth {max_depth}).")
            if isinstance(value, str):
                if len(value) > max_string_len:
                    raise ValueError(
                        f"Input text is too long (max {max_string_len} chars per field)."
                    )
                return
            if isinstance(value, list):
                if len(value) > max_array_items:
                    raise ValueError(
                        f"Input array has too many items (max {max_array_items} per array)."
                    )
                for item in value:
                    _walk(item, depth + 1)
                return
            if isinstance(value, dict):
                state["keys"] += len(value)
                if state["keys"] > max_keys:
                    raise ValueError(
                        f"Input payload has too many fields (max {max_keys} total keys)."
                    )
                for raw_key, nested in value.items():
                    key = str(raw_key).strip()
                    if not key:
                        raise ValueError("Input payload contains an empty field name.")
                    if len(key) > 100:
                        raise ValueError("Input field names must be 100 characters or fewer.")
                    _walk(nested, depth + 1)

        _walk(payload, depth=0)
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
    model_provider: str | None = None
    kind: str | None = None

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

    @field_validator("model_provider")
    @classmethod
    def search_model_provider_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value).strip().lower()).strip("-")
        return normalized or None


