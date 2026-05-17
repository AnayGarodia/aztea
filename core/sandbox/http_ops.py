"""sandbox_http_request — HTTP from inside the sandbox network with a persistent cookie jar.

# OWNS: sending HTTP requests through a container so the URL resolves
#       against the sandbox's compose service hostnames; cookie persistence
#       across calls so login flows compose correctly.
# NOT OWNS: SSRF — outbound HTTP from inside the sandbox is by definition
#           scoped to the sandbox network (or blocked by network policy).
# INVARIANTS:
#   * The cookie jar persists at <sandbox_dir>/cookies/<jar_key>.txt and is
#     bind-mounted into the helper container per call so each call sees
#     prior cookies.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from core.sandbox.docker_cli import run_docker
from core.sandbox.models import SandboxInvalidInput
from core.sandbox.secrets_store import all_secret_values, redact
from core.sandbox.state import SandboxState, get, sandbox_dir

_LOG = logging.getLogger("aztea.sandbox.http_ops")
_VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_DEFAULT_TIMEOUT = 30
_HARD_MAX_TIMEOUT = 120
_VALID_JAR_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def sandbox_http(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    method = str(payload.get("method") or "GET").strip().upper()
    if method not in _VALID_METHODS:
        raise SandboxInvalidInput(f"method must be one of {sorted(_VALID_METHODS)}")
    url = str(payload.get("url") or "").strip()
    if not url:
        raise SandboxInvalidInput("url is required")
    timeout = int(payload.get("timeout_seconds") or _DEFAULT_TIMEOUT)
    timeout = max(1, min(timeout, _HARD_MAX_TIMEOUT))
    jar_key = str(payload.get("jar_key") or "default").strip().lower()
    if not _VALID_JAR_RE.match(jar_key):
        raise SandboxInvalidInput("jar_key must match [a-z0-9_-]{1,32}")
    headers = payload.get("headers") or {}
    body = payload.get("body")
    body_b64 = payload.get("body_b64")
    jar_path = _ensure_jar_path(state, jar_key)
    container = _http_helper_container(state)
    if not shutil.which("docker"):
        raise SandboxInvalidInput("docker not available")
    return _curl_in_container(
        state=state,
        container=container,
        method=method,
        url=url,
        headers=headers,
        body=body,
        body_b64=body_b64,
        timeout=timeout,
        jar_path=jar_path,
    )


def _curl_in_container(
    *,
    state: SandboxState,
    container: str,
    method: str,
    url: str,
    headers: dict[str, Any],
    body: Any,
    body_b64: Any,
    timeout: int,
    jar_path: Path,
) -> dict[str, Any]:
    """Side-effect: run ``curl`` inside the container with the cookie jar mounted."""
    in_container_jar = "/tmp/aztea-cookies.txt"
    # Stage the jar into a place curl can read+write. We copy it in/out via
    # docker cp so the file at jar_path mirrors the curl-managed jar.
    if jar_path.exists():
        run_docker(["cp", str(jar_path), f"{container}:{in_container_jar}"], timeout=10, check=False)
    else:
        # Create empty file so curl can append.
        run_docker(
            ["exec", container, "sh", "-lc", f"true > {in_container_jar}"],
            timeout=5,
            check=False,
        )
    argv = [
        "exec",
        container,
        "sh",
        "-lc",
        _build_curl_command(
            method, url, headers, body, body_b64, timeout, in_container_jar
        ),
    ]
    proc = run_docker(argv, timeout=timeout + 10, check=False)
    # Persist jar back to the host so the next call sees the cookies.
    run_docker(["cp", f"{container}:{in_container_jar}", str(jar_path)], timeout=10, check=False)
    status_code, response_headers, response_body = _parse_curl_output(proc.stdout or "")
    state.touch()
    secret_values = all_secret_values(state.sandbox_id)
    return {
        "sandbox_id": state.sandbox_id,
        "status_code": status_code,
        "headers": response_headers,
        "body": redact(response_body, secret_values)[:64_000],
        "duration_ms": None,
        "jar_key": jar_path.stem,
        "stderr": redact((proc.stderr or "")[:2000], secret_values),
    }


def _build_curl_command(
    method: str,
    url: str,
    headers: dict[str, Any],
    body: Any,
    body_b64: Any,
    timeout: int,
    jar_path: str,
) -> str:
    parts = [
        "curl",
        "-sS",
        "-X",
        _shell_quote(method),
        "-D",
        "-",
        "-w",
        "'\\nAZTEA-STATUS:%{http_code}'",
        "--max-time",
        str(timeout),
        "-c",
        _shell_quote(jar_path),
        "-b",
        _shell_quote(jar_path),
    ]
    for k, v in (headers or {}).items():
        parts.extend(["-H", _shell_quote(f"{k}: {v}")])
    if body is not None:
        parts.extend(["--data-binary", _shell_quote(str(body))])
    elif body_b64 is not None:
        parts.extend(
            [
                "--data-binary",
                _shell_quote(f"@/dev/stdin"),
            ]
        )
        # We can't pipe stdin from this scope safely with shell quoting;
        # callers that need binary bodies should use sandbox_exec to
        # invoke curl directly. v1 simplification, documented as a
        # follow-up.
    parts.append(_shell_quote(url))
    return " ".join(parts)


def _parse_curl_output(stdout: str) -> tuple[int | None, dict[str, str], str]:
    """Pure-ish: separate status code, headers, and body from ``curl -D - -w ...`` output."""
    status_code: int | None = None
    headers: dict[str, str] = {}
    body = stdout
    marker = "AZTEA-STATUS:"
    if marker in stdout:
        body, _, status_part = stdout.rpartition(marker)
        body = body.rstrip("\n")
        try:
            status_code = int(status_part.strip())
        except ValueError:
            pass
    if "\r\n\r\n" in body:
        header_block, _, body = body.partition("\r\n\r\n")
        for line in header_block.splitlines():
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip()] = value.strip()
    return status_code, headers, body


def _http_helper_container(state: SandboxState) -> str:
    """Pure: pick a container that has ``curl`` to run the request from.

    Why: most app images already include curl. If none does, the caller
    can fall back to sandbox_exec with curl installed via a one-off.
    """
    for hint in ("app", "web", "api"):
        if hint in state.boot.services:
            return state.boot.services[hint]["container"]
    if not state.boot.services:
        raise SandboxInvalidInput("no services available; cannot host HTTP request")
    first = next(iter(state.boot.services.values()))
    return first.get("container") or next(iter(state.boot.services))


def _ensure_jar_path(state: SandboxState, jar_key: str) -> Path:
    jars = sandbox_dir(state.sandbox_id) / "cookies"
    jars.mkdir(parents=True, exist_ok=True, mode=0o700)
    return jars / f"{jar_key}.txt"


def _shell_quote(value: str) -> str:
    """Pure: minimal POSIX shell quote suitable for sh -lc."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxInvalidInput(f"sandbox '{sandbox_id}' not active")
    return state
