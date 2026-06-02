"""
site_navigator.py — Goal-directed web navigation over the accessibility tree.

Renders a URL with headless Chromium, reads the *accessibility tree* (role /
name / value) instead of a full-page screenshot or raw DOM, and uses an LLM to
resolve the caller's plain-English ``goal`` into structured data. Returns the
data plus a reusable affordance map of the page (the "site map" that Phase 1's
shared commons will sign and reuse).

Why the accessibility tree and not a screenshot: a clean a11y snapshot is
~4k tokens where the equivalent screenshot is ~50k, so goal resolution is far
cheaper and faster than raw browser control. See docs/runbooks for the wedge.

Cheapest-path-first (both no-browser paths are flag-gated, default off): a signed
API-spec replay (direct HTTP, no browser) → an HTTP-first static fetch (no browser
for SSR/static pages) → the full Chromium render. With both flags off the agent is
exactly the Chromium path, output-equivalent to before.

Input:
  {
    "url": "https://example.com",   # required, SSRF-checked
    "goal": "list the pricing tiers and what each includes",  # required for 'structured'
    "formats": ["structured"],      # any of structured|markdown|html|links (default structured)
    "wait_for": "networkidle",      # CSS selector or "networkidle" (default)
    "wait_ms": 1500,                # extra settle wait, max 6000 ms
    "force_refresh": false          # skip the API-spec replay; force a fresh render
  }

Output:
  {
    "url": str, "requested_url": str, "goal": str,
    "result": Any | None,           # structured answer to the goal (None if degraded/no goal)
    "site_map": {...},              # reusable affordance map + dom_fingerprint
    "markdown"/"html"/"links": ...,  # present only when that format was requested
    "source": "fresh|http_first|api_spec",   # which path served the call
    "reuse": {"reused": bool, "source": str},
    "modality_used": "accessibility_tree|http_first|api_spec",
    "cost_class": "cheap|expensive",  # cheap = a no-browser path served it
    "execution_time_ms": int,
    "llm_used": bool, "degraded_mode": bool,
    "error": {...}                  # only on failure
  }
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from typing import Any

import jsonschema

from agents import _html_extract, _site_fetch
from agents._contracts import agent_error as _err
from agents._contracts import llm_complete, parse_json_payload
from core import feature_flags, url_security
from core.site_maps import api_discovery as _api_discovery
from core.site_maps import authoring as _commons
from core.site_maps import freshness as _commons_freshness
from core.site_maps import graph as _commons_graph
from core.site_maps import normalize as _commons_normalize
from core.site_maps import store as _commons_store

_LOG = logging.getLogger(__name__)

# Sync gateway budget is 8 s; an LLM resolve usually pushes a real navigation
# past that, so callers are steered to the async path (hire_async / POST /jobs)
# which honors the 20-minute browser budget. The waits below keep the *render*
# bounded so we fail fast rather than hang the worker.
_NAV_TIMEOUT_MS = 15_000
_DEFAULT_WAIT_MS = 1_500
_MAX_WAIT_MS = 6_000

# Token-budget caps for the trimmed accessibility tree. ~400 nodes keeps the
# resolve prompt near the ~4k-token sweet spot that makes this cheaper than
# screenshot reasoning; names are truncated so one giant label can't blow it.
_AX_NODE_CAP = 400
_AX_NAME_TRUNCATE = 160
# Cap lines scanned from the aria snapshot, independent of how many rows we keep,
# so a hostile page with a giant accessibility tree can't burn unbounded work.
_AX_MAX_VISITED = 20_000
# Per-affordance-category cap: enough to map a dense nav/menu without letting a
# link-farm page balloon the site_map; the LLM rarely needs more than this to act.
_AFFORDANCE_CAP = 60
_RESULT_MAX_TOKENS = 1_200

_MODALITY_AX = "accessibility_tree"
_USER_AGENT = "Aztea-Site-Navigator/1.0 (headless; for authorized navigation)"

# This agent's own identity, mirrored from server/builtin_agents/constants.py
# (agents must not import server; the uuid5 is deterministic and stable). Used to
# sign + deposit maps into the shared commons under the agent's did:web key.
_SELF_AGENT_ID = "7b9e59b1-fba2-583c-b53b-86a710a888a5"
_SELF_OWNER_ID = "system:builtin-worker"

# Accessibility roles we surface as navigable affordances. Grouped so the
# site map reads like "here is how to act on this page" rather than a flat dump.
_LINK_ROLES = frozenset({"link"})
_BUTTON_ROLES = frozenset({"button"})
_INPUT_ROLES = frozenset({"textbox", "searchbox", "combobox"})
_HEADING_ROLES = frozenset({"heading"})

# A line of Playwright's aria_snapshot(): '- heading "Example" [level=1]' or
# '- link "Home":'. Captures (role, optional quoted name). Lines like '- /url: ...'
# or '- paragraph: prose' have no quoted name and are dropped as non-affordances.
_ARIA_LINE_RE = re.compile(r'^-\s+([a-z]+)(?:\s+"([^"]*)")?')

_SYSTEM_PROMPT = (
    "You extract structured data from a web page for an automated agent. You "
    "are given the page URL and its accessibility tree (roles, names, values) "
    "plus the page's navigable affordances. Answer the caller's GOAL using "
    "ONLY the provided page data. Return a single JSON object (no prose, no "
    "markdown fences) whose keys directly answer the goal. If the page does "
    "not contain the answer, return {\"found\": false, \"reason\": \"...\"}. "
    "Never invent data that is not present in the accessibility tree."
)

# Output formats a caller can request (Firecrawl parity). "structured" is the
# original goal-directed JSON extraction; markdown/html/links are derived from the
# page HTML and need no goal. Default keeps the original single-format behavior.
_VALID_FORMATS = ("structured", "markdown", "html", "links")
_DEFAULT_FORMATS: tuple[str, ...] = ("structured",)
_MARKDOWN_MAX_CHARS = 200_000
_JSON_RESOLVE_MAX_CHARS = 24_000


@dataclasses.dataclass(frozen=True)
class _RenderOutcome:
    """What a Chromium render produced: a11y rows, the rendered HTML (for markdown),
    and any captured JSON XHRs (for API discovery — empty when discovery is off)."""

    title: str
    rows: list[dict[str, str]]
    final_url: str
    html: str
    captures: list[dict[str, Any]]


def _parse_formats(payload: dict[str, Any]) -> tuple[str, ...]:
    """Pure: validated, de-duplicated output formats; defaults to ('structured',)."""
    raw = payload.get("formats")
    if not isinstance(raw, (list, tuple)) or not raw:
        return _DEFAULT_FORMATS
    seen: list[str] = []
    for fmt in raw:
        normalized = str(fmt or "").strip().lower()
        if normalized in _VALID_FORMATS and normalized not in seen:
            seen.append(normalized)
    return tuple(seen) or _DEFAULT_FORMATS


def _normalize_run_inputs(payload: dict[str, Any]) -> dict | tuple[Any, ...]:
    """Pure: validate ``payload`` at the boundary; returns parsed bag or error envelope.

    Why (rule 4): fail loudly here so a missing url/goal never reaches Playwright.
    ``goal`` is required only when 'structured' extraction is requested (the default);
    a pure markdown/html/links scrape needs no goal.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("site_navigator.missing_url", "url is required.")
    try:
        url = url_security.validate_outbound_url(raw_url, "url")
    except Exception as exc:  # noqa: BLE001 — surfaced as a structured envelope
        return _err("site_navigator.url_blocked", str(exc))
    formats = _parse_formats(payload)
    goal = str(payload.get("goal") or "").strip()
    if "structured" in formats and not goal:
        return _err("site_navigator.missing_goal", "goal is required for structured extraction.")
    schema = payload.get("schema")
    if schema is not None:
        if not isinstance(schema, dict):
            return _err("site_navigator.invalid_schema", "schema must be a JSON Schema object.")
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.exceptions.SchemaError as exc:
            return _err("site_navigator.invalid_schema", f"schema is not a valid JSON Schema: {exc.message}")
    wait_for = str(payload.get("wait_for") or "networkidle").strip() or "networkidle"
    try:
        wait_ms = min(int(payload.get("wait_ms") or _DEFAULT_WAIT_MS), _MAX_WAIT_MS)
    except (TypeError, ValueError):
        wait_ms = _DEFAULT_WAIT_MS
    force_refresh = bool(payload.get("force_refresh"))
    return (url, goal, formats, schema, wait_for, wait_ms, force_refresh)


