"""命令菜单（输入 / 时的候选列表）：普通用户只见用户命令，管理命令只注册到各管理员私聊。

chat scope 注册要求该管理员已与 Bot 建立私聊（先发过 /start）；Telegram 对未知会话返回
chat not found。管理员 /start 时会自动补注册，失败不阻塞启动或命令使用。
"""

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat

from .config import Settings
from .logging_config import get_logger

logger = get_logger(__name__)


def user_commands(settings: Settings) -> list[BotCommand]:
    commands = [
        BotCommand(command="start", description="打开主菜单"),
        BotCommand(command="query", description="查询订单状态 / 补收货品"),
        BotCommand(command="usage", description="查询已交付 eSIM 用量"),
    ]
    if settings.features.kyc:
        commands.append(BotCommand(command="kyc", description="补交订单证件"))
    return commands


def admin_commands() -> list[BotCommand]:
    return [
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


async def register_admin_menu(bot: Bot, chat_id: int, settings: Settings) -> bool:
    """给单个管理员私聊注册完整菜单；对方尚未私聊过 Bot 时 Telegram 拒绝，记录后跳过。"""
    try:
        await bot.set_my_commands(
            [*user_commands(settings), *admin_commands()],
            scope=BotCommandScopeChat(chat_id=chat_id),
        )
    except TelegramAPIError as exc:
        # Telegram 的错误描述由 API 生成，不含本地数据；chat not found 即该管理员未私聊 /start
        logger.warning("admin command menu registration failed for %s: %s", chat_id, exc)
        return False
    return True


async def register_bot_commands(bot: Bot, settings: Settings) -> None:
    """启动时全量注册：默认 scope + 各管理员私聊 scope；失败仅记录，不影响启动。"""
    try:
        await bot.set_my_commands(user_commands(settings))
    except TelegramAPIError as exc:
        logger.warning("command menu registration failed: %s", exc)
    for admin_id in settings.admin_ids:
        await register_admin_menu(bot, admin_id, settings)
