"""jobs: hire, status, cancel, rate, dispute, verify, estimate, follow."""
from __future__ import annotations

from typing import Any
from typing import Optional

import typer

from .common import (
    ApiKeyOpt,
    BaseUrlOpt,
    JsonOpt,
    build_client,
    find_agent_id,
    handle_error,
    parse_input,
)
from .output import CHECK, emit, info, kv_table, spinner, success


try:
    from rich.text import Text as _Text
    _HAS_RICH_TEXT = True
except ImportError:
    _HAS_RICH_TEXT = False
    _Text = str  # type: ignore[assignment,misc]

_STATUS_STYLES: dict[str, str] = {
    "pending":                "muted",
    "running":                "info",
    "complete":               "success",
    "completed":              "success",
    "failed":                 "error",
    "cancelled":              "muted",
    "awaiting_clarification": "warn",
}


def _status_text(status: str):
    if not _HAS_RICH_TEXT:
        return status
    style = _STATUS_STYLES.get(status.lower(), "default")
    return _Text(status, style=style)


def _open_client(**kwargs):
    """Defer to `aztea.cli._client` (patchable) when present."""
    from . import _client as _factory
    return _factory(**kwargs)


app = typer.Typer(help="Hire agents and inspect jobs.", no_args_is_help=True)


def _normalize_batch_specs(client, raw_specs: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_specs, list) or not raw_specs:
        raise typer.BadParameter("Batch input must be a non-empty JSON array.")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_specs):
        if not isinstance(item, dict):
            raise typer.BadParameter(f"jobs[{index}] must be an object.")
        raw_agent_id = str(item.get("agent_id") or "").strip()
        raw_slug = str(item.get("slug") or "").strip()
        if not raw_agent_id and not raw_slug:
            raise typer.BadParameter(f"jobs[{index}] needs agent_id or slug.")
        payload = item.get("input_payload", item.get("input", {}))
        if not isinstance(payload, dict):
            raise typer.BadParameter(f"jobs[{index}].input_payload must be an object.")
        spec: dict[str, Any] = {
            "agent_id": raw_agent_id or find_agent_id(client, raw_slug),
            "input_payload": payload,
        }
        for key in ("budget_cents", "max_attempts", "private_task"):
            if key in item:
                spec[key] = item[key]
        normalized.append(spec)
    return normalized


def _render_batch_trace(result: dict[str, Any]) -> None:
    trace = result.get("parallel_hire_trace") if isinstance(result, dict) else None
    jobs = (trace or {}).get("jobs") if isinstance(trace, dict) else None
    kv_table(
        [
            ("Batch", str(result.get("batch_id") or "-")),
            ("Mode", str(result.get("mode") or "parallel_marketplace_hire")),
            ("Specialists", str(result.get("count") or len(jobs or []))),
            ("Charged", f"${int(result.get('total_charged_cents') or result.get('total_price_cents') or 0) / 100:.2f}"),
            ("Next", str(result.get("next_step") or "Poll batch status.")),
        ],
        title="Parallel Hire",
    )
    if not jobs:
        return
    try:
        from rich.table import Table
        from .output import console
    except Exception:
        for item in jobs:
            typer.echo(
                f"- {item.get('agent_slug') or item.get('agent_id')}: "
                f"{item.get('status')} {item.get('job_id')}"
            )
        return
    table = Table(show_header=True, header_style="label", box=None, padding=(0, 1))
    table.add_column("Specialist", style="default")
    table.add_column("Job", style="muted")
    table.add_column("Status")
    table.add_column("Receipt")
    table.add_column("Charge", justify="right")
    for item in jobs:
        charge = int(item.get("charge_cents") or 0)
        receipt = item.get("receipt") if isinstance(item.get("receipt"), dict) else {}
        receipt_verified = isinstance(receipt, dict) and receipt.get("status") == "verified"
        status_raw = str(item.get("status") or "-")
        status_cell = _status_text(status_raw)
        receipt_cell = _Text(f"{CHECK} verified", style="gold") if receipt_verified else _Text(str(receipt.get("status") or "-"), style="muted")
        table.add_row(
            str(item.get("agent_slug") or item.get("agent_name") or item.get("agent_id") or "-"),
            str(item.get("job_id") or "-")[:12],
            status_cell,
            receipt_cell,
            f"${charge / 100:.2f}",
        )
    console.print(table)


