from __future__ import annotations

import json
import re
from typing import Any


def agent_error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def strip_json_fences(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_json_payload(raw_text: str) -> Any:
    return json.loads(strip_json_fences(raw_text))


def annotate_success(
    payload: dict[str, Any],
    *,
    billing_units_actual: int | None = None,
    llm_used: bool | None = None,
    degraded_mode: bool | None = None,
) -> dict[str, Any]:
    result = dict(payload)
    if billing_units_actual is not None:
        result["billing_units_actual"] = int(billing_units_actual)
    if llm_used is not None:
        result["llm_used"] = bool(llm_used)
    if degraded_mode is not None:
        result["degraded_mode"] = bool(degraded_mode)
    return result
