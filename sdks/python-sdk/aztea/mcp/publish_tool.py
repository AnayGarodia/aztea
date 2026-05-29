"""
publish_tool.py — the /publish_agent MCP tool, broken out of server.py to
keep that file within the line budget.

# OWNS: tool schema + dispatcher for `publish_agent` — the Wave 2
#       (2026-05-26) consumer-to-supplier conversion path that lets a
#       Claude Code user publish an agent from inside their chat session.
# NOT OWNS: the inference engine (core.publish_inference), the listing-
#       safety scanner (core.listing_safety), or the backend endpoints
#       (POST /registry/register / POST /onboarding/ingest). This module
#       composes those into one tool surface and converts every failure
#       into a structured error envelope.
# INVARIANTS:
#   - Multi-turn contract: missing required fields ⇒ return
#     {"error": {"code": "publish.missing_fields", "missing_fields": [...],
#                "suggestions": {...}}} — never raise.
#   - Safety contract: any `level="block"` finding from listing_safety ⇒
#     return {"error": {"code": "publish.safety_rejected", "findings": [...]}}
#   - Auth scope: existing `worker` (not a new scope per Wave 2 spec).
#   - Idempotency: the backend's `Idempotency-Key` header is honored; we
#     pass-through any `idempotency_key` argument as that header so a
#     re-call returns the cached response.

The MCP tool's "magical moment" (from the design doc lines 495-499):

    User in Claude Code:
      "Hey, I built a Stripe webhook validator. Can I sell that on Aztea?"
    Claude calls publish_agent(...):
      → inference → safety → backend POST → returns {agent_id, slug, ...}
    Claude:
      "Published. Your agent is live at aztea.ai/agents/<slug>. It's on
       probation until 5 successful calls. Want me to share the link?"
"""

from __future__ import annotations

from typing import Any

import requests

__all__ = ["PUBLISH_AGENT_TOOL", "dispatch_publish_agent"]


# Timeouts. Source fetch is shorter than the backend POST because a slow
# source URL almost always means the publisher pointed at a dead host;
# wait ~15s and fail loud. Backend POST is the normal HTTP_TIMEOUT (30s
# default) and is set by the dispatcher caller via `timeout=`.
_SOURCE_FETCH_TIMEOUT_SECONDS = 15.0


# ─── Tool schema ───────────────────────────────────────────────────────────