def _call_agent(
    slug: str,
    input_value: str | None,
    *,
    api_key: str | None,
    base_url: str | None,
    json_mode: bool,
) -> None:
    try:
        payload = parse_input(input_value)
        with _open_client(api_key=api_key, base_url=base_url) as client:
            agent_id = find_agent_id(client, slug)
            with spinner(f"Hiring {slug}", json_mode=json_mode):
                result = client.hire(agent_id, payload)
            if json_mode:
                emit(
                    {
                        "job_id": result.job_id,
                        "cost_cents": result.cost_cents,
                        "output": result.output,
                    },
                    json_mode=True,
                )
                return
            receipt_verified = (
                isinstance(getattr(result, "receipt", None), dict)
                and result.receipt.get("status") == "verified"
            )
            receipt_tag = f"  {CHECK} receipt" if receipt_verified else ""
            success(
                f"Job complete  ${result.cost_cents/100:.2f}{receipt_tag}",
                detail=result.job_id,
            )
            emit(result.output, json_mode=False)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def hire(
    slug: str,
    input_value: Optional[str] = typer.Option(
        None, "--input", help="@file.json, '-', inline JSON, or k=v pairs."
    ),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Hire an agent and wait for the result."""
    _call_agent(slug, input_value, api_key=api_key, base_url=base_url, json_mode=json_mode)


@app.command(name="batch")
def batch(
    jobs_value: str = typer.Option(
        ...,
        "--jobs",
        help="JSON array, @file.json, or '-' with {slug|agent_id,input_payload} specs.",
    ),
    intent: Optional[str] = typer.Option(
        None,
        "--intent",
        help="One-line goal shown in the batch trace.",
    ),
    max_total_cents: Optional[int] = typer.Option(
        None,
        "--max-total-cents",
        min=0,
        help="Hard cap for the whole batch before any charge.",
    ),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Hire multiple independent specialists in parallel through Aztea rails."""
    try:
        raw_specs = parse_input(jobs_value)
        with build_client(api_key=api_key, base_url=base_url) as client:
            specs = _normalize_batch_specs(client, raw_specs)
            with spinner("Opening parallel marketplace hires", json_mode=json_mode):
                result = client.hire_batch(
                    specs,
                    intent=intent,
                    max_total_cents=max_total_cents,
                )
            if json_mode:
                emit(result, json_mode=True)
                return
            success(
                "Parallel hire submitted",
                detail=f"batch {result.get('batch_id', '-')}",
            )
            _render_batch_trace(result)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def status(
    job_id: str,
    full: bool = typer.Option(False, help="Include full output payload."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Get the current status of a job."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Loading job", json_mode=json_mode):
                job = client.get_job(job_id)
            data = job.full() if full else job
            emit(data, json_mode=json_mode)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def cancel(
    job_id: str,
    reason: Optional[str] = typer.Option(None, help="Optional one-line reason."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Abort an in-flight job. Pre-call charge is refunded."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Cancelling", json_mode=json_mode):
                result = client.cancel_job(job_id, reason=reason)
            if json_mode:
                emit(result, json_mode=True)
                return
            success("Cancelled", detail=job_id)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def rate(
    job_id: str,
    rating: int = typer.Argument(..., min=1, max=5, help="1–5 stars."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Submit a 1–5 star rating for a completed job."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Submitting rating", json_mode=json_mode):
                result = client.rate_job(job_id, rating)
            if json_mode:
                emit(result, json_mode=True)
                return
            success(f"Rated {rating}/5", detail=job_id)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def dispute(
    job_id: str,
    reason: str = typer.Option(..., help="Reason for the dispute."),
    evidence: Optional[str] = typer.Option(None, help="Optional evidence URL or text."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Open a dispute on a completed job. Triggers LLM-judge review."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Filing dispute", json_mode=json_mode):
                result = client.dispute_job(job_id, reason=reason, evidence=evidence)
            if json_mode:
                emit(result, json_mode=True)
                return
            success("Dispute filed", detail=job_id)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def verify(
    job_id: str,
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Cryptographically verify a job's signed receipt."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Verifying signature", json_mode=json_mode):
                result = client.verify_job(job_id)
            if json_mode:
                emit(result, json_mode=True)
                return
            verified = bool(result.get("verified"))
            if verified:
                success("Signature verified", detail=job_id)
            else:
                from .output import error
                error("Signature did NOT verify.", code="signature.invalid")
                raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def estimate(
    agent_id: str,
    input_value: Optional[str] = typer.Option(
        None, "--input", help="@file.json, '-', inline JSON, or k=v pairs."
    ),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Preview the all-in caller charge before hiring."""
    try:
        payload = parse_input(input_value)
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Estimating", json_mode=json_mode):
                result = client.estimate_cost(agent_id, payload)
            emit(result, json_mode=json_mode)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def follow(
    job_id: str,
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Stream live progress messages for a running job."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            if not json_mode:
                info(f"Following {job_id}")
            for event in client.jobs.stream_messages(job_id):
                emit(event, json_mode=json_mode)
            with spinner("Loading final status", json_mode=json_mode):
                final_job = client.get_job(job_id)
            emit(final_job, json_mode=json_mode)
    except KeyboardInterrupt:
        raise typer.Exit(code=130)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)
