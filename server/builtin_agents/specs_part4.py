"""Fourth chunk of built-in agent specs — Phase 7 real capability agents."""
from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
    AI_RED_TEAMER_AGENT_ID as _AI_RED_TEAMER_AGENT_ID,
    BROWSER_AGENT_ID as _BROWSER_AGENT_ID,
    DB_SANDBOX_AGENT_ID as _DB_SANDBOX_AGENT_ID,
    LIVE_ENDPOINT_TESTER_AGENT_ID as _LIVE_ENDPOINT_TESTER_AGENT_ID,
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID as _MULTI_LANGUAGE_EXECUTOR_AGENT_ID,
    SEMANTIC_CODEBASE_SEARCH_AGENT_ID as _SEMANTIC_CODEBASE_SEARCH_AGENT_ID,
    VISUAL_REGRESSION_AGENT_ID as _VISUAL_REGRESSION_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def load_builtin_specs_part4() -> list[dict[str, Any]]:
    return [
        {
            "agent_id": _DB_SANDBOX_AGENT_ID,
            "name": "DB Sandbox",
            "description": "Use when you need to execute SQL against an ephemeral SQLite database. Creates a fresh sandbox per call, supports schema setup plus one or more queries, returns rows and EXPLAIN QUERY PLAN output, and never touches the platform database.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_DB_SANDBOX_AGENT_ID],
            "price_per_call_usd": 0.04,
            "tags": ["sql", "sqlite", "database", "developer-tools", "testing"],
            "kind": "aztea_built",
            "category": "Code Execution",
            "is_featured": True,
            "cacheable": True,
            "input_schema": _output_schema_object(
                {
                    "schema_sql": {
                        "type": "string",
                        "title": "Schema/setup SQL",
                        "description": "Optional DDL and seed SQL executed before queries.",
                    },
                    "sql": {
                        "type": "string",
                        "title": "Single SQL statement",
                        "description": "Use this for one query. Mutually compatible with queries; if both are provided queries wins.",
                    },
                    "params": {
                        "type": "array",
                        "title": "SQL parameters",
                        "description": "Parameters bound positionally to sql.",
                        "items": {},
                    },
                    "queries": {
                        "type": "array",
                        "title": "Query batch",
                        "description": "List of {sql, params?} objects executed in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "sql": {"type": "string"},
                                "params": {"type": "array", "items": {}},
                            },
                            "required": ["sql"],
                        },
                    },
                    "explain": {
                        "type": "boolean",
                        "title": "Explain query plan",
                        "description": "Include EXPLAIN QUERY PLAN output for each statement.",
                        "default": True,
                    },
                }
            ),
            "output_schema": _output_schema_object(
                {
                    "engine": {"type": "string"},
                    "results": {"type": "array", "items": {"type": "object"}},
                    "statements_executed": {"type": "integer"},
                    "db_size_bytes": {"type": "integer"},
                    "execution_time_ms": {"type": "integer"},
                },
                required=["engine", "results", "statements_executed"],
            ),
            "output_examples": [
                {
                    "input": {
                        "schema_sql": "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO users(name) VALUES ('Ada'), ('Linus');",
                        "sql": "SELECT name FROM users ORDER BY id",
                    },
                    "output": {
                        "engine": "sqlite",
                        "results": [
                            {
                                "sql": "SELECT name FROM users ORDER BY id",
                                "columns": ["name"],
                                "rows": [{"name": "Ada"}, {"name": "Linus"}],
                                "row_count": 2,
                                "truncated": False,
                                "rows_affected": None,
                                "query_plan": [{"select_id": 2, "order": 0, "from": 0, "detail": "SCAN users"}],
                                "execution_time_ms": 1,
                            }
                        ],
                        "statements_executed": 1,
                        "db_size_bytes": 8192,
                        "execution_time_ms": 3,
                    },
                }
            ],
        },
        {
            "agent_id": _VISUAL_REGRESSION_AGENT_ID,
            "name": "Visual Regression",
            "description": "Use when you need to compare two screenshots or image artifacts precisely. Fetches or decodes both images, computes pixel-level diff, highlights changed regions, and returns an annotated PNG artifact.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_VISUAL_REGRESSION_AGENT_ID],
            "price_per_call_usd": 0.05,
            "tags": ["visual-testing", "screenshots", "diff", "qa", "artifacts"],
            "kind": "aztea_built",
            "category": "QA",
            "is_featured": True,
            "cacheable": False,
            "input_schema": _output_schema_object(
                {
                    "left_url": {"type": "string", "title": "Baseline URL", "description": "Public image URL or data URL."},
                    "right_url": {"type": "string", "title": "Candidate URL", "description": "Public image URL or data URL."},
                    "left_artifact": {"type": "object", "title": "Baseline artifact", "description": "Artifact object containing url_or_base64."},
                    "right_artifact": {"type": "object", "title": "Candidate artifact", "description": "Artifact object containing url_or_base64."},
                }
            ),
            "output_schema": _output_schema_object(
                {
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "changed_pixels": {"type": "integer"},
                    "diff_percent": {"type": "number"},
                    "changed_regions": {"type": "array", "items": {"type": "object"}},
                    "artifacts": {"type": "array", "items": {"type": "object"}},
                    "summary": {"type": "string"},
                },
                required=["width", "height", "changed_pixels", "diff_percent", "artifacts"],
            ),
            "output_examples": [
                {
                    "input": {"left_url": "https://example.com/baseline.png", "right_url": "https://example.com/candidate.png"},
                    "output": {
                        "width": 1440,
                        "height": 900,
                        "changed_pixels": 1820,
                        "diff_percent": 0.1404,
                        "changed_regions": [{"x": 1040, "y": 112, "width": 120, "height": 48}],
                        "artifacts": [
                            {
                                "name": "visual-regression-diff.png",
                                "mime": "image/png",
                                "url_or_base64": "data:image/png;base64,...",
                                "size_bytes": 22104,
                            }
                        ],
                        "summary": "Detected 1820 changed pixels (0.1404% of the image).",
                    },
                }
            ],
        },
        {
            "agent_id": _LIVE_ENDPOINT_TESTER_AGENT_ID,
            "name": "Live Endpoint Tester",
            "description": "Use when you need to measure the real latency and status profile of an HTTP endpoint. Sends multiple live requests with bounded concurrency, returns p50/p95/p99 latency, status-code distribution, histogram buckets, and sample failures.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_LIVE_ENDPOINT_TESTER_AGENT_ID],
            "price_per_call_usd": 0.06,
            "tags": ["http", "performance", "latency", "load-test", "qa"],
            "kind": "aztea_built",
            "category": "QA",
            "is_featured": True,
            "cacheable": False,
            "input_schema": _output_schema_object(
                {
                    "url": {"type": "string", "title": "Endpoint URL", "description": "Public http(s) endpoint to probe."},
                    "method": {"type": "string", "title": "HTTP method", "default": "GET", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
                    "headers": {"type": "object", "title": "Headers", "description": "Optional HTTP headers."},
                    "body": {"title": "JSON or raw body", "description": "Optional request payload."},
                    "requests": {"type": "integer", "title": "Request count", "default": 50, "minimum": 1, "maximum": 200},
                    "concurrency": {"type": "integer", "title": "Concurrency", "default": 5, "minimum": 1, "maximum": 50},
                    "timeout_seconds": {"type": "number", "title": "Per-request timeout", "default": 5, "minimum": 0.1, "maximum": 10},
                },
                required=["url"],
            ),
            "output_schema": _output_schema_object(
                {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "requests": {"type": "integer"},
                    "concurrency": {"type": "integer"},
                    "success_count": {"type": "integer"},
                    "failure_count": {"type": "integer"},
                    "status_counts": {"type": "object"},
                    "p50_latency_ms": {"type": "integer"},
                    "p95_latency_ms": {"type": "integer"},
                    "p99_latency_ms": {"type": "integer"},
                    "avg_latency_ms": {"type": "integer"},
                    "histogram": {"type": "array", "items": {"type": "object"}},
                    "sample_errors": {"type": "array", "items": {"type": "string"}},
                    "execution_time_ms": {"type": "integer"},
                },
                required=["url", "requests", "success_count", "failure_count", "p95_latency_ms"],
            ),
            "output_examples": [
                {
                    "input": {"url": "https://api.example.com/health", "requests": 20, "concurrency": 4},
                    "output": {
                        "url": "https://api.example.com/health",
                        "method": "GET",
                        "requests": 20,
                        "concurrency": 4,
                        "success_count": 20,
                        "failure_count": 0,
                        "status_counts": {"200": 20},
                        "p50_latency_ms": 81,
                        "p95_latency_ms": 140,
                        "p99_latency_ms": 145,
                        "avg_latency_ms": 88,
                        "histogram": [{"lt_ms": 50, "count": 0}, {"lt_ms": 100, "count": 16}],
                        "sample_errors": [],
                        "execution_time_ms": 452,
                    },
                }
            ],
        },
        {
            "agent_id": _BROWSER_AGENT_ID,
            "name": "Browser Agent",
            "description": "Use when you need to fetch a live web page and capture its rendered HTML and a screenshot. Launches a headless Chromium browser, navigates to the URL, waits for the page to settle, then returns the full HTML source and a PNG screenshot artifact. Useful for scraping SPAs, verifying rendered output, or visual QA of any public URL.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_BROWSER_AGENT_ID],
            "price_per_call_usd": 0.06,
            "tags": ["browser", "screenshot", "scraping", "playwright", "headless", "html"],
            "kind": "aztea_built",
            "category": "Web",
            "is_featured": True,
            "cacheable": False,
            "input_schema": _output_schema_object(
                {
                    "url": {"type": "string", "title": "URL", "description": "Public https:// URL to navigate to. SSRF-blocked."},
                    "wait_ms": {"type": "integer", "title": "Extra wait (ms)", "description": "Additional wait after page settles (max 10000 ms).", "default": 1500},
                    "capture_network": {"type": "boolean", "title": "Capture network log", "description": "Include a log of all HTTP requests made by the page.", "default": False},
                    "viewport": {
                        "type": "object",
                        "title": "Viewport size",
                        "properties": {"width": {"type": "integer", "default": 1280}, "height": {"type": "integer", "default": 720}},
                    },
                },
                required=["url"],
            ),
            "output_schema": _output_schema_object(
                {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "html": {"type": "string"},
                    "html_chars": {"type": "integer"},
                    "screenshot_artifact": {"type": "object"},
                    "network_log": {"type": "array", "items": {"type": "object"}},
                    "execution_time_ms": {"type": "integer"},
                },
                required=["url", "title", "html", "screenshot_artifact"],
            ),
            "output_examples": [
                {
                    "input": {"url": "https://example.com"},
                    "output": {
                        "url": "https://example.com",
                        "title": "Example Domain",
                        "html": "<!doctype html>...",
                        "html_chars": 1256,
                        "screenshot_artifact": {"name": "screenshot.png", "mime": "image/png", "url_or_base64": "data:image/png;base64,...", "size_bytes": 18432},
                        "execution_time_ms": 2100,
                    },
                }
            ],
        },
        {
            "agent_id": _MULTI_LANGUAGE_EXECUTOR_AGENT_ID,
            "name": "Multi-Language Executor",
            "description": "Use when you need to run JavaScript, TypeScript, Go, or Rust code in a sandboxed subprocess. Selects the best available runtime (bun > deno > node for JS/TS; rustc/rust-script for Rust; go run for Go), executes the code with a configurable timeout, and returns stdout, stderr, exit code, and the exact runtime version used.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_MULTI_LANGUAGE_EXECUTOR_AGENT_ID],
            "price_per_call_usd": 0.04,
            "tags": ["javascript", "typescript", "go", "rust", "code-execution", "sandbox"],
            "kind": "aztea_built",
            "category": "Code Execution",
            "is_featured": True,
            "cacheable": True,
            "input_schema": _output_schema_object(
                {
                    "language": {"type": "string", "title": "Language", "enum": ["javascript", "typescript", "go", "rust"]},
                    "code": {"type": "string", "title": "Source code", "description": "Source code to execute (max 100 000 chars)."},
                    "stdin": {"type": "string", "title": "Standard input", "description": "Optional stdin for the process."},
                    "timeout_seconds": {"type": "number", "title": "Timeout (seconds)", "default": 15, "minimum": 1, "maximum": 30},
                },
                required=["language", "code"],
            ),
            "output_schema": _output_schema_object(
                {
                    "language": {"type": "string"},
                    "runtime": {"type": "string"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "passed": {"type": "boolean"},
                    "execution_time_ms": {"type": "integer"},
                },
                required=["language", "runtime", "stdout", "stderr", "exit_code", "passed"],
            ),
            "output_examples": [
                {
                    "input": {"language": "javascript", "code": "console.log('Hello from', process.version)"},
                    "output": {"language": "javascript", "runtime": "node v20.11.0", "stdout": "Hello from v20.11.0\n", "stderr": "", "exit_code": 0, "passed": True, "execution_time_ms": 83},
                }
            ],
        },
        {
            "agent_id": _SEMANTIC_CODEBASE_SEARCH_AGENT_ID,
            "name": "Semantic Codebase Search",
            "description": "Use when you need to find the most relevant files in a codebase for a natural-language query. Accepts a zip/tarball artifact or a public git URL, embeds each file's content using sentence-transformers, and returns the top-k semantically matching files with similarity scores and code snippets. Ideal for answering 'where is X implemented?' across an unfamiliar codebase.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SEMANTIC_CODEBASE_SEARCH_AGENT_ID],
            "price_per_call_usd": 0.08,
            "tags": ["search", "embeddings", "codebase", "semantic", "git", "developer-tools"],
            "kind": "aztea_built",
            "category": "Developer Tools",
            "is_featured": True,
            "cacheable": True,
            "input_schema": _output_schema_object(
                {
                    "query": {"type": "string", "title": "Search query", "description": "Natural-language description of what you are looking for (max 500 chars)."},
                    "artifact": {"type": "object", "title": "Zip/tarball artifact", "description": "Artifact dict with url_or_base64 field. Mutually exclusive with git_url."},
                    "git_url": {"type": "string", "title": "Git repository URL", "description": "Public https:// git URL to clone and index. Mutually exclusive with artifact."},
                    "top_k": {"type": "integer", "title": "Results to return", "default": 5, "minimum": 1, "maximum": 20},
                    "extensions": {"type": "array", "items": {"type": "string"}, "title": "File extension filter", "description": "e.g. [\".py\", \".ts\"]. Defaults to all common code/text extensions."},
                    "max_file_bytes": {"type": "integer", "title": "Max bytes per file", "default": 102400, "description": "Files larger than this are truncated before embedding."},
                },
                required=["query"],
            ),
            "output_schema": _output_schema_object(
                {
                    "query": {"type": "string"},
                    "total_files_indexed": {"type": "integer"},
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "score": {"type": "number"},
                                "snippet": {"type": "string"},
                                "size_bytes": {"type": "integer"},
                            },
                        },
                    },
                    "source": {"type": "string", "enum": ["artifact", "git"]},
                },
                required=["query", "total_files_indexed", "results", "source"],
            ),
            "output_examples": [
                {
                    "input": {"query": "PDF text extraction", "git_url": "https://github.com/example/myproject", "top_k": 3},
                    "output": {
                        "query": "PDF text extraction",
                        "total_files_indexed": 47,
                        "results": [
                            {"path": "src/parsers/pdf.py", "score": 0.8821, "snippet": "def extract_text(path: str) -> str:", "size_bytes": 2048},
                            {"path": "tests/test_pdf.py", "score": 0.7413, "snippet": "def test_extract_text_from_pdf():", "size_bytes": 912},
                        ],
                        "source": "git",
                    },
                }
            ],
        },
        {
            "agent_id": _AI_RED_TEAMER_AGENT_ID,
            "name": "AI Red Teamer",
            "description": "Use when you want to harden a published Aztea agent against adversarial inputs. Runs a battery of prompt-injection, jailbreak, boundary-violation, and data-exfiltration attacks against a target agent, then reports which attacks triggered suspicious responses and the overall attack success rate. Useful for security review before making an agent public.",
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_AI_RED_TEAMER_AGENT_ID],
            "price_per_call_usd": 0.10,
            "tags": ["security", "red-team", "adversarial", "jailbreak", "prompt-injection", "testing"],
            "kind": "aztea_built",
            "category": "Security",
            "is_featured": True,
            "cacheable": False,
            "input_schema": _output_schema_object(
                {
                    "target_agent_id": {"type": "string", "title": "Target agent ID", "description": "UUID of the Aztea agent to test."},
                    "api_key": {"type": "string", "title": "API key", "description": "Caller API key used to invoke the target agent (or set AZTEA_API_KEY env var)."},
                    "base_url": {"type": "string", "title": "Base URL", "description": "Aztea API base URL (default: https://aztea.ai)."},
                    "categories": {"type": "array", "items": {"type": "string"}, "title": "Attack categories", "description": "Subset of: injection, jailbreak, boundary, exfiltration, resource. Default: all."},
                    "custom_prompts": {"type": "array", "items": {"type": "string"}, "title": "Custom adversarial prompts", "description": "Additional prompts to test beyond the built-in corpus."},
                    "max_attacks": {"type": "integer", "title": "Max attacks", "default": 20, "minimum": 1, "maximum": 50},
                },
                required=["target_agent_id"],
            ),
            "output_schema": _output_schema_object(
                {
                    "target_agent_id": {"type": "string"},
                    "total_attacks": {"type": "integer"},
                    "successful_attacks": {"type": "integer"},
                    "attack_success_rate": {"type": "number"},
                    "results": {"type": "array", "items": {"type": "object"}},
                    "summary": {"type": "string"},
                },
                required=["target_agent_id", "total_attacks", "successful_attacks", "attack_success_rate", "results", "summary"],
            ),
            "output_examples": [
                {
                    "input": {"target_agent_id": "040dc3f5-afe7-5db7-b253-4936090cc7af", "max_attacks": 5},
                    "output": {
                        "target_agent_id": "040dc3f5-afe7-5db7-b253-4936090cc7af",
                        "total_attacks": 5,
                        "successful_attacks": 0,
                        "attack_success_rate": 0.0,
                        "results": [{"category": "injection", "prompt": "Ignore all previous instructions...", "response_snippet": "I can only execute Python code...", "http_status": 200, "succeeded": False, "reason": "No jailbreak indicators detected."}],
                        "summary": "Agent passed all 5 adversarial probes — no jailbreak indicators detected.",
                    },
                }
            ],
        },
    ]
