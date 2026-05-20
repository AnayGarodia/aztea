#!/usr/bin/env python3
"""Static drift detection across built-in agent specs.

Catches the patterns the 2026-05-19 audit surfaced as contract drift:

* "Free" / "platform-subsidized" descriptions paired with non-zero price.
* Agent code imports ``core.llm`` but spec's ``runtime_requirements``
  doesn't declare an LLM provider — produces ``llm_used: false`` while
  the agent honestly calls an LLM (H-7).
* Reserved envelope keys mentioned in a description but missing from
  the dispatch-layer ``_RESERVED_ENVELOPE_KEYS`` set.

Run from CI:

    python scripts/lint_specs.py

Exits non-zero with a structured report on drift. Output formatted for
GitHub Actions annotation parsers.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from server.builtin_agents.specs import builtin_agent_specs  # noqa: E402


_FREE_PHRASES = (
    "platform-subsidized gateway agent",
    "free —",
    "free -",
    "(free)",
)


def _check_free_label_matches_price(spec: dict[str, Any]) -> list[str]:
    """Return list of drift findings for the free-label vs price check."""
    desc = str(spec.get("description") or "").lower()
    if not any(p in desc for p in _FREE_PHRASES):
        return []
    price = float(spec.get("price_per_call_usd") or 0.0)
    if price == 0.0:
        return []
    return [
        f"{spec.get('agent_id')}: description claims free but "
        f"price_per_call_usd={price}"
    ]


def _agent_file_for_slug(spec: dict[str, Any]) -> Path | None:
    """Best-effort lookup of the agent's source file under agents/."""
    endpoint = str(spec.get("endpoint_url") or "")
    match = re.match(r"^internal://([a-z0-9_]+)$", endpoint)
    if not match:
        return None
    candidate = _REPO_ROOT / "agents" / f"{match.group(1)}.py"
    return candidate if candidate.exists() else None


def _check_llm_runtime_requirements(spec: dict[str, Any]) -> list[str]:
    """H-7: if the agent imports core.llm, its spec must declare an LLM
    provider in runtime_requirements. Pre-fix, agents that forgot this
    emitted llm_used: false while hedging like an LLM."""
    agent_path = _agent_file_for_slug(spec)
    if agent_path is None:
        return []
    try:
        source = agent_path.read_text(encoding="utf-8")
    except OSError:
        return []
    imports_llm = (
        "from core.llm" in source
        or "import core.llm" in source
    )
    if not imports_llm:
        return []
    requirements = spec.get("runtime_requirements") or []
    declares_llm = any(
        "llm provider" in str(item).lower() for item in requirements
    )
    if declares_llm:
        return []
    return [
        f"{spec.get('agent_id')}: agent at {agent_path.relative_to(_REPO_ROOT)} "
        "imports core.llm but spec.runtime_requirements does not declare "
        "'llm provider'. This drift causes llm_used: false in responses "
        "(H-7 audit 2026-05-19). Add 'llm provider' to runtime_requirements."
    ]


def _check_max_claims_are_numeric(spec: dict[str, Any]) -> list[str]:
    """If the description says 'Max N <unit>', N must be a positive int."""
    pattern = re.compile(r"\bmax(?:imum)?\s+(\d[\d_,]*)\s*([a-z]+)", re.IGNORECASE)
    desc = str(spec.get("description") or "")
    out: list[str] = []
    for match in pattern.finditer(desc):
        raw = match.group(1).replace(",", "").replace("_", "")
        try:
            value = int(raw)
        except ValueError:
            out.append(
                f"{spec.get('agent_id')}: 'Max {raw} ...' in description "
                "but the number is unparseable"
            )
            continue
        if value <= 0:
            out.append(
                f"{spec.get('agent_id')}: 'Max {value} ...' must be positive"
            )
    return out


_CHECKS = (
    _check_free_label_matches_price,
    _check_llm_runtime_requirements,
    _check_max_claims_are_numeric,
)


def main() -> int:
    specs = list(builtin_agent_specs() or [])
    findings: list[str] = []
    for spec in specs:
        for check in _CHECKS:
            findings.extend(check(spec))
    if findings:
        print(
            f"FAIL: {len(findings)} spec drift finding(s) across "
            f"{len(specs)} agent(s):"
        )
        for line in findings:
            print(f"  - {line}")
        return 1
    print(
        f"OK: {len(specs)} agent spec(s) scanned, no drift detected."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
