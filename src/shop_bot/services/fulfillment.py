"""私信交付与恢复。数据库记录是恢复依据，Telegram 通知失败不撤销付款。"""

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .operations import RuntimeState

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile

from ..db import Database
from ..logging_config import get_logger
from ..models import Order, OrderStatus, PurchaseState
from ..telegram_text import text_units
from .esim_media import EsimMedia, delivery_esims, qr_png
from .purchasing import Purchaser, split_payload_chunks

logger = get_logger(__name__)
RECOVERY_INTERVAL = 5
# 更改步骤顺序、数量或载荷含义时必须递增，不能复用旧方案的 cursor。
NOTIFICATION_PLAN_VERSION = 1


@dataclass(frozen=True)
class NotificationStep:
    text: str
    photo: EsimMedia | None = None
    photo_index: int = 0


def notification_steps(order: Order, esims: list[EsimMedia]) -> list[NotificationStep]:
    steps = [NotificationStep(f"🎉 你的订单 #{order.id} 已发货！")]
    if not esims:
        steps.extend(NotificationStep(text) for text in split_payload_chunks(order.payload or ""))
        return steps
    for index, esim in enumerate(esims):
        label = f"eSIM {index + 1}/{len(esims)}"
        heading = f"{label}\n\nICCID: {esim.iccid[:80]}"
        caption = f"{heading}\nLPA: {esim.lpa}\n\n扫码或按 LPA 安装码安装"
        if text_units(caption) <= 1024:
            steps.append(NotificationStep(caption, esim, index))
        else:
            steps.append(NotificationStep(f"{heading}\n\n完整 LPA 安装码见下一条消息。", esim, index))
            # LPA 已限制为 2000 UTF-8 字节，单独发送可完整保留且不会超过消息上限。
            steps.append(NotificationStep(f"订单 #{order.id} · {label}\n完整 LPA 安装码：\n{esim.lpa}"))
    return steps


async def notify_owner(
    db: Database,
    bot: Bot,
    order_id: int,
    *,
    resend: bool = False,
    runtime: RuntimeState | None = None,
) -> bool:
    async with db.order_operation(order_id):
        order = await db.get_order(order_id)
        if order is None or order.status != OrderStatus.DELIVERED:
            return False
        # 自动通知和手动补发均只使用当前采购已经核验、落库的货品。
        # 重绑后的 upstream_pending、冻结记录和旧引用的 payload 都不能发送。
        purchase = await db.get_purchase_by_order(order_id)
        if purchase is not None:
            expected_ref = purchase.upstream_request_id or f"STUB-{order_id:06d}"
            if purchase.state != PurchaseState.FULFILLED or order.upstream_ref != expected_ref:
                logger.warning(
                    "delivery notification blocked: unverified purchase or reference", extra={"order_id": order_id}
                )
                return False
            if purchase.upstream_request_id and await db.get_purchase_conflict(order_id, purchase.upstream_request_id):
                logger.warning("delivery notification blocked: shared upstream reference", extra={"order_id": order_id})
                return False
        if not order.notification_pending and not resend:
            return True
        if order.notification_retry_at and time.time() < order.notification_retry_at:
            return False
        if resend:
            await db.request_notification(order.id)
        owner = await db.get_user(order.user_id)
        if owner is None:
            return False
        try:
            # 收件人只从持久化订单取，不能使用命令所在群聊或查询者身份。
            # eSIM 订单：头部文本 1 条 + 每张 eSIM 一条图文（二维码图 + ICCID/LPA 进 caption）。
            # 非 eSIM（兑换券/激活等）：头部 + 文本货品分条。每步各存进度，断点续发不重购。
            esims = (
                delivery_esims(order.delivery_esims, order.payload, order.quantity)
                if (
                    order.delivery_esims is not None
                    or (purchase and purchase.request_type == "esim")
                    or order.input_request_type == "esim"
                    or (purchase is None and order.input_request_type is None)
                )
                else []
            )
            steps = notification_steps(order, esims)
            prepared = await db.prepare_notification(order.id, NOTIFICATION_PLAN_VERSION)
            if prepared is None:
                return False
            cursor = prepared.notification_cursor
            total = len(steps)
            if not 0 <= cursor <= total:
                logger.warning("invalid notification progress", extra={"order_id": order.id})
                return False
            for step in range(cursor, total):
                if runtime is not None:
                    runtime.beat("recovery")
                item = steps[step]
                if item.photo is None:
                    await bot.send_message(owner.telegram_id, item.text, parse_mode=None)
                else:
                    png = await asyncio.to_thread(qr_png, item.photo.lpa)
                    await bot.send_photo(
                        owner.telegram_id,
                        BufferedInputFile(png, filename=f"esim-{order.id}-{item.photo_index + 1}.png"),
                        caption=item.text,
                        parse_mode=None,
                        request_timeout=20,
                    )
                if not await db.advance_notification(order.id, step):
                    return False
        except TelegramRetryAfter as exc:
            await db.defer_notification(order.id, time.time() + exc.retry_after)
            logger.warning("delivery notification rate limited", extra={"order_id": order.id})
            return False
        except Exception as exc:
            logger.warning("delivery notification failed", extra={"order_id": order.id, "error": type(exc).__name__})
            return False
        await db.mark_notified(order.id)
        return True