PUBLISH_AGENT_TOOL: dict[str, Any] = {
    "name": "publish_agent",
    "description": (
        "Publish a new agent to the Aztea marketplace from inside Claude Code. "
        "The platform infers most fields (name, slug, description, input/output "
        "schemas, category, tags) from the handler source so the caller only "
        "has to supply what's truly novel.\n\n"
        "**Two-turn contract.** First call with `source` and any fields you "
        "already know. If the inference engine can fill the rest, you get a "
        "live agent (probation, $0.05/call default). If REQUIRED fields are "
        "still missing after inference, the response is "
        "`error.code = 'publish.missing_fields'` with a `missing_fields` list "
        "and a `suggestions` map — ask the user, re-call with the answers.\n\n"
        "**Safety gate.** Every publish runs through the listing-safety scanner "
        "(prompt-injection, secrets, blocked imports, near-clone detection) "
        "before reaching the backend. A block-level finding returns "
        "`error.code = 'publish.safety_rejected'` with the structured `findings` "
        "list so the user can fix them.\n\n"
        "**Author-hosted vs platform-hosted.** Set `endpoint_url` if the "
        "publisher is running the handler at their own URL (agent.md / "
        "Python-handler-with-endpoint path). Omit `endpoint_url` for the "
        "AgentServer / hosted-execution path — currently returns "
        "`publish.endpoint_required` until Wave 3's hosted execution lands.\n\n"
        "Requires a `worker`-scope API key (the same scope `aztea publish` CLI "
        "uses). Slug-collision behavior, probation, and price-jump caps are "
        "handled by the backend — this tool is a thin well-typed proxy. "
        "Idempotent-publish (resending with the same key returns the prior "
        "response) is NOT yet supported server-side; retries currently create "
        "duplicate agent records."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": (
                    "Either a URL to the handler source (https://github.com/.../handler.py "
                    "raw URL, gist raw URL) OR the inline Python source itself. "
                    "URL fetches are SSRF-guarded by the platform; private IPs / "
                    "tunneling services (ngrok, etc.) are blocked."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Human-readable agent name. If omitted, inferred from the "
                    "handler filename / function name."
                ),
            },
            "slug": {
                "type": "string",
                "description": (
                    "URL slug for /agents/<slug>. Kebab-case. Auto-generated "
                    "from name if omitted."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "1-2 sentence value prop for the catalog. Inferred from "
                    "function/module docstring if omitted."
                ),
            },
            "category": {
                "type": "string",
                "description": (
                    "Curated category — e.g. 'security', 'data', 'web', 'auth', "
                    "'developer-tools'. Inferred from source keywords if omitted."
                ),
            },
            "price_per_call_usd": {
                "type": "number",
                "description": (
                    "Per-call price in USD. Defaults to $0.05 (matches "
                    "`aztea publish` CLI default). Probation listings cap at "
                    "2× price-jump per PATCH; approved at 5×."
                ),
                "minimum": 0,
            },
            "input_schema": {
                "type": "object",
                "description": (
                    "JSON Schema for the agent's input payload. Inferred from "
                    "the handler signature (type hints, Pydantic BaseModel, "
                    "TypedDict) if omitted."
                ),
                "additionalProperties": True,
            },
            "output_schema": {
                "type": "object",
                "description": (
                    "JSON Schema for the agent's output. Inferred from the "
                    "handler return annotation if omitted."
                ),
                "additionalProperties": True,
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Tags for catalog filtering. Inferred from description "
                    "keywords if omitted. Capped at 5."
                ),
            },
            "visibility": {
                "type": "string",
                "enum": ["public", "private"],
                "description": (
                    "`public` (default) lists in the marketplace; `private` "
                    "creates the agent record but doesn't surface it in search."
                ),
            },
            "endpoint_url": {
                "type": "string",
                "description": (
                    "Author-hosted endpoint URL. Omit for the AgentServer "
                    "(platform-hosted) path. Required until Wave 3 hosted "
                    "execution ships — handler-only publishes currently return "
                    "publish.endpoint_required."
                ),
            },
            "hint": {
                "type": "string",
                "description": (
                    "Natural-language hint passed to the inference engine to "
                    "nudge description / category when the source is sparse."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "RESERVED for future server-side idempotency support. "
                    "Currently forwarded as the `Idempotency-Key` header to "
                    "POST /registry/register, but that endpoint does NOT yet "
                    "honor the header — retries WILL create duplicates today. "
                    "The header is included now so once the backend wires it "
                    "(see docs/idempotency.md), existing callers' retries "
                    "stop double-creating without any code change."
                ),
            },
        },
        "required": ["source"],
    },
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
        # idempotentHint is False until the backend honors the header on
        # POST /registry/register. The schema's idempotency_key field is
        # forward-looking — see its description.
        "idempotentHint": False,
    },
}


# ─── Dispatcher ────────────────────────────────────────────────────────────


