"""
logging_utils.py — structured JSON logging helpers for Aztea.
"""

from __future__ import annotations

import contextvars
import json
import logging
from datetime import datetime, timezone
from typing import Any

_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aztea_request_id",
    default=None,
)


def set_request_id(request_id: str | None) -> contextvars.Token:
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _REQUEST_ID.reset(token)


def get_request_id() -> str | None:
    return _REQUEST_ID.get()


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        request_id = getattr(record, "request_id", None) or get_request_id()
        record.request_id = request_id
        record.event = getattr(record, "event", None) or record.getMessage()
        data = getattr(record, "data", {})
        record.data = data if isinstance(data, dict) else {"value": data}
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        """Serialise a log record to a single-line JSON string with timestamp, level, and data."""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "request_id": getattr(record, "request_id", None),
            "event": getattr(record, "event", record.getMessage()),
            "data": getattr(record, "data", {}),
        }
        if record.exc_info:
            payload["data"] = {
                **(payload["data"] if isinstance(payload["data"], dict) else {}),
                "exception": self.formatException(record.exc_info),
            }
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_json_logging(level: int = logging.INFO) -> None:
    """Set up structured JSON logging on the root logger (idempotent)."""
    root = logging.getLogger()
    if getattr(root, "_aztea_json_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    handler.addFilter(_RequestContextFilter())
    root.handlers = [handler]
    root.setLevel(level)
    root._aztea_json_configured = True  # type: ignore[attr-defined]


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    data: dict[str, Any] | None = None,
) -> None:
    logger.log(level, event, extra={"event": event, "data": data or {}})
