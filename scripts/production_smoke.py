#!/usr/bin/env python3
"""End-to-end production smoke test for the buyer/Claude Code surface.

Required:
  AZTEA_BASE_URL=https://api.aztea.ai
  AZTEA_API_KEY=az_...               # preferred

Alternative auth:
  AZTEA_SMOKE_EMAIL=user@example.com
  AZTEA_SMOKE_PASSWORD=...

Optional:
  AZTEA_SMOKE_INCLUDE_CLI=1          # also checks npx aztea-cli resolution
  AZTEA_SMOKE_TIMEOUT=20

This script intentionally uses public HTTP surfaces and the stdio MCP manifest
path. It is safe to run after deploys against a funded smoke account.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import requests

BASE_URL = os.environ.get("AZTEA_BASE_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = float(os.environ.get("AZTEA_SMOKE_TIMEOUT", "20"))
VERSION = "1.0"
CLIENT = "production-smoke"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class SmokeFailure(RuntimeError):
    pass


def _headers(api_key: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Aztea-Version": VERSION,
        "X-Aztea-Client": CLIENT,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _request(
    method: str, path: str, *, api_key: str | None = None, body: Any = None
) -> dict[str, Any]:
    resp = requests.request(
        method,
        f"{BASE_URL}{path}",
        headers=_headers(api_key),
        json=body,
        timeout=TIMEOUT,
    )
    try:
        payload = resp.json()
    except Exception:
        payload = {"raw_body": resp.text}
    if not resp.ok:
        raise SmokeFailure(f"{method} {path} -> {resp.status_code}: {payload}")
    return payload if isinstance(payload, dict) else {"result": payload}


def _auth() -> str:
    api_key = os.environ.get("AZTEA_API_KEY", "").strip()
    if api_key:
        return api_key
    email = os.environ.get("AZTEA_SMOKE_EMAIL", "").strip()
    password = os.environ.get("AZTEA_SMOKE_PASSWORD", "")
    if not email or not password:
        raise SmokeFailure(
            "Set AZTEA_API_KEY or AZTEA_SMOKE_EMAIL/AZTEA_SMOKE_PASSWORD."
        )
    payload = _request(
        "POST", "/auth/login", body={"email": email, "password": password}
    )
    raw_key = str(payload.get("raw_key") or payload.get("api_key") or "").strip()
    if not raw_key:
        raise SmokeFailure(f"Login succeeded but no API key was returned: {payload}")
    return raw_key


def _search(api_key: str, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    payload = _request(
        "POST",
        "/registry/search",
        api_key=api_key,
        body={"query": query, "limit": limit},
    )
    results = payload.get("results") or []
    if not results:
        raise SmokeFailure(f"Search returned no results for {query!r}.")
    return [item.get("agent") or {} for item in results if isinstance(item, dict)]


def _find_agent(api_key: str, query: str, name_contains: str) -> dict[str, Any]:
    for agent in _search(api_key, query, limit=8):
        if name_contains.lower() in str(agent.get("name") or "").lower():
            return agent
    raise SmokeFailure(
        f"Could not find agent containing {name_contains!r} for query {query!r}."
    )


def _poll_job(api_key: str, job_id: str, *, timeout_s: float = 45.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _request("GET", f"/jobs/{job_id}", api_key=api_key)
        if str(last.get("status") or "").lower() in {"complete", "failed"}:
            return last
        time.sleep(1.0)
    raise SmokeFailure(
        f"Job {job_id} did not finish before timeout. Last status: {last}"
    )


def _check_mcp_manifest(api_key: str) -> None:
    cmd = [
        sys.executable,
        "scripts/aztea_mcp_server.py",
        "--base-url",
        BASE_URL,
        "--api-key",
        api_key,
        "--print-tools",
    ]
    proc = subprocess.run(
        cmd, cwd=os.getcwd(), text=True, capture_output=True, timeout=TIMEOUT
    )
    if proc.returncode != 0:
        raise SmokeFailure(f"MCP manifest failed: {proc.stderr[:1000]}")
    payload = json.loads(proc.stdout)
    tool_names = {tool.get("name") for tool in payload.get("tools") or []}
    if not {"aztea_search", "aztea_describe", "aztea_call"} <= tool_names:
        raise SmokeFailure(
            f"MCP lazy tools missing from manifest: {sorted(tool_names)}"
        )


def _check_cli_resolution() -> None:
    if os.environ.get("AZTEA_SMOKE_INCLUDE_CLI") != "1":
        return
    proc = subprocess.run(
        ["npx", "-y", "aztea-cli@latest", "--help"],
        text=True,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise SmokeFailure(f"npx aztea-cli@latest --help failed: {proc.stderr[:1000]}")


def main() -> int:
    checks: list[Check] = []

    def run(name: str, fn) -> Any:
        started = time.monotonic()
        try:
            value = fn()
        except Exception as exc:
            checks.append(Check(name, False, str(exc)))
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        checks.append(Check(name, True, f"{elapsed_ms}ms"))
        return value

    try:
        try:
            run("health", lambda: _request("GET", "/health"))
            api_key = run("auth", _auth)
            run("wallet", lambda: _request("GET", "/wallets/me", api_key=api_key))
            run("mcp manifest", lambda: _check_mcp_manifest(api_key))
            run("cli package", _check_cli_resolution)

            linter = run(
                "search linter",
                lambda: _find_agent(api_key, "lint python code", "Linter"),
            )
            type_checker = run(
                "search type checker",
                lambda: _find_agent(api_key, "type check python", "Type Checker"),
            )
            linter_id = linter["agent_id"]
            type_id = type_checker["agent_id"]

            run(
                "estimate",
                lambda: _request(
                    "POST",
                    f"/agents/{linter_id}/estimate",
                    api_key=api_key,
                    body={"code": "x=1\n"},
                ),
            )
            run(
                "direct call",
                lambda: _request(
                    "POST",
                    f"/registry/agents/{linter_id}/call",
                    api_key=api_key,
                    body={"code": "import os\nx=1\n", "language": "python"},
                ),
            )

            async_job = run(
                "async submit",
                lambda: _request(
                    "POST",
                    "/jobs",
                    api_key=api_key,
                    body={
                        "agent_id": linter_id,
                        "input_payload": {
                            "code": "import os\nx=1\n",
                            "language": "python",
                        },
                    },
                ),
            )
            run("async complete", lambda: _poll_job(api_key, async_job["job_id"]))

            batch = run(
                "batch submit",
                lambda: _request(
                    "POST",
                    "/jobs/batch",
                    api_key=api_key,
                    body={
                        "jobs": [
                            {
                                "agent_id": linter_id,
                                "input_payload": {
                                    "code": "x=1\n",
                                    "language": "python",
                                },
                            },
                            {
                                "agent_id": type_id,
                                "input_payload": {
                                    "code": "def f(x: int) -> str:\n    return x\n",
                                    "language": "python",
                                },
                            },
                        ]
                    },
                ),
            )
            for item in batch.get("jobs") or []:
                if item.get("job_id"):
                    run(
                        f"batch job {item['job_id']}",
                        lambda job_id=item["job_id"]: _poll_job(api_key, job_id),
                    )

            compare = run(
                "compare",
                lambda: _request(
                    "POST",
                    "/jobs/compare",
                    api_key=api_key,
                    body={
                        "agent_ids": [linter_id, type_id],
                        "input_payload": {"code": "x=1\n", "language": "python"},
                    },
                ),
            )
            compare_id = compare.get("compare_id")
            if compare_id:
                run(
                    "compare status",
                    lambda: _request(
                        "GET", f"/jobs/compare/{compare_id}", api_key=api_key
                    ),
                )

            run(
                "recipe",
                lambda: _request(
                    "POST",
                    "/recipes/audit-deps/run",
                    api_key=api_key,
                    body={
                        "input_payload": {
                            "manifest": '{"dependencies":{"lodash":"4.17.20"}}'
                        }
                    },
                ),
            )

            bad = run(
                "refund failure submit",
                lambda: _request(
                    "POST",
                    "/jobs",
                    api_key=api_key,
                    body={"agent_id": linter_id, "input_payload": {"code": 12345}},
                ),
            )
            failed = run(
                "refund failure complete", lambda: _poll_job(api_key, bad["job_id"])
            )
            if failed.get("status") != "failed":
                raise SmokeFailure(
                    f"Expected malformed job to fail, got {failed.get('status')}."
                )

            run(
                "ledger reconcile",
                lambda: _request("GET", "/ops/payments/reconcile", api_key=api_key),
            )
        except SmokeFailure:
            pass  # already recorded in checks; report below
    finally:
        print(
            json.dumps(
                {"base_url": BASE_URL, "checks": [c.__dict__ for c in checks]}, indent=2
            )
        )

    if any(not c.ok for c in checks):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
