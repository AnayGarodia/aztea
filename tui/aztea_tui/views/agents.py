from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, LoadingIndicator, Static
from textual import work

from ..api import AgentDetail, AzteaAPIError
from ..constants import STATUS_STYLES


class AgentDetailPanel(Widget):
    def __init__(self) -> None:
        super().__init__(id="panel-right")
        self._agent: AgentDetail | None = None

    def compose(self) -> ComposeResult:
        yield Static("Select an agent →", id="detail-placeholder")

    def show(self, agent: AgentDetail) -> None:
        self._agent = agent
        self.remove_children()
        self.mount(Static(agent.name, id="detail-name"))
        self.mount(Static(agent.description[:200], id="detail-desc"))
        self.mount(Static(f"\nPrice     {agent.price_display}", id="detail-price"))
        self.mount(Static(f"Trust     {agent.trust_score:.0f}/100", id="detail-meta"))
        self.mount(Static(f"Success   {agent.success_rate:.0%}", id="detail-meta"))
        self.mount(Static(f"Calls     {agent.total_calls:,}", id="detail-meta"))
        self.mount(Static(f"Status    {agent.status}", id="detail-meta"))
        self.mount(Static(f"Tags      {', '.join(agent.tags) or '-'}\n", id="detail-tags"))
        self.mount(Button("[h] Hire", variant="primary", id="btn-hire"))

    def get_agent(self) -> AgentDetail | None:
        return self._agent


class AgentBrowserView(Widget):
    BINDINGS = [
        Binding("h",     "hire",         "Hire",   show=True),
        Binding("/",     "focus_search", "Search", show=True),
        Binding("enter", "select",       "View",   show=False),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="panel-left"):
                yield Input(placeholder="/ filter by tag…", id="agent-search")
                yield LoadingIndicator(id="agent-loader")
                yield DataTable(id="agent-table", cursor_type="row")
                yield Static("", classes="empty-state", id="agent-empty")
            yield AgentDetailPanel()

    def on_mount(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.add_columns("Name", "Price", "Trust", "Success", "Status")
        table.cursor_type = "row"

    def load_data(self, tag: str | None = None) -> None:
        self._load_agents(tag=tag)

    @work(exclusive=True)
    async def _load_agents(self, tag: str | None = None) -> None:
        loader = self.query_one("#agent-loader", LoadingIndicator)
        loader.display = True
        self.query_one("#agent-empty", Static).update("")
        try:
            agents = await self.app.api.list_agents(tag=tag or None)
        except AzteaAPIError as e:
            self.query_one("#agent-empty", Static).update(f"[red]{e.user_message}[/red]")
            self.notify(e.message, severity="error")
            return
        except Exception:
            self.query_one("#agent-empty", Static).update(
                "[red]Unexpected error while loading agents. Please refresh.[/red]"
            )
            self.notify("Unexpected error while loading agents.", severity="error")
            return
        finally:
            loader.display = False

        table = self.query_one("#agent-table", DataTable)
        table.clear()
        if not agents:
            self.query_one("#agent-empty", Static).update("No agents found. Try a different tag.")
            return
        for a in agents:
            table.add_row(
                Text(a.name[:28]),
                Text(a.price_display, style="#00d4aa"),
                Text(f"{a.trust_score:.0f}", style="#10b981" if a.trust_score >= 70 else "#f59e0b"),
                Text(f"{a.success_rate:.0%}"),
                Text(a.status, style=STATUS_STYLES.get(a.status, "")),
                key=a.agent_id,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        agent_id = str(event.row_key.value)
        self._load_detail(agent_id)

    @work
    async def _load_detail(self, agent_id: str) -> None:
        try:
            detail = await self.app.api.get_agent(agent_id)
        except AzteaAPIError as e:
            self.notify(e.user_message, severity="error")
            return
        self.query_one(AgentDetailPanel).show(detail)

    def action_hire(self) -> None:
        agent = self.query_one(AgentDetailPanel).get_agent()
        if agent is None:
            self.notify("Select an agent first (↑↓, then h).", severity="warning")
            return
        from ..widgets.hire_modal import HireModal
        self.app.push_screen(HireModal(agent))

    def action_focus_search(self) -> None:
        self.query_one("#agent-search", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-hire":
            self.action_hire()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "agent-search":
            self.load_data(tag=event.value.strip() or None)
