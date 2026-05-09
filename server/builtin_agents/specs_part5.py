"""Fifth chunk of built-in agent specs — Phase 8 Claude-Code-aligned utility agents.

Each agent here performs real, deterministic tool work that Claude Code cannot
do from a chat session alone:
  - secret_scanner: regex/entropy detection over arbitrary text.
  - json_schema_validator: real ``jsonschema`` validation.
  - regex_tester: ``re`` execution under a timeout (catches ReDoS).
  - sql_explainer: SQLite EXPLAIN QUERY PLAN over a real schema.
  - git_diff_analyzer: structural parse + risk classification of unified diffs.
"""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
)
from server.builtin_agents.constants import (
    SECRET_SCANNER_AGENT_ID as _SECRET_SCANNER_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def load_builtin_specs_part5() -> list[dict[str, Any]]:
    return [
        {
            "agent_id": _SECRET_SCANNER_AGENT_ID,
            "name": "Secret Scanner",
            "description": (
                "Use to scan source code, env files, or arbitrary text for leaked credentials "
                "and high-entropy tokens. Runs a curated regex catalog (AWS, GCP, Stripe, GitHub, "
                "Slack, OpenAI, Anthropic, Google, Twilio, SendGrid, JWTs, PEM private keys) plus "
                "a Shannon-entropy heuristic. Findings include redacted previews only — full "
                "matches are never returned. No LLM, deterministic results."
            ),
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SECRET_SCANNER_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": [
                "security",
                "secrets",
                "credentials",
                "developer-tools",
                "compliance",
            ],
            "match_keywords": [
                "leaked secret",
                "leaked secrets",
                "leaked credential",
                "leaked credentials",
                "leaked api key",
                "secret in repo",
                "credential in repo",
                "hardcoded secret",
                "hardcoded credential",
                "hardcoded password",
                "hardcoded passwords",
                "leaked password",
                "leaked passwords",
                "passwords in code",
                "passwords in source",
                "passwords in this code",
                "scan for secrets",
                "scan code for",
                "scan source",
                "scan this code",
                "scan my code",
                "credential scan",
                "secret scanner",
                "secret scan",
                "find secrets",
                "find leaked",
                "leaked aws",
                "leaked github",
                "find aws",
                "high-entropy",
                "private key in",
                "aws_secret",
                "aws key",
                "aws keys",
                "github token",
                "stripe key",
                "openai api key",
                "api key in",
                "api keys in",
            ],
            "kind": "aztea_built",
            "category": "Security",
            "is_featured": True,
            "cacheable": True,
            "examples_sensitive": True,
            "pii_safe": True,
            "outputs_not_stored": True,
            "data_retention_policy": "Input is never stored. Scan results (redacted previews only) are held in memory for the duration of the API call and discarded immediately after. No input content or full secret values are written to disk or logs. Aztea platform audit logs record job metadata (agent_id, timestamp, cost) only — not the scanned content.",
            "input_schema": _output_schema_object(
                {
                    "content": {
                        "type": "string",
                        "title": "Source text",
                        "description": "Code or config text to scan. Max 200,000 characters.",
                        "maxLength": 200000,
                    },
                    "filename": {
                        "type": "string",
                        "title": "Filename hint",
                        "description": "Optional filename for context only.",
                    },
                    "min_entropy": {
                        "type": "number",
                        "title": "Min Shannon entropy",
                        "description": "Threshold for high-entropy heuristic. Set 0 or a negative value to disable. Default 4.5.",
                        "default": 4.5,
                        "minimum": -1,
                    },
                    "max_findings": {
                        "type": "integer",
                        "title": "Max findings",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 1000,
                    },
                },
                required=["content"],
            )
            | {"additionalProperties": True},
            # Aztea injects a `protocol` envelope on every job (private_task,
            # input_artifacts, preferred_*_formats). Strict additionalProperties
            # would reject every batch hire of this agent.
            "output_schema": _output_schema_object(
                {
                    "filename": {"type": ["string", "null"]},
                    "total_findings": {"type": "integer"},
                    "findings_by_severity": {"type": "object"},
                    "findings": {"type": "array", "items": {"type": "object"}},
                    "summary": {"type": "string"},
                },
                required=[
                    "total_findings",
                    "findings",
                    "findings_by_severity",
                    "summary",
                ],
            ),
            "output_examples": [
                {
                    "input": {
                        "content": "AWS_KEY=AKIAIOSFODNN7EXAMPLE\nSTRIPE=sk_<live>_<EXAMPLE_REDACTED>\n",
                        "filename": ".env",
                    },
                    "output": {
                        "filename": ".env",
                        "total_findings": 2,
                        "findings_by_severity": {
                            "critical": 2,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        },
                        "findings": [
                            {
                                "rule_id": "aws-access-key-id",
                                "rule_name": "AWS Access Key ID",
                                "severity": "critical",
                                "line": 1,
                                "column": 9,
                                "redacted_preview": "AKIA…[20 chars]…MPLE",
                                "match_length": 20,
                                "entropy": 3.821,
                                "remediation": "Rotate the IAM key immediately and audit CloudTrail for misuse.",
                            }
                        ],
                        "summary": "Found 2 potential leak(s): 2 critical.",
                    },
                }
            ],
        },
    ]
