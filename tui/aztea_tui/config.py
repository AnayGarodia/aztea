from __future__ import annotations

import json
import os
from pathlib import Path


def _config_dir() -> Path:
    override = os.environ.get("AZTEA_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".aztea"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def load_config() -> dict | None:
    path = _config_path()
    if not path.exists():
        return None
    try:
        cfg: dict = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    env_url = os.environ.get("AZTEA_BASE_URL")
    if env_url:
        cfg["base_url"] = env_url
    return cfg


def save_config(*, api_key: str, base_url: str, username: str) -> None:
    d = _config_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = _config_path().with_suffix(".tmp")
    tmp.write_text(json.dumps({"api_key": api_key, "base_url": base_url, "username": username}))
    tmp.replace(_config_path())


def clear_config() -> None:
    _config_path().unlink(missing_ok=True)
