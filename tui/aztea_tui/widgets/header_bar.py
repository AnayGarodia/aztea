from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ..api import AzteaAPIError


class HeaderBar(Widget):
    balance: reactive[str] = reactive("$--.--")
    connected: reactive[bool] = reactive(True)

    def __init__(self, username: str) -> None:
        super().__init__(id="header-bar")
        self.username = username

    def compose(self) -> ComposeResult:
        yield Static("◆  AZTEA", id="header-logo")
        yield Static(id="header-spacer")
        yield Static(self.username, id="header-user")
        yield Static(self.balance, id="header-bal")
        yield Static("●", id="header-conn")

    def watch_balance(self, val: str) -> None:
        try:
            self.query_one("#header-bal", Static).update(val)
        except Exception:
            pass

    def watch_connected(self, val: bool) -> None:
        try:
            conn = self.query_one("#header-conn", Static)
            conn.update("●" if val else "○")
            conn.styles.color = "#10b981" if val else "#ef4444"
        except Exception:
            pass

    def on_mount(self) -> None:
        self.set_interval(30, self._refresh_balance)
        self.call_after_refresh(self._refresh_balance)

    async def _refresh_balance(self) -> None:
        if not self.app.api:
            return
        try:
            wallet = await self.app.api.get_wallet()
            self.balance = wallet.balance_display
            self.connected = True
        except AzteaAPIError:
            self.connected = False
        except Exception:
            self.connected = False
