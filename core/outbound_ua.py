# SPDX-License-Identifier: Apache-2.0
"""
outbound_ua.py — outbound HTTP User-Agent helper.

# OWNS: the User-Agent string sent on every outbound request agents make to
#       third-party APIs (Wikipedia, SEC EDGAR, etc.).
# NOT OWNS: the User-Agent on hosted-client calls to api.aztea.ai — those
#       are intentionally branded "aztea-oss/1 hosted-client" so the hosted
#       side can identify OSS-instance traffic.
# INVARIANTS:
#   - Self-hosted instances must NOT impersonate the Aztea project on
#     outbound calls. Default UA is derived from SERVER_BASE_URL hostname,
#     not "aztea/1.0 (research-agent@aztea.dev)".
#   - The third-party services we call (SEC EDGAR especially) require a
#     contact email or URL. We give them the *operator's* SERVER_BASE_URL,
#     not ours, so abuse complaints reach the right operator.
# DECISIONS:
#   - The override env var is OUTBOUND_USER_AGENT (one knob, applies to all
#     outbound agents). Operators with a custom contact email set this.
#   - When SERVER_BASE_URL is unset, fall back to "aztea-oss/1 (+http://localhost)"
#     which is honest about being a self-hosted instance.

Use `outbound_user_agent()` (call-time, env-driven) instead of importing a
constant — env changes propagate without an import-cycle restart.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse


_DEFAULT_AGENT_NAME = "aztea-oss"
_DEFAULT_AGENT_VERSION = "1"


def outbound_user_agent() -> str:
    """Compose a User-Agent for outbound HTTP from a self-hosted instance.

    Resolution order:
      1. ``OUTBOUND_USER_AGENT`` env (the explicit override).
      2. ``aztea-oss/1 (+<host-or-url-from-SERVER_BASE_URL>)`` — read at
         call time so a server reload picks up new env values.
      3. ``aztea-oss/1 (+http://localhost)`` — last-resort default.
    """
    explicit = os.environ.get("OUTBOUND_USER_AGENT", "").strip()
    if explicit:
        return explicit
    base_url = os.environ.get("SERVER_BASE_URL", "").strip().rstrip("/")
    if base_url:
        host = urlparse(base_url).hostname or base_url
        return f"{_DEFAULT_AGENT_NAME}/{_DEFAULT_AGENT_VERSION} (+{base_url}; host={host})"
    return f"{_DEFAULT_AGENT_NAME}/{_DEFAULT_AGENT_VERSION} (+http://localhost)"
