import asyncio
import signal
import sys
from contextlib import AsyncExitStack

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeChat
from aiogram.webhook.aiohttp_server import setup_application
from aiohttp import web as aiohttp_web

from .config import Settings, get_settings
from .db import Database, FSMStorage
from .handlers import admin, balance, catalog, kyc, order, start
from .logging_config import get_logger, setup_logging
from .models import Product
from .services.backup import BackupManager
from .services.catalog_sync import sync_catalog
from .services.commbitz_api import CommbitzClient, CommbitzError, base_url_for
from .services.epay import EPayClient, EPayConfig
from .services.fulfillment import recovery_loop
from .services.operations import Operations, RuntimeState
from .services.purchasing import CommbitzPurchaser, DemoPurchaser, Purchaser
from .web.health import register_health_routes
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


async def register_bot_commands(bot: Bot, settings: Settings) -> None:
    """注册命令菜单（输入 / 时的候选列表）。

    普通用户只见用户命令；管理员按其私聊 scope 追加管理命令。注册失败不影响启动。
    """
    user_commands = [
        BotCommand(command="start", description="打开主菜单"),
        BotCommand(command="query", description="查询订单状态 / 补收货品"),
        BotCommand(command="usage", description="查询已交付 eSIM 用量"),
    ]
    if settings.features.kyc:
        user_commands.append(BotCommand(command="kyc", description="补交订单证件"))
    admin_commands = [
        BotCommand(command="products", description="商品列表（含下架）"),
        BotCommand(command="price", description="定价"),
        BotCommand(command="publish", description="上架"),
        BotCommand(command="unpublish", description="下架"),
        BotCommand(command="currency", description="设置计价币种"),
        BotCommand(command="orders", description="查看订单"),
        BotCommand(command="paid", description="确认收款并履约"),
        BotCommand(command="cancel", description="取消待支付订单"),
        BotCommand(command="refund", description="人工退款并关单"),
        BotCommand(command="dispatch", description="实体卡确认发货"),
        BotCommand(command="purchases", description="人工核对采购"),
        BotCommand(command="bind", description="绑定上游订单"),
        BotCommand(command="retry", description="重试被拒采购"),
        BotCommand(command="adjust", description="钱包调账"),
        BotCommand(command="status", description="运行状态"),
        BotCommand(command="ackalert", description="确认告警"),
    ]
    try:
        await bot.set_my_commands(user_commands)
        for admin_id in settings.admin_ids:
            await bot.set_my_commands(
                [*user_commands, *admin_commands],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
    except Exception as exc:
        logger.warning("command menu registration failed", extra={"error": type(exc).__name__})


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
    runtime = RuntimeState()

    async with AsyncExitStack() as resources:
        db = Database(settings.database_path)
        await db.connect()
        resources.push_async_callback(db.close)
        if not settings.upstream.provider and not await db.list_all_products():
            await db.seed_products(DEMO_PRODUCTS)

        # Commbitz 共享客户端：目录同步 + 采购 + 人工核对共用，随进程生命周期关闭
        commbitz_client = build_commbitz_client(settings)
        if commbitz_client is not None:
            resources.push_async_callback(commbitz_client.close)
            try:
                await sync_catalog(db, commbitz_client)
            except CommbitzError as exc:
                runtime.failures["catalog_sync"] = "上游目录同步失败，请检查配置和日志；恢复后重启服务重试同步"
                logger.warning("upstream catalog sync failed", extra={"error": type(exc).__name__})
            except Exception:
                runtime.failures["catalog_sync"] = "上游目录同步异常，请检查配置和日志"
                logger.error("upstream catalog sync failed unexpectedly")  # noqa: TRY400 - 异常原文可能含敏感信息
        purchaser = build_purchaser(commbitz_client)

        bot = Bot(settings.bot_token)
        resources.push_async_callback(bot.session.close)
        backups = BackupManager(settings.database_path, settings.backup)
        operations = Operations(db, bot, settings.admin_ids, settings.operations, runtime=runtime, backups=backups)
        if settings.operations.alerts_enabled and not settings.admin_ids:
            logger.warning("operator alerts have no recipients; configure admin_ids")

        epay_client = None
        if settings.epay.pid and settings.epay.key and settings.epay.url:
            epay_client = EPayClient(
                EPayConfig(
                    pid=settings.epay.pid,
                    key=settings.epay.key,
                    url=settings.epay.url,
                    type=settings.epay.type,
                    currency=settings.epay.currency,
                )
            )
            resources.push_async_callback(epay_client.close)
        dp = build_dispatcher(db, purchaser, epay_client, commbitz_client)
        dp["operations"] = operations
        dp.errors.register(operations.on_handler_error)
        app = aiohttp_web.Application()
        app["db"] = db
        app["purchaser"] = purchaser
        app["commbitz"] = commbitz_client
        app["bot"] = bot
        app["epay"] = epay_client
        register_telegram_routes(app, dp, bot, settings.webhook.path, settings.webhook.secret_token)
        register_health_routes(app, operations)
        setup_application(app, dp, bot=bot)
        if epay_client is not None:
            register_epay_routes(app, settings.payment.callback_path)

        runner = aiohttp_web.AppRunner(app)
        resources.push_async_callback(runner.cleanup)
        await runner.setup()
        await aiohttp_web.TCPSite(runner, settings.webhook.host, settings.webhook.port).start()
        webhook_url = f"{settings.webhook.url.rstrip('/')}{settings.webhook.path}"
        await register_bot_commands(bot, settings)
        await bot.set_webhook(webhook_url, secret_token=settings.webhook.secret_token)
        resources.push_async_callback(bot.delete_webhook)
        runtime.webhook_ready = True
        logger.info("webhook registered: %s", webhook_url)
        logger.info("listening on %s:%d", settings.webhook.host, settings.webhook.port)
        runtime.tasks["recovery"] = asyncio.create_task(recovery_loop(db, purchaser, bot, runtime))
        runtime.tasks["monitor"] = asyncio.create_task(operations.run())
        if settings.backup.enabled:
            runtime.tasks["backup"] = asyncio.create_task(backups.run())
        for name, task in runtime.tasks.items():
            runtime.beat(name)
            resources.push_async_callback(_stop_task, task)
        resources.callback(setattr, runtime, "stopping", True)
        stop = asyncio.Event()
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, stop.set)
            resources.callback(loop.remove_signal_handler, signal.SIGTERM)
        stop_task = asyncio.create_task(stop.wait())
        resources.push_async_callback(_stop_task, stop_task)
        done, _ = await asyncio.wait([stop_task, *runtime.tasks.values()], return_when=asyncio.FIRST_COMPLETED)
        if stop_task not in done:
            failed_name = next(name for name, task in runtime.tasks.items() if task in done)
            logger.error("background worker exited", extra={"error": failed_name})
            # supervisor(systemd) 将重启服务；告警失败不阻挡退出。
            try:
                async with asyncio.timeout(10):
                    await db.set_alert(f"worker_{failed_name}", f"后台任务 {failed_name} 退出，服务将重启")
                    await operations.deliver_alerts()
            except Exception:
                logger.warning("worker failure alert unavailable")
            raise RuntimeError(f"background worker exited: {failed_name}")


async def _stop_task(task: asyncio.Task) -> None:
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
