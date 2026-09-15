"""私信交付与恢复。数据库记录是恢复依据，Telegram 通知失败不撤销付款。"""

import asyncio

from aiogram import Bot

from ..db import Database
from ..logging_config import get_logger
from ..models import OrderStatus
from .orders import OrderError, mark_paid
from .upstream import UpstreamClient

logger = get_logger(__name__)
RECOVERY_INTERVAL = 30


async def notify_owner(db: Database, bot: Bot, order_id: int, *, resend: bool = False) -> bool:
    async with db.order_operation(order_id):
        order = await db.get_order(order_id)
        if order is None or order.status != OrderStatus.DELIVERED:
            return False
        if not order.notification_pending and not resend:
            return True
        if resend:
            await db.request_notification(order.id)
        owner = await db.get_user(order.user_id)
        if owner is None:
            return False
        text = f"🎉 你的订单 #{order.id} 已发货！"
        if order.payload:
            text += f"\n\n{order.payload}"
        try:
            # 收件人只从持久化订单取，不能使用命令所在群聊或查询者身份。
            await bot.send_message(owner.telegram_id, text)
        except Exception as exc:
            logger.warning("delivery notification failed", extra={"order_id": order.id, "error": type(exc).__name__})
            return False
        await db.mark_notified(order.id)
        return True


async def recover_once(db: Database, upstream: UpstreamClient, bot: Bot) -> None:
    for order in await db.list_recovery_orders():
        try:
            if order.status == OrderStatus.PAID:
                await mark_paid(db, upstream, order.id)
            await notify_owner(db, bot, order.id)
        except OrderError:
            logger.warning("delivery requires administrator retry", extra={"order_id": order.id})
        except Exception as exc:
            logger.warning("recovery failed", extra={"order_id": order.id, "error": type(exc).__name__})


async def recovery_loop(db: Database, upstream: UpstreamClient, bot: Bot) -> None:
    while True:
        try:
            await recover_once(db, upstream, bot)
        except Exception as exc:
            logger.warning("recovery scan failed", extra={"error": type(exc).__name__})
        await asyncio.sleep(RECOVERY_INTERVAL)
