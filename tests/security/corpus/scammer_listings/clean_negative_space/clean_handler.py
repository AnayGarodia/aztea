import json
import re
from dataclasses import dataclass


@dataclass
class Result:
    ok: bool
    count: int


def handler(payload):
    text = (payload or {}).get("text", "")
    words = re.findall(r"\S+", text)
    return {"ok": True, "count": len(words)}
