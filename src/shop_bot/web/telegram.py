"""Telegram webhook 来源认证。"""

import re

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web


def validate_webhook_secret(secret_token: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret_token):
        raise ValueError("webhook.secret_token must contain 1-256 letters, digits, underscores or hyphens")


def register_telegram_routes(
    app: web.Application, dispatcher: Dispatcher, bot: Bot, path: str, secret_token: str
) -> None:
    validate_webhook_secret(secret_token)
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=secret_token).register(app, path=path)
