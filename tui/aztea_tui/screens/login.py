from __future__ import annotations

import os

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from ..api import AzteaAPI, AzteaAPIError
from ..config import load_config, save_config

_DEFAULT_URL = os.environ.get("AZTEA_BASE_URL", "https://aztea.ai")


class LoginScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.quit", "Quit", show=True),
        Binding("tab", "toggle_mode", "Toggle Login Mode", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._key_mode = False

    def compose(self) -> ComposeResult:
        with Vertical(id="login-box"):
            yield Static("◆  AZTEA", id="login-logo")
            yield Static("AI Agent Marketplace", id="login-tagline")
            yield Input(placeholder="Email", id="email")
            yield Input(placeholder="Password", password=True, id="password")
            yield Input(placeholder="API Key  (az_...)", id="apikey", classes="hidden")
            with Horizontal(id="login-buttons"):
                yield Button("Login", variant="primary", id="btn-login")
                yield Button("Use API Key →", id="btn-toggle")
            yield Static("", id="login-error")

    # ── Mode toggle ───────────────────────────────────────────────────────────

    def _toggle_mode(self) -> None:
        self._key_mode = not self._key_mode
        self.query_one("#email", Input).display = not self._key_mode
        self.query_one("#password", Input).display = not self._key_mode
        self.query_one("#apikey", Input).display = self._key_mode
        self.query_one("#btn-toggle", Button).label = (
            "← Use Email" if self._key_mode else "Use API Key →"
        )
        self.query_one("#btn-login", Button).label = (
            "Connect" if self._key_mode else "Login"
        )

    def action_toggle_mode(self) -> None:
        self._toggle_mode()

    # ── Events ────────────────────────────────────────────────────────────────

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-toggle":
            self._toggle_mode()
        elif event.button.id == "btn-login":
            await self._do_login()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._do_login()

    # ── Login logic ───────────────────────────────────────────────────────────

    async def _do_login(self) -> None:
        error_label = self.query_one("#login-error", Static)
        error_label.update("")
        btn = self.query_one("#btn-login", Button)
        btn.disabled = True

        config = load_config()
        base_url = (config or {}).get("base_url", _DEFAULT_URL)
        api = AzteaAPI(None, base_url)

        try:
            if self._key_mode:
                key = self.query_one("#apikey", Input).value.strip()
                if not key:
                    error_label.update("[red]API key required.[/red]")
                    return
                username = await api.login_with_key(key)
                api.set_api_key(key)
                save_config(api_key=key, base_url=base_url, username=username)
            else:
                email = self.query_one("#email", Input).value.strip()
                password = self.query_one("#password", Input).value
                if not email or not password:
                    error_label.update("[red]Email and password required.[/red]")
                    return
                result = await api.login(email, password)
                api.set_api_key(result.api_key)
                save_config(
                    api_key=result.api_key,
                    base_url=base_url,
                    username=result.username,
                )

            self.app.api = api
            cfg = load_config() or {}
            from .main import MainScreen
            await self.app.switch_screen(MainScreen(username=cfg.get("username", "")))
        except AzteaAPIError as e:
            error_label.update(f"[red]{e.user_message}[/red]")
        except Exception:
            error_label.update(
                "[red]Unexpected login error. Please verify your URL, credentials, and network, then try again.[/red]"
            )
        finally:
            btn.disabled = False
