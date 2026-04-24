from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from .config import clear_config, load_config
from .api import AzteaAPI


class AzteaApp(App):
    CSS_PATH = "aztea.tcss"
    TITLE = "Aztea"
    BINDINGS = [
        Binding("ctrl+q", "app.quit", "Quit", show=True, priority=True),
        Binding("ctrl+c", "app.quit", "Quit", show=False, priority=True),
        Binding("ctrl+l", "logout", "Logout", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.api: AzteaAPI | None = None

    def on_mount(self) -> None:
        config = load_config()
        if config is None:
            from .screens.login import LoginScreen
            self.push_screen(LoginScreen())
        else:
            self.api = AzteaAPI(config["api_key"], config["base_url"])
            from .screens.main import MainScreen
            self.push_screen(MainScreen(username=config.get("username", "")))

    async def action_logout(self) -> None:
        clear_config()
        if self.api:
            self.api.close()
            self.api = None
        from .screens.login import LoginScreen
        await self.switch_screen(LoginScreen())


def run() -> None:
    app = AzteaApp()
    app.run()
