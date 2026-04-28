from __future__ import annotations

from pathlib import Path
import sys

_SDK_PATH = Path(__file__).resolve().parents[2] / "sdks" / "python-sdk"
if str(_SDK_PATH) not in sys.path:
    sys.path.insert(0, str(_SDK_PATH))

from aztea.config import clear_config, load_config, save_config  # noqa: E402,F401
