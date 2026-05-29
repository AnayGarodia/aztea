"""publish: list a new agent on Aztea from the terminal.

Usage:
    aztea publish ./agent.md --price 0.05 --tags research
    aztea publish ./my_handler.py --endpoint https://my.host/run

Auto-detects file kind. Runs the verification gate before hitting the server.
Fire-and-forget — exits as soon as the listing is registered. Buyers using
`aztea` MCP will see the new agent within ~5 s.

Note: SKILL.md hosted-skill publishing is no longer a public path
(2026-05-17). Specialized agents — code execution, live data, real
integrations — pass our value test; prompt-only SKILL.md tools do not.
Use the .py handler path or the agent.md external-endpoint path.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import typer

from .common import (
    ApiKeyOpt,
    BaseUrlOpt,
    JsonOpt,
    build_client,
    handle_error,
    resolve_settings,
    slugify,
)
from .output import (
    ARROW,
    BULLET,
    CHECK,
    CROSS,
    banner,
    console,
    emit,
    err_console,
    info,
    kv_table,
    spinner,
)
from ._detect import DetectionError, DetectionResult, detect
from . import wizard as _wizard

# Re-use the shared scanner. We prefer the vendored copy that ships with
# the SDK (so `pip install aztea` users get a working `publish` command);
# fall back to `core.listing_safety` for monorepo dev installs that may
# have an older SDK package without the vendored module.
try:
    from .._listing_safety import (
        LEVEL_BLOCK,
        LEVEL_WARN,
        VerificationFinding,
        has_block,
        scan_agent_md_endpoint,
        scan_clone_against,
        scan_python_handler,
        scan_skill_md,
    )
except ImportError:
    try:
        from core.listing_safety import (  # type: ignore[import-not-found]
            LEVEL_BLOCK,
            LEVEL_WARN,
            VerificationFinding,
            has_block,
            scan_agent_md_endpoint,
            scan_clone_against,
            scan_python_handler,
            scan_skill_md,
        )
    except ImportError:  # pragma: no cover — only on broken installs
        # Keep `aztea publish --help` working; refuse at call time.
        LEVEL_BLOCK = "block"
        LEVEL_WARN = "warn"
        VerificationFinding = None  # type: ignore[assignment]
        has_block = None  # type: ignore[assignment]
        scan_agent_md_endpoint = None  # type: ignore[assignment]
        scan_clone_against = None  # type: ignore[assignment]
        scan_python_handler = None  # type: ignore[assignment]
        scan_skill_md = None  # type: ignore[assignment]


# Single command (not a Typer sub-app) so `aztea publish <path>` reads
# naturally at the top level alongside `aztea hire ...`.

_DEFAULT_AGENT_MD_PRICE_USD = 0.05
_DEFAULT_PY_HANDLER_PRICE_USD = 0.05

# How long the pre-flight reachability check waits for an endpoint to answer.
# Long enough to catch a slow first-byte but short enough that a typo doesn't
# cost the author 30 seconds before the error message lands. Matches the
# server's probe budget loosely.
_ENDPOINT_PROBE_TIMEOUT_S = 8.0

# Required top-level fields in an agent.md JSON metadata block, in the order
# the user is most likely to forget them. The server validates this too, but
# catching it client-side gives a faster, more concrete error.
_AGENT_MD_REQUIRED_FIELDS = (
    "name",
    "description",
    "endpoint_url",
    "price_per_call_usd",
    "input_schema",
    "output_schema",
)


def _write_template_stub(kind: str) -> None:
    """Non-interactive `--from-template <kind>` path: write a placeholder
    starter file with stand-in values, then return. The user edits and
    re-runs `aztea publish <file>`.
    """
    from string import Template
    kind_lower = (kind or "").strip().lower()
    if kind_lower not in {"agent", "python"}:
        from .output import error
        error(
            f"Unknown template kind {kind!r}. Use one of: agent, python. "
            "(SKILL.md publishing was removed 2026-05-17.)",
            code="publish.template_kind",
        )
        raise typer.Exit(code=2)

    template_map = {
        "agent":  ("agent_md.template",  "agent.md"),
        "python": ("handler_py.template", "my_new_agent.py"),
    }
    template_name, out_filename = template_map[kind_lower]
    templates_dir = Path(__file__).parent / "templates"
    raw = (templates_dir / template_name).read_text()

    # Placeholder substitutions — user will edit before publishing.
    subs = {
        "name":        "my_new_agent",
        "description": "TODO: describe what this agent does in one sentence.",
        "emoji_line":  "",
        "body":        "TODO: fill in the rest.",
    }
    rendered = Template(raw).safe_substitute(subs)

    target = Path.cwd() / out_filename
    if target.exists():
        from .output import error
        error(
            f"{target.name} already exists in cwd; refusing to overwrite. "
            "Move it aside or run from a different directory.",
            code="publish.template_exists",
        )
        raise typer.Exit(code=1)
    target.write_text(rendered)
    from .output import success
    success(
        f"Wrote {target.name}",
        detail=f"Edit it, then `aztea publish {target.name}` to list.",
    )


def publish(
    path: Optional[Path] = typer.Argument(
        None,
        help=(
            "SKILL.md, agent.md, or .py handler. Omit to launch the "
            "interactive wizard."
        ),
    ),
    price: Optional[float] = typer.Option(
        None,
        "--price",
        help="Override price per call in USD. Required for .py handlers.",
    ),
    tags: Optional[str] = typer.Option(
        None,
        "--tags",
        help="Comma-separated tag list (overrides any tags in the file).",
    ),
    endpoint: Optional[str] = typer.Option(
        None,
        "--endpoint",
        help="Public HTTPS URL where your .py handler is hosted (required for .py).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate and run all safety checks but do not register.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Treat warnings as blocking errors.",
    ),
    explain: bool = typer.Option(
        False,
        "--explain",
        help="On block, print the matched line / detail for each finding.",
    ),
    from_template: Optional[str] = typer.Option(
        None,
        "--from-template",
        help=(
            "Generate a starter file (skill | agent | python) and exit "
            "without publishing. Equivalent to running the wizard but "
            "stopping after the file is written."
        ),
    ),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """List an agent on Aztea. Auto-detects file kind, or runs the wizard."""
    if scan_skill_md is None:
        from .output import error
        error(
            "core.listing_safety is not importable.",
            hint="Install the full Aztea checkout (pip install -e .) before using publish.",
            code="publish.missing_safety_module",
        )
        raise typer.Exit(code=1)

    if path is None:
        # `--from-template <kind>` is the non-interactive starter-file path:
        # write a placeholder file using the bundled template (no prompts,
        # no TTY needed) and exit. The user edits and re-runs `aztea publish
        # <file>` to actually list. Bypasses the wizard which would still
        # require a TTY for the slug/description prompts.
        if from_template is not None:
            _write_template_stub(from_template)
            return
        path = _wizard.run_wizard(
            api_key=api_key,
            base_url=base_url,
            json_mode=json_mode,
            from_template_only=False,
        )

    try:
        detection = detect(path)
    except DetectionError as exc:
        from .output import error
        error(str(exc), code="publish.detect")
        raise typer.Exit(code=1)

    # 2026-05-17: SKILL.md publishing was previously removed because
    # prompt-only tools failed the original value-test ("callers can
    # replicate with their own LLM in seconds").
    #
    # 2026-05-26 (Wave 3 platform pivot): re-opened. Hosted SKILL.md is
    # now the cheapest path from "I have an agent idea" to "I'm earning
    # per call" for a non-infra builder — exactly the wedge the platform
    # pivot needs. The original concern is now addressed by:
    #   * core/listing_safety.py:scan_skill_md — static prompt-injection
    #     + API-key + base64 + internal-path scans.
    #   * core/listing_safety_judge.py:judge_skill_md — LLM intent review
    #     on every new publish AND every edit-republish.
    #   * core/skill_executor.py — hardened prefix/suffix scaffolding the
    #     SKILL.md body cannot override at runtime.
    # The execution flow itself never sunsetted — only the publish path
    # was closed. This branch restores it.

    if not json_mode:
        banner(
            f"aztea publish · {detection.path.name}",
            subtitle=detection.reason,
        )

    # Stage 0 — local agent.md metadata pre-validation. Server validates this
    # too, but catching missing fields here saves a roundtrip and gives the
    # author a concrete "you forgot X" rather than a generic 422.
    if detection.kind == "agent_md":
        _validate_agent_md_metadata_or_exit(detection, json_mode=json_mode)

    findings: list[Any] = []  # list[VerificationFinding] (typed loose for the import-fallback path)
    findings.extend(_static_scan(detection))

    if _has_block_findings(findings):
        _emit_findings(findings, json_mode=json_mode, explain=explain)
        if json_mode:
            emit(
                {
                    "ok": False,
                    "stage": "static_scan",
                    "findings": [_finding_dict(f) for f in findings],
                },
                json_mode=True,
            )
        raise typer.Exit(code=2)

    if strict and any(f.level == LEVEL_WARN for f in findings):
        _emit_findings(findings, json_mode=json_mode, explain=explain)
        if json_mode:
            emit(
                {
                    "ok": False,
                    "stage": "strict_warn",
                    "findings": [_finding_dict(f) for f in findings],
                },
                json_mode=True,
            )
        raise typer.Exit(code=3)

    # Resolve the base URL once so the marketplace link in the receipt
    # matches whatever the call session actually targets.
    resolved_base, _ = resolve_settings(
        api_key=api_key, base_url=base_url, require_api_key=False
    )

    # Network-touching stages happen inside one client session.
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Loading existing listings", json_mode=json_mode):
                existing = _load_existing_for_clone_check(client)
            cand_name, cand_desc = _candidate_name_description(detection)
            findings.extend(
                scan_clone_against(cand_name, cand_desc, existing) if existing else []
            )

            if _has_block_findings(findings) or (
                strict and any(f.level == LEVEL_WARN for f in findings)
            ):
                _emit_findings(findings, json_mode=json_mode, explain=explain)
                if json_mode:
                    emit(
                        {
                            "ok": False,
                            "stage": "clone_check",
                            "findings": [_finding_dict(f) for f in findings],
                        },
                        json_mode=True,
                    )
                raise typer.Exit(code=2 if _has_block_findings(findings) else 3)

            # Stage 3 — pre-flight endpoint reachability for paths that hand
            # us a URL. Catches "endpoint typo" or "container not started" at
            # the CLI before the server roundtrip. The server reruns its own
            # probe; this is purely UX, not security. Skipped in --dry-run
            # (the user is intentionally not committing) and json mode (CI
            # contexts often deploy and probe separately).
            if not dry_run and not json_mode and detection.kind in {"agent_md", "python_handler"}:
                _probe_endpoint_or_exit(detection, endpoint, json_mode=json_mode)

            if dry_run:
                _emit_findings(findings, json_mode=json_mode, explain=explain)
                if not json_mode:
                    info("Dry run — no listing was created.")
                else:
                    emit(
                        {
                            "ok": True,
                            "dry_run": True,
                            "findings": [_finding_dict(f) for f in findings],
                            "would_register_kind": detection.kind,
                        },
                        json_mode=True,
                    )
                return

            with spinner("Publishing listing", json_mode=json_mode):
                receipt = _register(
                    client=client,
                    detection=detection,
                    price=price,
                    tags=_parse_tags(tags),
                    endpoint=endpoint,
                )

        _emit_findings(findings, json_mode=json_mode, explain=False)
        _emit_receipt(
            receipt,
            detection=detection,
            json_mode=json_mode,
            base_url=resolved_base,
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 — funnel into the standard error UX
        _handle_publish_error(exc, detection=detection, json_mode=json_mode)


# ---------------------------------------------------------------------------
# Static scan dispatch
# ---------------------------------------------------------------------------


def _static_scan(detection: DetectionResult) -> list[Any]:
    if detection.kind == "skill_md":
        return list(scan_skill_md(detection.raw))
    if detection.kind == "python_handler":
        return list(scan_python_handler(detection.raw))
    if detection.kind == "agent_md":
        # Pull the endpoint URL out of the manifest's JSON metadata block so
        # we can SSRF-check before paying a server roundtrip.
        endpoint = _extract_endpoint_from_agent_md(detection.raw)
        return list(scan_agent_md_endpoint(endpoint)) if endpoint else []
    return []


_AGENT_MD_JSON_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```")


def _extract_endpoint_from_agent_md(text: str) -> str:
    for match in _AGENT_MD_JSON_RE.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            url = payload.get("endpoint_url") or payload.get("endpoint")
            if isinstance(url, str) and url.strip():
                return url.strip()
    return ""


def _candidate_name_description(detection: DetectionResult) -> tuple[str, str]:
    """Pull (name, description) for clone-detection without parsing the whole file."""
    text = detection.raw
    if detection.kind == "agent_md":
        for match in _AGENT_MD_JSON_RE.finditer(text):
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return (
                    str(payload.get("name") or ""),
                    str(payload.get("description") or ""),
                )
    if detection.kind == "skill_md":
        # YAML front-matter fields, regex'd to stay dep-light.
        name = _frontmatter_field(text, "name")
        desc = _frontmatter_field(text, "description")
        return name, desc
    if detection.kind == "python_handler":
        # Use the file stem as a name, the docstring (if present) as desc.
        name = detection.path.stem.replace("_", "-")
        desc = _python_module_docstring(text) or ""
        return name, desc
    return "", ""


_FRONTMATTER_FIELD_RE = {
    "name": re.compile(r"(?m)^name\s*:\s*(.+?)\s*$"),
    "description": re.compile(r"(?m)^description\s*:\s*(.+?)\s*$"),
}


def _frontmatter_field(text: str, field: str) -> str:
    head = text[:4096]
    pattern = _FRONTMATTER_FIELD_RE.get(field)
    if pattern is None:
        return ""
    match = pattern.search(head)
    if not match:
        return ""
    return match.group(1).strip().strip("\"'")


def _python_module_docstring(text: str) -> str:
    """Cheap module-docstring extractor; avoids importing ast just for this."""
    head = text.lstrip()
    for quote in ('"""', "'''"):
        if head.startswith(quote):
            end = head.find(quote, 3)
            if end > 0:
                return head[3:end].strip()
    return ""


