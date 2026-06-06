"""Interactive `aztea publish` wizard.

Drops the user into a guided flow when `aztea publish` is invoked with no
path argument. Handles three publishing paths in plain English:

    1. External webhook (agent.md)      — manifest pointing at your URL
    2. Python handler (.py)             — scaffold + your endpoint URL
    3. AI-inferred publish               — point at an existing .py handler;
                                           the platform infers every field
                                           via core.publish_inference and
                                           publishes directly (Wave 2)

Paths 1+2 generate a starter file in the current working directory, then
dispatch into the existing `publish` flow so the safety scanner and
registration logic stay identical to the path-given case. Path 3 owns
its own end-to-end publish (it has all the data it needs after the
prompt loop) and returns a sentinel that publish.py recognises as
"already done".

SKILL.md hosted-skill publishing was removed 2026-05-17. The brutal test:
"can the caller's own LLM trivially replicate this from a prompt?"
SKILL.md tools failed that test. Specialized agents — code, live data,
real integrations — pass it, which is why .py and agent.md stay.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

import typer

from ..config import load_config
from .common import resolve_settings, slugify
from .output import (
    banner,
    console,
    err_console,
    info,
    success,
)
from . import prompts as _p

# Template files live alongside this module; loaded lazily on first use so
# import is cheap.
_TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# Public entry point — called from publish.py when path is None.
# ---------------------------------------------------------------------------


def run_wizard(
    *,
    api_key: str | None,
    base_url: str | None,
    json_mode: bool,
    from_template_only: bool = False,
) -> Path:
    """Run the wizard and return the path to the generated file.

    The caller (`publish.publish`) hands the returned path back into the
    normal publish pipeline (`detect()` + `_static_scan` + `_register`),
    so the wizard does NOT call the safety scanner itself.

    If `from_template_only=True`, generate the file and exit without
    publishing. Returning the path lets the caller decide.
    """
    if json_mode:
        err_console.print(
            "[error]✗[/error] The wizard is interactive; rerun with a file "
            "path or drop --json to use the questionnaire."
        )
        raise typer.Exit(code=2)
    if not _p._is_tty():
        err_console.print(
            "[error]✗[/error] `aztea publish` without a path requires an "
            "interactive terminal. Pipe a file path or use a TTY."
        )
        raise typer.Exit(code=2)

    resolved_base, saved_key = resolve_settings(
        api_key=api_key, base_url=base_url, require_api_key=False
    )
    if not saved_key:
        err_console.print(
            "[error]✗[/error] You're not signed in.\n"
            "  Run [code]aztea login[/code] first — that points the CLI at "
            "https://aztea.ai and saves your API key.\n"
            "  Or set [code]AZTEA_BASE_URL=http://localhost:8000[/code] for a "
            "local sandbox."
        )
        raise typer.Exit(code=2)

    cfg = load_config() or {}
    username = cfg.get("username") or None
    _greet(resolved_base, username)

    try:
        kind = _ask_kind()
        if kind == 1:
            path = _wizard_agent_md()
        elif kind == 2:
            path = _wizard_python_handler()
        else:
            # Wave 2 inferred-publish path: handles its own end-to-end POST.
            # On success it raises typer.Exit(0), so control never returns
            # here. On user-cancel it raises typer.Exit(130).
            _wizard_inferred_publish(
                resolved_base=resolved_base, api_key=saved_key,
            )
            raise RuntimeError(
                "_wizard_inferred_publish must exit; reached unreachable code"
            )
    except (KeyboardInterrupt, EOFError):
        # Ctrl-C / Ctrl-D mid-wizard: drop the user back to the shell cleanly
        # instead of dumping a traceback. Line-mode readline doesn't deliver
        # Escape to Python, so Ctrl-C is the documented cancel key.
        console.print()
        console.print("  [muted]Cancelled. No file was written.[/muted]")
        raise typer.Exit(code=130)

    success(f"Saved {path}")
    if from_template_only:
        info(
            "Template ready. Edit it as you like, then run "
            f"[code]aztea publish {path}[/code] when you're done."
        )
    return path


# ---------------------------------------------------------------------------
# Greeting + kind selector
# ---------------------------------------------------------------------------


def _greet(base_url: str, username: str | None) -> None:
    banner(
        "aztea publish",
        subtitle="Let's get your agent listed. I'll ask a few questions.",
    )
    console.print()
    who = f" (signed in as [accent]{username}[/accent])" if username else ""
    console.print(
        f"  [muted]Publishing to[/muted] [code]{base_url}[/code]{who}."
    )
    if "aztea.ai" in base_url:
        console.print(
            "  [muted]Override with[/muted] "
            "[code]AZTEA_BASE_URL=http://localhost:8000[/code] "
            "[muted]for a local sandbox.[/muted]"
        )
    console.print()


def _ask_kind() -> int:
    return _p.select_numeric(
        "What kind of agent are you publishing?",
        options=[
            (
                "External webhook",
                "agent.md manifest pointing at a URL you host",
            ),
            (
                "Python handler",
                ".py file with def handler(payload); you host the endpoint",
            ),
            (
                "AI-inferred publish",
                "point at an existing .py — inference fills the metadata, "
                "then publishes directly",
            ),
        ],
        default=3,
    )


# ---------------------------------------------------------------------------
# Path 1 — agent.md manifest
# ---------------------------------------------------------------------------


def _wizard_agent_md() -> Path:
    name = _p.ask(
        "Name (lowercase, dashes ok)",
        default=_default_name_from_cwd(),
        validator=_p.slug_validator,
    )
    description = _p.ask(
        "One-sentence description",
        validator=_p.description_validator,
    )
    endpoint_url = _p.ask(
        "Endpoint URL (https://…)",
        validator=_p.url_validator,
        hint="The HTTPS URL where your agent receives POST requests.",
    )
    input_field = _p.ask(
        "Input field name",
        default="task",
        validator=_p.identifier_validator,
    )
    input_desc = _p.ask(
        "Input field description",
        default="What you want the agent to do.",
        validator=_p.description_validator,
    )
    output_field = _p.ask(
        "Output field name",
        default="result",
        validator=_p.identifier_validator,
    )
    output_desc = _p.ask(
        "Output field description",
        default="The agent's response.",
        validator=_p.description_validator,
    )
    price = float(
        _p.ask(
            "Price per call (USD)",
            default="0.05",
            validator=_p.price_validator,
        )
    )
    tags = _parse_tags(
        _p.ask("Tags (comma-separated, optional)", default="", validator=_p.optional)
    )

    metadata = {
        "name": name,
        "description": description,
        "endpoint_url": endpoint_url,
        "price_per_call_usd": price,
        "tags": tags,
        "input_schema": {
            "type": "object",
            "properties": {
                input_field: {
                    "type": "string",
                    "title": "Input",
                    "description": input_desc,
                }
            },
            "required": [input_field],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                output_field: {
                    "type": "string",
                    "description": output_desc,
                }
            },
            "required": [output_field],
        },
    }
    rendered = Template(_load_template("agent_md.template")).substitute(
        name=name,
        metadata_json=json.dumps(metadata, indent=2),
    )
    return _write_file(f"{name}.agent.md", rendered)


# ---------------------------------------------------------------------------
# Path 3 — Python handler
# ---------------------------------------------------------------------------


def _wizard_python_handler() -> Path:
    name = _p.ask(
        "Name (lowercase, dashes ok)",
        default=_default_name_from_cwd(),
        validator=_p.slug_validator,
    )
    description = _p.ask(
        "One-sentence description",
        validator=_p.description_validator,
    )
    rendered = Template(_load_template("handler_py.template")).substitute(
        name=name,
        description=description,
    )
    body_choice = _p.confirm(
        "Open the starter handler in your editor?", default=True
    )
    if body_choice:
        edited = _p.multiline_or_editor(
            "Edit the handler.",
            initial=rendered,
            suffix=".py",
            editor_default_yes=True,
        )
        if edited:
            rendered = edited
    file_name = name.replace("-", "_") + ".py"
    path = _write_file(file_name, rendered)
    info(
        "Deploy this file at the URL you'll provide next, then we'll list it "
        "as an external endpoint."
    )
    return path


# ---------------------------------------------------------------------------
# Friendly error mapping for safety-scanner blocks
# ---------------------------------------------------------------------------

_REMEDIATION: dict[str, str] = {
    "skill.prompt_injection": (
        "Skill bodies are prompts the LLM treats as instructions. The phrase "
        "we matched looks like an attempt to override safety scaffolding. "
        "Rephrase as a description of behavior rather than a directive — "
        'e.g. change "ignore previous instructions" → "this skill summarizes '
        'the previous user turn".'
    ),
    "skill.embedded_api_key": (
        "We found a string that looks like an API key in the skill body. "
        "Hardcoded keys leak immediately. Take secrets via the caller's input "
        "or your own backend instead."
    ),
    "python.blocked_import": (
        "Your handler imports a module we don't allow for in-process "
        "listings (subprocess, socket, pickle, …). If you genuinely need it, "
        "host the handler yourself and register an external-endpoint "
        "listing instead."
    ),
    "python.blocked_builtin": (
        "Your handler calls eval/exec/compile or similar dynamic code. We "
        "block these to keep the in-process worker safe. Replace with "
        "explicit logic, or self-host the agent."
    ),
    "python.blocked_os_call": (
        "Your handler calls os.system / os.popen / os.exec*. Shell-out paths "
        "aren't allowed in-process; self-host the agent if you need them."
    ),
    "manifest.endpoint_is_aztea": (
        "Your endpoint URL points at an Aztea-owned host. Third-party agents "
        "must run on a host you control."
    ),
    "listing.duplicate": (
        "This listing's content is byte-identical to an agent already on Aztea. "
        "We don't host exact copies. If you own the original, update it instead "
        "of re-publishing; otherwise add your own distinct logic before listing."
    ),
    "listing.unreliable.schema": (
        "Your endpoint's response didn't match the output_schema you declared. "
        "Buyers parse results against that schema, so a mismatch breaks every "
        "integration. Fix the response shape or correct the declared schema, "
        "then re-publish."
    ),
    "listing.probe_unreachable": (
        "Your endpoint didn't answer any of our registration probes (timeout, "
        "network error, or 5xx). Confirm it's deployed and reachable over HTTPS "
        "and returns a non-5xx response to a POST, then re-publish."
    ),
}


def remediation_for(code: str) -> str | None:
    return _REMEDIATION.get(code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_name_from_cwd() -> str:
    raw = Path.cwd().name or "my-agent"
    cleaned = slugify(raw)
    if not re.match(r"^[a-z]", cleaned):
        cleaned = "my-" + cleaned
    return cleaned[:64] or "my-agent"


def _load_template(name: str) -> str:
    path = _TEMPLATES_DIR / name
    return path.read_text(encoding="utf-8")


def _write_file(name: str, content: str) -> Path:
    target = Path.cwd() / name
    if target.exists():
        if not _p.confirm(
            f"{target.name} already exists. Overwrite?", default=False
        ):
            err_console.print(
                f"[error]✗[/error] Refusing to overwrite {target}; "
                "rename it or move into a fresh directory."
            )
            raise typer.Exit(code=1)
    target.write_text(content, encoding="utf-8")
    return target


def _parse_tags(raw: str) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Wave 2 — AI-inferred publish path
# ---------------------------------------------------------------------------
#
# Different shape from the agent.md / python-handler scaffolders: those write
# starter files and let the normal publish pipeline (detect → safety scan →
# register) take over. The inferred path has all the data it needs after the
# user accepts/overrides each inferred field, so it does the full publish
# end-to-end here and raises typer.Exit() on completion. Returning a Path
# would either (a) re-trigger detection on the user's source — wrong, source
# is not a manifest — or (b) require synthesizing an agent.md from inference
# — wasteful round-trip through pipe-and-redirect.
#
# Shares the same backend endpoint as the MCP /publish_agent tool
# (POST /registry/register), so the CLI wizard and the MCP tool produce
# byte-identical post bodies. The inference + safety code paths are also
# shared (core.publish_inference, core.listing_safety).


def _wizard_inferred_publish(
    *, resolved_base: str, api_key: str | None,
) -> None:
    """End-to-end inferred publish — exits on completion via typer.Exit."""
    from .output import error  # local import keeps top-of-file clean

    if not api_key:
        error(
            "You're not signed in. Run `aztea login` first.",
            code="wizard.no_api_key",
        )
        raise typer.Exit(code=2)

    # Step 1: ask for the handler file.
    raw_path = _p.ask(
        "Path to your handler.py",
        default="handler.py",
        validator=_handler_path_validator,
    )
    handler_path = Path(raw_path).expanduser().resolve()
    try:
        handler_source = handler_path.read_text(encoding="utf-8")
    except OSError as exc:
        error(
            f"Could not read {handler_path}: {exc}",
            code="wizard.unreadable_handler",
        )
        raise typer.Exit(code=1)

    # Step 2: run inference. Lazy import so the SDK still imports without core.
    try:
        from core.publish_inference import infer
    except ImportError:
        error(
            "core.publish_inference is not importable. Use scripted "
            "`aztea publish <file> --price ... --endpoint ...` instead.",
            code="wizard.inference_unavailable",
        )
        raise typer.Exit(code=2)
    spec = infer(handler_source, filename=handler_path.name).to_jsonable()

    # Step 3: prompt for each field — inferred value is the default.
    console.print()
    info(
        "Inferred values below — press Enter to accept, or type to override."
    )
    console.print()

    name = _p.ask("Agent name", default=str(spec["name"]))
    slug = _p.ask("Slug (URL path)", default=str(spec["slug"]))
    description = _p.ask("Description", default=str(spec["description"]))
    category = _p.ask("Category", default=str(spec["category"]))
    price_str = _p.ask(
        "Price per call (USD)",
        default=f"{spec['price_per_call_usd']:.2f}",
        validator=_p.price_validator,
    )
    endpoint_url = _p.ask(
        "Public HTTPS endpoint URL where you host this handler",
        validator=_p.url_validator,
    )
    tags_raw = _p.ask(
        "Tags (comma-separated)",
        default=",".join(spec.get("tags") or []),
        validator=_p.optional,
    )

    # Step 4: confirm + post.
    payload = {
        "name": name.strip(),
        "slug": slug.strip(),
        "description": description.strip(),
        "category": category.strip() or "developer-tools",
        "price_per_call_usd": float(price_str),
        "endpoint_url": endpoint_url.strip(),
        "tags": _parse_tags(tags_raw),
        "input_schema": spec["input_schema"],
        "output_schema": spec["output_schema"],
    }

    console.print()
    if not _p.confirm("Ready to publish?", default=True):
        console.print("  [muted]Cancelled; nothing was sent.[/muted]")
        raise typer.Exit(code=130)

    # Step 5: POST. Reuse the shared SDK Session via requests.
    import requests as _requests
    try:
        resp = _requests.post(
            f"{resolved_base.rstrip('/')}/registry/register",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30.0,
        )
    except _requests.RequestException as exc:
        error(
            f"Could not reach {resolved_base}: {exc}",
            code="wizard.backend_unreachable",
        )
        raise typer.Exit(code=1)

    if 200 <= resp.status_code < 300:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        success(
            f"Published {payload['slug']} — agent_id={body.get('agent_id', '?')}",
            detail=(
                f"Review status: {body.get('review_status', 'probation')} · "
                f"Listed at {resolved_base}/agents/{payload['slug']}"
            ),
        )
        raise typer.Exit(code=0)

    # Failure path — show the backend's error body if present.
    try:
        body = resp.json()
    except ValueError:
        body = {"http_status": resp.status_code, "body": resp.text[:500]}
    error(
        f"Backend rejected the publish (HTTP {resp.status_code}).",
        detail=json.dumps(body, indent=2)[:800],
        code="wizard.backend_error",
    )
    raise typer.Exit(code=1)


def _handler_path_validator(value: str) -> tuple[bool, str]:
    raw = (value or "").strip()
    if not raw:
        return False, "Path is required."
    path = Path(raw).expanduser()
    if not path.exists():
        return False, f"No file at {path}."
    if path.is_dir():
        return False, f"{path} is a directory; point at the .py file."
    if not str(path).endswith(".py"):
        return False, "Expected a .py file. Inference only handles Python handlers."
    return True, raw


__all__ = ["run_wizard", "remediation_for"]