async def notify_refund(db: Database, bot: Bot, order_id: int) -> bool:
    """退款与通知分开恢复；成功发送后清除待通知标记。"""
    async with db.order_operation(order_id):
        order = await db.get_order(order_id)
        if order is None or order.status != OrderStatus.REFUNDED:
            return False
        if not order.notification_pending:
            return True
        owner = await db.get_user(order.user_id)
        if owner is None:
            return False
        balance = await db.get_balance(order.user_id, order.currency)
        try:
            await bot.send_message(
                owner.telegram_id,
                f"❌ 订单 #{order.id} 无法完成交付，{order.amount_text} 已退回余额\n"
                f"当前余额：{balance / 100:.2f} {order.currency}",
            )
        except Exception as exc:
            logger.warning("refund notification failed", extra={"order_id": order_id, "error": type(exc).__name__})
            return False
        await db.mark_notified(order_id)
        return True


async def recover_once(
    db: Database,
    purchaser: Purchaser,
    bot: Bot | None,
    runtime: RuntimeState | None = None,
) -> int:
    """恢复扫描：fulfill 幂等推进 paid 履约并收敛 delivered 订单的采购终态，补发未送达通知。"""
    failures = 0
    for order in await db.list_recovery_orders():
        if runtime is not None:
            runtime.beat("recovery")
        try:
            final = await purchaser.fulfill(db, order.id)
            if bot is None:
                continue
            if final is not None and final.status == OrderStatus.REFUNDED:
                await notify_refund(db, bot, final.id)
            else:
                await notify_owner(db, bot, order.id, runtime=runtime)
        except Exception as exc:
            failures += 1
            logger.warning("recovery failed", extra={"order_id": order.id, "error": type(exc).__name__})
    return failures


async def recovery_loop(db: Database, purchaser: Purchaser, bot: Bot, runtime: RuntimeState | None = None) -> None:
    while True:
        try:
            if runtime is not None:
                runtime.beat("recovery")
            failures = await recover_once(db, purchaser, bot, runtime)
            if runtime is not None:
                runtime.beat("recovery")
                if failures:
                    runtime.failures["recovery"] = f"履约恢复本轮有 {failures} 个订单处理异常，请检查订单日志"
                else:
                    runtime.failures.pop("recovery", None)
        except Exception as exc:
            if runtime is not None:
                runtime.failures["recovery"] = "履约扫描失败，请检查数据库和服务日志"
            logger.warning("recovery scan failed", extra={"error": type(exc).__name__})
        await asyncio.sleep(RECOVERY_INTERVAL)
