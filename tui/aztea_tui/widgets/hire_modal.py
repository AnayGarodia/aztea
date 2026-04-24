from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from ..api import AgentDetail, AzteaAPIError


class HireModal(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Cancel", show=True)]

    def __init__(self, agent: AgentDetail) -> None:
        super().__init__()
        self.agent = agent

    def compose(self) -> ComposeResult:
        with Vertical(id="hire-dialog"):
            yield Static(f"Hire: {self.agent.name}", id="hire-title")
            yield Static(
                f"Price: {self.agent.price_display} per call  •  "
                f"Trust: {self.agent.trust_score:.0f}/100  •  "
                f"Success: {self.agent.success_rate:.0%}",
                id="hire-price",
            )
            yield Static("\nPayload (JSON):", id="hire-payload-label")
            yield TextArea("{}", language="json", id="payload-editor", theme="monokai")
            with Horizontal(id="hire-actions"):
                yield Button("Submit", variant="primary", id="btn-submit")
                yield Button("Cancel", id="btn-cancel")
            yield Static("", id="hire-result")
            yield Static("", id="hire-error")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss()
        elif event.button.id == "btn-submit":
            await self._submit()

    async def _submit(self) -> None:
        raw = self.query_one("#payload-editor", TextArea).text.strip()
        error_label = self.query_one("#hire-error", Static)
        result_label = self.query_one("#hire-result", Static)
        error_label.update("")

        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as e:
            error_label.update(f"[red]Invalid JSON: {e}[/red]")
            return

        btn = self.query_one("#btn-submit", Button)
        btn.disabled = True
        result_label.update("[yellow]Submitting…[/yellow]")

        try:
            result = await self.app.api.hire_agent(self.agent.agent_id, payload)
            pretty = json.dumps(result, indent=2)
            # Truncate very long outputs for display
            preview = pretty[:1200] + ("\n…" if len(pretty) > 1200 else "")
            result_label.update(f"[green]✓ Done[/green]\n\n[dim]{preview}[/dim]")
        except AzteaAPIError as e:
            error_label.update(f"[red]{e.user_message}[/red]")
            result_label.update("")
        except Exception:
            error_label.update("[red]Unexpected error while submitting this job.[/red]")
            result_label.update("")
        finally:
            btn.disabled = False
