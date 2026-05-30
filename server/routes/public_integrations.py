"""Anonymous, IP-rate-limited tool-manifest endpoints for external frameworks.

OWNS: ``GET /api/integrations/openai-tools.json`` and
      ``GET /api/integrations/gemini-tools.json`` — read-only manifests
      that an OpenAI Agents SDK / Codex / Gemini Tools integrator can wire
      up *without an Aztea API key*. They are deliberately minimal: the
      tool name + description + JSON-Schema parameters are enough to wire
      a function-calling client; pricing and privacy hints are passed
      through for the LLM to use during selection.

NOT OWNS: the authenticated ``/openai/tools`` / ``/openai/responses-tools`` /
      ``/codex/tools`` / ``/gemini/tools`` endpoints (they live in
      ``server/application_parts/part_007.py`` and emit per-caller signal).
      Manifest construction (``core/tool_adapters.py``). Caching
      (``core/integrations_cache.py``).

INVARIANTS:
  - These routes MUST NOT require auth. The whole point is anonymous discovery.
  - They MUST NOT share the private ``_agents_list_cache`` (see
    ``core/integrations_cache.py`` docstring for the leak risk reasoning).
  - The response must NEVER carry ``owner_id``, ``review_status``, or any
    ``by_client``/``trust_score_by_client`` map. ``scrub_agents_for_public``
    in ``core/tool_adapters.py`` is the defense-in-depth gate.
  - ``Cache-Control: public, max-age=60`` + ``ETag`` MUST be emitted, and
    ``If-None-Match`` MUST honor the conditional GET (returning 304).
  - Only the schema version in ``PUBLIC_MANIFEST_SCHEMA_VERSION`` is
    accepted via ``?version=YYYY-MM-DD``; unknown values return 400 with
    a structured ``error_codes.integrations.unknown_schema_version``.

DECISIONS:
  - Only chat-completions + gemini are exposed in v0. responses-tools /
    codex variants stay private until an integrator asks. Adding a third
    public format is a one-line dispatch entry below.
  - Rate-limit is 60/minute per IP — matches ``/registry/agents``. slowapi
    is injected via factory so this module avoids the part_001 cycle.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from core import error_codes, integrations_cache, tool_adapters
from core.tool_adapters import PUBLIC_MANIFEST_SCHEMA_VERSION


_PUBLIC_RATE_LIMIT = "60/minute"


def _validate_version(version: str | None) -> None:
    """Raise 400 if the caller pinned an unrecognised schema version.

    None means "I'll take whatever you serve" — the latest. Pinning the
    current version returns it explicitly. Pinning any other date is an
    integrator bug we'd rather surface than silently coerce.
    """
    if version is None:
        return
    if version == PUBLIC_MANIFEST_SCHEMA_VERSION:
        return
    raise HTTPException(
        status_code=400,
        detail=error_codes.make_error(
            "integrations.unknown_schema_version",
            f"Unknown manifest schema version {version!r}.",
            details={
                "supplied_version": version,
                "supported_versions": [PUBLIC_MANIFEST_SCHEMA_VERSION],
            },
        ),
    )


def _manifest_response(
    request: Request,
    manifest: dict[str, Any],
    etag: str,
) -> Response:
    """Emit the manifest body with ETag + max-age=60. Returns 304 on If-None-Match hit.

    ETags are returned quoted-string per RFC 7232; ``If-None-Match`` is
    matched verbatim (no W/ weak-validator support — the body is fully
    deterministic so weak validation buys nothing).
    """
    if_none_match = request.headers.get("if-none-match") or ""
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=60",
        "X-Aztea-Schema-Version": PUBLIC_MANIFEST_SCHEMA_VERSION,
    }
    if if_none_match.strip() == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=manifest, headers=headers)


def _build_public_manifest(
    active_agents_fn: Callable[[], list[dict[str, Any]]],
    builder: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    scrubbed = tool_adapters.scrub_agents_for_public(active_agents_fn())
    return builder(scrubbed, audience="public")


def create_router(
    *,
    limiter: Any,
    active_agents_fn: Callable[[], list[dict[str, Any]]],
) -> APIRouter:
    """Build the public integrations router.

    Why factory: ``limiter`` and ``active_agents_fn`` live in the sharded
    application namespace; importing them at module-load time would create
    a cycle. ``limiter`` is the slowapi ``Limiter`` instance from
    ``part_001.py`` (IP-keyed for anonymous callers). ``active_agents_fn``
    is ``_mcp_active_agents`` from ``part_007.py`` — the same source the
    authenticated tools endpoints use, so the catalog stays in lockstep.
    """
    router = APIRouter()

    # WHY paths lack /api: the FastAPI app sits behind an ``api_prefix_compat``
    # middleware (part_001.py) that transparently strips a leading ``/api``
    # from every request. Registering the canonical path here without the
    # prefix means BOTH ``/integrations/openai-tools.json`` AND
    # ``/api/integrations/openai-tools.json`` reach the handler — matching
    # the pattern used by ``server/routes/admin_usage.py``.
    @router.get(
        "/integrations/openai-tools.json",
        tags=["Integrations"],
        summary=(
            "Public, anonymous OpenAI Chat Completions tool manifest for "
            "Aztea agents. IP rate-limited; no API key required."
        ),
    )
    @limiter.limit(_PUBLIC_RATE_LIMIT)
    def public_openai_tools(
        request: Request,
        version: str | None = Query(None),
    ) -> Response:
        _validate_version(version)
        manifest, etag = integrations_cache.get_public_manifest(
            "openai_chat",
            lambda: _build_public_manifest(active_agents_fn, tool_adapters.build_openai_chat_manifest),
        )
        return _manifest_response(request, manifest, etag)

    @router.get(
        "/integrations/gemini-tools.json",
        tags=["Integrations"],
        summary=(
            "Public, anonymous Gemini functionDeclarations manifest for "
            "Aztea agents. IP rate-limited; no API key required."
        ),
    )
    @limiter.limit(_PUBLIC_RATE_LIMIT)
    def public_gemini_tools(
        request: Request,
        version: str | None = Query(None),
    ) -> Response:
        _validate_version(version)
        manifest, etag = integrations_cache.get_public_manifest(
            "gemini",
            lambda: _build_public_manifest(active_agents_fn, tool_adapters.build_gemini_manifest),
        )
        return _manifest_response(request, manifest, etag)

    return router
