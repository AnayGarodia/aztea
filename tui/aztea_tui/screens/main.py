from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import ContentSwitcher, Footer, ListItem, ListView, Static

from ..constants import NAV_ITEMS
from ..widgets.header_bar import HeaderBar


class MainScreen(Screen):
    BINDINGS = [
        Binding("1", "show_agents",    "Agents",    show=True),
        Binding("2", "show_jobs",      "Jobs",      show=True),
        Binding("3", "show_wallet",    "Wallet",    show=True),
        Binding("4", "show_my_agents", "My Agents", show=True),
        Binding("escape", "focus_nav", "Focus Nav", show=False),
        Binding("r", "refresh",        "Refresh",   show=True),
        Binding("q", "app.quit",       "Quit",      show=True),
    ]

    def __init__(self, username: str) -> None:
        super().__init__()
        self.username = username

    def compose(self) -> ComposeResult:
        yield HeaderBar(username=self.username)
        with Horizontal(id="app-layout"):
            with Vertical(id="sidebar"):
                yield ListView(
                    *[
                        ListItem(Static(label), id=f"nav-{view_id}", name=view_id)
                        for _, view_id, label in NAV_ITEMS
                    ],
                    id="nav-list",
                )
            with ContentSwitcher(initial="agents", id="content-area"):
                from ..views.agents import AgentBrowserView
                from ..views.jobs import JobListView
                from ..views.my_agents import MyAgentsView
                from ..views.wallet import WalletView

                yield AgentBrowserView(id="agents")
                yield JobListView(id="jobs")
                yield WalletView(id="wallet")
                yield MyAgentsView(id="my-agents")
        yield Footer()

    def on_mount(self) -> None:
        self._select_nav("agents")
        # Trigger initial data load
        self.call_after_refresh(self._load_current)

    def _select_nav(self, view_id: str) -> None:
        try:
            nav = self.query_one("#nav-list", ListView)
            for i, (_, vid, _) in enumerate(NAV_ITEMS):
                if vid == view_id:
                    nav.index = i
                    break
        except Exception:
            pass

    def _switch(self, view_id: str) -> None:
        self.query_one(ContentSwitcher).current = view_id
        self._select_nav(view_id)
        self._load_current()

    def _load_current(self) -> None:
        current_id = self.query_one(ContentSwitcher).current
        if current_id:
            try:
                widget = self.query_one(f"#{current_id}")
                if hasattr(widget, "load_data"):
                    widget.load_data()
            except Exception:
                pass

    def action_show_agents(self) -> None:    self._switch("agents")
    def action_show_jobs(self) -> None:      self._switch("jobs")
    def action_show_wallet(self) -> None:    self._switch("wallet")
    def action_show_my_agents(self) -> None: self._switch("my-agents")
    def action_refresh(self) -> None:        self._load_current()
    def action_focus_nav(self) -> None:
        self.query_one("#nav-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        view_id = event.item.name
        if view_id:
            self.query_one(ContentSwitcher).current = view_id
            self._load_current()
