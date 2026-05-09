"""Utilities for shaping large agent outputs for buyer-facing responses."""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

_MAX_STRING_CHARS = 2048
_MAX_LIST_ITEMS = 50
_MAX_DICT_ITEMS = 100
_MAX_DEPTH = 6
_TRUNCATE_HEAD_CHARS = 1536
_TRUNCATE_TAIL_CHARS = 256
assert _TRUNCATE_HEAD_CHARS + _TRUNCATE_TAIL_CHARS <= _MAX_STRING_CHARS

# Heuristic floors for "this string is base64 of meaningful binary content".
_BASE64_MIN_RAW_CHARS = 1024
_BASE64_ALPHABET_SAMPLE_CHARS = 256
_BASE64_MIN_ALPHABET_DIVERSITY = 8
_BASE64_MIN_DECODED_BYTES = 768

_ARTIFACT_PREVIEW_CHARS = 96
_ARTIFACT_DIGEST_CHARS = 16


def _truncate_string(value: str) -> tuple[str, bool]:
    if len(value) <= _MAX_STRING_CHARS:
        return value, False
    head = value[:_TRUNCATE_HEAD_CHARS]
    tail = value[-_TRUNCATE_TAIL_CHARS:]
    omitted = len(value) - len(head) - len(tail)
    return f"{head}\n...[truncated {omitted} chars]...\n{tail}", True


def _looks_like_base64_blob(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < _BASE64_MIN_RAW_CHARS or len(stripped) % 4 != 0:
        return False
    if len(set(stripped[:_BASE64_ALPHABET_SAMPLE_CHARS])) < _BASE64_MIN_ALPHABET_DIVERSITY:
        return False
    try:
        decoded = base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded) >= _BASE64_MIN_DECODED_BYTES


def _shape_artifact_blob(value: str) -> dict[str, Any]:
    stripped = value.strip()
    preview = stripped[:_ARTIFACT_PREVIEW_CHARS]
    size_bytes = 0
    try:
        size_bytes = len(base64.b64decode(stripped, validate=True))
    except (binascii.Error, ValueError):
        size_bytes = len(stripped.encode("utf-8", errors="ignore"))
    digest = hashlib.sha256(stripped.encode("utf-8", errors="ignore")).hexdigest()[:_ARTIFACT_DIGEST_CHARS]
    return {
        "_artifact_id": digest,
        "size_bytes": size_bytes,
        "preview": preview,
    }


def _shape(value: Any, *, depth: int) -> tuple[Any, bool]:
    if depth >= _MAX_DEPTH:
        if isinstance(value, str):
            if _looks_like_base64_blob(value):
                return _shape_artifact_blob(value), True
            return _truncate_string(value)
        if isinstance(value, list):
            return {"_truncated_items": len(value), "preview": value[:3]}, True
        if isinstance(value, dict):
            preview_items = list(value.items())[:5]
            return {"_truncated_keys": len(value), "preview": dict(preview_items)}, True
        return value, False

    if isinstance(value, str):
        if _looks_like_base64_blob(value):
            return _shape_artifact_blob(value), True
        return _truncate_string(value)

    if isinstance(value, list):
        truncated = False
        items = value
        if len(items) > _MAX_LIST_ITEMS:
            items = items[:_MAX_LIST_ITEMS]
            truncated = True
        shaped_items: list[Any] = []
        for item in items:
            shaped, item_truncated = _shape(item, depth=depth + 1)
            shaped_items.append(shaped)
            truncated = truncated or item_truncated
        if len(value) > _MAX_LIST_ITEMS:
            shaped_items.append({"_truncated_items": len(value) - _MAX_LIST_ITEMS})
        return shaped_items, truncated

    if isinstance(value, dict):
        truncated = False
        items = list(value.items())
        if len(items) > _MAX_DICT_ITEMS:
            items = items[:_MAX_DICT_ITEMS]
            truncated = True
        shaped_dict: dict[str, Any] = {}
        for key, item in items:
            shaped, item_truncated = _shape(item, depth=depth + 1)
            shaped_dict[str(key)] = shaped
            truncated = truncated or item_truncated
        if len(value) > _MAX_DICT_ITEMS:
            shaped_dict["_truncated_keys"] = len(value) - _MAX_DICT_ITEMS
        return shaped_dict, truncated

    return value, False


def shape_output(payload: Any, mode: str = "summary") -> tuple[Any, bool]:
    normalized_mode = str(mode or "summary").strip().lower()
    if normalized_mode == "full":
        return payload, False
    return _shape(payload, depth=0)