def dispatch_publish_agent(
    arguments: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    session: requests.Session,
    timeout: float,
) -> tuple[bool, dict[str, Any]]:
    """Dispatch a /publish_agent MCP call.

    Returns (ok, payload). On structured errors (missing fields, safety
    rejection, backend 4xx) returns ok=False with a `{"error": ...}` body.
    Never raises — the MCP transport layer surfaces failures via the
    payload, not via exceptions.
    """
    if not api_key:
        return False, _err(
            "auth.api_key_missing",
            "Set AZTEA_API_KEY before publishing. Generate a worker-scope "
            "key at https://aztea.ai/account/keys.",
        )

    source_raw = arguments.get("source")
    if not isinstance(source_raw, str) or not source_raw.strip():
        return False, _err(
            "publish.source_required",
            "`source` is required: either an https URL to handler source or "
            "the inline Python source string.",
        )

    # Step 1: resolve source → raw handler text + filename (if URL).
    handler_source, filename, source_err = _resolve_source(source_raw)
    if source_err is not None:
        return False, source_err
    if not handler_source.strip():
        return False, _err(
            "publish.empty_source",
            "Source resolved to empty content. Check the URL or the inline "
            "payload.",
        )

    # Step 2: run inference.
    inferred = _run_inference(
        handler_source,
        hint=str(arguments.get("hint") or "").strip() or None,
        filename=filename,
    )

    # Step 3: merge caller args (highest precedence) over inferred.
    merged = _merge_overrides(inferred, arguments)

    # Step 4: validate the must-have set after merge.
    missing = _final_missing_fields(merged, inferred)
    if missing:
        return False, _err(
            "publish.missing_fields",
            "Inference could not fill every required field. Re-call with the "
            "missing fields, or accept the suggestions inline.",
            extra={
                "missing_fields": missing,
                "suggestions": _suggestions_from_inferred(inferred, missing),
            },
        )

    # Step 5: enforce endpoint contract (Wave 3 ships hosted execution).
    endpoint_url = str(merged.get("endpoint_url") or "").strip()
    if not endpoint_url:
        return False, _err(
            "publish.endpoint_required",
            "An `endpoint_url` is required until hosted execution ships (Wave 3). "
            "Run your handler at a public URL — see "
            "https://github.com/AnayGarodia/aztea/blob/main/docs/agent-builder.md — "
            "and re-call with endpoint_url=<your URL>.",
        )

    # Step 6: listing-safety scan on the resolved source.
    safety_findings = _run_listing_safety(handler_source, endpoint_url)
    blockers = [f for f in safety_findings if str(f.get("level")) == "block"]
    if blockers:
        return False, _err(
            "publish.safety_rejected",
            "The handler source failed the listing-safety scanner. Fix the "
            "blocking findings and retry.",
            extra={"findings": safety_findings},
        )

    # Step 7: POST to /registry/register.
    return _post_to_register(
        merged,
        endpoint_url=endpoint_url,
        base_url=base_url,
        api_key=api_key,
        session=session,
        timeout=timeout,
        idempotency_key=str(arguments.get("idempotency_key") or "").strip() or None,
    )


# ─── Step 1: source resolution ─────────────────────────────────────────────