# ---------------------------------------------------------------------------
# Clone-check input
# ---------------------------------------------------------------------------


def _load_existing_for_clone_check(client: Any) -> list[dict[str, Any]]:
    try:
        agents = client.list_agents()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for agent in agents or []:
        name = getattr(agent, "name", None)
        desc = getattr(agent, "description", None)
        if name or desc:
            out.append({"name": name or "", "description": desc or ""})
    return out


# ---------------------------------------------------------------------------
# Registration dispatch
# ---------------------------------------------------------------------------


def _register(
    *,
    client: Any,
    detection: DetectionResult,
    price: Optional[float],
    tags: list[str],
    endpoint: Optional[str],
) -> dict[str, Any]:
    # skill_md is refused at the entry point of `publish()` — we never reach
    # here for it. If you're reading this confused: that's intentional;
    # SKILL.md publishing was removed 2026-05-17.
    if detection.kind == "agent_md":
        body: dict[str, Any] = {"manifest_content": detection.raw}
        if price is not None:
            body["price_per_call_usd"] = float(price)
        if tags:
            body["tags"] = tags
        return client._request_json(
            "POST", "/onboarding/ingest", json_body=body, timeout=90.0,
        )

    if detection.kind == "python_handler":
        if not endpoint:
            raise typer.BadParameter(
                "Publishing a .py handler requires --endpoint <https URL> where "
                "your handler is reachable. Run `aztea publish --help` for the "
                "polling-worker alternative.",
                param_hint="--endpoint",
            )
        name, desc = _candidate_name_description(detection)
        if not desc:
            desc = (
                f"Author-hosted Python handler for {name}. Registered via "
                "aztea publish."
            )
        return client.registry.register(
            name=name or detection.path.stem,
            description=desc,
            endpoint_url=endpoint,
            price_per_call_usd=float(
                price if price is not None else _DEFAULT_PY_HANDLER_PRICE_USD
            ),
            tags=tags or None,
        )

    raise RuntimeError(f"Unhandled listing kind: {detection.kind}")  # pragma: no cover


