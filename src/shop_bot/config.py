"""配置加载。

默认从工作目录下的 ``config.yaml`` 读取；可用 ``SHOP_BOT_CONFIG`` 指定其他路径。
环境变量（前缀 ``SHOP_BOT_``）优先级高于 YAML 文件，方便注入密钥而不提交到代码库。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class UpstreamSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOP_BOT_UPSTREAM_")

    base_url: str = ""
    api_key: str = ""


class WebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOP_BOT_WEBHOOK_")

    host: str = "127.0.0.1"  # 监听地址（通常前面有反向代理）
    port: int = 8080
    path: str = "/webhook"  # Telegram 推送更新的路径
    url: str = ""  # 公网 HTTPS 地址，例如 https://bot.example.com
    secret_token: str = ""  # Telegram webhook 密钥，防伪造请求


class PaymentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOP_BOT_PAYMENT_")

    callback_path: str = "/payment/callback"  # 支付网关回调路径
    secret: str = ""  # 签名验证共享密钥


class EPaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOP_BOT_EPAY_")

    pid: str = ""      # 商户 ID
    key: str = ""      # 商户密钥
    url: str = ""      # 网关地址，例如 https://pay.example.com
    type: str = "alipay"  # 默认支付方式


class LoggingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOP_BOT_LOGGING_")

    level: str = "INFO"  # DEBUG/INFO/WARNING/ERROR/CRITICAL
    log_dir: str = ""  # 日志文件目录；空字符串表示只输出到 stdout
    json_logs: bool = False  # 是否用 JSON 格式（生产环境建议开）


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOP_BOT_", env_nested_delimiter="__")

    bot_token: str = ""
    admin_ids: list[int] = Field(default_factory=list)
    database_path: str = "shop_bot.db"
    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    payment: PaymentSettings = Field(default_factory=PaymentSettings)
    epay: EPaySettings = Field(default_factory=EPaySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


def _config_path() -> Path:
    return Path(os.environ.get("SHOP_BOT_CONFIG", "config.yaml"))


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


@lru_cache
def get_settings() -> Settings:
    # YAML 里显式存在的键（含嵌套），若环境变量已设置则跳过，保证环境变量的覆盖优先级
    yaml_data = _load_yaml(_config_path())
    merged = {
        k: v
        for k, v in yaml_data.items()
        if not _has_env_override(k)
    }
    return Settings(**merged)


def _has_env_override(key: str) -> bool:
    """检查某个顶层键是否有环境变量覆盖（含嵌套键的前缀匹配）。"""
    prefix = f"SHOP_BOT_{key.upper()}"
    return any(e == prefix or e.startswith(prefix + "__") for e in os.environ)
