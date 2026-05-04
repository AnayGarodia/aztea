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
    GIT_DIFF_ANALYZER_AGENT_ID as _GIT_DIFF_ANALYZER_AGENT_ID,
)
from server.builtin_agents.constants import (
    JSON_SCHEMA_VALIDATOR_AGENT_ID as _JSON_SCHEMA_VALIDATOR_AGENT_ID,
)
from server.builtin_agents.constants import (
    REGEX_TESTER_AGENT_ID as _REGEX_TESTER_AGENT_ID,
)
from server.builtin_agents.constants import (
    SECRET_SCANNER_AGENT_ID as _SECRET_SCANNER_AGENT_ID,
)
from server.builtin_agents.constants import (
    SQL_EXPLAINER_AGENT_ID as _SQL_EXPLAINER_AGENT_ID,
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
                        "description": "Threshold for high-entropy heuristic. Set 0 to disable. Default 4.5.",
                        "default": 4.5,
                        "minimum": 0,
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
            ),
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
        {
            "agent_id": _JSON_SCHEMA_VALIDATOR_AGENT_ID,
            "name": "JSON Schema Validator",
            "description": (
                "Use to validate a JSON document against a JSON Schema (draft 2020-12, 2019-09, "
                "or 7) using the real ``jsonschema`` Python library. Returns structured per-path "
                "errors with JSON Pointer and JSONPath locations, the violating keyword, and the "
                "schema rule. Remote ``$ref`` URLs are blocked; use embedded ``$defs`` or local "
                "fragment refs only. Useful for verifying API request/response shapes, config "
                "files, or tool-call payloads. No LLM."
            ),
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[
                _JSON_SCHEMA_VALIDATOR_AGENT_ID
            ],
            "price_per_call_usd": 0.01,
            "tags": ["json", "schema", "validation", "developer-tools", "api"],
            "kind": "aztea_built",
            "category": "Developer Tools",
            "is_featured": True,
            "cacheable": True,
            "input_schema": _output_schema_object(
                {
                    "document": {
                        "title": "Document",
                        "description": "JSON document to validate. Pass as object or as JSON-encoded string.",
                    },
                    "schema": {
                        "type": "object",
                        "title": "JSON Schema",
                        "description": "The JSON Schema object the document must satisfy.",
                    },
                    "draft": {
                        "type": "string",
                        "title": "Draft",
                        "description": "JSON Schema draft to validate against.",
                        "enum": ["2020-12", "2019-09", "7"],
                        "default": "2020-12",
                    },
                },
                required=["document", "schema"],
            ),
            "output_schema": _output_schema_object(
                {
                    "valid": {"type": "boolean"},
                    "draft": {"type": "string"},
                    "error_count": {"type": "integer"},
                    "errors": {"type": "array", "items": {"type": "object"}},
                    "truncated": {"type": "boolean"},
                    "summary": {"type": "string"},
                },
                required=["valid", "draft", "error_count", "errors", "summary"],
            ),
            "output_examples": [
                {
                    "input": {
                        "document": {"name": "alice", "age": "thirty"},
                        "schema": {
                            "type": "object",
                            "required": ["name", "age"],
                            "properties": {
                                "name": {"type": "string"},
                                "age": {"type": "integer"},
                            },
                        },
                    },
                    "output": {
                        "valid": False,
                        "draft": "2020-12",
                        "error_count": 1,
                        "errors": [
                            {
                                "path": "/age",
                                "json_path": "$.age",
                                "message": "'thirty' is not of type 'integer'",
                                "validator": "type",
                                "validator_value": "integer",
                                "schema_path": "/properties/age/type",
                            }
                        ],
                        "truncated": False,
                        "summary": "1 validation error: 'thirty' is not of type 'integer'",
                    },
                }
            ],
        },
        {
            "agent_id": _REGEX_TESTER_AGENT_ID,
            "name": "Regex Tester",
            "description": (
                "Use to test a Python regex against sample strings and get real matches, groups, "
                "named groups, and per-sample timing. Each sample runs in a subprocess with a hard "
                "wall-clock budget so a catastrophic-backtracking pattern cannot hang the call — "
                "any sample that exceeds the timeout is reported as a backtracking risk. Supports "
                "findall/match/search/fullmatch/sub. No LLM."
            ),
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_REGEX_TESTER_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["regex", "testing", "developer-tools", "redos", "python"],
            "kind": "aztea_built",
            "category": "Developer Tools",
            "is_featured": True,
            "cacheable": True,
            "input_schema": _output_schema_object(
                {
                    "pattern": {
                        "type": "string",
                        "title": "Pattern",
                        "description": "Python regex pattern.",
                        "maxLength": 2000,
                    },
                    "samples": {
                        "type": "array",
                        "title": "Samples",
                        "description": "1..50 strings to test the pattern against (max 2KB each).",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 50,
                    },
                    "operation": {
                        "type": "string",
                        "enum": ["findall", "match", "search", "fullmatch", "sub"],
                        "default": "findall",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "Replacement string when operation='sub'.",
                    },
                    "flags": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "IGNORECASE",
                                "MULTILINE",
                                "DOTALL",
                                "VERBOSE",
                                "ASCII",
                            ],
                        },
                        "default": [],
                    },
                    "timeout_ms_per_sample": {
                        "type": "integer",
                        "default": 200,
                        "minimum": 1,
                        "maximum": 2000,
                    },
                },
                required=["pattern", "samples"],
            ),
            "output_schema": _output_schema_object(
                {
                    "pattern": {"type": "string"},
                    "compiled": {"type": "boolean"},
                    "compile_error": {"type": ["string", "null"]},
                    "operation": {"type": "string"},
                    "results": {"type": "array", "items": {"type": "object"}},
                    "catastrophic_risk": {"type": "boolean"},
                    "summary": {"type": "string"},
                },
                required=[
                    "pattern",
                    "compiled",
                    "operation",
                    "results",
                    "catastrophic_risk",
                    "summary",
                ],
            ),
            "output_examples": [
                {
                    "input": {
                        "pattern": r"\b\d{3}-\d{4}\b",
                        "samples": ["call 555-1234 today", "no number here"],
                        "operation": "findall",
                    },
                    "output": {
                        "pattern": r"\b\d{3}-\d{4}\b",
                        "compiled": True,
                        "compile_error": None,
                        "operation": "findall",
                        "results": [
                            {
                                "sample_index": 0,
                                "match_count": 1,
                                "matches": [
                                    {
                                        "start": 5,
                                        "end": 13,
                                        "match": "555-1234",
                                        "groups": [],
                                        "named_groups": {},
                                    }
                                ],
                                "elapsed_ms": 1.2,
                                "timed_out": False,
                            },
                            {
                                "sample_index": 1,
                                "match_count": 0,
                                "matches": [],
                                "elapsed_ms": 0.8,
                                "timed_out": False,
                            },
                        ],
                        "catastrophic_risk": False,
                        "summary": "Pattern matched 1 occurrence(s) across 2 sample(s).",
                    },
                }
            ],
        },
        {
            "agent_id": _SQL_EXPLAINER_AGENT_ID,
            "name": "SQL Explainer",
            "description": (
                "Use to run EXPLAIN QUERY PLAN against a real SQLite database populated from your "
                "schema. Spins an in-memory SQLite per call, executes restricted DDL/seed SQL "
                "(no ATTACH, VACUUM, pragmas, or virtual tables), then runs EXPLAIN for one or "
                "more SELECT/WITH statements. Surfaces full scans, temp B-trees, correlated "
                "subqueries, and provides index suggestions. Read-only after schema setup. "
                "SQLite-only, but plan shapes generalize to other relational engines. No LLM."
            ),
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SQL_EXPLAINER_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["sql", "sqlite", "performance", "developer-tools", "database"],
            "kind": "aztea_built",
            "category": "Developer Tools",
            "is_featured": True,
            "cacheable": True,
            "input_schema": _output_schema_object(
                {
                    "schema_sql": {
                        "type": "string",
                        "title": "Schema/seed SQL",
                        "description": "DDL plus optional seed INSERTs. Max 30,000 chars.",
                        "maxLength": 30000,
                    },
                    "queries": {
                        "type": "array",
                        "title": "Queries",
                        "description": "1..10 SELECT or WITH statements to EXPLAIN.",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 10,
                    },
                    "params": {
                        "type": "array",
                        "description": "Optional positional/named params, parallel to queries.",
                        "items": {},
                    },
                },
                required=["schema_sql", "queries"],
            ),
            "output_schema": _output_schema_object(
                {
                    "queries": {"type": "array", "items": {"type": "object"}},
                    "total_issues": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                required=["queries", "total_issues", "summary"],
            ),
            "output_examples": [
                {
                    "input": {
                        "schema_sql": "CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT); INSERT INTO users VALUES (1,'a@x'),(2,'b@x');",
                        "queries": ["SELECT * FROM users WHERE email = 'a@x'"],
                    },
                    "output": {
                        "queries": [
                            {
                                "sql": "SELECT * FROM users WHERE email = 'a@x'",
                                "plan": [
                                    {"id": 2, "parent": 0, "detail": "SCAN users"}
                                ],
                                "issues": ["Full scan on `users`"],
                                "suggestions": [
                                    "Consider an index on the WHERE/JOIN columns used against `users`."
                                ],
                                "elapsed_ms": 0.3,
                            }
                        ],
                        "total_issues": 1,
                        "summary": "Found 1 potential plan issue(s) across 1 query/queries.",
                    },
                }
            ],
        },
        {
            "agent_id": _GIT_DIFF_ANALYZER_AGENT_ID,
            "name": "Git Diff Analyzer",
            "description": (
                "Use to parse a unified ``git diff`` and surface structural risk: file/hunk/line "
                "counts, language breakdown, sensitive-surface tags (auth, money, migrations, "
                "public_api, tests), removed-test detection, net-error-handling-decrease detection, "
                "TODO/FIXME counts, and inline credential-pattern detection. Pure parsing, no LLM. "
                "Ideal for pre-PR triage from Claude Code."
            ),
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_GIT_DIFF_ANALYZER_AGENT_ID],
            "price_per_call_usd": 0.01,
            "tags": ["git", "diff", "code-review", "risk", "developer-tools"],
            "kind": "aztea_built",
            "category": "Developer Tools",
            "is_featured": True,
            "cacheable": True,
            "input_schema": _output_schema_object(
                {
                    "diff": {
                        "type": "string",
                        "title": "Unified git diff",
                        "description": "Output of `git diff` (with `diff --git ...` headers). Max 500,000 chars.",
                        "maxLength": 500000,
                    },
                    "extra_risk_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional caller-defined globs to flag (currently informational).",
                    },
                },
                required=["diff"],
            ),
            "output_schema": _output_schema_object(
                {
                    "file_count": {"type": "integer"},
                    "hunk_count": {"type": "integer"},
                    "added_lines": {"type": "integer"},
                    "removed_lines": {"type": "integer"},
                    "binary_files": {"type": "integer"},
                    "files": {"type": "array", "items": {"type": "object"}},
                    "risk_summary": {"type": "object"},
                    "summary": {"type": "string"},
                },
                required=[
                    "file_count",
                    "added_lines",
                    "removed_lines",
                    "files",
                    "risk_summary",
                    "summary",
                ],
            ),
            "output_examples": [
                {
                    "input": {
                        "diff": "diff --git a/auth/login.py b/auth/login.py\n--- a/auth/login.py\n+++ b/auth/login.py\n@@ -1,3 +1,4 @@\n def login(u, p):\n+    # TODO: rate limit\n     return u == 'a'\n",
                    },
                    "output": {
                        "file_count": 1,
                        "hunk_count": 1,
                        "added_lines": 1,
                        "removed_lines": 0,
                        "binary_files": 0,
                        "files": [
                            {
                                "path": "auth/login.py",
                                "old_path": None,
                                "change_type": "modified",
                                "language": "python",
                                "added": 1,
                                "removed": 0,
                                "hunks": 1,
                                "is_binary": False,
                                "risk_tags": ["auth"],
                                "warnings": [
                                    "1 new TODO/FIXME/XXX/HACK comment(s) added."
                                ],
                            }
                        ],
                        "risk_summary": {
                            "auth_changes": 1,
                            "money_changes": 0,
                            "migration_changes": 0,
                            "public_api_changes": 0,
                            "test_files": 0,
                            "tests_removed": False,
                            "error_handling_removed": False,
                            "secret_pattern_added": False,
                            "todos_added": 1,
                        },
                        "summary": "1 file(s), 1 hunk(s), +1/-0 lines. 1 auth-surface file(s) touched.",
                    },
                }
            ],
        },
    ]
