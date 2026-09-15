from pathlib import Path

import pytest

from shop_bot.config import Settings, get_settings


def test_defaults_load():
    s = Settings()
    assert s.bot_token == ""
    assert s.admin_ids == []
    assert s.database_path == "shop_bot.db"
    assert s.upstream.base_url == ""


def test_yaml_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "bot_token: yaml-token\n"
        "admin_ids: [1, 2]\n"
        "upstream:\n"
        "  base_url: https://api.example.com\n"
    )
    monkeypatch.setenv("SHOP_BOT_CONFIG", str(cfg))
    monkeypatch.delenv("SHOP_BOT_BOT_TOKEN", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.bot_token == "yaml-token"
    assert s.admin_ids == [1, 2]
    assert s.upstream.base_url == "https://api.example.com"
    get_settings.cache_clear()


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("bot_token: yaml-token\n")
    monkeypatch.setenv("SHOP_BOT_CONFIG", str(cfg))
    monkeypatch.setenv("SHOP_BOT_BOT_TOKEN", "env-token")
    get_settings.cache_clear()
    s = get_settings()
    assert s.bot_token == "env-token"
    get_settings.cache_clear()
