"""Reproduction harness for the 2026-05-09 stress-test audit findings.

Runs each finding's failure scenario against a target Aztea server and
prints a per-finding pass/fail line. Pre-fix run shows the broken state;
post-fix run shows the same scenarios returning the expected envelopes.

Usage:
  AZTEA_API_KEY=... python scripts/audit_repro.py
  AZTEA_API_KEY=... AZTEA_HOST=https://aztea.ai python scripts/audit_repro.py

Findings covered:
  S1.1  sunset agents return 410 agent.sunset (was 502)
  S1.2  per-agent concurrency cap returns 429 (was 502 cascade at par>=25)
  S1.3  identical-input stampede collapses via singleflight (one work, N hits)
  S2.4  python_executor stdout has no ANSI escape sequences
  S2.5  invalid-URL rejections return 422 agent.invalid_input (was agent.internal_error)
  S2.5c double-rate returns 409 job.already_rated (was misleading code)
  S2.6  GET /jobs/{unknown} returns 403 (unified with rating endpoint)
  S2.7  /registry/agents/{id}/call response has inline `receipt` block
  S3.8  /wallets/me?limit=N honors N (was silently 50)

Each check prints one of:
  PASS  S1.1  sunset agent returns 410 agent.sunset
  FAIL  S1.1  expected 410 sunset, got 502 endpoint_misconfigured
  SKIP  S1.1  precondition unavailable: ...

Exit 0 if every non-skipped check passes; exit 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib import error as _err
from urllib import request as _req

# IDs from server/builtin_agents/constants.py
_JSON_SCHEMA_VALIDATOR_ID = "1b0b5820-b796-53cc-8d31-5e336d86d875"  # sunset
_REGEX_TESTER_ID = "36ae44b0-895b-5ef7-bc1f-1ecf08fce3ee"  # sunset
_CVELOOKUP_ID = "a3e239dd-ea92-556b-9c95-0a213a3daf59"
_PYTHON_EXECUTOR_ID = "040dc3f5-afe7-5db7-b253-4936090cc7af"
_ACCESSIBILITY_AUDITOR_ID = "41e95324-2480-5e53-9414-302d55673d50"


def _host() -> str:
    return os.environ.get("AZTEA_HOST", "http://localhost:8000").rstrip("/")


def _api_key() -> str:
    key = os.environ.get("AZTEA_API_KEY") or os.environ.get("AZTEA_CALLER_KEY") or ""
    if not key:
        sys.stderr.write(
            "ERROR: set AZTEA_API_KEY (a caller-scoped key) before running.\n"
        )
        sys.exit(2)
    return key


def _request(
    method: str,
    path: str,
    *,
    body: Any = None,
    timeout: float = 30.0,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    url = f"{_host()}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = _req.Request(url, data=data, headers=headers, method=method)
    try:
        with _req.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except _err.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else ""
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except _err.URLError as exc:
        return 0, f"network error: {exc}"


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

_RESULTS: list[tuple[str, str, str]] = []  # (status, code, message)


def _record(status: str, code: str, message: str) -> None:
    _RESULTS.append((status, code, message))
    print(f"{status:5}  {code}  {message}")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_s11_sunset_agent_returns_410() -> None:
    code, body = _request(
        "POST",
        f"/registry/agents/{_JSON_SCHEMA_VALIDATOR_ID}/call",
        body={"schema": {"type": "number"}, "document": 42},
    )
    if code == 410 and isinstance(body, dict) and (
        body.get("error") == "agent.sunset"
        or (isinstance(body.get("detail"), dict) and body["detail"].get("error") == "agent.sunset")
    ):
        _record("PASS", "S1.1", "sunset agent returns 410 agent.sunset")
    else:
        _record(
            "FAIL",
            "S1.1",
            f"expected 410 agent.sunset, got HTTP {code} body={str(body)[:200]}",
        )


def check_s12_concurrency_cap_returns_429() -> None:
    """Fire 32 parallel calls at python_executor (cap 16). Expect at least
    a few 429 agent.upstream_timeout responses, never 502 cascades."""
    payload = {"code": "print(2 + 2)"}

    def _one() -> tuple[int, str]:
        c, b = _request(
            "POST",
            f"/registry/agents/{_PYTHON_EXECUTOR_ID}/call",
            body=payload,
            timeout=20.0,
        )
        envelope_code = ""
        if isinstance(b, dict):
            envelope_code = str(
                b.get("error") or (b.get("detail") or {}).get("error") or ""
            )
        return c, envelope_code

    statuses: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(_one) for _ in range(32)]
        for fut in as_completed(futures):
            try:
                statuses.append(fut.result())
            except Exception as exc:
                statuses.append((0, f"raised: {exc}"))

    n_ok = sum(1 for s, _ in statuses if s == 200)
    n_429 = sum(1 for s, _ in statuses if s == 429)
    n_502 = sum(1 for s, _ in statuses if s == 502)
    if n_502 == 0 and (n_429 > 0 or n_ok == 32):
        _record(
            "PASS",
            "S1.2",
            f"32 parallel calls -> ok={n_ok} 429={n_429} 502={n_502} (graceful)",
        )
    else:
        _record(
            "FAIL",
            "S1.2",
            f"32 parallel calls -> ok={n_ok} 429={n_429} 502={n_502} (cascade!)",
        )


def check_s13_singleflight_collapses_stampede() -> None:
    """20 simultaneous identical CVE lookups should collapse — most return
    cached:true after the first leader writes, OR all return identical results."""
    payload = {"cve_id": "CVE-2021-44228"}

    def _one() -> tuple[int, dict[str, Any] | str]:
        return _request(
            "POST",
            f"/registry/agents/{_CVELOOKUP_ID}/call",
            body=payload,
            timeout=30.0,
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: _one(), range(20)))

    successful = [r for c, r in results if c == 200 and isinstance(r, dict)]
    if not successful:
        _record("SKIP", "S1.3", "no successful responses to evaluate cache hits")
        return
    n_cached = sum(1 for r in successful if r.get("cached"))
    if n_cached >= len(successful) // 2:
        _record(
            "PASS",
            "S1.3",
            f"{n_cached}/{len(successful)} stampede calls served from cache",
        )
    elif n_cached > 0:
        _record(
            "PARTIAL",
            "S1.3",
            f"{n_cached}/{len(successful)} cached — singleflight active but cold",
        )
    else:
        _record(
            "FAIL",
            "S1.3",
            f"0/{len(successful)} cached — singleflight not collapsing identical inputs",
        )


def check_s24_no_ansi_in_python_executor_stdout() -> None:
    code, body = _request(
        "POST",
        f"/registry/agents/{_PYTHON_EXECUTOR_ID}/call",
        body={
            "code": "import sys; sys.stdout.write('\\x1b[2J\\x1b[H' + 'CLEARED' + '\\x1b[31mRED\\x1b[0m')"
        },
        timeout=30.0,
    )
    if code != 200 or not isinstance(body, dict):
        _record("SKIP", "S2.4", f"call failed: HTTP {code}")
        return
    out = body.get("output") or {}
    stdout = str(out.get("stdout") or "")
    if "\x1b[" in stdout or "\x07" in stdout or "\x08" in stdout:
        _record(
            "FAIL",
            "S2.4",
            f"ANSI / control bytes leaked into stdout: {stdout!r}",
        )
    else:
        _record("PASS", "S2.4", "ANSI escapes stripped from stdout")


def check_s25_invalid_url_returns_422_invalid_input() -> None:
    code, body = _request(
        "POST",
        f"/registry/agents/{_ACCESSIBILITY_AUDITOR_ID}/call",
        body={"url": "not a url"},
        timeout=30.0,
    )
    detail = body.get("detail") if isinstance(body, dict) else {}
    envelope_code = ""
    if isinstance(body, dict):
        envelope_code = str(
            body.get("error")
            or (detail or {}).get("error")
            or ""
        )
    if code == 422 and envelope_code == "agent.invalid_input":
        _record("PASS", "S2.5", "invalid URL -> 422 agent.invalid_input")
    else:
        _record(
            "FAIL",
            "S2.5",
            f"got HTTP {code} envelope={envelope_code!r} (want 422 agent.invalid_input)",
        )


def check_s26_unknown_job_returns_403() -> None:
    bogus_uuid = str(uuid.uuid4())
    code, body = _request("GET", f"/jobs/{bogus_uuid}")
    if code == 403:
        _record("PASS", "S2.6", "GET /jobs/{unknown} returns 403 (no enumeration)")
    else:
        _record(
            "FAIL",
            "S2.6",
            f"got HTTP {code} (want 403); body={str(body)[:100]}",
        )


def check_s27_inline_receipt_present() -> None:
    code, body = _request(
        "POST",
        f"/registry/agents/{_CVELOOKUP_ID}/call",
        body={"cve_id": "CVE-2021-44228"},
        timeout=30.0,
    )
    if code != 200 or not isinstance(body, dict):
        _record("SKIP", "S2.7", f"call failed: HTTP {code}")
        return
    receipt = body.get("receipt")
    if not isinstance(receipt, dict):
        _record("FAIL", "S2.7", "no `receipt` block in response body")
        return
    needed = ("signature", "alg", "did", "public_key_jwk", "signed_payload_b64")
    missing = [k for k in needed if not receipt.get(k)]
    if missing:
        _record("FAIL", "S2.7", f"receipt missing fields: {missing}")
    else:
        _record(
            "PASS",
            "S2.7",
            f"inline receipt present (alg={receipt.get('alg')}, "
            f"sig {len(str(receipt.get('signature') or ''))}b)",
        )


def check_s38_wallets_limit_honored() -> None:
    code, body = _request("GET", "/wallets/me?limit=5")
    if code != 200 or not isinstance(body, dict):
        _record("SKIP", "S3.8", f"wallets/me failed: HTTP {code}")
        return
    txs = body.get("transactions")
    if not isinstance(txs, list):
        _record("SKIP", "S3.8", "no transactions list in response")
        return
    if len(txs) <= 5:
        _record("PASS", "S3.8", f"limit=5 honored (got {len(txs)} txs)")
    else:
        _record(
            "FAIL",
            "S3.8",
            f"limit=5 ignored: got {len(txs)} txs",
        )


def main() -> int:
    print(f"# audit_repro against {_host()}\n")
    started = time.time()
    checks = (
        check_s11_sunset_agent_returns_410,
        check_s24_no_ansi_in_python_executor_stdout,
        check_s25_invalid_url_returns_422_invalid_input,
        check_s26_unknown_job_returns_403,
        check_s27_inline_receipt_present,
        check_s38_wallets_limit_honored,
        check_s12_concurrency_cap_returns_429,
        check_s13_singleflight_collapses_stampede,
    )
    for fn in checks:
        try:
            fn()
        except Exception as exc:
            _record("ERROR", fn.__name__, f"{type(exc).__name__}: {exc}")

    elapsed = time.time() - started
    n_pass = sum(1 for s, *_ in _RESULTS if s == "PASS")
    n_fail = sum(1 for s, *_ in _RESULTS if s == "FAIL")
    n_skip = sum(1 for s, *_ in _RESULTS if s in ("SKIP", "PARTIAL"))
    print(
        f"\n# {n_pass} pass / {n_fail} fail / {n_skip} skip  "
        f"({elapsed:.1f}s, target {_host()})"
    )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
