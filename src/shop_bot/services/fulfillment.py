"""私信交付与恢复。数据库记录是恢复依据，Telegram 通知失败不撤销付款。"""

import asyncio

from aiogram import Bot

from ..db import Database
from ..logging_config import get_logger
from ..models import OrderStatus, PurchaseState
from .purchasing import Purchaser, split_payload_chunks

logger = get_logger(__name__)
RECOVERY_INTERVAL = 5


async def notify_owner(db: Database, bot: Bot, order_id: int, *, resend: bool = False) -> bool:
    async with db.order_operation(order_id):
        order = await db.get_order(order_id)
        if order is None or order.status != OrderStatus.DELIVERED:
            return False
        # 冻结采购（迁移发现的重复上游单等）在人工核对归属前，同一货品可能
        # 关联多个买家——自动通知与手动补发都必须被拦截（审计第四轮 P1）。
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is not None and purchase.state == PurchaseState.SUBMISSION_UNKNOWN:
            logger.warning(
                "delivery notification blocked: purchase frozen for manual reconciliation",
                extra={"order_id": order_id},
            )
            return False
        if not order.notification_pending and not resend:
            return True
        if resend:
            await db.request_notification(order.id)
        owner = await db.get_user(order.user_id)
        if owner is None:
            return False
        header = f"🎉 你的订单 #{order.id} 已发货！"
        try:
            # 收件人只从持久化订单取，不能使用命令所在群聊或查询者身份。
            # 多张 eSIM 按块分条发送，避免超过消息长度；中断重发可能重复内容，但不会重新采购。
            chunks = split_payload_chunks(order.payload) if order.payload else []
            await bot.send_message(owner.telegram_id, header)
            for chunk in chunks:
                await bot.send_message(owner.telegram_id, chunk)
        except Exception as exc:
            logger.warning("delivery notification failed", extra={"order_id": order.id, "error": type(exc).__name__})
            return False
        await db.mark_notified(order.id)
        return True


async def recover_once(db: Database, purchaser: Purchaser, bot: Bot | None) -> None:
    """恢复扫描：fulfill 幂等推进 paid 履约并收敛 delivered 订单的采购终态，补发未送达通知。"""
    for order in await db.list_recovery_orders():
        try:
            await purchaser.fulfill(db, order.id)
            if bot is not None:
                await notify_owner(db, bot, order.id)
        except Exception as exc:
            logger.warning("recovery failed", extra={"order_id": order.id, "error": type(exc).__name__})


async def recovery_loop(db: Database, purchaser: Purchaser, bot: Bot) -> None:
    while True:
        try:
            await recover_once(db, purchaser, bot)
        except Exception as exc:
            logger.warning("recovery scan failed", extra={"error": type(exc).__name__})
        await asyncio.sleep(RECOVERY_INTERVAL)
