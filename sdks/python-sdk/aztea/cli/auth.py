"""auth: login, logout, whoami."""
from __future__ import annotations

from typing import Optional

import typer

from ..config import clear_config, config_path, load_config, save_config
from .common import ApiKeyOpt, BaseUrlOpt, JsonOpt, build_client, handle_error
from .output import banner, emit, kv_table, spinner, success


def _new_client(**kwargs):
    """Resolve AzteaClient through the top-level package so patches apply."""
    from . import AzteaClient as _AzteaClient
    return _AzteaClient(**kwargs)


app = typer.Typer(help="Sign in, sign out, and inspect your account.", no_args_is_help=True)


@app.command()
def login(
    email: Optional[str] = typer.Option(None, help="Account email."),
    password: Optional[str] = typer.Option(None, help="Account password.", hide_input=True),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help="Use an existing az_ API key instead of password login.",
    ),
    base_url: str = typer.Option("https://aztea.ai", help="Aztea server base URL."),
    json_mode: bool = JsonOpt,
) -> None:
    """Sign in and save credentials to ~/.aztea/config.json."""
    try:
        with _new_client(base_url=base_url, api_key=api_key, client_id="aztea-cli-login") as client:
            if api_key:
                with spinner("Verifying key", json_mode=json_mode):
                    profile = client.auth.me()
                username = str(profile.get("username") or "")
                save_config(api_key=api_key, base_url=base_url, username=username)
                if json_mode:
                    emit({"username": username, "base_url": base_url, "saved": True}, json_mode=True)
                    return
                success(f"Signed in as {username or 'user'}", detail=base_url)
                return

            login_email = email or typer.prompt("Email")
            login_password = password or typer.prompt("Password", hide_input=True)
            with spinner("Signing in", json_mode=json_mode):
                data = client.auth.login(login_email, login_password)
            raw_key = str(data.get("raw_api_key") or "")
            username = str(data.get("username") or "")
            save_config(api_key=raw_key, base_url=base_url, username=username)
            if json_mode:
                emit({"username": username, "base_url": base_url, "saved": True}, json_mode=True)
                return
            success(f"Signed in as {username}", detail=base_url)
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)


@app.command()
def logout(json_mode: bool = JsonOpt) -> None:
    """Forget the saved API key."""
    clear_config()
    if json_mode:
        emit({"logged_out": True}, json_mode=True)
        return
    success("Logged out", detail=str(config_path()))


@app.command()
def whoami(
    api_key: Optional[str] = ApiKeyOpt,
    base_url: Optional[str] = BaseUrlOpt,
    json_mode: bool = JsonOpt,
) -> None:
    """Show the active account, masked API key, and wallet balance."""
    cfg = load_config() or {}
    if not cfg.get("api_key") and not api_key:
        if json_mode:
            emit({"signed_in": False}, json_mode=True)
            return
        from .output import warn
        warn("Not signed in. Run `aztea login`.")
        raise typer.Exit(code=1)

    try:
        with build_client(api_key=api_key, base_url=base_url) as client:
            with spinner("Loading account", json_mode=json_mode):
                profile = client.auth.me()
                wallet = None
                try:
                    wallet = client.get_wallet()
                except Exception:
                    pass

            key = api_key or cfg.get("api_key") or ""
            masked = (key[:9] + "…" + key[-4:]) if len(key) > 16 else "az_…"
            base = client.base_url

            if json_mode:
                emit(
                    {
                        "signed_in": True,
                        "username": profile.get("username"),
                        "email": profile.get("email"),
                        "scopes": profile.get("scopes"),
                        "api_key": masked,
                        "base_url": base,
                        "balance_cents": getattr(wallet, "balance_cents", None),
                    },
                    json_mode=True,
                )
                return

            banner("aztea", "current account")
            balance = (
                f"${(wallet.balance_cents / 100):.2f}"
                if wallet is not None
                else "—"
            )
            kv_table(
                [
                    ("user",     str(profile.get("username") or "—")),
                    ("email",    str(profile.get("email") or "—")),
                    ("scopes",   ", ".join(profile.get("scopes") or []) or "—"),
                    ("api key",  masked),
                    ("base url", base),
                    ("balance",  balance),
                ]
            )
    except typer.Exit:
        raise
    except Exception as exc:
        handle_error(exc)
