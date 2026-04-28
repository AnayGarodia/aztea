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
