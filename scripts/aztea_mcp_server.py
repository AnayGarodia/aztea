#!/usr/bin/env python3
"""Compat shim — the real Aztea MCP server lives in ``aztea.mcp.server``.

Pre-1.6.2 this file was the canonical Python MCP server (2840 lines).
1.6.2 consolidated the MCP surface into the ``aztea`` SDK package so
``pip install aztea`` ships a real server (no more npm/JS dependency).

Anything that still invokes ``python scripts/aztea_mcp_server.py``
keeps working through this shim. New callers should prefer:

    aztea mcp serve              # CLI wrapper
    aztea-mcp                    # console_script
    python -m aztea.mcp.server   # module form
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from a dev tree where the SDK isn't pip-installed.
_SDK_PATH = Path(__file__).resolve().parent.parent / "sdks" / "python-sdk"
if str(_SDK_PATH) not in sys.path:
    sys.path.insert(0, str(_SDK_PATH))

from aztea.mcp.server import main


if __name__ == "__main__":
    main()
