"""dispute: file or check a job dispute from the terminal.

Usage:
    aztea dispute                                   # interactive picker
    aztea dispute <job_id> --reason "..."           # direct
    aztea dispute <job_id> --reason "..." --yes     # scripted (no confirm)
    aztea dispute --status <job_id>                 # check existing dispute
    aztea dispute <job_id> --reason "..." --dry-run # preview cost only

The wizard fetches the user's recent disputable jobs, shows them as a
numbered list with eligibility reasons, and walks the buyer through
reason + optional evidence + confirmation. The exact filing deposit is
quoted from `GET /ops/dispute-policy` so the user knows what they're
committing to before any money moves.

Same `_file_dispute()` core is reused by `aztea jobs dispute` so the
back-compat sub-app and the new top-level command cannot drift.
"""
from __future__ import annotations

from typing import Any, Optional

import typer

from .common import (
    ApiKeyOpt,
    BaseUrlOpt,
    JsonOpt,
    handle_error,
)


def _open_client(**kwargs):
    """Defer to `aztea.cli._client` (patchable) so tests can monkeypatch it."""
    from . import _client as _factory

    return _factory(**kwargs)
from .output import (
    console,
    emit,
    err_console,
    info,
    kv_table,
    spinner,
    success,
)


# Map structured server error codes to one-line user-facing remediation.
# `handle_error` looks up the AzteaError's `code` attribute (set by the
# SDK from the `error.code` field of the JSON response body) and renders
# the corresponding hint when present.
_DISPUTE_ERROR_HINTS: dict[str, str] = {
    "dispute.window_expired": (
        "The dispute window for this job has closed. Disputes must be "
        "filed within the window after the job completes."
    ),
    "dispute.already_filed": (
        "A dispute already exists. Run `aztea dispute --status <job_id>` "
        "to see its current state."
    ),
    "dispute.already_rated": (
        "You already rated this job. Disputes must be filed before a "
        "quality rating is submitted."
    ),
    "dispute.not_completed": (
        "This job hasn't finished yet. Wait for it to complete (or fail) "
        "before filing a dispute."
    ),
    "dispute.invalid_window": (
        "Dispute window could not be computed for this job. Contact "
        "support with the job_id."
    ),
    "dispute.filing_deposit_insufficient_balance": (
        "Insufficient balance for the filing deposit. Top up with "
        "`aztea wallet topup --amount <usd>` and retry."
    ),
    "dispute.clawback_insufficient_balance": (
        "The agent's escrow couldn't be locked. This usually clears in a "
        "minute — retry once."
    ),
    "job.self_dispute_not_allowed": (
        "You can't dispute a job served by an agent you own."
    ),
}


