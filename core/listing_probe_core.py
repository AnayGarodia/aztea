"""Pure endpoint-probe transport for listing verification.

# OWNS: the HTTP-issuing probe loop (one synthetic + adversarial suite, plus
#   output-example replay) and a single-probe helper. The HTTP client and the
#   body reader are INJECTED so this module stays import-clean of ``server/``
#   and of the ``requests`` session object.
# NOT OWNS: the registration-policy decisions (raise-on-block, require-at-least-
#   one-success) — those stay in the thin server wrapper
#   ``server.application_parts.part_003._run_listing_safety_probe``. The response
#   scanners live in ``core.listing_safety_probe``.
# INVARIANTS:
#   - Never raises on a transport error; a failed/timed-out/5xx POST is counted,
#     not propagated. Callers inspect ``ProbeSuiteResult`` and decide policy.
#   - ``core/`` must not import ``server/`` — this extraction (H6 of the
#     2026-06-03 publish-verification review) is the whole point: both the server
#     probe wrapper and ``core.listing_reliability`` call into here instead of one
#     importing the other.
# DECISIONS:
#   - Behaviour is byte-for-byte what ``_run_listing_safety_probe`` did before the
#     extraction (same nonce/UA rotation, same 5xx-counts-as-unreachable rule,
#     same 401/403-skip on example replay). Only the raise/return boundary moved.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from core import listing_safety as _ls

_LOG = logging.getLogger(__name__)

# Default per-probe timeout for core-side callers (the server wrapper passes its
# own ``_LISTING_SAFETY_PROBE_TIMEOUT``).
DEFAULT_PROBE_TIMEOUT = 3.0

# Hard cap on bytes read from a probe response — a malicious endpoint could
# otherwise stream a huge body and OOM the worker. Matches the server reader.
_PROBE_BODY_MAX_BYTES = 256 * 1024

# Injected-callable type aliases (documentation only).
HttpPost = Callable[..., Any]
ReadBody = Callable[[Any], Any]

# Default UA rotation pool. Server callers pass their own (kept identical); this
# default exists so ``core.listing_reliability`` and tests don't have to.
_DEFAULT_PROBE_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "aztea-registration-probe/1.0",
)

# 5xx is treated as "endpoint did not respond sanely" — same rule the server
# wrapper used. 4xx is a real response we still inspect.
_SERVER_ERROR_FLOOR = 500
_SERVER_ERROR_CEIL = 600

# Output-example replay caps to the first N declared examples to bound latency.
_MAX_EXAMPLE_REPLAYS = 3


@dataclass
class ProbeResponse:
    """One probe round-trip outcome. ``ok`` means a non-5xx response came back."""

    status: int
    body: Any
    headers: dict[str, str] | None
    ok: bool


@dataclass
class ProbeSuiteResult:
    """Aggregate of a full probe suite. Policy (raise / reject) is the caller's."""

    findings: list[_ls.VerificationFinding] = field(default_factory=list)
    successful_probes: int = 0
    payloads_attempted: int = 0


def new_probe_nonce() -> str:
    """Per-registration nonce (uuid4 hex). Short enough to keep payloads small."""
    return uuid.uuid4().hex


def read_probe_body(resp: Any) -> Any:
    """Read up to ``_PROBE_BODY_MAX_BYTES`` of a response, preferring JSON.

    Default body reader for core-side callers (reliability collector, tests). The
    server keeps its own equivalent. Returns dict/list for JSON, else a string.
    """
    try:
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192, decode_unicode=False):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= _PROBE_BODY_MAX_BYTES:
                break
        raw = b"".join(chunks)[:_PROBE_BODY_MAX_BYTES]
    except Exception:  # noqa: BLE001 — iter_content unsupported (e.g. mocks)
        try:
            raw = resp.text.encode("utf-8", errors="replace")[:_PROBE_BODY_MAX_BYTES]
        except Exception:  # noqa: BLE001
            return ""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""


def build_probe_headers(
    nonce: str, user_agents: tuple[str, ...] = _DEFAULT_PROBE_USER_AGENTS,
) -> dict[str, str]:
    chosen_ua = user_agents[
        int.from_bytes(nonce[:4].encode(), "big") % len(user_agents)
    ]
    return {
        "Content-Type": "application/json",
        "User-Agent": chosen_ua,
        "Authorization": f"Bearer aztea-probe-{nonce}",
        "X-Aztea-Probe": nonce,
    }


