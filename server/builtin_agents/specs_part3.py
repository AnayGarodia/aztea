"""Third chunk of built-in agent specs — new agents added in v2."""
from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
    MULTI_FILE_EXECUTOR_AGENT_ID as _MULTI_FILE_EXECUTOR_AGENT_ID,
    LINTER_AGENT_ID as _LINTER_AGENT_ID,
    SHELL_EXECUTOR_AGENT_ID as _SHELL_EXECUTOR_AGENT_ID,
    TYPE_CHECKER_AGENT_ID as _TYPE_CHECKER_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def load_builtin_specs_part3() -> list[dict[str, Any]]:
    return [
    {
        "agent_id": _MULTI_FILE_EXECUTOR_AGENT_ID,
        "name": "Multi-File Python Executor",
        "description": "Use when running a multi-file Python project with dependencies. Writes all files to a sandbox tempdir, optionally installs requirements.txt packages via pip, then runs the entry point and returns stdout, stderr, exit code, and an expert explanation. Single-file use cases should use Python Code Executor instead.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_MULTI_FILE_EXECUTOR_AGENT_ID],
        "price_per_call_usd": 0.03,
        "tags": ["code-execution", "python", "developer-tools", "compute"],
        "kind": "aztea_built",
        "category": "Code Execution",
        "is_featured": True,
        "input_schema": _output_schema_object(
            {
                "files": {
                    "type": "array",
                    "title": "Project files",
                    "description": "List of {path, content} objects. Max 20 files, 50KB each.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path, e.g. src/main.py"},
                            "content": {"type": "string", "description": "File content as a string"},
                        },
                        "required": ["path", "content"],
                    },
                },
                "requirements": {
                    "type": "string",
                    "title": "Requirements",
                    "description": "Optional requirements.txt content; packages are pip-installed before running.",
                    "maxLength": 2000,
                },
                "entry_point": {
                    "type": "string",
                    "title": "Entry point",
                    "description": "Which file to run (default: main.py).",
                    "default": "main.py",
                },
                "stdin": {
                    "type": "string",
                    "title": "Stdin",
                    "description": "Optional data fed to the process stdin.",
                },
                "timeout": {
                    "type": "integer",
                    "title": "Timeout (seconds)",
                    "description": "Execution timeout in seconds (max 30).",
                    "default": 15,
                    "minimum": 1,
                    "maximum": 30,
                },
                "explain": {
                    "type": "boolean",
                    "title": "Explain output",
                    "description": "Whether to include an expert explanation of what the output means.",
                    "default": True,
                },
            },
            required=["files"],
        ),
        "output_schema": _output_schema_object(
            {
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_code": {"type": "integer"},
                "timed_out": {"type": "boolean"},
                "execution_time_ms": {"type": "integer"},
                "files_written": {"type": "integer"},
                "packages_installed": {"type": "array", "items": {"type": "string"}},
                "install_error": {"type": ["string", "null"]},
                "explanation": {"type": "string"},
            },
            required=["stdout", "exit_code", "timed_out"],
        ),
        "output_examples": [
            {
                "input": {
                    "files": [
                        {"path": "utils.py", "content": "def add(a, b):\n    return a + b"},
                        {"path": "main.py", "content": "from utils import add\nprint(add(3, 4))"},
                    ],
                    "entry_point": "main.py",
                },
                "output": {
                    "stdout": "7\n",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "execution_time_ms": 42,
                    "files_written": 2,
                    "packages_installed": [],
                    "install_error": None,
                    "explanation": "The project imports add() from utils.py and prints the sum of 3 + 4, which is 7.",
                },
            }
        ],
    },
    {
        "agent_id": _LINTER_AGENT_ID,
        "name": "Linter Agent",
        "description": "Use when you want to lint Python, JavaScript, or TypeScript code without a local toolchain. For Python, runs ruff (style, bugs, complexity). For JS/TS, runs ESLint with a minimal built-in ruleset. Returns structured issues with rule IDs, line numbers, severity, and whether a fix is available. Faster and cheaper than a full code review.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_LINTER_AGENT_ID],
        "price_per_call_usd": 0.01,
        "tags": ["linting", "ruff", "python", "javascript", "developer-tools", "code-quality"],
        "kind": "aztea_built",
        "category": "Code Execution",
        "is_featured": True,
        "input_schema": _output_schema_object(
            {
                "code": {
                    "type": "string",
                    "title": "Source code",
                    "description": "Code to lint. Max 30,000 characters.",
                    "maxLength": 30000,
                },
                "language": {
                    "type": "string",
                    "title": "Language",
                    "description": "Programming language. 'auto' detects from code patterns.",
                    "default": "auto",
                    "enum": ["python", "javascript", "typescript", "auto"],
                },
                "filename": {
                    "type": "string",
                    "title": "Filename hint",
                    "description": "Optional filename for extension-based language detection, e.g. 'app.py'.",
                },
                "checks": {
                    "type": "array",
                    "title": "Checks",
                    "description": "Which categories to check (default: all).",
                    "items": {"type": "string", "enum": ["style", "bugs", "complexity"]},
                    "default": ["style", "bugs", "complexity"],
                },
            },
            required=["code"],
        ),
        "output_schema": _output_schema_object(
            {
                "language": {"type": "string"},
                "tool": {"type": "string", "description": "'ruff' for Python, 'llm' for JS/TS"},
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rule": {"type": "string"},
                            "message": {"type": "string"},
                            "line": {"type": ["integer", "null"]},
                            "column": {"type": ["integer", "null"]},
                            "severity": {"type": "string"},
                            "fix_available": {"type": "boolean"},
                        },
                    },
                },
                "total_issues": {"type": "integer"},
                "error_count": {"type": "integer"},
                "warning_count": {"type": "integer"},
                "clean": {"type": "boolean"},
                "summary": {"type": "string"},
            },
            required=["language", "tool", "issues", "total_issues", "clean", "summary"],
        ),
        "output_examples": [
            {
                "input": {"code": "import os\nimport sys\n\ndef add(x,y):\n    return x+y\n", "language": "python"},
                "output": {
                    "language": "python",
                    "tool": "ruff",
                    "issues": [
                        {"rule": "F401", "message": "'os' imported but unused", "line": 1, "column": 1, "severity": "error", "fix_available": True},
                        {"rule": "F401", "message": "'sys' imported but unused", "line": 2, "column": 1, "severity": "error", "fix_available": True},
                        {"rule": "E231", "message": "Missing whitespace after ','", "line": 4, "column": 8, "severity": "warning", "fix_available": True},
                    ],
                    "total_issues": 3,
                    "error_count": 2,
                    "warning_count": 1,
                    "clean": False,
                    "summary": "ruff found 3 issues: 2 unused imports and 1 formatting issue.",
                },
            }
        ],
    },
    {
        "agent_id": _SHELL_EXECUTOR_AGENT_ID,
        "name": "Shell Executor",
        "description": "Run sandboxed shell commands (npm, node, python, pip, ruff, mypy, tsc, git log/diff/status, make, cargo, go, pytest) and get real stdout/stderr/exit code. Use for verifying builds, running tests, checking lint, inspecting git history — anything that needs an actual shell.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_SHELL_EXECUTOR_AGENT_ID],
        "price_per_call_usd": 0.03,
        "tags": ["developer-tools", "shell", "execution", "ci"],
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run", "example": "npm test"},
                "working_dir": {"type": "string", "default": "/tmp", "description": "Working directory (must exist on server)"},
                "env": {"type": "object", "description": "Extra environment variables", "additionalProperties": {"type": "string"}},
                "timeout": {"type": "integer", "default": 15, "minimum": 1, "maximum": 60, "description": "Timeout in seconds"},
            },
            "required": ["command"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "timed_out": {"type": "boolean"},
                "elapsed_seconds": {"type": "number"},
            },
            "required": ["command", "exit_code", "stdout", "stderr", "timed_out", "elapsed_seconds"],
        },
        "output_examples": [
            {
                "input": {"command": "python3 --version"},
                "output": {
                    "command": "python3 --version",
                    "exit_code": 0,
                    "stdout": "Python 3.11.6\n",
                    "stderr": "",
                    "timed_out": False,
                    "elapsed_seconds": 0.08,
                },
            }
        ],
    },
    {
        "agent_id": _TYPE_CHECKER_AGENT_ID,
        "name": "Type Checker",
        "description": "Run mypy (Python) or tsc (TypeScript) on submitted code and return structured type errors with file, line, column, error code, and message. Closes the gap between writing code and knowing it type-checks.",
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_TYPE_CHECKER_AGENT_ID],
        "price_per_call_usd": 0.02,
        "tags": ["developer-tools", "type-checking", "python", "typescript"],
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Source code to type-check"},
                "language": {"type": "string", "enum": ["python", "typescript"], "default": "python"},
                "stubs": {
                    "type": "object",
                    "description": "Additional files needed for type resolution (filename → content)",
                    "additionalProperties": {"type": "string"},
                },
                "strict": {"type": "boolean", "default": False, "description": "Enable strict mode (--strict for mypy / strict tsconfig)"},
            },
            "required": ["code"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "language": {"type": "string"},
                "passed": {"type": "boolean"},
                "error_count": {"type": "integer"},
                "errors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "line": {"type": ["integer", "null"]},
                            "col": {"type": ["integer", "null"]},
                            "code": {"type": "string"},
                            "message": {"type": "string"},
                        },
                    },
                },
                "raw_output": {"type": "string"},
                "tool_version": {"type": "string"},
            },
            "required": ["language", "passed", "error_count", "errors", "raw_output", "tool_version"],
        },
        "output_examples": [
            {
                "input": {"code": "def greet(name: str) -> str:\n    return 42\n", "language": "python"},
                "output": {
                    "language": "python",
                    "passed": False,
                    "error_count": 1,
                    "errors": [{"file": "main.py", "line": 2, "col": 12, "code": "return-value", "message": "Incompatible return value type (got \"int\", expected \"str\")"}],
                    "raw_output": "main.py:2:12: error: Incompatible return value type (got \"int\", expected \"str\")  [return-value]\n",
                    "tool_version": "mypy 1.8.0",
                },
            }
        ],
    },
    ]
