"""pipelines: run."""
from __future__ import annotations

import time
from typing import Optional

import typer

from .common import ApiKeyOpt, BaseUrlOpt, JsonOpt, build_client, handle_error, parse_input
from .output import emit, info, spinner, success


app = typer.Typer(help="Run multi-step pipelines.", no_args_is_help=True)


@app.command()
def run(
    pipeline_id: str,
    input_value: Optional[str] = typer.Option(
        None, "--input", help="@file.json, '-', inline JSON, or k=v pairs."
    ),
    poll_interval: float = typer.Option(2.0, help="Polling interval in seconds."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Run a saved pipeline and stream its progress."""
    try:
        payload = parse_input(input_value)
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Starting pipeline", json_mode=json_mode):
                created = client.run_pipeline(pipeline_id, payload)
            run_id = str(created.get("run_id") or "")
            if not json_mode:
                info(f"Pipeline run {run_id}")

            terminal = {"complete", "failed", "cancelled"}
            while True:
                status = client.get_pipeline_run(pipeline_id, run_id)
                emit(status, json_mode=json_mode)
                if str(status.get("status") or "") in terminal:
                    if not json_mode:
                        success(f"Pipeline {status.get('status')}", detail=run_id)
                    return
                time.sleep(max(0.2, poll_interval))
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)
