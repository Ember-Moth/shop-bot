import asyncio
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .config import get_settings
from .db import Database
from .handlers import admin, catalog, order, start
from .logging_config import get_logger, setup_logging
from .models import Product
from .services.epay import EPayClient, EPayConfig
from .services.upstream import StubUpstreamClient, UpstreamClient
from .web.payment import register_epay_routes

logger = get_logger(__name__)

DEMO_PRODUCTS = [
    # 换成真实商品目录（或直接在数据库里管理商品）
    Product(id=1, name="示例商品 A", description="演示用商品", price_cents=999, currency="USD"),
    Product(id=2, name="示例商品 B", description="演示用商品", price_cents=1999, currency="USD"),
]


def build_upstream() -> UpstreamClient:
    """接入真实上游后，把 StubUpstreamClient 换成真实 HTTP 客户端。"""
    return StubUpstreamClient()


def build_dispatcher(db: Database, upstream: UpstreamClient, epay: EPayClient | None) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), db=db, upstream=upstream, epay=epay)
    dp.include_router(start.router)
    dp.include_router(catalog.router)
    dp.include_router(order.router)
    dp.include_router(admin.router)
    return dp


async def amain() -> None:
    settings = get_settings()
    setup_logging(
        level=settings.logging.level,
        log_dir=settings.logging.log_dir or None,
        json_logs=settings.logging.json_logs,
    )
    if not settings.bot_token:
        raise SystemExit("SHOP_BOT_BOT_TOKEN is not set")

    db = Database(settings.database_path)
    await db.connect()
    if not await db.list_products():
        await db.seed_products(DEMO_PRODUCTS)
        logger.info("seeded %d demo products", len(DEMO_PRODUCTS))
    upstream = build_upstream()
    bot = Bot(settings.bot_token)

    # EPay 客户端（如果配置了的话）
    epay_client = None
    if settings.epay.pid and settings.epay.key and settings.epay.url:
        epay_client = EPayClient(
            EPayConfig(
                pid=settings.epay.pid,
                key=settings.epay.key,
                url=settings.epay.url,
                type=settings.epay.type,
            )
        )
        logger.info("epay client initialized")

    dp = build_dispatcher(db, upstream, epay_client)

    app = web.Application()
    app["db"] = db
    app["upstream"] = upstream
    app["bot"] = bot
    app["epay"] = epay_client

    # Telegram bot webhook 端点
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path=settings.webhook.path)
    setup_application(app, dp, bot=bot)

    # EPay 支付回调端点
    if epay_client is not None:
        register_epay_routes(app, settings.payment.callback_path)
        logger.info("epay callback registered: %s", settings.payment.callback_path)

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
        if epay_client is not None:
            await epay_client.close()
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
