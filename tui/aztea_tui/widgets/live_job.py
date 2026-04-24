from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Log, Pretty, Static, TabbedContent, TabPane

from ..api import AzteaAPIError
from ..constants import STATUS_STYLES, TERMINAL_STATUSES


class LiveJobWidget(Widget):
    job_status: reactive[str] = reactive("pending")

    def __init__(self, job_id: str) -> None:
        super().__init__()
        self.job_id = job_id
        self._poll_timer = None

    def compose(self) -> ComposeResult:
        yield Static("", id="job-status-badge")
        yield Static("", id="job-cost")
        with TabbedContent(initial="output"):
            with TabPane("Output", id="output"):
                yield Pretty({}, id="job-output")
            with TabPane("Input", id="input"):
                yield Pretty({}, id="job-input")
            with TabPane("Messages", id="messages"):
                yield Log(id="job-messages", highlight=True, max_lines=200)

    def on_mount(self) -> None:
        self._load_job()
        if self.job_status not in TERMINAL_STATUSES:
            self._poll_timer = self.set_interval(2, self._poll)

    def watch_job_status(self, status: str) -> None:
        try:
            badge = self.query_one("#job-status-badge", Static)
            style = STATUS_STYLES.get(status, "")
            badge.update(Text(f" {status.upper().replace('_', ' ')} ", style=style))
        except Exception:
            pass
        if status in TERMINAL_STATUSES and self._poll_timer:
            self._poll_timer.stop()
            self._poll_timer = None

    @work
    async def _load_job(self) -> None:
        try:
            job = await self.app.api.get_job(self.job_id)
        except AzteaAPIError as e:
            self.notify(e.user_message, severity="error")
            return
        except Exception:
            self.notify("Unexpected error while loading the job.", severity="error")
            return

        self.job_status = job.status
        try:
            self.query_one("#job-input", Pretty).update(job.input_payload)
            self.query_one("#job-cost", Static).update(
                f"Cost: {job.cost_display}  •  Created: {job.created_display}"
                + (f"  •  Done: {job.completed_display}" if job.status in TERMINAL_STATUSES else "")
            )
            if job.output_payload:
                self.query_one("#job-output", Pretty).update(job.output_payload)
            elif job.error_message:
                self.query_one("#job-output", Pretty).update({"error": job.error_message})
        except Exception:
            pass

        # Load message history
        self._load_messages()

        if job.status not in TERMINAL_STATUSES and self._poll_timer is None:
            self._poll_timer = self.set_interval(2, self._poll)

    @work
    async def _load_messages(self) -> None:
        try:
            msgs = await self.app.api.list_job_messages(self.job_id)
        except AzteaAPIError as e:
            self.query_one("#job-messages", Log).write_line(f"[error] {e.user_message}")
            return
        log = self.query_one("#job-messages", Log)
        for m in msgs:
            msg_type = m.get("type", "")
            payload = m.get("payload", {})
            log.write_line(f"[{msg_type}] {payload}")

    @work(exclusive=True)
    async def _poll(self) -> None:
        try:
            job = await self.app.api.get_job(self.job_id)
        except AzteaAPIError as e:
            self.query_one("#job-messages", Log).write_line(f"[poll] {e.user_message}")
            return
        self.job_status = job.status
        try:
            if job.output_payload:
                self.query_one("#job-output", Pretty).update(job.output_payload)
            elif job.error_message:
                self.query_one("#job-output", Pretty).update({"error": job.error_message})
        except Exception:
            pass
