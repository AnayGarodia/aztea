from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def config_dir() -> Path:
    override = os.environ.get("AZTEA_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".aztea"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict[str, Any] | None:
    path = config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            disk = json.loads(path.read_text())
            if isinstance(disk, dict):
                raw = disk
        except (OSError, json.JSONDecodeError):
            raw = {}
    env_url = os.environ.get("AZTEA_BASE_URL")
    if env_url:
        raw["base_url"] = env_url
    env_key = os.environ.get("AZTEA_API_KEY")
    if env_key:
        raw["api_key"] = env_key
    return raw or None


def save_config(*, api_key: str, base_url: str, username: str) -> None:
    target_dir = config_dir()
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = config_path().with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "api_key": api_key,
                "base_url": base_url,
                "username": username,
            }
        )
    )
    tmp.replace(config_path())


def clear_config() -> None:
    config_path().unlink(missing_ok=True)
