"""jobs: hire, status, cancel, rate, dispute, verify, estimate, follow."""
from __future__ import annotations

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
from .output import emit, info, spinner, success


def _open_client(**kwargs):
    """Defer to `aztea.cli._client` (patchable) when present."""
    from . import _client as _factory
    return _factory(**kwargs)


app = typer.Typer(help="Hire agents and inspect jobs.", no_args_is_help=True)


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
            success(
                f"Job complete  ${result.cost_cents/100:.2f}",
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
