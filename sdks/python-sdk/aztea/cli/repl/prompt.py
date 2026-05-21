"""Theme accent + helpers for the REPL.

# OWNS: the single brand-accent colour shared by the input frame, the
#        prompt symbol, and the wordmark — kept in one place so a future
#        theme tweak doesn't drift across files.
# NOT OWNS: the actual input rendering. The full-screen Application
#        in app.py uses prompt_toolkit's ``Frame`` widget to draw the
#        rectangle around the input field — we no longer need
#        prompt_message / right_prompt / bottom_toolbar callables.
# DECISIONS:
#   - V5 dropped the bottom_toolbar entirely. Earlier iterations used the
#     toolbar to render the bottom border of the input box; now the
#     Frame widget owns all four borders and the toolbar is gone (it had
#     been showing as a yellow / cream strip in some terminals because
#     prompt_toolkit's default toolbar style overrode our colours).
#   - Auth state ("Signed in as alice") is exposed on demand via /whoami
#     and /status. There is no persistent status line in the chrome.
"""
from __future__ import annotations

from ..splash import _detect_terminal_mode


# Single-accent colour for the input frame, matched to terminal theme.
_ACCENT_DARK = "#7EB9B0"
_ACCENT_LIGHT = "#063F43"


def _accent() -> str:
    return _ACCENT_LIGHT if _detect_terminal_mode() == "light" else _ACCENT_DARK
