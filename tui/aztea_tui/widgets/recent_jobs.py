from __future__ import annotations

from collections import deque

from textual import work
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Log, Static

from ..constants import TERMINAL_STATUSES


class RecentJobsPane(Widget):
    def __init__(self) -> None:
        super().__init__(id="recent-jobs-pane")
        self._active_job_id: str | None = None
        self._recent_lines: deque[str] = deque(maxlen=8)

    def compose(self) -> ComposeResult:
        yield Static("Recent jobs", id="recent-jobs-title")
        yield Static("Loading…", id="recent-jobs-list")
        yield Log(id="recent-jobs-log", max_lines=50, highlight=True)

    def on_mount(self) -> None:
        self.set_interval(8, self._refresh_jobs)
        self.call_after_refresh(self._refresh_jobs)

    @work(exclusive=True)
    async def _refresh_jobs(self) -> None:
        if not self.app.api:
            return
        try:
            rows, _ = await self.app.api.list_jobs(limit=5)
        except Exception:
            self.query_one("#recent-jobs-list", Static).update("Unable to load jobs.")
            return
        if not rows:
            self.query_one("#recent-jobs-list", Static).update("No jobs yet.")
            self._active_job_id = None
            return
        lines = [f"{row.short_id}  {row.status:<24} {row.cost_display}" for row in rows[:5]]
        self.query_one("#recent-jobs-list", Static).update("\n".join(lines))
        active = next((row for row in rows if row.status not in TERMINAL_STATUSES), None)
        if active and active.job_id != self._active_job_id:
            self._active_job_id = active.job_id
            self._tail_active_job(active.job_id)

    @work(exclusive=True)
    async def _tail_active_job(self, job_id: str) -> None:
        if not self.app.api:
            return
        log = self.query_one("#recent-jobs-log", Log)
        log.clear()
        self._recent_lines.clear()
        try:
            async for event in self.app.api.stream_job_messages(job_id):
                event_type = str(event.get("type") or "message")
                payload = event.get("payload") or {}
                line = f"[{job_id[:8]}] {event_type}: {payload}"
                self._recent_lines.append(line)
                log.clear()
                for item in self._recent_lines:
                    log.write_line(item)
        except Exception as exc:
            log.write_line(f"[stream] {exc}")
