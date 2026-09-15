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
    cfg.write_text("bot_token: yaml-token\nadmin_ids: [1, 2]\nupstream:\n  base_url: https://api.example.com\n")
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


@pytest.mark.parametrize("group, override", [("webhook", "PORT"), ("epay", "KEY")])
def test_partial_nested_environment_preserves_yaml(tmp_path, monkeypatch, group, override):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "webhook:\n  url: https://bot.example.com\n  path: /custom\n  secret_token: yaml-secret\n"
        "epay:\n  pid: '1000'\n  url: https://pay.example.com\n  type: wxpay\n"
    )
    monkeypatch.setenv("SHOP_BOT_CONFIG", str(cfg))
    monkeypatch.setenv(f"SHOP_BOT_{group.upper()}__{override}", "9000" if group == "webhook" else "env-secret")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.webhook.url == "https://bot.example.com"
        assert settings.webhook.path == "/custom"
        assert settings.webhook.secret_token == "yaml-secret"
        assert settings.epay.pid == "1000"
        assert settings.epay.url == "https://pay.example.com"
        assert settings.epay.type == "wxpay"
        if group == "webhook":
            assert settings.webhook.port == 9000
        else:
            assert settings.epay.key == "env-secret"
    finally:
        get_settings.cache_clear()
