from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, LoadingIndicator, Static

from ..api import AgentRow, AzteaAPIError
from ..constants import STATUS_STYLES


class MyAgentsView(Widget):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("My Agents", id="my-agents-heading")
            yield LoadingIndicator(id="my-loader")
            yield DataTable(id="my-table", cursor_type="row")
            yield Static("", classes="empty-state", id="my-empty")
            yield Static(
                "\n[dim]Register agents via the SDK or web dashboard at aztea.ai[/dim]",
                id="my-hint",
            )

    def on_mount(self) -> None:
        table = self.query_one("#my-table", DataTable)
        table.add_columns("Name", "Status", "Price", "Calls", "Trust", "Success")
        self.load_data()

    def load_data(self) -> None:
        self._load_my_agents()

    @work(exclusive=True)
    async def _load_my_agents(self) -> None:
        loader = self.query_one("#my-loader", LoadingIndicator)
        loader.display = True
        try:
            agents = await self.app.api.list_my_agents()
        except AzteaAPIError as e:
            self.notify(f"Could not load your agents: {e.message}", severity="error")
            return
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")
            return
        finally:
            loader.display = False

        table = self.query_one("#my-table", DataTable)
        table.clear()

        if not agents:
            self.query_one("#my-empty", Static).update(
                "You haven't registered any agents yet.\n"
                "Use the Python SDK or aztea.ai to register one."
            )
            return

        self.query_one("#my-empty", Static).update("")
        for a in agents:
            trust_style = "#10b981" if a.trust_score >= 70 else "#f59e0b"
            table.add_row(
                Text(a.name[:30]),
                Text(a.status, style=STATUS_STYLES.get(a.status, "")),
                Text(a.price_display, style="#00d4aa"),
                Text(f"{a.total_calls:,}"),
                Text(f"{a.trust_score:.0f}", style=trust_style),
                Text(f"{a.success_rate:.0%}"),
                key=a.agent_id,
            )
