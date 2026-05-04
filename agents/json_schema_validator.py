"""
json_schema_validator.py — Validate a JSON document against a JSON Schema
using the real ``jsonschema`` library. No LLM. Returns structured per-path
errors.

Owns:
  - Parsing and basic safety bounds on document and schema sizes.
  - Iterating ``Draft202012Validator`` errors and projecting them into a
    Claude-Code-friendly shape (json_pointer + message + schema rule).
  - Defensive handling of unloadable schemas (returns a structured error,
    never an exception).

Does NOT own:
  - Fetching ``$ref`` URLs over the network. We disable remote refs to avoid
    SSRF; embedded $defs / local $ref still resolve.
  - Schema authoring. Caller provides the schema.

Input:
  {
    "document": object | array | str,         # required
    "schema": object,                         # required, must be a JSON Schema
    "draft": "2020-12" | "2019-09" | "7"     # optional, default "2020-12"
  }

Document may be passed as JSON-encoded string OR as already-parsed Python
object. Schema must be a Python object (parsed JSON).

Output:
  {
    "valid": bool,
    "draft": str,
    "error_count": int,
    "errors": [
      {
        "path": str,           # JSON pointer like "/items/3/name"
        "json_path": str,      # JSONPath-ish like "$.items[3].name"
        "message": str,
        "validator": str,      # which keyword triggered (required, type, enum...)
        "validator_value": any,
        "schema_path": str
      }
    ],
    "summary": str
  }
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

try:
    from jsonschema import Draft7Validator, Draft201909Validator, Draft202012Validator
    from jsonschema.exceptions import SchemaError

    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JSONSCHEMA_AVAILABLE = False
    SchemaError = Exception  # type: ignore[assignment, misc]

_MAX_DOCUMENT_CHARS = 200_000
_MAX_SCHEMA_CHARS = 50_000
_MAX_ERRORS = 100
_VALID_SCHEMA_TYPES = {
    "object",
    "array",
    "string",
    "integer",
    "number",
    "boolean",
    "null",
}
_SUPPORTED_DRAFTS = ("2020-12", "2019-09", "7")

_DRAFT_MAP: dict[str, Any] = {}
if _JSONSCHEMA_AVAILABLE:
    _DRAFT_MAP = {
        "2020-12": Draft202012Validator,
        "2019-09": Draft201909Validator,
        "7": Draft7Validator,
    }


def _err(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **details}}


def _to_json_path(path: list[Any]) -> str:
    """Convert a jsonschema absolute_path deque to JSONPath syntax."""
    parts = ["$"]
    for piece in path:
        if isinstance(piece, int):
            parts.append(f"[{piece}]")
        else:
            parts.append(f".{piece}")
    return "".join(parts)


def _to_json_pointer(path: list[Any]) -> str:
    """Convert path to RFC 6901 JSON Pointer."""
    if not path:
        return ""
    encoded = []
    for piece in path:
        token = str(piece).replace("~", "~0").replace("/", "~1")
        encoded.append(token)
    return "/" + "/".join(encoded)


class _SubsetValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        path: list[Any] | None = None,
        validator: str | None = None,
        validator_value: Any = None,
        schema_path: list[Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.path = list(path or [])
        self.validator = validator or "unknown"
        self.validator_value = validator_value
        self.schema_path = list(schema_path or [])


def _schema_error(message: str, **kwargs: Any) -> None:
    raise _SubsetValidationError(message, **kwargs)


def _serialized_len(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _find_remote_ref(node: Any) -> str | None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            parsed = urlparse(ref)
            if parsed.scheme in {"http", "https", "file"}:
                return ref
        for value in node.values():
            found = _find_remote_ref(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_remote_ref(value)
            if found:
                return found
    return None


def _check_schema_shape(schema: Any, path: list[Any] | None = None) -> None:
    current_path = list(path or [])
    if not isinstance(schema, dict):
        return
    declared_type = schema.get("type")
    if isinstance(declared_type, str):
        if declared_type not in _VALID_SCHEMA_TYPES:
            _schema_error(
                f"{declared_type!r} is not a valid JSON Schema type.",
                path=current_path,
                validator="type",
                validator_value=declared_type,
                schema_path=current_path + ["type"],
            )
    elif isinstance(declared_type, list):
        invalid = [item for item in declared_type if item not in _VALID_SCHEMA_TYPES]
        if invalid:
            _schema_error(
                f"{invalid[0]!r} is not a valid JSON Schema type.",
                path=current_path,
                validator="type",
                validator_value=declared_type,
                schema_path=current_path + ["type"],
            )
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        _schema_error(
            "properties must be an object.",
            path=current_path,
            validator="properties",
            validator_value=properties,
            schema_path=current_path + ["properties"],
        )
    if isinstance(properties, dict):
        for key, value in properties.items():
            _check_schema_shape(value, current_path + ["properties", key])
    items = schema.get("items")
    if items is not None and isinstance(items, dict):
        _check_schema_shape(items, current_path + ["items"])


def _type_matches(value: Any, declared_type: str) -> bool:
    if declared_type == "object":
        return isinstance(value, dict)
    if declared_type == "array":
        return isinstance(value, list)
    if declared_type == "string":
        return isinstance(value, str)
    if declared_type == "boolean":
        return isinstance(value, bool)
    if declared_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(
            value, bool
        )
    if declared_type == "null":
        return value is None
    return True


def _validate_subset(
    document: Any,
    schema: dict[str, Any],
    path: list[Any] | None = None,
    schema_path: list[Any] | None = None,
) -> list[_SubsetValidationError]:
    current_path = list(path or [])
    current_schema_path = list(schema_path or [])
    errors: list[_SubsetValidationError] = []

    declared_type = schema.get("type")
    if isinstance(declared_type, str):
        if not _type_matches(document, declared_type):
            errors.append(
                _SubsetValidationError(
                    f"{document!r} is not of type '{declared_type}'",
                    path=current_path,
                    validator="type",
                    validator_value=declared_type,
                    schema_path=current_schema_path + ["type"],
                )
            )
            return errors
    elif isinstance(declared_type, list):
        if not any(_type_matches(document, item) for item in declared_type):
            errors.append(
                _SubsetValidationError(
                    f"{document!r} is not of type {declared_type!r}",
                    path=current_path,
                    validator="type",
                    validator_value=declared_type,
                    schema_path=current_schema_path + ["type"],
                )
            )
            return errors

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and document not in enum_values:
        errors.append(
            _SubsetValidationError(
                f"{document!r} is not one of {enum_values!r}",
                path=current_path,
                validator="enum",
                validator_value=enum_values,
                schema_path=current_schema_path + ["enum"],
            )
        )

    if isinstance(document, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in document:
                    errors.append(
                        _SubsetValidationError(
                            f"{key!r} is a required property",
                            path=current_path,
                            validator="required",
                            validator_value=required,
                            schema_path=current_schema_path + ["required"],
                        )
                    )
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, subschema in properties.items():
                if key in document and isinstance(subschema, dict):
                    errors.extend(
                        _validate_subset(
                            document[key],
                            subschema,
                            current_path + [key],
                            current_schema_path + ["properties", key],
                        )
                    )
        return errors

    if isinstance(document, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(document) < min_items:
            errors.append(
                _SubsetValidationError(
                    f"{document!r} is too short",
                    path=current_path,
                    validator="minItems",
                    validator_value=min_items,
                    schema_path=current_schema_path + ["minItems"],
                )
            )
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(document) > max_items:
            errors.append(
                _SubsetValidationError(
                    f"{document!r} is too long",
                    path=current_path,
                    validator="maxItems",
                    validator_value=max_items,
                    schema_path=current_schema_path + ["maxItems"],
                )
            )
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(document):
                errors.extend(
                    _validate_subset(
                        item,
                        items,
                        current_path + [index],
                        current_schema_path + ["items"],
                    )
                )
        return errors

    if isinstance(document, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(document) < min_length:
            errors.append(
                _SubsetValidationError(
                    f"{document!r} is too short",
                    path=current_path,
                    validator="minLength",
                    validator_value=min_length,
                    schema_path=current_schema_path + ["minLength"],
                )
            )
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(document) > max_length:
            errors.append(
                _SubsetValidationError(
                    f"{document!r} is too long",
                    path=current_path,
                    validator="maxLength",
                    validator_value=max_length,
                    schema_path=current_schema_path + ["maxLength"],
                )
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, document) is None:
            errors.append(
                _SubsetValidationError(
                    f"{document!r} does not match pattern {pattern!r}",
                    path=current_path,
                    validator="pattern",
                    validator_value=pattern,
                    schema_path=current_schema_path + ["pattern"],
                )
            )
        return errors

    if isinstance(document, (int, float)) and not isinstance(document, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and document < minimum:
            errors.append(
                _SubsetValidationError(
                    f"{document!r} is less than the minimum of {minimum}",
                    path=current_path,
                    validator="minimum",
                    validator_value=minimum,
                    schema_path=current_schema_path + ["minimum"],
                )
            )
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and document > maximum:
            errors.append(
                _SubsetValidationError(
                    f"{document!r} is greater than the maximum of {maximum}",
                    path=current_path,
                    validator="maximum",
                    validator_value=maximum,
                    schema_path=current_schema_path + ["maximum"],
                )
            )
    return errors


def run(payload: dict) -> dict:
    """Validate a JSON document against a JSON Schema."""
    if not isinstance(payload, dict):
        return _err(
            "json_schema_validator.invalid_payload", "payload must be an object"
        )

    document = payload.get("document")
    if document is None:
        return _err("json_schema_validator.missing_document", "'document' is required")

    if isinstance(document, str):
        if len(document) > _MAX_DOCUMENT_CHARS:
            return _err(
                "json_schema_validator.document_too_large",
                f"document exceeds {_MAX_DOCUMENT_CHARS} chars",
            )
        try:
            document = json.loads(document)
        except json.JSONDecodeError as exc:
            return _err(
                "json_schema_validator.invalid_json",
                f"document is not valid JSON: {exc.msg} at line {exc.lineno} col {exc.colno}",
                line=exc.lineno,
                column=exc.colno,
            )
    else:
        if _serialized_len(document) > _MAX_DOCUMENT_CHARS:
            return _err(
                "json_schema_validator.document_too_large",
                f"document exceeds {_MAX_DOCUMENT_CHARS} chars when serialized to JSON",
            )

    schema = payload.get("schema")
    if not isinstance(schema, dict):
        return _err(
            "json_schema_validator.missing_schema",
            "'schema' is required and must be a JSON Schema object",
        )

    remote_ref = _find_remote_ref(schema)
    if remote_ref:
        return _err(
            "json_schema_validator.remote_ref_not_supported",
            f"remote $ref targets are not supported: {remote_ref}",
        )

    try:
        _check_schema_shape(schema)
    except _SubsetValidationError as exc:
        return _err(
            "json_schema_validator.invalid_schema",
            f"schema is not a valid JSON Schema: {exc.message}",
            schema_path=exc.schema_path,
        )

    schema_serialized = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    if len(schema_serialized) > _MAX_SCHEMA_CHARS:
        return _err(
            "json_schema_validator.schema_too_large",
            f"schema exceeds {_MAX_SCHEMA_CHARS} chars",
        )

    draft = str(payload.get("draft") or "2020-12").strip()
    using_jsonschema = bool(_DRAFT_MAP)
    if using_jsonschema:
        supported_drafts = sorted(_DRAFT_MAP.keys())
    else:
        supported_drafts = list(_SUPPORTED_DRAFTS)
    if draft not in supported_drafts:
        return _err(
            "json_schema_validator.unsupported_draft",
            f"draft must be one of {supported_drafts}; got {draft!r}",
        )
    validator_cls = _DRAFT_MAP.get(draft)

    if using_jsonschema and validator_cls is not None:
        try:
            validator_cls.check_schema(schema)
        except SchemaError as exc:
            return _err(
                "json_schema_validator.invalid_schema",
                f"schema is not a valid JSON Schema: {exc.message}",
                schema_path=list(exc.absolute_path)
                if hasattr(exc, "absolute_path")
                else None,
            )
        except Exception as exc:
            return _err(
                "json_schema_validator.invalid_schema",
                f"schema validation failed: {exc}",
            )

    projected: list[dict[str, Any]] = []
    raw_error_count = 0
    if using_jsonschema and validator_cls is not None:
        validator = validator_cls(schema)
        raw_errors = list(validator.iter_errors(document))
        raw_error_count = len(raw_errors)
        for error in raw_errors[:_MAX_ERRORS]:
            path_list = list(error.absolute_path)
            schema_path_list = list(error.absolute_schema_path)
            projected.append(
                {
                    "path": _to_json_pointer(path_list),
                    "json_path": _to_json_path(path_list),
                    "message": error.message,
                    "validator": error.validator,
                    "validator_value": error.validator_value
                    if isinstance(
                        error.validator_value,
                        (str, int, float, bool, list, dict, type(None)),
                    )
                    else str(error.validator_value),
                    "schema_path": _to_json_pointer(schema_path_list),
                }
            )
    else:
        subset_errors = _validate_subset(document, schema)
        raw_error_count = len(subset_errors)
        for error in subset_errors[:_MAX_ERRORS]:
            projected.append(
                {
                    "path": _to_json_pointer(error.path),
                    "json_path": _to_json_path(error.path),
                    "message": error.message,
                    "validator": error.validator,
                    "validator_value": error.validator_value,
                    "schema_path": _to_json_pointer(error.schema_path),
                }
            )

    valid = raw_error_count == 0
    if valid:
        summary = "Document is valid against the supplied schema."
    elif raw_error_count == 1:
        summary = f"1 validation error: {projected[0]['message']}"
    else:
        summary = (
            f"{raw_error_count} validation errors. First: {projected[0]['message']}"
        )

    return {
        "valid": valid,
        "draft": draft,
        "error_count": raw_error_count,
        "errors": projected,
        "truncated": raw_error_count > _MAX_ERRORS,
        "summary": summary,
    }
