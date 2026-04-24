from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, LoadingIndicator, Static

from ..api import AzteaAPIError


class WalletView(Widget):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Wallet", id="wallet-heading")
            yield LoadingIndicator(id="wallet-loader")
            yield Static("", id="wallet-balance")
            yield Static("", id="wallet-trust")
            yield Button("+ Deposit", variant="primary", id="btn-deposit")
            yield Static("Recent charges:", id="charges-heading")
            yield DataTable(id="charges-table", cursor_type="row")
            yield Static("", classes="empty-state", id="wallet-empty")

    def on_mount(self) -> None:
        table = self.query_one("#charges-table", DataTable)
        table.add_columns("Job ID", "Agent", "Cost", "When")

    def load_data(self) -> None:
        self._load_wallet()

    @work(exclusive=True)
    async def _load_wallet(self) -> None:
        loader = self.query_one("#wallet-loader", LoadingIndicator)
        loader.display = True
        try:
            wallet = await self.app.api.get_wallet()
            jobs, _ = await self.app.api.list_jobs(limit=25)
        except AzteaAPIError as e:
            self.notify(f"Wallet error: {e.message}", severity="error")
            return
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")
            return
        finally:
            loader.display = False

        self.query_one("#wallet-balance", Static).update(
            f"[bold #00d4aa]Balance  {wallet.balance_display}[/]"
        )
        trust_str = f"{wallet.trust:.2f}" if wallet.trust is not None else "—"
        self.query_one("#wallet-trust", Static).update(f"[dim]Trust    {trust_str}[/]")

        table = self.query_one("#charges-table", DataTable)
        table.clear()
        charged = [j for j in jobs if j.cost_display != "$0.00"]
        if not charged:
            self.query_one("#wallet-empty", Static).update("No charges yet.")
        else:
            self.query_one("#wallet-empty", Static).update("")
            for j in charged:
                table.add_row(
                    Text(j.short_id, style="dim"),
                    Text(j.agent_id[:24]),
                    Text(j.cost_display, style="#00d4aa"),
                    Text(j.created_display),
                    key=j.job_id,
                )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-deposit":
            wallet = None
            try:
                wallet = await self.app.api.get_wallet()
            except Exception:
                pass
            self.notify(
                "To deposit funds, visit aztea.ai/wallet — Stripe-powered top-up.",
                severity="information",
                timeout=6,
            )
