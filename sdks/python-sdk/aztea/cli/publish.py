"""publish: list a new agent on Aztea from the terminal.

Usage:
    aztea publish ./word-counter.skill.md
    aztea publish ./agent.md --price 0.05 --tags research
    aztea publish ./my_handler.py --endpoint https://my.host/run

Auto-detects file kind. Runs the verification gate before hitting the server.
Fire-and-forget — exits as soon as the listing is registered. Buyers using
`aztea` MCP will see the new agent within ~5 s.
"""
from __future__ import annotations

import json
import re
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

_DEFAULT_SKILL_PRICE_USD = 0.02
_DEFAULT_AGENT_MD_PRICE_USD = 0.05
_DEFAULT_PY_HANDLER_PRICE_USD = 0.05


def _write_template_stub(kind: str) -> None:
    """Non-interactive `--from-template <kind>` path: write a placeholder
    starter file with stand-in values, then return. The user edits and
    re-runs `aztea publish <file>`.
    """
    from string import Template
    kind_lower = (kind or "").strip().lower()
    if kind_lower not in {"skill", "agent", "python"}:
        from .output import error
        error(
            f"Unknown template kind {kind!r}. Use one of: skill, agent, python.",
            code="publish.template_kind",
        )
        raise typer.Exit(code=2)

    template_map = {
        "skill":  ("skill_md.template",  "my_new_skill.skill.md"),
        "agent":  ("agent_md.template",  "agent.md"),
        "python": ("handler_py.template", "my_new_agent.py"),
    }
    template_name, out_filename = template_map[kind_lower]
    templates_dir = Path(__file__).parent / "templates"
    raw = (templates_dir / template_name).read_text()

    # Placeholder substitutions — user will edit before publishing.
    subs = {
        "name":        "my-new-skill" if kind_lower == "skill" else "my_new_agent",
        "description": "TODO: describe what this agent does in one sentence.",
        "emoji_line":  "",
        "body":        "TODO: write the skill body here. See https://aztea.ai/docs/skill-md for the format.",
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

    if not json_mode:
        banner(
            f"aztea publish · {detection.path.name}",
            subtitle=detection.reason,
        )

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
        handle_error(exc)


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
    if detection.kind == "skill_md":
        # Precedence: --price flag > frontmatter `price_usd` > default.
        # Pre-1.7.0 the frontmatter step was missing — every skill
        # without a CLI flag landed at $0.02 even when the file said
        # `price_usd: 0.005`. (P3-23 from the eval.)
        effective_price = price
        if effective_price is None:
            try:
                from core.skill_parser import parse_skill_md as _parse_for_price
                _parsed = _parse_for_price(detection.raw, source=str(detection.path))
                if _parsed.price_per_call_usd is not None:
                    effective_price = _parsed.price_per_call_usd
            except Exception:
                # If parsing fails here, /skills will surface the same
                # error with a better message — don't block the flow.
                pass
        if effective_price is None:
            effective_price = _DEFAULT_SKILL_PRICE_USD
        body = {
            "skill_md": detection.raw,
            "price_per_call_usd": float(effective_price),
        }
        # 1.7.3 — listing-safety scan + AST + clone-detection can run
        # 60+ seconds on the server. The SDK default 30s read timeout
        # was less than the server budget, so users saw ReadTimeout
        # + a phantom listing (the server completed but the client
        # gave up). 90s leaves margin even on a busy host.
        return client._request_json(
            "POST", "/skills", json_body=body, timeout=90.0,
        )

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
        "skill_md": "hosted skill",
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


__all__ = ["publish"]
