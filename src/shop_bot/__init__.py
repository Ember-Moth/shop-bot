import asyncio
import signal
import sys
from contextlib import AsyncExitStack

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.webhook.aiohttp_server import setup_application
from aiohttp import web as aiohttp_web

from .config import Settings, get_settings
from .db import Database, FSMStorage
from .handlers import admin, balance, catalog, kyc, order, start
from .logging_config import get_logger, setup_logging
from .models import Product
from .services.catalog_sync import sync_catalog
from .services.commbitz_api import CommbitzClient, CommbitzError, base_url_for
from .services.epay import EPayClient, EPayConfig
from .services.fulfillment import recovery_loop
from .services.purchasing import CommbitzPurchaser, DemoPurchaser, Purchaser
from .web.payment import register_epay_routes
from .web.telegram import register_telegram_routes, validate_webhook_secret

logger = get_logger(__name__)

DEMO_PRODUCTS = [
    # 换成真实商品目录（或直接在数据库里管理商品）
    Product(id=1, name="示例商品 A", description="演示用商品", price_cents=999, currency="USD"),
    Product(id=2, name="示例商品 B", description="演示用商品", price_cents=1999, currency="USD"),
]


def build_commbitz_client(settings: Settings) -> CommbitzClient | None:
    """配置了 Commbitz 且密钥齐全时返回共享客户端。

    provider=commbitz 但密钥缺失时抛 SystemExit 拒绝启动——绝不能静默降级为
    DemoPurchaser 给真实买家发模拟货品（审计 P1-7）。
    """
    cfg = settings.upstream
    if cfg.provider != "commbitz":
        return None
    if not (cfg.api_key and cfg.secret_key):
        raise SystemExit(
            "upstream.provider is 'commbitz' but api_key/secret_key is missing; "
            "refusing to start with demo fulfillment in production mode"
        )
    base_url = cfg.base_url or base_url_for(cfg.environment)
    return CommbitzClient(base_url, cfg.api_key, cfg.secret_key, timeout=cfg.timeout)


def build_purchaser(commbitz_client: CommbitzClient | None) -> Purchaser:
    """开发方案规则 10：采购记录/状态机（阶段 B）已实现，配置 commbitz 即启用真实采购。"""
    if commbitz_client is not None:
        return CommbitzPurchaser(commbitz_client)
    return DemoPurchaser()


def build_dispatcher(
    db: Database, purchaser: Purchaser, epay: EPayClient | None, commbitz: CommbitzClient | None
) -> Dispatcher:
    # FSM 状态持久化到 SQLite，事件隔离用内存（同一 bot 实例内并发事件串行化）
    dp = Dispatcher(
        storage=FSMStorage(db),
        events_isolation=SimpleEventIsolation(),
        db=db,
        purchaser=purchaser,
        epay=epay,
        commbitz=commbitz,
    )
    dp.include_router(start.router)
    dp.include_router(catalog.router)
    dp.include_router(order.router)
    dp.include_router(kyc.router)
    dp.include_router(balance.router)
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

    validate_webhook_secret(settings.webhook.secret_token)

    async with AsyncExitStack() as resources:
        db = Database(settings.database_path)
        await db.connect()
        resources.push_async_callback(db.close)
        if not await db.list_products():
            await db.seed_products(DEMO_PRODUCTS)

        # Commbitz 共享客户端：目录同步 + 采购 + 人工核对共用，随进程生命周期关闭
        commbitz_client = build_commbitz_client(settings)
        if commbitz_client is not None:
            resources.push_async_callback(commbitz_client.close)
            try:
                await sync_catalog(db, commbitz_client)
            except CommbitzError as exc:
                logger.warning("upstream catalog sync failed: %s", exc)
            except Exception:
                logger.exception("upstream catalog sync failed unexpectedly")
        purchaser = build_purchaser(commbitz_client)

        bot = Bot(settings.bot_token)
        resources.push_async_callback(bot.session.close)

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
            resources.push_async_callback(epay_client.close)
        dp = build_dispatcher(db, purchaser, epay_client, commbitz_client)
        app = aiohttp_web.Application()
        app["db"] = db
        app["purchaser"] = purchaser
        app["commbitz"] = commbitz_client
        app["bot"] = bot
        app["epay"] = epay_client
        register_telegram_routes(app, dp, bot, settings.webhook.path, settings.webhook.secret_token)
        setup_application(app, dp, bot=bot)
        if epay_client is not None:
            register_epay_routes(app, settings.payment.callback_path)

        runner = aiohttp_web.AppRunner(app)
        resources.push_async_callback(runner.cleanup)
        await runner.setup()
        await aiohttp_web.TCPSite(runner, settings.webhook.host, settings.webhook.port).start()
        webhook_url = f"{settings.webhook.url.rstrip('/')}{settings.webhook.path}"
        await bot.set_webhook(webhook_url, secret_token=settings.webhook.secret_token)
        resources.push_async_callback(bot.delete_webhook)
        logger.info("webhook registered: %s", webhook_url)
        logger.info("listening on %s:%d", settings.webhook.host, settings.webhook.port)
        recovery_task = asyncio.create_task(recovery_loop(db, purchaser, bot))
        resources.push_async_callback(_stop_recovery, recovery_task)
        stop = asyncio.Event()
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, stop.set)
            resources.callback(loop.remove_signal_handler, signal.SIGTERM)
        await stop.wait()


async def _stop_recovery(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def main() -> None:
    if sys.platform != "win32":
        try:
            import uvloop
        except ImportError:
            pass
        else:
            uvloop.install()
    asyncio.run(amain())