def _resolve_source(
    source: str,
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Return (handler_text, filename_or_None, error_envelope_or_None).

    If `source` starts with http(s)://, fetch it through the SSRF guard.
    Otherwise treat as inline source text. The filename, if a URL is
    fetched, is the last path segment — used by inference for naming.
    """
    src = source.strip()
    if src.lower().startswith(("http://", "https://")):
        validation = _validate_outbound_url(src)
        if validation is not None:
            return "", None, validation
        try:
            resp = requests.get(src, timeout=_SOURCE_FETCH_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            return "", None, _err(
                "publish.source_fetch_failed",
                f"Could not fetch source URL: {exc}",
            )
        if resp.status_code >= 400:
            return "", None, _err(
                "publish.source_fetch_failed",
                f"Source URL returned HTTP {resp.status_code}.",
            )
        # Filename = last path segment, used for naming inference.
        filename = src.rsplit("/", 1)[-1] or None
        return resp.text, filename, None
    return src, None, None


def _validate_outbound_url(url: str) -> dict[str, Any] | None:
    """Run the SSRF guard. Returns None on success, an error envelope on rejection.

    Lazy import so the MCP package stays importable when `core` isn't on
    the path (e.g. when the SDK is installed standalone via PyPI). When
    the guard is unavailable, we REFUSE to fetch the URL rather than
    silently proceed — the previous behavior would have allowed an
    attacker-controlled URL (e.g. http://169.254.169.254/) to hit the
    publisher's network from a sandboxed MCP session. /review caught
    this 2026-05-27. The user gets a clear error pointing at the
    workaround (use inline source instead) rather than a silent SSRF.
    """
    try:
        from core.url_security import validate_outbound_url
    except ImportError:
        return _err(
            "publish.url_fetch_unavailable",
            "The SSRF guard (core.url_security) is not importable in this "
            "install, so URL-source publishing is disabled to prevent "
            "unguarded outbound fetches. Pass the handler source inline "
            "in the `source` field (paste the Python text) instead of a "
            "URL, or install the full Aztea checkout (pip install -e .) "
            "where `core` is on the path.",
        )
    try:
        validate_outbound_url(url)
    except Exception as exc:  # broad: the guard raises a family of types
        return _err(
            "publish.source_url_rejected",
            f"Source URL rejected by the SSRF guard: {exc}",
        )
    return None


# ─── Step 2: inference ─────────────────────────────────────────────────────


def _run_inference(
    handler_source: str, *, hint: str | None, filename: str | None,
) -> dict[str, Any]:
    """Run core.publish_inference.infer() and return the spec as a dict.

    Lazy import so this module stays importable when `core` is unavailable.
    """
    try:
        from core.publish_inference import infer
    except ImportError:
        # No inference available — return an empty spec so the caller falls
        # through to "missing_fields" for everything.
        return {
            "name": "Untitled Agent",
            "slug": "untitled-agent",
            "description": "",
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {"type": "object"},
            "price_per_call_usd": 0.05,
            "category": "developer-tools",
            "tags": [],
            "missing": ["name", "description", "input_schema", "output_schema"],
        }
    spec = infer(handler_source, hint=hint, filename=filename)
    return spec.to_jsonable()


# ─── Step 3: merge overrides ───────────────────────────────────────────────


# Fields the caller may override. Order matters for the merged dict's
# stable iteration; matches the order in PUBLISH_AGENT_TOOL.input_schema.
_OVERRIDABLE_FIELDS: tuple[str, ...] = (
    "name", "slug", "description", "category", "price_per_call_usd",
    "input_schema", "output_schema", "tags",
)


def _merge_overrides(
    inferred: dict[str, Any], arguments: dict[str, Any],
) -> dict[str, Any]:
    """Caller-supplied args win, inferred values fill the gaps."""
    merged: dict[str, Any] = dict(inferred)  # shallow copy
    for field in _OVERRIDABLE_FIELDS:
        if field in arguments and arguments[field] not in (None, "", [], {}):
            merged[field] = arguments[field]
    # endpoint_url + visibility are NOT inferred fields, but must come along.
    if "endpoint_url" in arguments and arguments["endpoint_url"]:
        merged["endpoint_url"] = arguments["endpoint_url"]
    merged["visibility"] = arguments.get("visibility") or "public"
    return merged


# ─── Step 4: final-missing check ───────────────────────────────────────────


# Fields the backend REQUIRES (cannot be merged from defaults).
_BACKEND_REQUIRED_FIELDS: tuple[str, ...] = (
    "name", "description", "input_schema", "output_schema",
    "price_per_call_usd",
)


def _final_missing_fields(
    merged: dict[str, Any], inferred: dict[str, Any],
) -> list[str]:
    """Return required fields that are still empty after merge.

    `Untitled Agent` from the fallback path counts as missing — the user
    almost certainly does not want to ship an agent named that.
    """
    missing: list[str] = []
    for field in _BACKEND_REQUIRED_FIELDS:
        val = merged.get(field)
        if val is None or val == "" or val == [] or val == {}:
            missing.append(field)
        elif field == "name" and val == "Untitled Agent":
            missing.append("name")
        elif field == "input_schema" and (
            isinstance(val, dict) and val.get("properties") == {}
        ):
            missing.append("input_schema")
    return missing


def _suggestions_from_inferred(
    inferred: dict[str, Any], missing: list[str],
) -> dict[str, Any]:
    """Return the inferred values for the missing fields so Claude can show
    the user what the engine thinks the answer is and ask for confirmation
    or override in one turn."""
    return {field: inferred.get(field) for field in missing}


# ─── Step 6: listing safety ────────────────────────────────────────────────


def _run_listing_safety(
    handler_source: str, endpoint_url: str,
) -> list[dict[str, Any]]:
    """Run the listing-safety scanner. Returns a list of finding dicts.

    Lazy import — if `core.listing_safety` is unavailable (standalone SDK
    install), return an empty list. This is the SAME contract the CLI uses:
    the publish flow doesn't block on a missing scanner because the backend
    re-runs it on /registry/register.
    """
    try:
        from core.listing_safety import (
            scan_agent_md_endpoint,
            scan_python_handler,
        )
    except ImportError:
        return []
    findings: list[Any] = []
    try:
        findings.extend(scan_python_handler(handler_source))
    except Exception:
        # Scanner crashed — better to surface zero findings than to block
        # the publish. Backend will re-scan.
        pass
    if endpoint_url:
        try:
            findings.extend(scan_agent_md_endpoint(endpoint_url))
        except Exception:
            # Same reasoning as the python_handler scan above — endpoint
            # scanner failures should not block publish; the backend
            # re-runs the safety probe authoritatively.
            pass
    out: list[dict[str, Any]] = []
    for f in findings:
        if isinstance(f, dict):
            out.append(f)
            continue
        # VerificationFinding dataclass — flatten.
        out.append({
            "code": getattr(f, "code", "unknown"),
            "level": getattr(f, "level", "warn"),
            "message": getattr(f, "message", str(f)),
            "detail": getattr(f, "detail", {}) or {},
        })
    return out


# ─── Step 7: backend POST ──────────────────────────────────────────────────


def _post_to_register(
    merged: dict[str, Any],
    *,
    endpoint_url: str,
    base_url: str,
    api_key: str,
    session: requests.Session,
    timeout: float,
    idempotency_key: str | None,
) -> tuple[bool, dict[str, Any]]:
    body: dict[str, Any] = {
        "name": merged["name"],
        "description": merged["description"],
        "endpoint_url": endpoint_url,
        "price_per_call_usd": float(merged["price_per_call_usd"]),
        "tags": list(merged.get("tags") or []),
        "input_schema": merged.get("input_schema") or {},
        "output_schema": merged.get("output_schema") or {},
    }
    if merged.get("category"):
        body["category"] = merged["category"]
    if merged.get("slug"):
        body["slug"] = merged["slug"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        resp = session.post(
            f"{base_url.rstrip('/')}/registry/register",
            headers=headers,
            json=body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, _err(
            "publish.backend_unreachable",
            f"Could not reach the backend: {exc}",
        )
    try:
        payload = resp.json()
    except ValueError:
        return False, _err(
            "publish.bad_backend_response",
            f"Backend returned HTTP {resp.status_code} with non-JSON body.",
            extra={"http_status": resp.status_code, "body": resp.text[:500]},
        )
    ok = 200 <= resp.status_code < 300
    if not ok:
        # Pass through the backend's error envelope when possible.
        if isinstance(payload, dict) and "error" in payload:
            return False, payload
        return False, _err(
            "publish.backend_error",
            f"Backend returned HTTP {resp.status_code}.",
            extra={"http_status": resp.status_code, "body": payload},
        )
    return True, payload


# ─── Error envelope helper ─────────────────────────────────────────────────


def _err(
    code: str, message: str, *, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the structured `{"error": {...}}` envelope every failure path returns."""
    body: dict[str, Any] = {"code": code, "message": message}
    if extra:
        body.update(extra)
    return {"error": body}
