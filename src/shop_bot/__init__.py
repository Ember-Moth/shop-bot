import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .config import get_settings
from .db import Database
from .handlers import admin, catalog, order, start
from .models import Product
from .services.upstream import StubUpstreamClient, UpstreamClient
from .web.payment import register_payment_routes

logger = logging.getLogger(__name__)

DEMO_PRODUCTS = [
    # 换成真实商品目录（或直接在数据库里管理商品）
    Product(id=1, name="示例商品 A", description="演示用商品", price_cents=999, currency="USD"),
    Product(id=2, name="示例商品 B", description="演示用商品", price_cents=1999, currency="USD"),
]


def build_upstream() -> UpstreamClient:
    """接入真实上游后，把 StubUpstreamClient 换成真实 HTTP 客户端。"""
    return StubUpstreamClient()


def build_dispatcher(db: Database, upstream: UpstreamClient) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), db=db, upstream=upstream)
    dp.include_router(start.router)
    dp.include_router(catalog.router)
    dp.include_router(order.router)
    dp.include_router(admin.router)
    return dp


async def amain() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("SHOP_BOT_BOT_TOKEN is not set")

    db = Database(settings.database_path)
    await db.connect()
    if not await db.list_products():
        await db.seed_products(DEMO_PRODUCTS)
        logger.info("seeded %d demo products", len(DEMO_PRODUCTS))
    upstream = build_upstream()
    dp = build_dispatcher(db, upstream)
    bot = Bot(settings.bot_token)

    app = web.Application()
    app["db"] = db
    app["upstream"] = upstream
    app["bot"] = bot
    app["payment_secret"] = settings.payment.secret

    # Telegram bot webhook 端点
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path=settings.webhook.path)
    setup_application(app, dp, bot=bot)

    # 支付网关回调端点
    register_payment_routes(app, settings.payment.callback_path)

    # 告诉 Telegram 往哪里推更新
    webhook_url = f"{settings.webhook.url.rstrip('/')}{settings.webhook.path}"
    await bot.set_webhook(webhook_url)
    logger.info("webhook registered: %s", webhook_url)
    logger.info("payment callback: %s", settings.payment.callback_path)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook.host, settings.webhook.port)
    try:
        await site.start()
        logger.info("listening on %s:%d", settings.webhook.host, settings.webhook.port)
        await asyncio.Event().wait()  # 一直跑
    finally:
        await bot.delete_webhook()
        await runner.cleanup()
        await db.close()


def main() -> None:
    if sys.platform != "win32":
        try:
            import uvloop
        except ImportError:
            pass
        else:
            uvloop.install()
    asyncio.run(amain())
