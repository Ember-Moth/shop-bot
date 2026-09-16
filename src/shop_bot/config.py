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
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class UpstreamSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOP_BOT_UPSTREAM_")

    provider: str = ""  # "commbitz" 启用上游目录同步；发货仍为模拟，见 docs/reseller-bot-development.md
    environment: str = "uat"  # uat / live
    api_key: str = ""
    secret_key: str = ""
    timeout: float = 15.0  # 上游请求超时（秒）
    base_url: str = ""  # 可选覆盖；留空按 environment 选择


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

    pid: str = ""  # 商户 ID
    key: str = ""  # 商户密钥
    url: str = ""  # 网关地址，例如 https://pay.example.com
    type: str = "alipay"  # 默认支付方式


class FeaturesSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOP_BOT_FEATURES_")

    kyc: bool = True  # 是否向买家开放 KYC 证件补交入口（按钮/命令）


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
    features: FeaturesSettings = Field(default_factory=FeaturesSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Pydantic 对不同来源的嵌套字典递归合并，环境变量仅覆盖指定字段。
        return env_settings, init_settings, dotenv_settings, file_secret_settings


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
    return Settings(**_load_yaml(_config_path()))
