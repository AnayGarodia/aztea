"""
OpenAPI response fragments shared by route modules (keeps error response models DRY).
"""

from __future__ import annotations

from typing import Any

from core import models as core_models

OPENAPI_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    400: {"model": core_models.ErrorResponse, "description": "Bad request."},
    401: {"model": core_models.ErrorResponse, "description": "Missing or invalid authorization header."},
    402: {"model": core_models.ErrorResponse, "description": "Insufficient balance."},
    403: {"model": core_models.ErrorResponse, "description": "Forbidden."},
    404: {"model": core_models.ErrorResponse, "description": "Resource not found."},
    409: {"model": core_models.ErrorResponse, "description": "Conflict."},
    410: {"model": core_models.ErrorResponse, "description": "Lease expired."},
    413: {"model": core_models.ErrorResponse, "description": "Payload too large."},
    422: {"model": core_models.ErrorResponse, "description": "Validation error."},
    429: {"model": core_models.RateLimitErrorResponse, "description": "Rate limit exceeded."},
    500: {"model": core_models.ErrorResponse, "description": "Internal server error."},
    502: {"model": core_models.ErrorResponse, "description": "Upstream request failed."},
    503: {"model": core_models.ErrorResponse, "description": "Upstream service unavailable."},
}


def pick_error_responses(*codes: int) -> dict[int, dict[str, Any]]:
    return {code: OPENAPI_ERROR_RESPONSES[code] for code in codes if code in OPENAPI_ERROR_RESPONSES}