def probe_once(
    url: str,
    payload: dict[str, Any],
    *,
    http_post: HttpPost,
    read_body: ReadBody,
    timeout: float,
    headers: dict[str, str],
    job_id: str,
) -> ProbeResponse | None:
    """Issue one probe POST. Returns None on transport error (never raises).

    A 5xx comes back as ``ProbeResponse(ok=False)`` so the caller can count it
    as unreachable without re-reading the body.
    """
    envelope = {"job_id": job_id, "input_payload": dict(payload)}
    try:
        resp = http_post(
            url,
            json=envelope,
            timeout=timeout,
            allow_redirects=False,
            headers=headers,
            stream=True,
        )
    except Exception:  # noqa: BLE001 — transport error is non-fatal by contract
        _LOG.debug("listing probe POST failed (non-fatal)", exc_info=True)
        return None
    status = getattr(resp, "status_code", 0)
    if _SERVER_ERROR_FLOOR <= status < _SERVER_ERROR_CEIL:
        _LOG.debug("listing probe got 5xx (%s) — counting as unreachable", status)
        return ProbeResponse(status=status, body=None, headers=None, ok=False)
    body = read_body(resp)
    try:
        response_headers: dict[str, str] | None = {
            str(k): str(v) for k, v in dict(resp.headers).items()
        }
    except Exception:  # noqa: BLE001 — header shape varies across clients/mocks
        response_headers = None
    return ProbeResponse(status=status, body=body, headers=response_headers, ok=True)


def _build_payloads(input_schema: dict | None, nonce: str) -> tuple[list[dict], bool]:
    """Return (payloads, synthetic_present). Synthetic is always index 0 if present."""
    payloads: list[dict] = []
    synthetic = _ls.synthesize_input_from_schema(input_schema)
    has_synthetic = bool(synthetic)
    if has_synthetic:
        payloads.append(synthetic)
    payloads.extend(_ls.adversarial_probes(nonce=nonce))
    return payloads, has_synthetic


def _replay_output_examples(
    url: str,
    output_examples: list | None,
    *,
    http_post: HttpPost,
    read_body: ReadBody,
    timeout: float,
    headers: dict[str, str],
    nonce: str,
) -> list[_ls.VerificationFinding]:
    """Replay up to ``_MAX_EXAMPLE_REPLAYS`` declared examples; WARN on mismatch.

    A 401/403 is treated as the seller correctly rejecting an unsigned probe (the
    endpoint_signing_secret does not exist yet), not a contract failure.
    """
    findings: list[_ls.VerificationFinding] = []
    if not (isinstance(output_examples, list) and output_examples):
        return findings
    for example in output_examples[:_MAX_EXAMPLE_REPLAYS]:
        if not isinstance(example, dict):
            continue
        example_input = example.get("input")
        declared_output = example.get("output")
        if not isinstance(example_input, dict) or not isinstance(declared_output, dict):
            continue
        resp = probe_once(
            url, example_input, http_post=http_post, read_body=read_body,
            timeout=timeout, headers=headers, job_id=f"probe-example-{nonce}",
        )
        if resp is None or not resp.ok:
            continue
        if resp.status in (401, 403):
            continue
        findings.extend(
            _ls.evaluate_output_example_replay(
                example_input=example_input,
                declared_output=declared_output,
                actual_output=resp.body,
            )
        )
    return findings


def run_probe_suite(
    url: str,
    *,
    input_schema: dict | None,
    output_schema: dict | None,
    output_examples: list | None = None,
    http_post: HttpPost,
    read_body: ReadBody,
    timeout: float,
    user_agents: tuple[str, ...] = _DEFAULT_PROBE_USER_AGENTS,
    nonce: str | None = None,
    schema_validator: Callable[[Any, dict], list] | None = None,
) -> ProbeSuiteResult:
    """Run the synthetic+adversarial probe suite (plus example replay) over ``url``.

    Pure transport: returns findings + how many probes the endpoint answered. The
    caller decides whether a BLOCK finding or zero successes refuses the listing.

    ``schema_validator`` (injected, optional) is applied to the *synthetic* probe
    response only — it lets a caller add a real JSON-Schema BLOCK on a
    non-conforming response without coupling this module to ``jsonschema``.
    """
    nonce = nonce or new_probe_nonce()
    headers = build_probe_headers(nonce, user_agents)
    job_id = f"probe-{nonce}"
    payloads, has_synthetic = _build_payloads(input_schema, nonce)

    result = ProbeSuiteResult(payloads_attempted=len(payloads))
    for index, payload in enumerate(payloads):
        resp = probe_once(
            url, payload, http_post=http_post, read_body=read_body,
            timeout=timeout, headers=headers, job_id=job_id,
        )
        if resp is None or not resp.ok:
            continue
        result.successful_probes += 1
        is_synthetic = has_synthetic and index == 0
        result.findings.extend(
            _ls.evaluate_probe_response(
                resp.body,
                output_schema=output_schema if is_synthetic else None,
                response_headers=resp.headers,
            )
        )
        if is_synthetic and schema_validator is not None and output_schema:
            result.findings.extend(schema_validator(resp.body, output_schema))

    result.findings.extend(
        _replay_output_examples(
            url, output_examples, http_post=http_post, read_body=read_body,
            timeout=timeout, headers=headers, nonce=nonce,
        )
    )
    return result


__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "HttpPost",
    "ProbeResponse",
    "ProbeSuiteResult",
    "ReadBody",
    "build_probe_headers",
    "new_probe_nonce",
    "probe_once",
    "read_probe_body",
    "run_probe_suite",
]