def _parse_tags(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# UX helpers
# ---------------------------------------------------------------------------


def _emit_findings(
    findings: list[Any],
    *,
    json_mode: bool,
    explain: bool,
) -> None:
    if json_mode:
        # In JSON mode the receipt at the end carries findings; nothing to
        # render mid-stream.
        return
    if not findings:
        return
    for finding in findings:
        glyph = (
            CROSS
            if finding.level == LEVEL_BLOCK
            else "!" if finding.level == LEVEL_WARN
            else BULLET
        )
        style = (
            "error"
            if finding.level == LEVEL_BLOCK
            else "warn" if finding.level == LEVEL_WARN
            else "muted"
        )
        target = err_console if finding.level == LEVEL_BLOCK else console
        target.print(f"[{style}]{glyph}[/{style}]  {finding.message}")
        if finding.level == LEVEL_BLOCK:
            hint = _wizard.remediation_for(getattr(finding, "code", "") or "")
            if hint:
                target.print(f"    [muted]{hint}[/muted]")
        if explain and finding.detail:
            target.print(f"    [muted]{json.dumps(finding.detail, default=str)}[/muted]")


def _emit_receipt(
    receipt: dict[str, Any],
    *,
    detection: DetectionResult,
    json_mode: bool,
    base_url: str | None = None,
) -> None:
    agent = receipt.get("agent") or {}
    agent_id = receipt.get("agent_id") or agent.get("agent_id") or ""
    name = agent.get("name") or _candidate_name_description(detection)[0] or detection.path.stem
    review_status = receipt.get("review_status") or agent.get("review_status") or "approved"

    if json_mode:
        emit(
            {
                "ok": True,
                "kind": detection.kind,
                "agent_id": agent_id,
                "slug": slugify(name),
                "review_status": review_status,
                "raw": receipt,
            },
            json_mode=True,
        )
        return

    console.print()
    console.print(f"[success]{CHECK}[/success]  Listed [code]{slugify(name)}[/code]")
    rows: list[tuple[str, str]] = [
        ("agent_id", agent_id or "—"),
        ("name", name or "—"),
        ("kind", _human_kind(detection.kind)),
        ("review", review_status or "—"),
    ]
    did = agent.get("did") or agent.get("did_web")
    if did:
        rows.append(("did", str(did)))
    kv_table(rows)
    console.print()
    console.print(
        "[muted]Buyers using `aztea` MCP will see this listing within 5 s.[/muted]"
    )
    if review_status == "probation":
        console.print(
            f"[warn]{ARROW}[/warn]  [muted]probation: visible in the marketplace, "
            "ranked last in auto-hire until the listing accumulates a track record. "
            "Direct hires aren't affected.[/muted]"
        )
    if base_url and agent_id and base_url.startswith(("http://", "https://")):
        marketplace_url = f"{base_url.rstrip('/')}/agents/{agent_id}"
        console.print(
            f"[muted]View at[/muted] [code]{marketplace_url}[/code]"
        )
    console.print(
        f"[muted]Try it: [/muted][code]aztea hire {slugify(name)} --input "
        "'{\"task\":\"...\"}'[/code]"
    )
    console.print()


def _human_kind(kind: str) -> str:
    return {
        "agent_md": "external endpoint (manifest)",
        "python_handler": "external endpoint (python)",
    }.get(kind, kind)


def _has_block_findings(findings: list[Any]) -> bool:
    return any(getattr(f, "level", "") == LEVEL_BLOCK for f in findings)


def _finding_dict(f: Any) -> dict[str, Any]:
    return {
        "code": getattr(f, "code", ""),
        "level": getattr(f, "level", ""),
        "message": getattr(f, "message", ""),
        "detail": getattr(f, "detail", {}) or {},
    }


# ---------------------------------------------------------------------------
# Pre-flight validation helpers (added 2026-05-17)
# ---------------------------------------------------------------------------


def _validate_agent_md_metadata_or_exit(
    detection: DetectionResult,
    *,
    json_mode: bool,
) -> None:
    """Local pre-validation of the agent.md JSON metadata block.

    Server runs the authoritative validation, but catching the common mistakes
    here gives the author a concrete "missing field X" error within milliseconds
    rather than a 422 with a generic schema message. Failures here are blocking;
    the publish flow exits with a non-zero code.
    """
    from .output import error

    metadata: dict[str, Any] | None = None
    for match in _AGENT_MD_JSON_RE.finditer(detection.raw):
        try:
            candidate = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            error(
                f"Couldn't parse the JSON metadata block in {detection.path.name}: {exc}.",
                hint=(
                    "agent.md needs one fenced ```json``` block at the top "
                    "containing the listing metadata. Run `aztea publish "
                    "--from-template agent` for a working starter."
                ),
                code="publish.agent_md.json_invalid",
            )
            raise typer.Exit(code=2)
        if isinstance(candidate, dict):
            metadata = candidate
            break

    if metadata is None:
        error(
            f"No JSON metadata block found in {detection.path.name}.",
            hint=(
                "agent.md must contain one ```json``` fenced block with "
                "fields name, description, endpoint_url, price_per_call_usd, "
                "input_schema, output_schema. See "
                "https://aztea.ai/docs/agent-md."
            ),
            code="publish.agent_md.no_metadata",
        )
        raise typer.Exit(code=2)

    missing = [f for f in _AGENT_MD_REQUIRED_FIELDS if not metadata.get(f)]
    if missing:
        error(
            f"agent.md metadata is missing required fields: {', '.join(missing)}.",
            hint=(
                "Every agent.md listing needs all six. "
                "input_schema / output_schema must be valid JSON Schema objects "
                "with `type: object`. See https://aztea.ai/docs/agent-md for "
                "examples."
            ),
            code="publish.agent_md.missing_fields",
        )
        raise typer.Exit(code=2)

    # Price sanity. The server rejects out-of-range too, but a $250 typo is
    # the kind of mistake the author wants to catch instantly.
    price = metadata.get("price_per_call_usd")
    try:
        price_val = float(price)
    except (TypeError, ValueError):
        error(
            f"price_per_call_usd must be a number, got {price!r}.",
            code="publish.agent_md.price_not_number",
        )
        raise typer.Exit(code=2)
    if price_val < 0.0 or price_val > 25.0:
        error(
            f"price_per_call_usd={price_val} is outside the allowed range (0–25 USD).",
            hint="Most third-party tools price between $0.01 and $1.00.",
            code="publish.agent_md.price_out_of_range",
        )
        raise typer.Exit(code=2)

    # Endpoint must be https in prod. The SSRF check on the server catches
    # private / loopback URLs; here we just nudge the obvious case so a
    # missing scheme doesn't waste a roundtrip.
    endpoint_url = str(metadata.get("endpoint_url", "")).strip()
    if not endpoint_url.startswith(("http://", "https://")):
        error(
            f"endpoint_url must be an http(s) URL; got {endpoint_url!r}.",
            hint="Prefix with `https://`.",
            code="publish.agent_md.endpoint_scheme",
        )
        raise typer.Exit(code=2)

    # Schemas must at least be objects with type=object. The server runs the
    # full JSON Schema validator; this catches the trivial "I put a string
    # instead of {}" mistake.
    for field in ("input_schema", "output_schema"):
        schema = metadata.get(field)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            error(
                f"{field} must be a JSON Schema object with `type: object`.",
                hint=(
                    "Use {\"type\": \"object\", \"properties\": {...}, "
                    "\"required\": [...]}."
                ),
                code=f"publish.agent_md.{field}_invalid",
            )
            raise typer.Exit(code=2)


def _probe_endpoint_or_exit(
    detection: DetectionResult,
    cli_endpoint: Optional[str],
    *,
    json_mode: bool,
) -> None:
    """Pre-flight reachability check for paths that hand us a public URL.

    Why: a typo or a not-yet-deployed endpoint is the most common reason a
    fresh listing fails the server probe. Hitting it client-side gives the
    author sub-second feedback ("DNS doesn't resolve") instead of waiting on
    the server to retry, time out, and translate the failure into a 400.

    This is purely UX. Security still lives on the server (SSRF, probe,
    safety scan re-run). Skipping it would not weaken security.

    Set ``AZTEA_SKIP_ENDPOINT_PROBE=1`` in tests / CI where the endpoint is
    deliberately a fixture (mock host, no real DNS).
    """
    import os as _os
    if _os.environ.get("AZTEA_SKIP_ENDPOINT_PROBE", "").strip() in {"1", "true", "yes"}:
        return

    from .output import error, warn as _warn

    endpoint_url = ""
    if detection.kind == "python_handler":
        endpoint_url = (cli_endpoint or "").strip()
    elif detection.kind == "agent_md":
        endpoint_url = _extract_endpoint_from_agent_md(detection.raw)
    if not endpoint_url:
        # python_handler without --endpoint is caught later in _register with
        # a typer.BadParameter; agent.md without endpoint_url is caught in
        # the metadata validator above. Both have their own clear messages.
        return

    parsed = urllib.parse.urlparse(endpoint_url)
    host = parsed.hostname or ""
    if not host:
        error(
            f"Endpoint URL {endpoint_url!r} has no hostname.",
            code="publish.endpoint.no_host",
        )
        raise typer.Exit(code=2)

    # DNS — the cheapest and most common failure mode. socket.getaddrinfo
    # respects the system resolver / hosts file.
    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        error(
            f"Could not resolve hostname {host!r}: {exc.strerror or exc}.",
            hint=(
                "Check the URL for typos. If the host is correct, make sure "
                "DNS has propagated before re-publishing."
            ),
            code="publish.endpoint.dns",
        )
        raise typer.Exit(code=2)

    # HTTP reachability. We send a HEAD with a tight budget. A 405 / 501 /
    # any 4xx/5xx is still "reachable" — only network errors are blocking.
    request = urllib.request.Request(endpoint_url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=_ENDPOINT_PROBE_TIMEOUT_S) as resp:  # noqa: S310
            status = resp.status
    except urllib.error.HTTPError as http_exc:
        # The endpoint answered; it just doesn't speak HEAD. Server probe
        # will hit it properly with POST. Surface as info, not failure.
        if not json_mode:
            _warn(
                f"Endpoint answered HEAD with HTTP {http_exc.code}; "
                "treating as reachable. The server probe will retry with POST."
            )
        return
    except urllib.error.URLError as url_exc:
        reason = getattr(url_exc, "reason", url_exc)
        error(
            f"Could not reach {endpoint_url}: {reason}.",
            hint=(
                "Make sure the endpoint is publicly reachable over HTTPS. If "
                "you're testing locally, deploy first or use a tunnel like "
                "ngrok / Cloudflare Tunnel."
            ),
            code="publish.endpoint.unreachable",
        )
        raise typer.Exit(code=2)
    except (TimeoutError, socket.timeout):
        error(
            f"Endpoint {endpoint_url} did not respond within "
            f"{int(_ENDPOINT_PROBE_TIMEOUT_S)} s.",
            hint=(
                "Cold-start time is normal for serverless functions, but the "
                "server probe is even tighter. Warm the endpoint, then retry."
            ),
            code="publish.endpoint.timeout",
        )
        raise typer.Exit(code=2)
    except Exception as exc:  # noqa: BLE001 — bubble any unexpected I/O issue up
        error(
            f"Couldn't probe {endpoint_url}: {exc}.",
            code="publish.endpoint.probe_failed",
        )
        raise typer.Exit(code=2)
    # Anything in the 1xx–5xx range means the host is reachable; the server
    # probe runs the real semantic check.
    _ = status  # silence linter; we only care that the HEAD succeeded.


def _handle_publish_error(
    exc: Exception,
    *,
    detection: Optional[DetectionResult],
    json_mode: bool,
) -> None:
    """Turn server error envelopes into actionable messages, fall back to handle_error.

    The server returns structured envelopes for listing-specific failures
    (listing.safety_block, REGISTRY_AGENT_LIMIT, skills.public_publish_disabled,
    etc.). Without this, the user sees a generic AzteaError stringified.
    """
    from .output import error

    payload = _extract_error_envelope(exc)
    if payload is None:
        handle_error(exc)
        return

    code = str(payload.get("code") or "")
    message = str(payload.get("message") or "")
    detail = payload.get("detail") or payload.get("data") or {}
    if not isinstance(detail, dict):
        detail = {}
    hint = detail.get("hint") or None

    # Special-case the message envelopes we know we generate server-side. The
    # rest fall back to generic "code: message" rendering, which is still
    # better than `AzteaError: 400 Bad Request`.
    if code == "skills.public_publish_disabled":
        error(
            message
            or "SKILL.md publishing is no longer publicly available.",
            hint=(
                hint
                or "Publish a .py handler or agent.md manifest with `aztea publish` instead."
            ),
            code=code,
        )
        raise typer.Exit(code=1)

    if code == error_codes_str("REGISTRY_AGENT_LIMIT"):
        current = detail.get("current")
        max_ = detail.get("max")
        suffix = ""
        if isinstance(current, int) and isinstance(max_, int):
            suffix = f" ({current}/{max_} used)"
        error(
            (message or "Per-owner agent limit reached.") + suffix,
            hint="Delete or archive an existing listing, then retry.",
            code=code,
        )
        raise typer.Exit(code=1)

    if code == "listing.safety_block":
        # The server already gave us the specific finding. Pass it through.
        finding_code = (detail or {}).get("code") or ""
        finding_hint = _wizard.remediation_for(finding_code)
        error(
            message or "Listing was blocked by the safety scanner.",
            hint=finding_hint
            or "Run with `--explain` to see the matched line and detail.",
            code=code,
        )
        raise typer.Exit(code=2)

    # Unknown server envelope. Render generically but at least show the code
    # so a support handler can correlate.
    error(
        message or "Publish failed.",
        hint=hint if isinstance(hint, str) else None,
        code=code or "publish.server_error",
    )
    raise typer.Exit(code=1)


def _extract_error_envelope(exc: Exception) -> Optional[dict[str, Any]]:
    """Pull a structured error envelope off an SDK exception if present.

    Aztea SDK exceptions carry a `.body` (dict) or `.response_body` (str) on
    HTTP errors. Most publish-related routes return
    {"detail": {"code": ..., "message": ..., "data": ...}} or just
    {"detail": "string"}. Normalise both shapes into a flat dict.
    """
    body = getattr(exc, "body", None)
    if body is None:
        body_str = getattr(exc, "response_body", None)
        if isinstance(body_str, str):
            try:
                body = json.loads(body_str)
            except json.JSONDecodeError:
                return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str):
        return {"code": "", "message": detail}
    return None


def error_codes_str(name: str) -> str:
    """Best-effort import of canonical error code constants.

    The SDK doesn't bundle core.error_codes; users on `pip install aztea`
    won't have it. We fall back to the literal string the server uses, which
    is also what core.error_codes defines.
    """
    try:
        from core import error_codes as _ec  # type: ignore[import-not-found]
        return getattr(_ec, name, name)
    except ImportError:
        # Mirror the canonical names that ship in core/error_codes.py.
        return {
            "REGISTRY_AGENT_LIMIT": "registry.agent_limit",
        }.get(name, name)


__all__ = ["publish"]
