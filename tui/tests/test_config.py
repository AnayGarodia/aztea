import importlib


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(tmp_path))
    import aztea_tui.config as cfg_mod
    importlib.reload(cfg_mod)
    cfg_mod.save_config(api_key="az_abc", base_url="http://localhost:8000", username="alice")
    cfg = cfg_mod.load_config()
    assert cfg is not None
    assert cfg["api_key"] == "az_abc"
    assert cfg["username"] == "alice"
    assert cfg["base_url"] == "http://localhost:8000"


def test_missing_config_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(tmp_path))
    import aztea_tui.config as cfg_mod
    importlib.reload(cfg_mod)
    assert cfg_mod.load_config() is None


def test_corrupt_config_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text("not json")
    import aztea_tui.config as cfg_mod
    importlib.reload(cfg_mod)
    assert cfg_mod.load_config() is None


def test_env_var_overrides_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AZTEA_BASE_URL", "https://aztea.ai")
    import aztea_tui.config as cfg_mod
    importlib.reload(cfg_mod)
    cfg_mod.save_config(api_key="az_x", base_url="http://localhost:8000", username="bob")
    cfg = cfg_mod.load_config()
    assert cfg is not None
    assert cfg["base_url"] == "https://aztea.ai"


def test_clear_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(tmp_path))
    import aztea_tui.config as cfg_mod
    importlib.reload(cfg_mod)
    cfg_mod.save_config(api_key="az_x", base_url="http://localhost:8000", username="bob")
    assert cfg_mod.load_config() is not None
    cfg_mod.clear_config()
    assert cfg_mod.load_config() is None