def _import_playwright() -> Any:
    """Side-effect: lazy Playwright import (rule 11 exception — heavy, not on every worker)."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
        return sync_playwright
    except ImportError:
        return _err(
            "site_navigator.tool_unavailable",
            "playwright is not installed on this executor. Install it with: "
            "pip install playwright && playwright install chromium",
        )


def _is_chromium_missing(exc: BaseException) -> bool:
    """Pure: detect the 'chromium not installed' signal from a launch failure."""
    msg = str(exc)
    return "Executable doesn't exist" in msg or "playwright install" in msg


def _install_request_guard(context: Any) -> None:  # noqa: ANN401
    """Side-effect: abort in-page requests that pivot to blocked/private targets.

    Replicated from browser_agent rather than imported: the SSRF guard is the
    one security-critical line and each browser agent owns its own so a refactor
    of one can't silently weaken another.
    """

    def _guard(route: Any) -> None:  # noqa: ANN401
        try:
            url_security.validate_outbound_url(route.request.url, "url")
        except Exception:  # noqa: BLE001 — any validation failure means block
            route.abort()
            return
        route.continue_()

    context.route("**/*", _guard)


def _parse_aria_snapshot(text: str) -> list[dict[str, str]]:
    """Pure: parse Playwright aria_snapshot() text into capped {role, name} rows.

    aria_snapshot() returns a compact YAML-ish tree ('- heading "X" [level=1]',
    '- link "Y":'). We keep role-bearing affordances; prose lines ('- paragraph:
    text') and url lines ('- /url: ...') carry no quoted name and are dropped.
    Bounded by _AX_NODE_CAP rows kept and _AX_MAX_VISITED lines scanned so a
    hostile page with a giant tree can't burn unbounded work. Unnamed inputs
    (textbox/searchbox/combobox) are kept — they are still actionable.
    """
    rows: list[dict[str, str]] = []
    visited = 0
    for line in str(text or "").splitlines():
        if visited >= _AX_MAX_VISITED or len(rows) >= _AX_NODE_CAP:
            break
        visited += 1
        match = _ARIA_LINE_RE.match(line.strip())
        if not match:
            continue
        role = (match.group(1) or "").strip()
        name = (match.group(2) or "").strip()[:_AX_NAME_TRUNCATE]
        if role and (name or role in _INPUT_ROLES):
            rows.append({"role": role, "name": name})
    return rows


def _extract_affordances(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    """Pure: group flattened a11y rows into the navigable-affordance map."""
    links: list[str] = []
    buttons: list[str] = []
    inputs: list[str] = []
    headings: list[str] = []
    for row in rows:
        role = row.get("role", "")
        name = row.get("name", "")
        if role in _LINK_ROLES and name and len(links) < _AFFORDANCE_CAP:
            links.append(name)
        elif role in _BUTTON_ROLES and name and len(buttons) < _AFFORDANCE_CAP:
            buttons.append(name)
        elif role in _INPUT_ROLES and len(inputs) < _AFFORDANCE_CAP:
            inputs.append(name or f"<{role}>")
        elif role in _HEADING_ROLES and name and len(headings) < _AFFORDANCE_CAP:
            headings.append(name)
    return {"links": links, "buttons": buttons, "inputs": inputs, "headings": headings}


def _dom_fingerprint(normalized_url: str, rows: list[dict[str, str]]) -> str:
    """Structural (value-stripped) fingerprint for Phase 1 freshness checks.

    Delegates to the commons so authoring (here) and reuse-validation (the
    Phase 1 commons) hash identically. Value-stripped (roles only) so it drifts
    on structure and carries no page content / PII.
    """
    return _commons_normalize.dom_fingerprint(normalized_url, [r.get("role", "") for r in rows])


def _build_site_map(
    *, final_url: str, title: str, rows: list[dict[str, str]],
    affordances: dict[str, list[str]], normalized_url: str,
) -> dict[str, Any]:
    """Pure: assemble the reusable site map artifact (Phase 1 will sign + share this)."""
    return {
        "final_url": final_url,
        "title": title,
        "affordances": affordances,
        # Phase 3: a light navigation graph (sections by heading + entry links)
        # so a planner can reason about where to go without re-reading the tree.
        "graph": _commons_graph.build_navigation_graph(rows),
        "node_count": len(rows),
        "dom_fingerprint": _dom_fingerprint(normalized_url, rows),
        "schema": "aztea/site-map/2",
    }


def _parse_or_unstructured(text: str) -> Any:
    """Pure: parse JSON, or tag non-JSON prose so a caller never blindly indexes it."""
    try:
        return parse_json_payload(text)
    except ValueError:
        return {"_unstructured": True, "text": text}


def _is_structured(obj: Any) -> bool:
    """Pure: a real JSON object/array (not the _unstructured-prose envelope)."""
    if isinstance(obj, dict) and obj.get("_unstructured"):
        return False
    return isinstance(obj, (dict, list))


def _schema_errors(obj: Any, schema: dict[str, Any]) -> tuple[bool, str]:
    """Pure: (True,'') if obj validates against schema, else (False, first error)."""
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
    return (True, "") if not errors else (False, errors[0].message)


def _resolve_with_optional_schema(user: str, schema: dict[str, Any] | None) -> tuple[Any, bool]:
    """Side-effect (LLM): resolve ``user`` to JSON, optionally enforcing ``schema``.

    Degrades to (None, False) with no provider (caller keeps the retrieved data). With a
    schema (the /extract path), the output is validated; on a mismatch we retry once with
    the error fed back, and if it still fails we return a typed _extraction_failed marker
    rather than silently handing back non-conforming data.
    """
    text = llm_complete(_SYSTEM_PROMPT, user, max_tokens=_RESULT_MAX_TOKENS, agent_slug="site_navigator")
    if text is None:
        return None, False
    obj = _parse_or_unstructured(text)
    if schema is None or not _is_structured(obj):
        return obj, True
    ok, err = _schema_errors(obj, schema)
    if ok:
        return obj, True
    retry = llm_complete(
        _SYSTEM_PROMPT,
        f"{user}\n\nYour previous answer did NOT match the required JSON schema "
        f"(error: {err}). Return ONLY JSON conforming to this schema:\n"
        f"{json.dumps(schema, ensure_ascii=False)[:_JSON_RESOLVE_MAX_CHARS]}",
        max_tokens=_RESULT_MAX_TOKENS, agent_slug="site_navigator",
    )
    if retry is not None:
        retry_obj = _parse_or_unstructured(retry)
        if _is_structured(retry_obj) and _schema_errors(retry_obj, schema)[0]:
            return retry_obj, True
    return {"_extraction_failed": True, "reason": err, "raw": obj}, True


def _resolve_goal(
    goal: str, url: str, rows: list[dict[str, str]], affordances: dict[str, list[str]],
    schema: dict[str, Any] | None = None,
) -> tuple[Any, bool]:
    """Side-effect (LLM): resolve ``goal`` against the a11y data. Returns (result, llm_used).

    Graceful degradation: no provider → (None, False) so the caller still gets the
    retrieved site map. ``schema`` (the /extract path) enforces a JSON Schema on the output.
    """
    user = (
        f"URL: {url}\nGOAL: {goal}\n\n"
        f"ACCESSIBILITY_TREE (trimmed):\n{json.dumps(rows, ensure_ascii=False)}\n\n"
        f"AFFORDANCES:\n{json.dumps(affordances, ensure_ascii=False)}"
    )
    return _resolve_with_optional_schema(user, schema)


def _capture_ax(page: Any) -> tuple[str, list[dict[str, str]]]:
    """Side-effect: read title + the accessibility tree (via aria_snapshot) off the page.

    Why aria_snapshot and not page.accessibility: current Playwright removed the
    Accessibility class; aria_snapshot() is the supported way to get a compact
    role/name view of the page.
    """
    title = page.title() or ""
    try:
        aria = page.locator("body").aria_snapshot()
    except Exception:  # noqa: BLE001 — empty AX on failure degrades gracefully
        _LOG.debug("aria_snapshot failed", exc_info=True)
        aria = ""
    return title, _parse_aria_snapshot(aria)


def _safe_page_content(page: Any) -> str:
    """Side-effect: page.content() for the markdown/html formats; '' on failure."""
    try:
        return page.content() or ""
    except Exception:  # noqa: BLE001 — markdown is optional; degrade to empty HTML
        _LOG.debug("page.content() failed", exc_info=True)
        return ""


def _drive_chromium(
    pw: Any, url: str, *, wait_for: str, wait_ms: int, captures: list[dict[str, Any]] | None,
) -> dict | _RenderOutcome:
    """Side-effect: launch Chromium, navigate, capture a11y + HTML (+ JSON XHRs when a
    capture sink is provided). Returns a _RenderOutcome or an error envelope.

    ``captures`` is an optional sink (not a boolean flag): pass a list to record JSON
    XHR/fetch traffic for API discovery, or None to skip the listener overhead.
    """
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as launch_exc:  # noqa: BLE001
        if _is_chromium_missing(launch_exc):
            return _err(
                "site_navigator.tool_unavailable",
                "Headless Chromium is not provisioned on this worker. Run "
                "`playwright install chromium` on the executor. The call was not billed.",
            )
        raise
    context = browser.new_context(user_agent=_USER_AGENT)
    _install_request_guard(context)
    page = context.new_page()
    if captures is not None:
        _api_discovery.attach_xhr_capture(page, captures)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        if wait_for == "networkidle":
            page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT_MS)
        else:
            page.wait_for_selector(wait_for, timeout=_NAV_TIMEOUT_MS)
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
        title, rows = _capture_ax(page)
        return _RenderOutcome(
            title=title, rows=rows, final_url=page.url,
            html=_safe_page_content(page), captures=captures or [],
        )
    finally:
        context.close()
        browser.close()


def _attach_formats(result: dict[str, Any], formats: tuple[str, ...], html: str | None) -> None:
    """Side-effect: attach markdown/html/links outputs derived from the page HTML.

    Structured-only callers (the default) get nothing extra. Best-effort: a
    conversion failure logs and is skipped rather than breaking the result.
    """
    if not html or not any(f in formats for f in ("markdown", "html", "links")):
        return
    try:
        if "markdown" in formats:
            result["markdown"] = _html_extract.to_markdown(html)[:_MARKDOWN_MAX_CHARS]
        if "html" in formats:
            result["html"] = html[:_MARKDOWN_MAX_CHARS]
        if "links" in formats:
            result["links"] = _html_extract.extract_links(html)
    except Exception:  # noqa: BLE001 — formats are additive; never break the result
        _LOG.warning("format derivation failed", exc_info=True)


def _build_result(
    *, url: str, requested_url: str, goal: str, title: str,
    rows: list[dict[str, str]], elapsed_ms: int, prior_map: dict[str, Any] | None = None,
    source: str = "fresh", modality: str = _MODALITY_AX, cost_class: str = "expensive",
    formats: tuple[str, ...] = _DEFAULT_FORMATS, html: str | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure-ish: resolve the goal and shape the response (LLM call inside _resolve_goal).

    ``source``/``modality``/``cost_class`` describe which path served the call; the
    defaults reproduce the original Chromium-path output. Non-structured formats
    (markdown/html/links) are derived from ``html`` when requested.
    """
    affordances = _extract_affordances(rows)
    site_map = _build_site_map(
        final_url=url, title=title, rows=rows, affordances=affordances,
        normalized_url=_commons_normalize.normalize_site_key(url),
    )
    result, llm_used = (
        _resolve_goal(goal, url, rows, affordances, schema)
        if "structured" in formats and goal else (None, False)
    )
    # prior_map surfaces that the shared commons already holds a signed map for this
    # site (network-effect coverage, visible in-band). reused is True only on the
    # API-spec replay path, where we genuinely skipped a render.
    reuse: dict[str, Any] = {
        "reused": source == "api_spec", "source": source,
        "commons_map_available": bool(prior_map),
    }
    if prior_map:
        reuse["commons_map_id"] = prior_map.get("map_id")
        reuse["commons_author_did"] = prior_map.get("author_did")
    out = {
        "url": url,
        "requested_url": requested_url,
        "goal": goal,
        "result": result,
        "site_map": site_map,
        "source": source,
        "reuse": reuse,
        "modality_used": modality,
        # Phase 3: advisory — 'screenshot' signals the a11y tree was too sparse
        # (canvas/image page) and a vision pass would do better. Advice only.
        "modality_recommended": _commons_graph.recommend_modality(rows),
        "cost_class": cost_class,
        "execution_time_ms": elapsed_ms,
        "llm_used": llm_used,
        "degraded_mode": not llm_used,
    }
    _attach_formats(out, formats, html)
    return out


def _attach_observation_receipt(
    result: dict[str, Any], *, requested_url: str, final_url: str, rows: list[dict[str, str]],
) -> None:
    """Side-effect: mint a signed proof-of-observation receipt over the result and
    attach it under ``observation_receipt``. Provenance, not truth. Best-effort —
    a signing/persistence failure never breaks the navigation.
    """
    try:
        from core import observation_receipts as _receipts
        from core.registry.identity_backfill import ensure_agent_signing_keys

        private_pem, _public_pem, did = ensure_agent_signing_keys(_SELF_AGENT_ID)
        if not (private_pem and did):
            return
        dom_snapshot = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        receipt = _receipts.issue_observation_receipt(
            agent_id=_SELF_AGENT_ID, private_pem=private_pem, signer_did=did,
            request_url=requested_url, final_url=final_url,
            dom_snapshot=dom_snapshot, extraction=result.get("result"),
        )
        if receipt is not None:
            result["observation_receipt"] = receipt
    except Exception:  # noqa: BLE001 — attestation is additive; never break the call
        _LOG.warning("observation receipt attach failed", exc_info=True)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Navigate ``url`` and resolve ``goal`` over the cheapest path that works.

    Order, cheapest first: a signed API-spec replay (no browser) → an HTTP-first
    static fetch (no browser) → the full Chromium render. Both no-browser paths are
    flag-gated and fail closed — with AZTEA_API_DISCOVERY and AZTEA_HTTP_FIRST off,
    this is exactly the Chromium path, output-equivalent to before.
    """
    parsed = _normalize_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed  # error envelope
    url, goal, formats, schema, wait_for, wait_ms, force_refresh = parsed
    t_start = time.monotonic()
    if feature_flags.api_discovery_enabled() and not force_refresh:
        replayed = _try_api_spec_replay(
            url=url, goal=goal, formats=formats, schema=schema, t_start=t_start,
        )
        if replayed is not None:
            return replayed
    if feature_flags.http_first_enabled():
        http_outcome = _try_http_first(
            url=url, goal=goal, formats=formats, schema=schema, t_start=t_start,
        )
        if http_outcome is not None:
            return http_outcome
    return _run_chromium_path(
        url=url, goal=goal, formats=formats, schema=schema,
        wait_for=wait_for, wait_ms=wait_ms, t_start=t_start,
    )


def _run_chromium_path(
    *, url: str, goal: str, formats: tuple[str, ...], schema: dict[str, Any] | None,
    wait_for: str, wait_ms: int, t_start: float,
) -> dict[str, Any]:
    """The full headless render path (the original run() body; behavior unchanged)."""
    sync_playwright = _import_playwright()
    if isinstance(sync_playwright, dict):
        return sync_playwright  # error envelope
    captures: list[dict[str, Any]] | None = [] if feature_flags.api_discovery_enabled() else None
    try:
        with sync_playwright() as pw:
            outcome = _drive_chromium(pw, url, wait_for=wait_for, wait_ms=wait_ms, captures=captures)
    except Exception as exc:  # noqa: BLE001 — uniform envelope so settlement refunds
        return _err(
            "site_navigator.navigation_failed",
            f"Navigation failed: {type(exc).__name__}: {exc}",
        )
    if isinstance(outcome, dict):  # error envelope from _drive_chromium
        return outcome
    # Defense-in-depth: the route guard already aborts navigation to a blocked host,
    # but re-validate the post-redirect final URL before trusting it.
    try:
        final_url = url_security.validate_outbound_url(outcome.final_url, "url")
    except Exception as exc:  # noqa: BLE001 — surfaced as a structured envelope
        return _err("site_navigator.url_blocked", f"final URL after redirects is blocked: {exc}")
    commons_on = feature_flags.sitemap_commons_enabled()
    # Read BEFORE authoring this run, so prior_map reflects earlier navigations.
    prior_map = _commons.find_reusable_map(final_url) if commons_on else None
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    result = _build_result(
        url=final_url, requested_url=url, goal=goal, title=outcome.title, rows=outcome.rows,
        elapsed_ms=elapsed_ms, prior_map=prior_map, source="fresh", modality=_MODALITY_AX,
        cost_class="expensive", formats=formats, html=outcome.html, schema=schema,
    )
    if commons_on:
        _commons.author_map(
            agent_id=_SELF_AGENT_ID, owner_id=_SELF_OWNER_ID, url=final_url,
            map_json=result["site_map"], roles=[r.get("role", "") for r in outcome.rows],
        )
    if feature_flags.api_discovery_enabled() and outcome.captures:
        _maybe_author_api_spec(
            final_url=final_url, goal=goal, rows=outcome.rows, captures=outcome.captures,
        )
    if feature_flags.observation_receipts_enabled():
        _attach_observation_receipt(result, requested_url=url, final_url=final_url, rows=outcome.rows)
    return result


def _try_http_first(
    *, url: str, goal: str, formats: tuple[str, ...], schema: dict[str, Any] | None, t_start: float,
) -> dict[str, Any] | None:
    """HTTP-first static path. Returns None (fall through to Chromium) when the page
    is JS-rendered, blocked, or non-static — the named reason is logged for tuning."""
    fetched = _site_fetch.fetch_static_html(url)
    if fetched is None:
        return None
    analysis = _html_extract.analyze_html(fetched.html)
    if analysis.needs_browser:
        _LOG.info("http_first -> chromium fallback: %s", analysis.reason)
        return None
    try:
        final_url = url_security.validate_outbound_url(fetched.final_url, "url")
    except ValueError:
        return None  # post-redirect host blocked -> let the Chromium path handle it
    commons_on = feature_flags.sitemap_commons_enabled()
    prior_map = _commons.find_reusable_map(final_url) if commons_on else None
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    result = _build_result(
        url=final_url, requested_url=url, goal=goal, title=_html_extract.title_of(fetched.html),
        rows=analysis.rows, elapsed_ms=elapsed_ms, prior_map=prior_map, source="http_first",
        modality="http_first", cost_class="cheap", formats=formats, html=fetched.html, schema=schema,
    )
    if commons_on:
        _commons.author_map(
            agent_id=_SELF_AGENT_ID, owner_id=_SELF_OWNER_ID, url=final_url,
            map_json=result["site_map"], roles=[r.get("role", "") for r in analysis.rows],
        )
    if feature_flags.observation_receipts_enabled():
        _attach_observation_receipt(result, requested_url=url, final_url=final_url, rows=analysis.rows)
    return result


def _try_api_spec_replay(
    *, url: str, goal: str, formats: tuple[str, ...], schema: dict[str, Any] | None, t_start: float,
) -> dict[str, Any] | None:
    """Signed API-spec replay (no browser). Returns None on no-spec / drift / failure."""
    spec = _commons.find_reusable_api_spec(url)
    if spec is None:
        return None
    try:
        endpoint = _api_discovery.reconstruct_endpoint(spec, {})  # v1: literal, no params
    except ValueError:
        return None
    body = _api_discovery.replay(endpoint, method=str(spec.get("method") or "GET"))
    if body is None:
        return None
    is_fresh, _reason = _commons_freshness.validate_api_spec_before_replay(
        spec,
        recompute_response_fingerprint=lambda: _commons_normalize.response_shape_fingerprint(body),
    )
    try:
        _commons_store.bump_api_spec_hit(str(spec.get("api_spec_id") or ""), fresh=is_fresh)
    except Exception:  # noqa: BLE001 — a counter write must never break the call
        _LOG.debug("bump_api_spec_hit failed", exc_info=True)
    if not is_fresh:
        return None  # drift -> fall through to a fresh render
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return _build_api_spec_result(
        url=url, goal=goal, formats=formats, spec=spec, body=body, elapsed_ms=elapsed_ms, schema=schema,
    )


def _build_api_spec_result(
    *, url: str, goal: str, formats: tuple[str, ...], spec: dict[str, Any], body: Any,
    elapsed_ms: int, schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape an API-spec-replay result: the JSON body + optional LLM resolve, no browser."""
    if "structured" in formats and goal:
        result, llm_used = _resolve_goal_from_json(goal, url, body, schema)
        if not llm_used:
            result = body  # no provider -> still hand back the discovered data (degrade, don't drop)
    else:
        result, llm_used = body, False
    out: dict[str, Any] = {
        "url": url,
        "requested_url": url,
        "goal": goal,
        "result": result,
        "site_map": _api_spec_site_map(spec),
        "source": "api_spec",
        "reuse": {
            "reused": True, "source": "api_spec", "api_spec_id": spec.get("api_spec_id"),
            "commons_author_did": spec.get("author_did"), "commons_map_available": True,
        },
        "modality_used": "api_spec",
        "cost_class": "cheap",
        "execution_time_ms": elapsed_ms,
        "llm_used": llm_used,
        "degraded_mode": not llm_used,
    }
    if "markdown" in formats:
        out["markdown"] = (
            "```json\n"
            + json.dumps(body, indent=2, ensure_ascii=False)[:_MARKDOWN_MAX_CHARS]
            + "\n```"
        )
    return out


def _api_spec_site_map(spec: dict[str, Any]) -> dict[str, Any]:
    """The required site_map field for the replay path: describes the discovered API
    (not a DOM affordance map, since no page was rendered)."""
    return {
        "final_url": f"{spec.get('endpoint_scheme')}://{spec.get('endpoint_host')}{spec.get('path_template')}",
        "title": "",
        "affordances": {"links": [], "buttons": [], "inputs": [], "headings": []},
        "api_spec": {
            "api_spec_id": spec.get("api_spec_id"),
            "method": spec.get("method"),
            "endpoint_host": spec.get("endpoint_host"),
            "response_fingerprint": spec.get("response_fingerprint"),
        },
        "node_count": 0,
        "schema": "aztea/site-map/2",
    }


def _resolve_goal_from_json(
    goal: str, url: str, body: Any, schema: dict[str, Any] | None = None,
) -> tuple[Any, bool]:
    """Side-effect (LLM): resolve ``goal`` against a discovered API's JSON body.

    Degrades to (None, False) with no provider so the caller still gets the raw body
    from _build_api_spec_result. ``schema`` enforces a JSON Schema (the /extract path).
    """
    user = (
        f"URL: {url}\nGOAL: {goal}\n\n"
        f"API_RESPONSE (JSON from the site's own backing endpoint):\n"
        f"{json.dumps(body, ensure_ascii=False)[:_JSON_RESOLVE_MAX_CHARS]}"
    )
    return _resolve_with_optional_schema(user, schema)


def _maybe_author_api_spec(
    *, final_url: str, goal: str, rows: list[dict[str, str]], captures: list[dict[str, Any]],
) -> None:
    """Best-effort: compile the best captured JSON XHR into a signed API spec.

    Authoring enforces the same-registrable-domain gate, so a cross-origin capture is
    never registered. Never raises — discovery is additive.
    """
    try:
        candidate = _api_discovery.select_candidate(captures, goal=goal, rows=rows)
        if candidate is None:
            return
        _commons.author_api_spec(
            agent_id=_SELF_AGENT_ID, owner_id=_SELF_OWNER_ID,
            page_url=final_url, capture=candidate,
        )
    except Exception:  # noqa: BLE001 — discovery is additive; never break the call
        _LOG.warning("api_spec authoring hook failed", exc_info=True)
