from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, DataTable, LoadingIndicator, Static

from ..api import AzteaAPIError
from ..constants import STATUS_STYLES
from ..widgets.live_job import LiveJobWidget


class JobDetailPanel(Widget):
    def __init__(self) -> None:
        super().__init__(id="panel-right")

    def compose(self) -> ComposeResult:
        yield Static("Select a job →", id="detail-placeholder")

    def show_job(self, job_id: str) -> None:
        self.remove_children()
        self.mount(LiveJobWidget(job_id))


class JobListView(Widget):
    BINDINGS = [Binding("r", "refresh", "Refresh", show=False)]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._next_cursor: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="panel-left"):
                yield LoadingIndicator(id="job-loader")
                yield DataTable(id="job-table", cursor_type="row")
                yield Button("Load more ↓", id="btn-more")
                yield Static("", classes="empty-state", id="job-empty")
            yield JobDetailPanel()

    def on_mount(self) -> None:
        table = self.query_one("#job-table", DataTable)
        table.add_columns("ID", "Agent", "Status", "Created", "Cost")
        self.query_one("#btn-more", Button).display = False

    def load_data(self) -> None:
        self._load_jobs()

    @work(exclusive=True)
    async def _load_jobs(self) -> None:
        loader = self.query_one("#job-loader", LoadingIndicator)
        loader.display = True
        try:
            rows, cursor = await self.app.api.list_jobs()
        except AzteaAPIError as e:
            self.notify(f"Failed to load jobs: {e.message}", severity="error")
            return
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")
            return
        finally:
            loader.display = False

        self._next_cursor = cursor
        table = self.query_one("#job-table", DataTable)
        table.clear()

        if not rows:
            self.query_one("#job-empty", Static).update("No jobs yet. Hire an agent to create one.")
            self.query_one("#btn-more", Button).display = False
            return

        self.query_one("#job-empty", Static).update("")
        for r in rows:
            table.add_row(
                Text(r.short_id, style="dim"),
                Text(r.agent_id[:22]),
                Text(r.status, style=STATUS_STYLES.get(r.status, "")),
                Text(r.created_display),
                Text(r.cost_display, style="#00d4aa"),
                key=r.job_id,
            )
        self.query_one("#btn-more", Button).display = cursor is not None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        job_id = str(event.row_key.value)
        self.query_one(JobDetailPanel).show_job(job_id)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-more" and self._next_cursor:
            self._load_more()

    @work
    async def _load_more(self) -> None:
        try:
            rows, cursor = await self.app.api.list_jobs(cursor=self._next_cursor)
        except AzteaAPIError as e:
            self.notify(f"Load more failed: {e.message}", severity="error")
            return
        self._next_cursor = cursor
        table = self.query_one("#job-table", DataTable)
        for r in rows:
            table.add_row(
                Text(r.short_id, style="dim"),
                Text(r.agent_id[:22]),
                Text(r.status, style=STATUS_STYLES.get(r.status, "")),
                Text(r.created_display),
                Text(r.cost_display, style="#00d4aa"),
                key=r.job_id,
            )
        self.query_one("#btn-more", Button).display = cursor is not None
