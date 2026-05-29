"""Reflex eval harness runner — scaffold.

Currently exercises the fixture loader + schema enforcement. The Claude
Code SDK headless driver lands when the SDK exposes a stable trace API.
See README.md for the fixture format.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE / "fixtures"
SCHEMA_FILE = HERE / "schema.json"


@dataclass(frozen=True)
class Fixture:
    """Parsed fixture; safe to pass around the rest of the runner."""
    id: str
    intent: str
    expected_specialist_slug: str
    failure_bucket_if_wrong: str
    notes: str
    min_runs: int
    tags: tuple[str, ...]


def _load_schema() -> dict[str, Any]:
    with open(SCHEMA_FILE, encoding="utf-8") as f:
        return json.load(f)


def _validate(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    """Best-effort validation. Uses jsonschema if installed; otherwise
    a minimal hand-rolled check for the required-fields subset."""
    try:
        import jsonschema  # type: ignore[import-not-found]
        jsonschema.validate(payload, schema)
        return
    except ModuleNotFoundError:
        pass
    # Fallback: required-fields + enum check. Good enough for CI.
    for k in schema.get("required", []):
        if k not in payload:
            raise ValueError(f"missing required field: {k}")
    enum = (schema.get("properties", {})
            .get("failure_bucket_if_wrong", {}).get("enum"))
    if enum and payload.get("failure_bucket_if_wrong") not in enum:
        raise ValueError(
            f"failure_bucket_if_wrong must be one of {enum}"
        )


def load_fixtures() -> list[Fixture]:
    """Load and validate every JSON fixture in fixtures/."""
    schema = _load_schema()
    out: list[Fixture] = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _validate(data, schema)
        out.append(Fixture(
            id=str(data["id"]),
            intent=str(data["intent"]),
            expected_specialist_slug=str(data["expected_specialist_slug"]),
            failure_bucket_if_wrong=str(data["failure_bucket_if_wrong"]),
            notes=str(data.get("notes") or ""),
            min_runs=int(data.get("min_runs", 5)),
            tags=tuple(data.get("tags") or []),
        ))
    return out


def main(argv: list[str] | None = None) -> int:
    """Command-line entry. Currently dumps fixtures + their config."""
    argv = argv or sys.argv[1:]
    fixtures = load_fixtures()
    print(f"Loaded {len(fixtures)} fixture(s):")
    for f in fixtures:
        print(f"  {f.id} → {f.expected_specialist_slug}  (min_runs={f.min_runs})")
    print()
    print("Driver wiring deferred until the Claude Code SDK exposes a")
    print("stable headless trace API. See README.md for what this will do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
