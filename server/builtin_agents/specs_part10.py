"""Spec for the live_sandbox built-in agent."""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
)
from server.builtin_agents.constants import (
    LIVE_SANDBOX_AGENT_ID as _LIVE_SANDBOX_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def load_builtin_specs_part10() -> list[dict[str, Any]]:
    return [
        {
            "agent_id": _LIVE_SANDBOX_AGENT_ID,
            "name": "Live Sandbox",
            "description": (
                "Spin up a Docker-backed clone of the user's project — services, "
                "DB, env, network — and poke at it like staging. Supports the "
                "full lifecycle (start/exec/snapshot/restore/fork/stop), DB "
                "ops (query/snapshot/restore/introspect; EXPLAIN-plan as a "
                "stubbed v0 surface), filesystem ops (read/write/atomic "
                "patch/glob/grep — also reachable under the documented "
                "sandbox_fs_* aliases), HTTP from inside the sandbox network "
                "with persistent cookies (sandbox_http or sandbox_http_request), "
                "hash-chained Ed25519-signed receipts per action. Browser / "
                "tunnels / k8s_apply / public-tunnel / webhook-capture surfaces "
                "return structured stub envelopes with planned schemas + "
                "follow-up issue references — they never hard-error. "
                "Isolation: Docker default — gVisor (runsc) is OPT-IN via "
                "isolation_backend='gvisor' and only when the host has runsc "
                "registered; the default response carries isolation.applied="
                "'docker'. Containers run as a non-root UID (1000:1000) on "
                "the direct-launch boot strategies (dockerfile / custom / "
                "devcontainer / nix); compose stacks honour whatever the "
                "user's compose file declares. Concurrency: this agent is "
                "rate-limited at the transport layer to the platform's "
                "per-key RPM (see metadata.concurrency); fan-outs above "
                "that drop into the standard 429 retry-after path."
            ),
            "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[_LIVE_SANDBOX_AGENT_ID],
            "price_per_call_usd": 0.05,
            "tags": [
                "sandbox",
                "developer-tools",
                "code-execution",
                "docker",
                "database",
                "ci",
            ],
            "match_keywords": [
                "live sandbox",
                "spin up sandbox",
                "boot the project",
                "boot the repo",
                "boot the app",
                "reproduce the bug in a sandbox",
                "clone of production",
                "throwaway environment",
                "docker compose up the repo",
                "run the user's app",
            ],
            "block_keywords": [
                "screenshot a website",
                "scan dependencies",
                "lookup cve",
                "audit my dockerfile",
            ],
            "category": "Developer Tools",
            "cacheable": False,
            "runtime_requirements": [
                "host: docker (daemon reachable from server)",
                "host: git (for source.kind='git')",
                "host: libfaketime (optional, for clock.frozen_at)",
                "host: rsync (optional, sync_from_local falls back to shutil)",
                (
                    "container: the default boot image (ubuntu:22.04) is "
                    "intentionally minimal — only the binaries from the "
                    "user's Dockerfile / compose / devcontainer are "
                    "guaranteed inside. For sandbox_exec you can set "
                    "boot.base_image to a fatter image (e.g. ubuntu:24.04, "
                    "node:20, python:3.12, devcontainers/base) when you "
                    "need curl / python3 / git inside the container."
                ),
            ],
            "metadata": {
                "concurrency": {
                    "rate_limited_by": "platform",
                    "note": (
                        "Calls are accounted under the platform per-key RPM "
                        "(see /health.rate_limit). 13+ parallel calls from a "
                        "single key will hit 429 retry_after_seconds=60 like "
                        "any other heavy agent. Aztea hosted defaults: "
                        "caller-scope ~12 RPM. Fan-outs above that should "
                        "use manage_workflow.hire_batch which schedules "
                        "around the limit."
                    ),
                    "recommended_max_parallel_from_one_key": 8,
                },
                "isolation_backends": ["docker", "gvisor"],
                "default_isolation": "docker",
                "host_kernel_visibility": {
                    "status": "leaks_via_uname",
                    "note": (
                        "Under the default Docker backend, ``uname -a`` inside "
                        "a sandbox returns the host kernel string (e.g. AWS "
                        "build identifier + Ubuntu LTS + build date). Docker "
                        "does not expose a flag to virtualise this; the kernel "
                        "is shared with the host by design. Switch to "
                        "isolation_backend='gvisor' for syscall-level "
                        "virtualisation when the host has runsc registered."
                    ),
                },
                "container_user": "non_root_1000_1000_on_direct_launch",
            },
            "tooling_kind": "sandbox_orchestration",
            "stability_tier": "beta",
            "codex_recommended": True,
            "short_use_cases": [
                "boot the user's repo and run their tests",
                "reproduce a bug in a clone of production",
                "snapshot/restore around a risky migration",
                "fork a sandbox to try three fixes in parallel",
            ],
            "examples_sensitive": False,
            "input_schema": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "description": (
                            "Sandbox verb to run. See description for the full "
                            "list; real-implementation verbs include sandbox_start, "
                            "sandbox_exec, sandbox_db_query, sandbox_snapshot, "
                            "sandbox_restore, sandbox_fork. Browser / public-tunnel "
                            "/ k8s verbs return structured stub envelopes."
                        ),
                    },
                    "input": {
                        "type": "object",
                        "description": "Action-specific payload (see action docs).",
                    },
                    "workspace_id": {
                        "type": "string",
                        "description": (
                            "Reserved for future workspace integration; "
                            "ignored in v0 but echoed into receipts."
                        ),
                    },
                    "idempotency_key": {
                        "type": "string",
                        "description": (
                            "Optional caller-supplied idempotency key. Mirrored "
                            "into the action's signed receipt."
                        ),
                    },
                },
                "additionalProperties": True,
            },
            "output_schema": _output_schema_object(
                {
                    "sandbox_id": {"type": "string"},
                    "action": {"type": "string"},
                    "receipt": {
                        "type": "object",
                        "properties": {
                            "did": {"type": "string"},
                            "alg": {"type": "string"},
                            "payload": {"type": "object"},
                            "signature": {"type": "string"},
                            "hash": {"type": "string"},
                        },
                    },
                },
                required=["receipt"],
            ),
            "output_examples": [
                {
                    "input": {
                        "action": "sandbox_start",
                        "input": {
                            "source": {
                                "kind": "git",
                                "url": "https://github.com/example/node-pg-demo.git",
                                "ref": "main",
                            },
                            "boot": {"strategy": "auto"},
                            "lifetime": {"max_minutes": 30},
                            "network": {"egress": "isolated"},
                        },
                    },
                    "output": {
                        "sandbox_id": "sbx_<hex>",
                        "status": "ready",
                        "boot_strategy_detected": "docker_compose",
                        "services": {
                            "web": {"container": "sbx-...-web"},
                            "db": {"container": "sbx-...-db"},
                        },
                        "boot_timing": {"clone": 4.2, "ready": 38.5, "total": 47.1},
                        "receipt": {
                            "did": "did:web:aztea.ai:agents:live-sandbox",
                            "alg": "Ed25519",
                        },
                    },
                },
                {
                    "input": {
                        "action": "sandbox_exec",
                        "input": {
                            "sandbox_id": "sbx_<hex>",
                            "cmd": "npm test -- --grep checkout",
                            "cwd": "/app",
                        },
                    },
                    "output": {
                        "sandbox_id": "sbx_<hex>",
                        "stdout": "PASS  test/checkout.test.js\n",
                        "stderr": "",
                        "exit_code": 0,
                        "timed_out": False,
                        "duration_ms": 14820,
                        "receipt": {
                            "alg": "Ed25519",
                        },
                    },
                },
            ],
        }
    ]