def dispute(
    job_id: Optional[str] = typer.Argument(
        None,
        help="Job ID to dispute. Omit to launch the interactive picker.",
    ),
    reason: Optional[str] = typer.Option(
        None,
        "--reason",
        help="Why the result fell short. Required for non-interactive use.",
    ),
    evidence: Optional[str] = typer.Option(
        None,
        "--evidence",
        help="Optional URL or text supporting your claim.",
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help="Show the current state of an existing dispute for <job_id>.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the deposit/escrow preview but do not file.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the final confirmation prompt.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        max=200,
        help="Maximum number of recent jobs the picker considers.",
    ),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Open a dispute on a recent job — pick from a list or pass a job_id."""
    # Status-check short-circuit — no filing, no confirm prompt.
    if status:
        if not job_id:
            console.print(
                "[error]--status requires a job_id:[/error] "
                "[code]aztea dispute <job_id> --status[/code]"
            )
            raise typer.Exit(code=2)
        try:
            with _open_client(api_key=api_key, base_url=base_url) as client:
                with spinner("Loading dispute", json_mode=json_mode):
                    record = client.get_dispute(job_id)
                _emit_dispute_status(record, json_mode=json_mode)
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001 — funnel through standard error UX
            _handle_dispute_error(exc)
        return

    # Pick: explicit job_id, or wizard.
    if job_id is None:
        from . import dispute_wizard as _wizard

        try:
            picked = _wizard.run_wizard(
                api_key=api_key,
                base_url=base_url,
                json_mode=json_mode,
                limit=limit,
            )
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001
            _handle_dispute_error(exc)
            return
        job_id, reason, evidence = picked

    # If user gave a job_id but no reason, prompt interactively.
    if not reason:
        from rich.prompt import Prompt

        if json_mode:
            err_console.print(
                "[error]✗[/error] --json mode requires --reason."
            )
            raise typer.Exit(code=2)
        if not _is_tty():
            err_console.print(
                "[error]✗[/error] --reason is required when not on a TTY."
            )
            raise typer.Exit(code=2)
        console.print()
        console.print("[label]Reason for the dispute[/label]")
        console.print(
            "  [muted]Why did the result fail to meet your expectations?[/muted]"
        )
        reason = Prompt.ask("[teal]>[/teal]", console=console).strip()
        if not reason:
            err_console.print("[error]✗[/error] Reason is required.")
            raise typer.Exit(code=2)
        if evidence is None:
            console.print()
            console.print("[label]Optional evidence[/label]")
            console.print(
                "  [muted]URL or text supporting your claim. Press Enter to skip.[/muted]"
            )
            ev = Prompt.ask("[teal]>[/teal]", default="", console=console).strip()
            evidence = ev or None

    try:
        with _open_client(api_key=api_key, base_url=base_url) as client:
            policy = _fetch_policy_quietly(client)
            _file_dispute(
                client=client,
                job_id=job_id,
                reason=reason,
                evidence=evidence,
                yes=yes,
                dry_run=dry_run,
                json_mode=json_mode,
                policy=policy,
            )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _handle_dispute_error(exc)


def _file_dispute(
    *,
    client: Any,
    job_id: str,
    reason: str,
    evidence: str | None,
    yes: bool,
    dry_run: bool,
    json_mode: bool,
    policy: dict[str, Any] | None,
) -> None:
    """Render preview, confirm, and file. Shared between top-level and
    `aztea jobs dispute` to guarantee identical behavior."""
    job_price_cents = _safe_int(_lookup_job_price_cents(client, job_id))
    deposit_cents = _estimate_deposit_cents(job_price_cents, policy)

    # Preview: skipped in --json mode (machine output stays clean) and
    # when --yes is passed (caller has already consented).
    if not json_mode and not yes:
        _render_preview(
            job_id=job_id,
            reason=reason,
            evidence=evidence,
            deposit_cents=deposit_cents,
            agent_payout_cents=_estimate_agent_payout_cents(job_price_cents),
            policy=policy,
        )

    if dry_run:
        if json_mode:
            emit(
                {
                    "ok": True,
                    "dry_run": True,
                    "job_id": job_id,
                    "reason": reason,
                    "evidence": evidence,
                    "estimated_deposit_cents": deposit_cents,
                },
                json_mode=True,
            )
            return
        info("Dry run — no dispute filed.")
        return

    if not yes and not json_mode:
        from rich.prompt import Confirm

        if not _is_tty():
            err_console.print(
                "[error]✗[/error] Confirmation prompt requires a TTY. "
                "Pass --yes for non-interactive use."
            )
            raise typer.Exit(code=2)
        confirmed = Confirm.ask(
            "[label]File this dispute?[/label]", default=False, console=console
        )
        if not confirmed:
            info("Cancelled — no dispute filed.")
            return

    with spinner("Filing dispute", json_mode=json_mode):
        receipt = client.dispute_job(job_id, reason=reason, evidence=evidence)
    _emit_dispute_receipt(receipt, job_id=job_id, json_mode=json_mode)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _render_preview(
    *,
    job_id: str,
    reason: str,
    evidence: str | None,
    deposit_cents: int,
    agent_payout_cents: int,
    policy: dict[str, Any] | None,
) -> None:
    judges_required = (policy or {}).get("judges_required", 2)
    judges_total = (policy or {}).get("judges_total", 3)
    rows: list[tuple[str, str]] = [
        ("job_id", job_id),
        ("reason", _truncate(reason, 80)),
    ]
    if evidence:
        rows.append(("evidence", _truncate(evidence, 80)))
    rows.extend(
        [
            ("filing deposit", f"${deposit_cents / 100:.2f}  (refunded if you win)"),
            (
                "agent escrow lock",
                f"~${agent_payout_cents / 100:.2f}  (held until judges decide)",
            ),
            (
                "judge panel",
                f"{judges_required}-of-{judges_total} LLM + admin tie-break",
            ),
        ]
    )
    console.print()
    kv_table(rows, title="Dispute preview")
    console.print()


def _emit_dispute_receipt(
    receipt: dict[str, Any], *, job_id: str, json_mode: bool
) -> None:
    dispute_id = receipt.get("dispute_id") or receipt.get("id") or ""
    if json_mode:
        emit(
            {
                "ok": True,
                "job_id": job_id,
                "dispute_id": dispute_id,
                "status": receipt.get("status"),
                "raw": receipt,
            },
            json_mode=True,
        )
        return
    console.print()
    success("Dispute filed", detail=dispute_id or "—")
    rows: list[tuple[str, str]] = [
        ("dispute_id", dispute_id or "—"),
        ("status", str(receipt.get("status") or "pending")),
        ("side", str(receipt.get("side") or "—")),
    ]
    deposit = receipt.get("filing_deposit_cents")
    if isinstance(deposit, int):
        rows.append(("deposit", f"${deposit / 100:.2f}"))
    kv_table(rows)
    console.print()
    console.print(
        f"[muted]Track with[/muted] [code]aztea dispute --status {job_id}[/code]"
    )
    console.print()


def _emit_dispute_status(record: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        emit(record, json_mode=True)
        return
    judgments = record.get("judgments") or []
    judge_votes = sum(1 for j in judgments if isinstance(j, dict) and j.get("vote"))
    rows: list[tuple[str, str]] = [
        ("dispute_id", str(record.get("dispute_id") or "—")),
        ("job_id", str(record.get("job_id") or "—")),
        ("status", str(record.get("status") or "—")),
        ("side", str(record.get("side") or "—")),
        ("filed_at", str(record.get("filed_at") or "—")),
        ("judges", f"{judge_votes} voted"),
    ]
    deposit = record.get("filing_deposit_cents")
    if isinstance(deposit, int):
        rows.append(("deposit", f"${deposit / 100:.2f}"))
    outcome = record.get("outcome")
    if outcome:
        rows.append(("outcome", str(outcome)))
    evidence = record.get("evidence")
    if evidence:
        rows.append(("evidence", _truncate(str(evidence), 80)))
    console.print()
    kv_table(rows, title="Dispute status")
    console.print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_policy_quietly(client: Any) -> dict[str, Any] | None:
    # Soft-fail: if the policy endpoint isn't reachable (older server), the
    # preview falls back to "estimated" wording rather than crashing.
    try:
        return dict(client.get_dispute_policy() or {})
    except Exception:  # noqa: BLE001 — policy is informational, never blocking
        return None


def _lookup_job_price_cents(client: Any, job_id: str) -> int | None:
    try:
        job = client.get_job(job_id)
    except Exception:  # noqa: BLE001 — best-effort price preview
        return None
    # SDK returns a `JobRecord` dataclass for typed access, or a dict for
    # raw responses. Handle both.
    for attr in ("price_cents", "caller_charge_cents"):
        value = getattr(job, attr, None)
        if value is None and isinstance(job, dict):
            value = job.get(attr)
        if value is not None:
            return _safe_int(value)
    return None


def _estimate_deposit_cents(
    job_price_cents: int | None, policy: dict[str, Any] | None
) -> int:
    if not job_price_cents or job_price_cents <= 0:
        return _safe_int((policy or {}).get("filing_deposit_min_cents")) or 5
    bps = _safe_int((policy or {}).get("filing_deposit_bps")) or 500
    min_cents = _safe_int((policy or {}).get("filing_deposit_min_cents")) or 5
    return max(min_cents, (job_price_cents * bps) // 10_000)


def _estimate_agent_payout_cents(job_price_cents: int | None) -> int:
    # Agent share is 90% of the job price (10% platform fee). The CLI
    # surfaces this as the "escrow lock" so the user understands the
    # magnitude of the clawback they're triggering.
    if not job_price_cents or job_price_cents <= 0:
        return 0
    return (job_price_cents * 90) // 100


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _is_tty() -> bool:
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


def _handle_dispute_error(exc: Exception) -> None:
    """Map dispute-specific error codes to friendly hints, then fall back
    to the generic CLI error funnel.

    Order matters: the structured-shortfall branch must run BEFORE the
    generic per-code branch, since insufficient-balance errors carry the
    same `code` but only the structured detail has the exact deficit
    needed to render an actionable top-up amount.
    """
    from .output import error as _print_error

    # Resolve the structured error code from either the exception's `code`
    # attribute (set by SDK from the response body's `error.code`) OR from
    # the FastAPI HTTPException-shaped detail dict (`{"detail": {"error": ...}}`)
    # that the SDK preserves verbatim.
    detail = getattr(exc, "detail", None) or getattr(exc, "body", None)
    nested: dict | None = None
    if isinstance(detail, dict):
        nested = detail.get("detail") if isinstance(detail.get("detail"), dict) else detail
    nested_error = nested.get("error") if isinstance(nested, dict) else None
    code = (
        getattr(exc, "code", None)
        or getattr(exc, "error_code", None)
        or nested_error
    )

    # Shortfall override fires only for the *filing-deposit* case — that's
    # the one the user can fix by topping up. Clawback-insufficient touches
    # the agent's wallet, not the user's; the generic hint is correct there.
    if (
        code == "dispute.filing_deposit_insufficient_balance"
        and isinstance(nested, dict)
    ):
        shortfall_cents = max(
            0,
            _safe_int(nested.get("required_cents"))
            - _safe_int(nested.get("balance_cents")),
        )
        hint = _DISPUTE_ERROR_HINTS[code]
        if shortfall_cents > 0:
            hint = (
                f"Top up at least ${shortfall_cents / 100:.2f} with "
                f"`aztea wallet topup --amount {shortfall_cents / 100:.2f}` and retry."
            )
        _print_error(str(exc) or code, hint=hint, code=code)
        raise typer.Exit(code=1)

    if isinstance(code, str) and code in _DISPUTE_ERROR_HINTS:
        message = str(exc) or _DISPUTE_ERROR_HINTS[code]
        _print_error(message, hint=_DISPUTE_ERROR_HINTS[code], code=code)
        raise typer.Exit(code=1)

    handle_error(exc)


__all__ = ["dispute", "_file_dispute"]
