"""wallet: balance, topup, connect, withdraw, withdrawals."""
from __future__ import annotations

import webbrowser
from typing import Optional

import typer

from .common import ApiKeyOpt, BaseUrlOpt, JsonOpt, build_client, handle_error
from .output import emit, info, kv_table, spinner, success


app = typer.Typer(help="Inspect and fund your wallet.", no_args_is_help=True)


@app.command()
def balance(
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Show your wallet balance."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Loading wallet", json_mode=json_mode):
                wallet = client.get_wallet()
            if json_mode:
                emit(wallet, json_mode=True)
                return
            kv_table(
                [
                    ("balance",  f"${wallet.balance_cents / 100:.2f}"),
                    ("currency", "USD"),
                    ("escrow",   f"${getattr(wallet, 'escrow_cents', 0) / 100:.2f}"),
                ],
                title="wallet",
            )
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def topup(
    amount: float = typer.Argument(..., help="Amount in dollars (e.g. 5.00)."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    open_browser: bool = typer.Option(True, help="Open the checkout URL in your browser."),
    json_mode: bool = JsonOpt,
) -> None:
    """Add credits via Stripe Checkout."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Creating checkout session", json_mode=json_mode):
                session = client.create_topup_session(round(amount * 100))
            url = session.get("checkout_url") if isinstance(session, dict) else None
            if open_browser and isinstance(url, str):
                webbrowser.open(url)
            if json_mode:
                emit(session, json_mode=True)
                return
            success(f"Top-up session ready  ${amount:.2f}")
            if isinstance(url, str):
                info(url)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def connect(
    return_url: Optional[str] = typer.Option(None, help="URL Stripe redirects to after onboarding."),
    open_browser: bool = typer.Option(True, help="Open the onboarding URL in your browser."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Start (or resume) Stripe Connect onboarding so you can withdraw."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Checking Stripe status", json_mode=json_mode):
                status_dict = client.get_connect_status()
            if status_dict.get("charges_enabled"):
                if json_mode:
                    emit({"already_connected": True, **status_dict}, json_mode=True)
                    return
                success("Already connected to Stripe.")
                return
            with spinner("Starting onboarding", json_mode=json_mode):
                session = client.start_connect_onboarding(return_url=return_url)
            url = str(session.get("onboarding_url") or "")
            if open_browser and url:
                webbrowser.open(url)
            if json_mode:
                emit(session, json_mode=True)
                return
            success("Stripe onboarding started")
            if url:
                info(url)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def withdraw(
    amount: float = typer.Argument(..., help="Amount in dollars (min $1.00, max $10,000)."),
    memo: Optional[str] = typer.Option(None, help="Optional memo recorded on the transfer."),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Transfer funds from your wallet to your connected Stripe account."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Processing withdrawal", json_mode=json_mode):
                result = client.withdraw(round(amount * 100), memo=memo)
            if json_mode:
                emit(result, json_mode=True)
                return
            success(f"Withdrawal queued  ${amount:.2f}")
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def withdrawals(
    limit: int = typer.Option(25, min=1, max=200),
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """List recent withdrawals to your connected Stripe account."""
    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Loading withdrawals", json_mode=json_mode):
                result = client.list_withdrawals(limit=limit)
            emit(result, json_mode=json_mode)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)
