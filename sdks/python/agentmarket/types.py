from __future__ import annotations

from enum import Enum
from typing import TypeAlias

JSONPrimitive: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONPrimitive | dict[str, "JSONValue"] | list["JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]


class MessageType(str, Enum):
    CLARIFICATION_REQUEST = "clarification_request"
    CLARIFICATION_RESPONSE = "clarification_response"
    PROGRESS = "progress"
    PARTIAL_RESULT = "partial_result"
    ARTIFACT = "artifact"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    NOTE = "note"
