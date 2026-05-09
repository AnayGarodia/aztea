"""Shared pytest fixtures for the SDK tests.

Stubs the on-disk credential file so CLI tests don't have to. The CLI's
`load_config()` reads `$AZTEA_CONFIG_DIR/config.json`; without it,
`resolve_settings(require_api_key=True)` exits 1 with "No API key
configured".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stub_creds(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "_aztea_cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "api_key": "test-key",
                "base_url": "http://localhost:8000",
                "username": "alice",
            }
        )
    )
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(cfg_dir))
    # Optional env override for tests that want to assert the base-URL surfaces.
    monkeypatch.setenv("AZTEA_BASE_URL", "http://localhost:8000")
