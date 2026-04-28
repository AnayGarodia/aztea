from __future__ import annotations

import sys
from pathlib import Path

_SDK_PATH = Path(__file__).resolve().parents[1] / "sdks" / "python-sdk"
if str(_SDK_PATH) not in sys.path:
    sys.path.insert(0, str(_SDK_PATH))

from aztea.cli import app


if __name__ == "__main__":
    app()
